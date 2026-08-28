"""
Repeater pipeline hook.

If two consecutive user text messages in the same session are identical and
the content has not triggered within the configured cooldown window, the
system repeats the message once and suppresses the normal capability chain.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.channel import set_reply_policy_override
from app.common.identity import GroupHumanIntentType, classify_group_human_intent
from app.common.intent_runtime import decision_from_pre
from app.common.logging import get_logger
from app.common.types import CapabilityResult, MessageType, Role, RouteType, Turn
from app.orchestrator.effect_handlers import effect_handler_opt_in_enabled
from app.orchestrator.effects import (
    EFFECT_COMMIT_SEMANTICS_AUDIT_AFTER_SIDE_EFFECT,
    EFFECT_COMMIT_SEMANTICS_GATE_BEFORE_SIDE_EFFECT,
    EffectCommitRecord,
)
from app.orchestrator.flow import MessageEffect, StepResult
from app.orchestrator.pipeline import PipelineContext
from app.plugin.hooks import RESULT_PRODUCER_OWNER_KEY, HookAbort, HookPoint
from app.preprocessing.pii import detect_and_mask
from plugins.repeater.store import RepeaterStore

logger = get_logger(__name__)
_MENTION_PREFIX_RE = re.compile(r"^\s*(?:@\S+[\s\u2005\u00a0]+)+")
_NON_TEXT_PLACEHOLDERS = {"[图片]", "[语音]", "[视频]", "[文件]"}
_URL_RE = re.compile(
    r"(?i)(?:https?://|www\.|(?:[a-z0-9-]+\.)+(?:com|cn|net|org|io|ai)\b)"
)
_MAX_REPEAT_LENGTH = 120


def _normalize(text: str) -> str:
    return " ".join(str(text or "").strip().split())


def _current_user_original_content(ctx: PipelineContext) -> str:
    session = ctx.session
    if session is None:
        return ""
    current_trace_id = str(ctx.event.trace_id or "")
    for turn in reversed(list(session.turns or [])):
        if turn.role != Role.USER:
            continue
        if current_trace_id and str(turn.trace_id or "") != current_trace_id:
            continue
        return str(turn.metadata.get("original_content") or "")
    return ""


def _reply_text_for_repeat(ctx: PipelineContext, cleaned: str) -> str:
    event = ctx.event
    pre = ctx.pre
    candidates = [
        _current_user_original_content(ctx),
        event.metadata.get("original_content"),
        pre.original_text if pre is not None else "",
        event.message.content,
    ]
    for candidate in candidates:
        text = str(candidate or "")
        if text:
            return text
    return cleaned


def _looks_like_mention(text: str) -> bool:
    return bool(_MENTION_PREFIX_RE.match(str(text or "")))


def _ignored_repeat_reason(ctx: PipelineContext, text: str) -> str:
    normalized = _normalize(text)
    if not normalized:
        return "empty_content"
    if normalized in _NON_TEXT_PLACEHOLDERS:
        return "non_text_placeholder"
    if normalized.startswith("/"):
        return "command_content"
    if _looks_like_mention(normalized):
        return "mention_content"
    if len(normalized) > _MAX_REPEAT_LENGTH:
        return "content_too_long"
    if _URL_RE.search(normalized):
        return "link_content"
    _masked, pii_map = detect_and_mask(normalized)
    if pii_map or "<PII:" in normalized:
        return "pii_or_secret_content"
    if ctx.pre is not None and (ctx.pre.sensitive or ctx.pre.block_reason):
        return "sensitive_content"
    human_intent = classify_group_human_intent(
        normalized,
        decision=decision_from_pre(ctx.pre),
    )
    if human_intent.type != GroupHumanIntentType.NONE:
        return "identity_or_handoff_content"
    return ""


def _is_group_context(ctx: PipelineContext) -> bool:
    session_id = str(ctx.event.session_id or "")
    session_kind = str(ctx.event.metadata.get("session_kind") or "").strip().lower()
    if session_kind == "group":
        return True
    return session_id.endswith("@chatroom")


def _repeater_policy_session_id(ctx: PipelineContext) -> str:
    """Use the operator-facing group ID for config and trigger history.

    Managed channel events use a canonical session ID internally, while group
    configuration remains keyed by the verified external group ID exposed by
    the SDK roster and admin UI.
    """

    event = ctx.event
    canonical_session_id = str(event.session_id or "").strip()
    if canonical_session_id and not canonical_session_id.startswith("cx1:"):
        return canonical_session_id
    metadata = dict(event.metadata or {})
    session_metadata = dict(ctx.session.metadata or {}) if ctx.session is not None else {}
    for value in (
        event.external_conversation_id,
        metadata.get("external_conversation_id"),
        metadata.get("external_session_id"),
        getattr(ctx.session, "external_conversation_id", "") if ctx.session is not None else "",
        session_metadata.get("external_conversation_id"),
        session_metadata.get("external_session_id"),
        canonical_session_id,
    ):
        session_id = str(value or "").strip()
        if session_id:
            return session_id
    return ""


def _previous_turn_before_current(ctx: PipelineContext) -> Turn | None:
    session = ctx.session
    if session is None:
        return None
    turns = list(session.turns or [])
    if len(turns) < 2:
        return None
    current_trace_id = str(ctx.event.trace_id or "")

    for idx in range(len(turns) - 1, -1, -1):
        turn = turns[idx]
        if turn.role == Role.USER and str(turn.trace_id or "") == current_trace_id:
            if idx == 0:
                return None
            return turns[idx - 1]
    return turns[-2]


def _sender_id_from_event(ctx: PipelineContext) -> str:
    metadata = dict(ctx.event.metadata or {})
    return str(
        metadata.get("sender_wxid")
        or metadata.get("sender_id")
        or ctx.event.user_id
        or ""
    ).strip()


def _sender_id_from_turn(turn: Turn) -> str:
    metadata = dict(turn.metadata or {})
    return str(
        metadata.get("sender_wxid")
        or metadata.get("sender_id")
        or metadata.get("user_id")
        or ""
    ).strip()


def _repeat_member_reason(ctx: PipelineContext, previous_turn: Turn) -> str:
    current_sender = _sender_id_from_event(ctx)
    previous_sender = _sender_id_from_turn(previous_turn)
    if not current_sender or not previous_sender:
        return "sender_identity_unavailable"
    if current_sender == previous_sender:
        return "same_sender"
    return ""


@dataclass
class RepeaterHook:
    store: RepeaterStore
    record_trigger_enabled: bool = True
    name: str = "repeater.repeat"
    point: HookPoint = HookPoint.BEFORE_ROUTE
    # Run after command hooks, but before wxbot group reply-policy suppression.
    priority: int = 15

    async def run(self, ctx: PipelineContext) -> None:
        event = ctx.event
        session = ctx.session
        pre = ctx.pre
        if session is None or pre is None:
            return
        if not _is_group_context(ctx):
            return
        if event.message.type != MessageType.TEXT:
            return
        if bool(event.metadata.get("is_self_sent")):
            ctx.extras["repeater"] = {
                "triggered": False,
                "reason": "self_message",
                "content": "",
            }
            return
        if event.metadata.get("image_url"):
            return

        policy_session_id = _repeater_policy_session_id(ctx)
        cfg = await self.store.get_config(event.tenant_id, policy_session_id)
        if not cfg.get("enabled"):
            return

        current = _normalize(pre.cleaned_text or event.message.content)
        if not current:
            return
        reply_text = _reply_text_for_repeat(ctx, current)
        ignored_reason = _ignored_repeat_reason(ctx, reply_text or current)
        if ignored_reason:
            ctx.extras["repeater"] = {
                "triggered": False,
                "reason": ignored_reason,
                "content": "",
            }
            return
        previous_turn = _previous_turn_before_current(ctx)
        if previous_turn is None or previous_turn.role != Role.USER:
            ctx.extras["repeater"] = {
                "triggered": False,
                "reason": "previous_turn_not_user",
                "content": current,
            }
            return

        previous = _normalize(previous_turn.content)
        if not previous or previous != current:
            return
        member_reason = _repeat_member_reason(ctx, previous_turn)
        if member_reason:
            ctx.extras["repeater"] = {
                "triggered": False,
                "reason": member_reason,
                "content": "",
            }
            return

        cooldown_seconds = int(cfg.get("cooldown_seconds") or 300)
        if not await self.store.should_trigger(
            event.tenant_id,
            policy_session_id,
            current,
            cooldown_seconds,
        ):
            ctx.extras["repeater"] = {
                "triggered": False,
                "reason": "cooldown",
                "content": current,
            }
            return

        if self.record_trigger_enabled:
            await self.store.record_trigger(
                event.tenant_id,
                policy_session_id,
                current,
                trace_id=event.trace_id,
            )
        # Repeater is an explicit group-side capability. Once it matches, a
        # channel adapter should not re-apply generic mention/keyword gating.
        set_reply_policy_override(
            ctx.extras,
            force_send=True,
            mention_sender=False,
            reason="repeater_triggered",
            metadata={"plugin": "repeater"},
        )
        ctx.extras["repeater"] = {
            "triggered": True,
            "reason": "repeat_match",
            "content": current,
        }
        logger.info(
            "repeater.triggered",
            session_id=event.session_id,
            trace_id=event.trace_id,
            content_length=len(current),
        )
        raise HookAbort(reply_text, reason="repeater_triggered")


def _sync_repeater_signal(ctx: PipelineContext) -> dict[str, object]:
    repeater = ctx.extras.get("repeater")
    signal = dict(repeater) if isinstance(repeater, dict) else {"triggered": False}
    ctx.signals["repeater"] = signal
    return signal


def _record_trigger_effect(
    ctx: PipelineContext,
    signal: dict[str, object],
    *,
    trigger_as_effect: bool = False,
) -> MessageEffect:
    policy_session_id = _repeater_policy_session_id(ctx)
    return MessageEffect(
        type="record_repeater_trigger",
        owner="repeater",
        payload={
            "commit_semantics": (
                EFFECT_COMMIT_SEMANTICS_GATE_BEFORE_SIDE_EFFECT
                if trigger_as_effect
                else EFFECT_COMMIT_SEMANTICS_AUDIT_AFTER_SIDE_EFFECT
            ),
            "tenant_id": ctx.event.tenant_id,
            "session_id": policy_session_id,
            "user_id": ctx.event.user_id,
            "content": str(signal.get("content") or ""),
            "reason": str(signal.get("reason") or ""),
            "trace_id": ctx.event.trace_id,
        },
        idempotency_key=(
            "repeater:trigger:"
            f"{ctx.event.tenant_id}:{policy_session_id}:{ctx.event.trace_id}"
        ),
    )


@dataclass
class RepeaterDetectStep:
    store: RepeaterStore
    effect_handler_enabled: bool = False
    kind: str = "plugin.repeater.detect"
    owner: str = "repeater"
    name: str = "Detect group repeater"
    permissions: list[str] = field(default_factory=lambda: ["storage:shared"])
    inputs: set[str] = field(default_factory=lambda: {"event", "session", "pre"})
    outputs: set[str] = field(
        default_factory=lambda: {"signals.repeater", "result", "effects.record_repeater_trigger"}
    )
    timeout_seconds: float = 1.0
    error_policy: str = "fail_open"

    async def run(self, ctx: PipelineContext) -> StepResult:
        trigger_as_effect = self.effect_handler_enabled or effect_handler_opt_in_enabled(
            ctx,
            effect_type="record_repeater_trigger",
            owner="repeater",
        )
        try:
            await RepeaterHook(
                self.store,
                record_trigger_enabled=not trigger_as_effect,
            ).run(ctx)
        except HookAbort as exc:
            signal = _sync_repeater_signal(ctx)
            # The adapter, not HookAbort payload, binds this result to the
            # compiled repeater owner. FlowRunner independently rebinds it from
            # the compiled step before any durable boundary.
            ctx.extras[RESULT_PRODUCER_OWNER_KEY] = self.owner
            return StepResult(
                action="stop",
                reason=exc.reason,
                result=CapabilityResult(route=RouteType.CANNED, reply_text=exc.reply_text),
                finalize=True,
                skip_output_safety=True,
                route_label=RouteType.CANNED.value,
                publish_outbound=True,
                append_assistant_turn=True,
                effects=[
                    _record_trigger_effect(
                        ctx,
                        signal,
                        trigger_as_effect=trigger_as_effect,
                    )
                ],
            )
        signal = _sync_repeater_signal(ctx)
        return StepResult(reason=str(signal.get("reason") or "not_triggered"))


@dataclass
class RepeaterTriggerEffectHandler:
    store: RepeaterStore

    async def __call__(
        self,
        effect: MessageEffect,
        ctx: PipelineContext,
        record: EffectCommitRecord,
    ) -> None:
        _ = record
        payload = dict(effect.payload)
        content = str(payload.get("content") or "").strip()
        if not content:
            raise ValueError("repeater trigger effect missing content")
        event_id = await self.store.record_trigger(
            str(payload.get("tenant_id") or ctx.event.tenant_id),
            str(payload.get("session_id") or ctx.event.session_id),
            content,
            trace_id=str(payload.get("trace_id") or ctx.event.trace_id or ctx.trace_id),
        )
        ctx.signals.setdefault("effects", {}).setdefault("repeater", []).append(
            {
                "type": effect.type,
                "owner": effect.owner,
                "idempotency_key": effect.idempotency_key,
                "event_id": int(event_id or 0),
                "status": "recorded",
            }
        )
