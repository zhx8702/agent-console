from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.channel import set_reply_policy_override
from app.channel.models import configuration_session_id
from app.common.intent_runtime import decision_from_pre
from app.common.logging import get_logger
from app.common.types import (
    CapabilityResult,
    Channel,
    MessageType,
    RouteType,
)
from app.orchestrator.flow import StepResult
from app.orchestrator.pipeline import PipelineContext
from app.plugin.hooks import HookAbort, HookPoint
from plugins.tibo_reset.intent import (
    TiboResetIntent,
    TiboResetIntentType,
    classify_tibo_reset_followup,
    classify_tibo_reset_intent,
    format_tibo_reset_reply,
)
from plugins.tibo_reset.store import TiboResetStore

logger = get_logger(__name__)

_FOLLOWUP_CONTEXT_VARIABLE = "tibo_reset_followup_context"
_DEFAULT_FOLLOWUP_WINDOW_SECONDS = 600.0
_MAX_FOLLOWUP_CONTEXT_SENDERS = 20


def _query_text(ctx: PipelineContext) -> str:
    normalized = str(
        ctx.event.metadata.get("wxbot_normalized_content")
        or ctx.event.metadata.get("cleaned_content")
        or ""
    ).strip()
    if normalized:
        return normalized
    if ctx.pre is not None:
        return str(ctx.pre.cleaned_text or ctx.pre.original_text or "").strip()
    return str(ctx.event.message.content or "").strip()


def _sender_id_from_event(ctx: PipelineContext) -> str:
    return str(
        ctx.event.metadata.get("sender_wxid")
        or ctx.event.metadata.get("sender_id")
        or ctx.event.user_id
        or ""
    ).strip()


def _aware_datetime(value: object) -> datetime | None:
    try:
        parsed = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _followup_context(ctx: PipelineContext) -> dict[str, dict[str, object]]:
    if ctx.session is None:
        return {}
    raw = ctx.session.variables.get(_FOLLOWUP_CONTEXT_VARIABLE)
    if not isinstance(raw, dict):
        return {}
    return {str(sender): dict(record) for sender, record in raw.items() if isinstance(record, dict)}


def _contextual_intent(
    ctx: PipelineContext,
    current_text: str,
    *,
    followup_window_seconds: float,
) -> TiboResetIntent:
    session = ctx.session
    current_sender = _sender_id_from_event(ctx)
    if session is None or not current_sender or current_sender == "unknown":
        return TiboResetIntent(TiboResetIntentType.NONE, 0.0, current_text)

    record = _followup_context(ctx).get(current_sender)
    if record is None:
        return TiboResetIntent(TiboResetIntentType.NONE, 0.0, current_text)
    handled_at = _aware_datetime(record.get("handled_at"))
    received_at = _aware_datetime(ctx.event.received_at)
    if handled_at is None or received_at is None:
        return TiboResetIntent(TiboResetIntentType.NONE, 0.0, current_text)
    age_seconds = (received_at - handled_at).total_seconds()
    if age_seconds < 0 or age_seconds > followup_window_seconds:
        return TiboResetIntent(TiboResetIntentType.NONE, 0.0, current_text)
    try:
        previous_type = TiboResetIntentType(str(record.get("intent") or ""))
    except ValueError:
        return TiboResetIntent(TiboResetIntentType.NONE, 0.0, current_text)
    previous_intent = TiboResetIntent(previous_type, 1.0, "")
    return classify_tibo_reset_followup(
        current_text,
        previous_intent,
        decision=decision_from_pre(ctx.pre),
    )


def _remember_handled_intent(
    ctx: PipelineContext,
    intent: TiboResetIntent,
    *,
    followup_window_seconds: float,
) -> None:
    session = ctx.session
    sender_id = _sender_id_from_event(ctx)
    handled_at = _aware_datetime(ctx.event.received_at)
    if session is None or not sender_id or sender_id == "unknown" or handled_at is None:
        return

    records = _followup_context(ctx)
    retained: list[tuple[str, dict[str, object], datetime]] = []
    for key, record in records.items():
        record_time = _aware_datetime(record.get("handled_at"))
        if record_time is None:
            continue
        age_seconds = (handled_at - record_time).total_seconds()
        if 0 <= age_seconds <= followup_window_seconds:
            retained.append((key, record, record_time))
    retained.sort(key=lambda item: item[2], reverse=True)
    bounded = {
        key: record for key, record, _record_time in retained[: _MAX_FOLLOWUP_CONTEXT_SENDERS - 1]
    }
    bounded[sender_id] = {
        "intent": intent.type.value,
        "handled_at": handled_at.isoformat(),
        "trace_id": ctx.event.trace_id,
    }
    session.variables[_FOLLOWUP_CONTEXT_VARIABLE] = bounded


