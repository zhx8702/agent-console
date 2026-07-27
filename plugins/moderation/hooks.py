"""
Pipeline hooks for per-session moderation.

The audit hook records keyword matches and optionally pushes a webhook.
Separate reminder hooks either replace the reply before capability runs or
append a reminder to the generated reply afterward.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx

from app.common.logging import get_logger
from app.common.safe_url import OutboundURLPolicy, safe_post, split_allowed_hosts
from app.common.types import CapabilityResult, RouteType, channel_id_value
from app.orchestrator.effect_handlers import effect_handler_opt_in_enabled
from app.orchestrator.effects import (
    EFFECT_COMMIT_SEMANTICS_AUDIT_AFTER_SIDE_EFFECT,
    EFFECT_COMMIT_SEMANTICS_GATE_BEFORE_SIDE_EFFECT,
    EffectCommitRecord,
)
from app.orchestrator.flow import MessageEffect, StepResult
from app.orchestrator.pipeline import PipelineContext
from app.plugin.hooks import HookAbort, HookPoint
from plugins.moderation.store import DEFAULT_REMINDER_TEXT, ModerationStore

logger = get_logger(__name__)

REMINDER_TEXT = DEFAULT_REMINDER_TEXT
VALID_REMINDER_MODES = {"off", "append", "replace"}


async def _scope_execution_allowed(
    store: ModerationStore,
    *,
    tenant_id: str,
    session_id: str,
) -> bool:
    """Fail closed when the moderation owner cannot be freshly authorized."""

    gate = getattr(store, "scope_execution_allowed", None)
    if not callable(gate):
        return False
    try:
        return (
            await gate(str(tenant_id or ""), str(session_id or ""))
            is True
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return False


def _clip(value: object, limit: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _is_wecom_webhook(webhook_url: str) -> bool:
    return webhook_url.startswith("https://qyapi.weixin.qq.com/cgi-bin/webhook/send")


def _normalize_mode(value: object) -> str:
    mode = str(value or "off").strip().lower()
    if mode in VALID_REMINDER_MODES:
        return mode
    return "off"


@dataclass
class ModerationAuditHook:
    store: ModerationStore
    audit_enabled: bool = True
    name: str = "moderation.audit"
    point: HookPoint = HookPoint.AFTER_PREPROCESS
    priority: int = 20

    async def run(self, ctx: PipelineContext) -> None:
        event = ctx.event
        pre = ctx.pre
        if pre is None:
            return

        cfg = await self.store.get_config(event.tenant_id, event.session_id)
        if not cfg or not cfg.get("enabled"):
            return

        matched = await self.store.match_keywords(
            event.tenant_id, event.session_id, pre.cleaned_text
        )
        if not matched:
            return

        reminder_mode = _normalize_mode(cfg.get("reminder_mode"))
        action = {
            "off": "flagged",
            "append": "reminder_append",
            "replace": "reminder_replace",
        }[reminder_mode]
        event_id: int | None = None
        webhook_status = (
            "pending"
            if cfg.get("webhook_enabled") and cfg.get("webhook_url") and not self.audit_enabled
            else ""
        )
        if self.audit_enabled:
            event_id = await self.store.log_event(
                tenant_id=event.tenant_id,
                session_id=event.session_id,
                user_id=event.user_id,
                message_text=pre.original_text,
                matched=matched,
                trace_id=event.trace_id,
                action=action,
                webhook_status="pending"
                if cfg.get("webhook_enabled") and cfg.get("webhook_url")
                else "",
                session_name=str(event.metadata.get("session_name") or ""),
                sender_name=str(event.metadata.get("sender_name") or ""),
            )
            webhook_status = await self._send_webhook_if_needed(
                ctx,
                cfg=cfg,
                matched=matched,
            )
            if webhook_status:
                await self.store.update_event(event_id, webhook_status=webhook_status)

        logger.warning(
            "moderation.keywords_matched",
            session_id=event.session_id,
            user_id=event.user_id,
            matched=matched,
            reminder_mode=reminder_mode,
            webhook_status=webhook_status or "",
        )
        ctx.extras["_moderation_matched"] = matched
        ctx.extras["_moderation_config"] = cfg
        ctx.extras["_moderation_event_id"] = event_id
        ctx.extras["_moderation_action"] = action
        ctx.extras["_moderation_webhook_status"] = webhook_status
        ctx.extras["_moderation_reminder_mode"] = reminder_mode
        ctx.extras["_moderation_reminder_text"] = self._reminder_text(cfg)

    @staticmethod
    def _reminder_text(cfg: dict) -> str:
        text = str(cfg.get("reminder_text") or "").strip()
        return text or REMINDER_TEXT

    @staticmethod
    def _webhook_text(ctx: PipelineContext, matched: list[str], cfg: dict) -> str:
        session_name = str(ctx.event.metadata.get("session_name") or ctx.event.session_id or "-")
        sender_name = str(ctx.event.metadata.get("sender_name") or ctx.event.user_id or "-")
        message_text = ctx.pre.original_text if ctx.pre else ""
        keywords = "、".join(matched) or "无"
        reminder_mode = ctx.extras.get("_moderation_reminder_mode") or _normalize_mode(
            cfg.get("reminder_mode")
        )
        return (
            "群敏感词告警\n"
            f"群聊：{_clip(session_name, 120)}\n"
            f"成员：{_clip(sender_name, 120)}\n"
            f"关键词：{_clip(keywords, 200)}\n"
            f"内容：{_clip(message_text, 1200)}\n"
            f"处置：{_clip(reminder_mode, 64)}"
        )

    async def _send_webhook_if_needed(
        self,
        ctx: PipelineContext,
        *,
        cfg: dict,
        matched: list[str],
    ) -> str:
        if not cfg.get("webhook_enabled"):
            return ""
        webhook_url = str(cfg.get("webhook_url") or "").strip()
        if not webhook_url:
            return "skipped:no_url"

        webhook_text = self._webhook_text(ctx, matched, cfg)
        payload = {
            "msg_type": "txt",
            "text": webhook_text,
            "tenant_id": ctx.event.tenant_id,
            "session_id": ctx.event.session_id,
            "user_id": ctx.event.user_id,
            "trace_id": ctx.event.trace_id,
            "channel": channel_id_value(ctx.event.channel),
            "source": str(ctx.event.metadata.get("source") or ""),
            "message_text": ctx.pre.original_text if ctx.pre else "",
            "matched_keywords": matched,
            "reminder_mode": ctx.extras.get("_moderation_reminder_mode") or _normalize_mode(
                cfg.get("reminder_mode")
            ),
            "reminder_text": self._reminder_text(cfg),
        }
        if _is_wecom_webhook(webhook_url):
            payload = {
                "msgtype": "text",
                "text": {
                    "content": webhook_text,
                },
            }
        settings = getattr(self.store, "settings", None)
        allowed_hosts = split_allowed_hosts(
            getattr(settings, "moderation_webhook_allowed_hosts", "qyapi.weixin.qq.com")
        )
        policy = OutboundURLPolicy(
            require_https=True,
            # An empty allowlist must fail closed instead of changing the
            # meaning of OutboundURLPolicy.allowed_hosts to "allow public".
            allowed_hosts=allowed_hosts or frozenset({"invalid.invalid"}),
            max_redirects=0,
            max_response_bytes=64 * 1024,
            timeout_seconds=5.0,
            allowed_response_content_types=(
                "application/json",
                "application/problem+json",
                "text/plain",
            ),
        )
        try:
            async with httpx.AsyncClient(
                timeout=5.0,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                # Matching/config/audit work can race with a global or scoped
                # disable.  Re-authorize at the irreversible privacy-egress
                # boundary instead of relying on the pipeline entry gate.
                if not await _scope_execution_allowed(
                    self.store,
                    tenant_id=ctx.event.tenant_id,
                    session_id=ctx.event.session_id,
                ):
                    return "skipped:scope_disabled"
                resp = await safe_post(
                    client,
                    webhook_url,
                    json=payload,
                    policy=policy,
                )
            if 200 <= resp.status_code < 300:
                return "sent"
            return f"error:{resp.status_code}"
        except Exception as exc:
            logger.warning(
                "moderation.webhook_failed",
                session_id=ctx.event.session_id,
                error_class=exc.__class__.__name__,
            )
            return f"error:{exc.__class__.__name__}"


@dataclass
class ModerationReplaceReminderHook:
    store: ModerationStore
    name: str = "moderation.reminder_replace"
    point: HookPoint = HookPoint.BEFORE_CAPABILITY
    priority: int = 5

    async def run(self, ctx: PipelineContext) -> None:
        if ctx.extras.get("_moderation_reminder_mode") != "replace":
            return
        reminder_text = str(ctx.extras.get("_moderation_reminder_text") or REMINDER_TEXT)
        raise HookAbort(reminder_text, reason="moderation_reminder_replace")


@dataclass
class ModerationAppendReminderHook:
    store: ModerationStore
    name: str = "moderation.reminder_append"
    point: HookPoint = HookPoint.AFTER_CAPABILITY
    priority: int = 95

    async def run(self, ctx: PipelineContext) -> None:
        result = ctx.result
        if result is None:
            return
        if ctx.extras.get("_moderation_reminder_mode") != "append":
            return
        reminder_text = str(ctx.extras.get("_moderation_reminder_text") or REMINDER_TEXT)
        if reminder_text in result.reply_text:
            return
        base = result.reply_text.strip()
        result.reply_text = (
            f"{base}\n\n{reminder_text}" if base else reminder_text
        )


def _set_input_signal(ctx: PipelineContext) -> dict[str, object]:
    matched = list(ctx.extras.get("_moderation_matched") or [])
    cfg = ctx.extras.get("_moderation_config")
    config = dict(cfg) if isinstance(cfg, dict) else {}
    signal: dict[str, object] = {
        "matched": bool(matched),
        "keywords": matched,
    }
    if config:
        signal.update(
            {
                "enabled": bool(config.get("enabled")),
                "reminder_mode": str(
                    ctx.extras.get("_moderation_reminder_mode")
                    or _normalize_mode(config.get("reminder_mode"))
                ),
                "reminder_text": str(
                    ctx.extras.get("_moderation_reminder_text") or REMINDER_TEXT
                ),
                "webhook_enabled": bool(config.get("webhook_enabled")),
            }
        )
    ctx.signals.setdefault("moderation", {})["input"] = signal
    return signal


def _moderation_audit_effect(
    ctx: PipelineContext,
    signal: dict[str, object],
    *,
    audit_as_effect: bool = False,
) -> MessageEffect:
    cfg = ctx.extras.get("_moderation_config")
    config = dict(cfg) if isinstance(cfg, dict) else {}
    return MessageEffect(
        type="write_audit_event",
        owner="moderation",
        payload={
            "commit_semantics": (
                EFFECT_COMMIT_SEMANTICS_GATE_BEFORE_SIDE_EFFECT
                if audit_as_effect
                else EFFECT_COMMIT_SEMANTICS_AUDIT_AFTER_SIDE_EFFECT
            ),
            "tenant_id": ctx.event.tenant_id,
            "session_id": ctx.event.session_id,
            "user_id": ctx.event.user_id,
            "session_name": str(ctx.event.metadata.get("session_name") or ""),
            "sender_name": str(ctx.event.metadata.get("sender_name") or ""),
            "message_text": ctx.pre.original_text if ctx.pre else "",
            "event_id": ctx.extras.get("_moderation_event_id"),
            "action": str(ctx.extras.get("_moderation_action") or "flagged"),
            "keywords": list(signal.get("keywords") or []),
            "reminder_mode": str(signal.get("reminder_mode") or ""),
            "reminder_text": str(signal.get("reminder_text") or REMINDER_TEXT),
            "webhook_enabled": bool(signal.get("webhook_enabled")),
            "webhook_url": str(config.get("webhook_url") or ""),
            "webhook_status": str(ctx.extras.get("_moderation_webhook_status") or ""),
            "trace_id": ctx.event.trace_id,
        },
        idempotency_key=(
            "moderation:audit:"
            f"{ctx.event.tenant_id}:{ctx.event.session_id}:{ctx.event.trace_id}"
        ),
    )


@dataclass
class ModerationInspectInputStep:
    store: ModerationStore
    effect_handler_enabled: bool = False
    kind: str = "plugin.moderation.inspect_input"
    owner: str = "moderation"
    name: str = "Inspect input moderation"
    permissions: list[str] = field(default_factory=lambda: ["storage:shared"])
    inputs: set[str] = field(default_factory=lambda: {"event", "session", "pre"})
    outputs: set[str] = field(
        default_factory=lambda: {"signals.moderation.input", "effects.write_audit_event"}
    )
    timeout_seconds: float = 1.5
    error_policy: str = "fail_open"

    async def run(self, ctx: PipelineContext) -> StepResult:
        if ctx.pre is None:
            ctx.signals.setdefault("moderation", {})["input"] = {
                "matched": False,
                "keywords": [],
                "reason": "no_preprocessed_message",
            }
            return StepResult(reason="no_preprocessed_message")
        audit_as_effect = self.effect_handler_enabled or effect_handler_opt_in_enabled(
            ctx,
            effect_type="write_audit_event",
            owner="moderation",
        )
        await ModerationAuditHook(
            self.store,
            audit_enabled=not audit_as_effect,
        ).run(ctx)
        signal = _set_input_signal(ctx)
        effects = [
            _moderation_audit_effect(ctx, signal, audit_as_effect=audit_as_effect)
        ] if signal["matched"] else []
        return StepResult(
            reason="matched" if signal["matched"] else "not_matched",
            effects=effects,
        )


@dataclass
class ModerationAuditEffectHandler:
    store: ModerationStore

    async def __call__(
        self,
        effect: MessageEffect,
        ctx: PipelineContext,
        record: EffectCommitRecord,
    ) -> None:
        _ = record
        payload = dict(effect.payload)
        matched = [str(item) for item in payload.get("keywords") or [] if str(item)]
        if not matched:
            return
        cfg = {
            "webhook_enabled": bool(payload.get("webhook_enabled")),
            "webhook_url": str(payload.get("webhook_url") or ""),
            "reminder_mode": str(payload.get("reminder_mode") or "off"),
            "reminder_text": str(payload.get("reminder_text") or REMINDER_TEXT),
        }
        action = str(payload.get("action") or "flagged")
        event_id = await self.store.log_event(
            tenant_id=str(payload.get("tenant_id") or ctx.event.tenant_id),
            session_id=str(payload.get("session_id") or ctx.event.session_id),
            user_id=str(payload.get("user_id") or ctx.event.user_id),
            message_text=str(payload.get("message_text") or ""),
            matched=matched,
            trace_id=str(payload.get("trace_id") or ctx.event.trace_id or ctx.trace_id),
            action=action,
            webhook_status="pending" if cfg["webhook_enabled"] and cfg["webhook_url"] else "",
            session_name=str(payload.get("session_name") or ""),
            sender_name=str(payload.get("sender_name") or ""),
        )
        ctx.extras["_moderation_event_id"] = event_id
        ctx.extras["_moderation_action"] = action
        ctx.extras["_moderation_matched"] = matched
        ctx.extras["_moderation_config"] = cfg
        ctx.extras["_moderation_reminder_mode"] = _normalize_mode(cfg.get("reminder_mode"))
        ctx.extras["_moderation_reminder_text"] = str(
            cfg.get("reminder_text") or REMINDER_TEXT
        )
        webhook_status = await ModerationAuditHook(self.store)._send_webhook_if_needed(
            ctx,
            cfg=cfg,
            matched=matched,
        )
        if webhook_status:
            await self.store.update_event(event_id, webhook_status=webhook_status)
        ctx.extras["_moderation_webhook_status"] = webhook_status
        ctx.signals.setdefault("effects", {}).setdefault("moderation", []).append(
            {
                "type": effect.type,
                "owner": effect.owner,
                "idempotency_key": effect.idempotency_key,
                "event_id": int(event_id or 0),
                "webhook_status": webhook_status,
                "status": "recorded",
            }
        )


@dataclass
class ModerationEnforceInputStep:
    store: ModerationStore
    kind: str = "plugin.moderation.enforce_input"
    owner: str = "moderation"
    name: str = "Enforce input moderation"
    permissions: list[str] = field(default_factory=lambda: ["storage:shared"])
    inputs: set[str] = field(
        default_factory=lambda: {
            "event",
            "session",
            "pre",
            "route",
            "signals.moderation.input",
        }
    )
    outputs: set[str] = field(default_factory=lambda: {"result"})
    timeout_seconds: float = 1.0
    error_policy: str = "fail_closed"

    async def run(self, ctx: PipelineContext) -> StepResult:
        _ = self.store
        if ctx.extras.get("_moderation_reminder_mode") != "replace":
            return StepResult(reason="not_replace")
        reminder_text = str(ctx.extras.get("_moderation_reminder_text") or REMINDER_TEXT)
        return StepResult(
            action="stop",
            reason="moderation_reminder_replace",
            result=CapabilityResult(route=RouteType.CANNED, reply_text=reminder_text),
            finalize=True,
            skip_output_safety=True,
            route_label=RouteType.CANNED.value,
        )


@dataclass
class ModerationDecorateOutputStep:
    store: ModerationStore
    kind: str = "plugin.moderation.decorate_output"
    owner: str = "moderation"
    name: str = "Decorate moderated output"
    permissions: list[str] = field(default_factory=lambda: ["storage:shared"])
    inputs: set[str] = field(
        default_factory=lambda: {
            "event",
            "session",
            "result",
            "signals.moderation.input",
        }
    )
    outputs: set[str] = field(default_factory=lambda: {"result"})
    timeout_seconds: float = 1.0
    error_policy: str = "fail_open"

    async def run(self, ctx: PipelineContext) -> StepResult:
        if ctx.result is None:
            return StepResult(reason="no_result")
        before = ctx.result.reply_text
        await ModerationAppendReminderHook(self.store).run(ctx)
        if ctx.result.reply_text == before:
            return StepResult(reason="not_appended")
        return StepResult(reason="appended", result=ctx.result)