@dataclass
class TiboResetIntentHook:
    store: TiboResetStore
    name: str = "tibo_reset.intent"
    point: HookPoint = HookPoint.BEFORE_ROUTE
    priority: int = 18

    async def run(self, ctx: PipelineContext) -> None:
        event = ctx.event
        if event.channel != Channel.WECHAT:
            return
        if event.message.type != MessageType.TEXT:
            return
        if bool(event.metadata.get("is_self_sent")):
            return
        if not str(event.session_id or "").endswith("@chatroom"):
            return

        query_text = _query_text(ctx)
        intent = classify_tibo_reset_intent(
            query_text,
            decision=decision_from_pre(ctx.pre),
        )
        match_source = "direct_rule"
        followup_window_seconds = max(
            60.0,
            float(
                getattr(
                    getattr(self.store, "settings", None),
                    "tibo_reset_followup_window_seconds",
                    _DEFAULT_FOLLOWUP_WINDOW_SECONDS,
                )
                or _DEFAULT_FOLLOWUP_WINDOW_SECONDS
            ),
        )
        if not intent.should_handle:
            intent = _contextual_intent(
                ctx,
                query_text,
                followup_window_seconds=followup_window_seconds,
            )
            match_source = "handled_context"
        if not intent.should_handle:
            return
        scope_session_id = configuration_session_id(event, ctx.session)
        try:
            if not await self.store.is_scope_enabled(
                event.tenant_id,
                scope_session_id,
            ):
                ctx.signals["tibo_reset"] = {
                    "intent": intent.type.value,
                    "confidence": intent.confidence,
                    "match_source": match_source,
                    "reason": "scope_disabled",
                    "scope_session_id": scope_session_id,
                }
                return
            stats = await self.store.reset_stats()
        except Exception as exc:
            logger.warning(
                "tibo_reset.intent_lookup_failed",
                tenant_id=event.tenant_id,
                session_id=event.session_id,
                scope_session_id=scope_session_id,
                error=str(exc),
            )
            return

        _remember_handled_intent(
            ctx,
            intent,
            followup_window_seconds=followup_window_seconds,
        )
        set_reply_policy_override(
            ctx.extras,
            force_send=True,
            mention_sender=True,
            reason="tibo_reset_intent",
            metadata={
                "plugin": "tibo_reset",
                "intent": intent.type.value,
                "confidence": intent.confidence,
                "match_source": match_source,
            },
        )
        ctx.signals["tibo_reset"] = {
            "intent": intent.type.value,
            "confidence": intent.confidence,
            "match_source": match_source,
            "week_count": int(stats.get("week_count") or 0),
            "today_count": int(stats.get("today_count") or 0),
        }
        logger.info(
            "tibo_reset.intent_answered",
            tenant_id=event.tenant_id,
            session_id=event.session_id,
            intent=intent.type.value,
            match_source=match_source,
            trace_id=event.trace_id,
        )
        raise HookAbort(
            format_tibo_reset_reply(intent, stats),
            reason=f"tibo_reset_{intent.type.value}",
        )


@dataclass
class TiboResetIntentStep:
    store: TiboResetStore
    kind: str = "plugin.tibo_reset.intent"
    owner: str = "tibo_reset"
    name: str = "Answer Tibo reset questions"
    permissions: list[str] = field(default_factory=lambda: ["storage:plugin", "hooks:pipeline"])
    inputs: set[str] = field(default_factory=lambda: {"event", "session", "pre"})
    outputs: set[str] = field(default_factory=lambda: {"signals.tibo_reset", "result"})
    timeout_seconds: float = 2.0
    error_policy: str = "fail_open"

    async def run(self, ctx: PipelineContext) -> StepResult:
        try:
            await TiboResetIntentHook(self.store).run(ctx)
        except HookAbort as exc:
            return StepResult(
                action="stop",
                reason=exc.reason,
                result=CapabilityResult(
                    route=RouteType.CANNED,
                    reply_text=exc.reply_text,
                    metadata={"response_guard_allow_echo": True},
                ),
                finalize=True,
                skip_output_safety=True,
                route_label=RouteType.CANNED.value,
                publish_outbound=True,
                append_assistant_turn=True,
            )
        return StepResult(reason="no_tibo_reset_intent")
