from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.billing import BillingCoordinator, BillingReservation, BillingResource, BillingSubject
from app.channel.models import configuration_session_id
from app.commands import CommandDefinition, CommandRegistryService, CommandSkip
from app.common.canned import degradation_text
from app.common.logging import get_logger
from app.common.types import CapabilityResult, MessageType, RouteType
from app.orchestrator.effect_handlers import effect_handler_opt_in_enabled
from app.orchestrator.effects import (
    EFFECT_COMMIT_SEMANTICS_AUDIT_AFTER_SIDE_EFFECT,
    EFFECT_COMMIT_SEMANTICS_GATE_BEFORE_SIDE_EFFECT,
    EffectCommitRecord,
)
from app.orchestrator.flow import MessageEffect, StepResult
from app.orchestrator.outcome import RetryableProcessingError
from app.orchestrator.owner_gate import (
    DEFAULT_OWNER_GATE_TIMEOUT_SECONDS,
    OwnerExecutionGate,
    evaluate_owner_execution,
    owner_gate_failure_is_retryable,
)
from app.orchestrator.pipeline import PipelineContext
from app.plugin.hooks import RESULT_PRODUCER_OWNER_KEY, HookAbort, HookPoint
from plugins.commands.store import CommandStore

logger = get_logger(__name__)

_COMMAND_PREFIX_RE = re.compile(r"^\s*(?:@\S+[\s\u2005\u00a0]+)*")
_COMMAND_NOISE_CHARS = " \t\r\n\u2005\u00a0\u200b\u200c\u200d\u2060\ufeff'\"`“”‘’"
_MENTION_TOKEN_RE = re.compile(r"^@\S+$")
_CJK_RE = re.compile(r"[\u3400-\u9fff]")


def _strip_mention_prefix(text: str) -> str:
    return _COMMAND_PREFIX_RE.sub("", str(text or ""), count=1)


def _strip_command_noise_prefix(text: str) -> str:
    cleaned = _strip_mention_prefix(text)
    while cleaned:
        trimmed = cleaned.lstrip(_COMMAND_NOISE_CHARS)
        if trimmed == cleaned:
            break
        cleaned = trimmed
    return cleaned.strip()


def _parse_command(text: str) -> tuple[str, list[str]]:
    cleaned = _strip_command_noise_prefix(text)
    if not cleaned.startswith("/"):
        return "", []
    parts = cleaned.split()
    if not parts:
        return "", []
    return parts[0].strip().lower(), parts[1:]


def _parse_bare_chinese_command_alias(
    text: str,
    service: CommandRegistryService,
) -> tuple[str, list[str]]:
    cleaned = _strip_command_noise_prefix(text)
    parts = cleaned.split()
    if not parts:
        return "", []
    raw_command = parts[0].strip().lower()
    if not raw_command or raw_command.startswith("/") or not _CJK_RE.search(raw_command):
        return "", []
    command = f"/{raw_command}"
    if service.resolve(command) is None:
        return "", []
    return command, parts[1:]


def _normalize_command_args(args: list[str], *, mentioned_me: bool) -> list[str]:
    normalized = [str(item or "").strip() for item in args if str(item or "").strip()]
    if not mentioned_me:
        return normalized
    while normalized and _MENTION_TOKEN_RE.match(normalized[0]):
        normalized.pop(0)
    while normalized and _MENTION_TOKEN_RE.match(normalized[-1]):
        normalized.pop()
    return normalized


def _is_admin(ctx: PipelineContext, cfg: dict) -> bool:
    sender_id = str(
        ctx.event.user_id
        or ctx.event.metadata.get("sender_id")
        or ctx.event.metadata.get("sender_wxid")
        or ""
    ).strip()
    if not sender_id:
        return False
    return sender_id in set(cfg.get("admin_user_ids") or [])


def _enabled_command_scopes(
    *,
    command: str,
    definition: CommandDefinition,
    user_commands: set[str],
    admin_commands: set[str],
) -> tuple[bool, bool]:
    canonical_command = definition.normalized_command()
    if (
        definition.plugin_name == "credits"
        and canonical_command == "/榜单"
        and command in {canonical_command, *definition.normalized_aliases()}
    ):
        return canonical_command in user_commands, canonical_command in admin_commands
    return command in user_commands, command in admin_commands


async def _reserve_command_charge(
    billing: BillingCoordinator | None,
    ctx: PipelineContext,
    command: str,
    metadata: dict | None = None,
) -> BillingReservation | None:
    if billing is None or billing.provider("credits") is None:
        return None
    subject = BillingSubject(
        tenant_id=ctx.event.tenant_id,
        session_id=configuration_session_id(ctx.event, ctx.session),
        user_id=ctx.event.user_id,
        display_name=str(ctx.event.metadata.get("sender_name") or ""),
    )
    resource = BillingResource(
        kind="command",
        operation=command,
        reference=ctx.event.trace_id,
        metadata={"command": command, **dict(metadata or {})},
    )
    reservation = await billing.reserve(subject, resource)
    if reservation.amount <= 0:
        return reservation
    ctx.extras["_billing_command_reservation"] = reservation
    return reservation


async def _capture_command_charge(
    billing: BillingCoordinator | None,
    reservation: BillingReservation | None,
) -> None:
    if billing is None or reservation is None or reservation.amount <= 0:
        return
    await billing.capture(reservation)


async def _release_command_charge(
    billing: BillingCoordinator | None,
    reservation: BillingReservation | None,
) -> None:
    if billing is None or reservation is None or reservation.amount <= 0:
        return
    await billing.release(reservation)


def _command_billing_effects(ctx: PipelineContext) -> list[MessageEffect]:
    reservation = ctx.extras.get("_billing_command_reservation")
    if not isinstance(reservation, BillingReservation) or reservation.amount <= 0:
        return []
    payload = {
        "commit_semantics": EFFECT_COMMIT_SEMANTICS_AUDIT_AFTER_SIDE_EFFECT,
        "tenant_id": reservation.subject.tenant_id,
        "session_id": reservation.subject.session_id,
        "user_id": reservation.subject.user_id,
        "display_name": reservation.subject.display_name,
        "provider": reservation.provider,
        "reservation_id": reservation.reservation_id,
        "amount": reservation.amount,
        "currency": reservation.currency,
        "resource_kind": reservation.resource.kind,
        "resource_operation": reservation.resource.operation,
        "resource_reference": reservation.resource.reference,
        "resource_metadata": dict(reservation.resource.metadata),
        "trace_id": ctx.event.trace_id,
    }
    effects = [
        MessageEffect(
            type="reserve_credits",
            owner="commands",
            payload=dict(payload),
            idempotency_key=f"commands:reserve_credits:{reservation.reservation_id}",
        )
    ]
    settlement = str(ctx.extras.get("_billing_command_settlement") or "")
    if settlement in {"captured", "released"}:
        effect_type = "capture_credits" if settlement == "captured" else "release_credits"
        effects.append(
            MessageEffect(
                type=effect_type,
                owner="commands",
                payload={
                    **payload,
                    "commit_semantics": (
                        EFFECT_COMMIT_SEMANTICS_GATE_BEFORE_SIDE_EFFECT
                        if bool(ctx.extras.get("_billing_command_settlement_as_effect"))
                        else EFFECT_COMMIT_SEMANTICS_AUDIT_AFTER_SIDE_EFFECT
                    ),
                    "settlement": settlement,
                },
                idempotency_key=(f"commands:{effect_type}:{reservation.reservation_id}"),
            )
        )
    return effects


@dataclass
class CommandDispatchOutcome:
    reply_text: str
    reason: str
    command: str
    plugin_name: str
    suppress_outbound: bool = False


def _is_group_command_context(ctx: PipelineContext) -> bool:
    event_kind = str(ctx.event.metadata.get("session_kind") or "").strip().lower()
    if event_kind:
        return event_kind == "group"
    if ctx.session is not None:
        session_kind = str((ctx.session.metadata or {}).get("session_kind") or "").strip().lower()
        if session_kind:
            return session_kind == "group"
    return str(ctx.event.session_id or "").lower().endswith("@chatroom")


def _silent_group_slash_outcome(
    ctx: PipelineContext,
    *,
    explicit_slash_candidate: bool,
    command: str,
    reason: str,
    plugin_name: str = "",
) -> CommandDispatchOutcome | None:
    """Consume rejected group slash commands without falling through to Agent/LLM."""

    if not explicit_slash_candidate or not _is_group_command_context(ctx):
        return None
    signal = ctx.signals.setdefault("command", {})
    signal["candidate"] = True
    signal["suppressed"] = True
    logger.info(
        "commands.group_slash_suppressed",
        tenant_id=ctx.event.tenant_id,
        session_id=ctx.event.session_id,
        user_id=ctx.event.user_id,
        plugin_name=plugin_name,
        command=command,
        reason=reason,
    )
    return CommandDispatchOutcome(
        reply_text="",
        reason=reason,
        command=command,
        plugin_name=plugin_name or "commands",
        suppress_outbound=True,
    )


def _apply_silent_command_suppression(ctx: PipelineContext) -> None:
    ctx.extras["interaction_mode"] = "observed"
    ctx.event.metadata["reply_allowed"] = False
    ctx.extras["suppress_outbound"] = True
    ctx.extras["skip_assistant_turn"] = True
    ctx.extras["skip_state_transition"] = True


async def _command_owner_allowed(
    *,
    ctx: PipelineContext,
    command: str,
    definition: CommandDefinition,
    owner_gate: OwnerExecutionGate | None,
    owner_gate_timeout_seconds: float,
) -> bool:
    """Gate the resolved command owner before invoking plugin-owned code."""

    plugin_name = str(definition.plugin_name or "").strip()
    if not plugin_name:
        ctx.signals["command"] = {
            "matched": False,
            "command": command,
            "plugin_name": "",
            "reason": "owner_missing",
            "owner_gate_reason": "command_owner_missing",
        }
        return False

    decision = await evaluate_owner_execution(
        owner_gate,
        plugin_name,
        ctx,
        timeout_seconds=owner_gate_timeout_seconds,
    )
    if decision.allowed:
        return True

    if owner_gate_failure_is_retryable(decision.reason):
        ctx.signals["command"] = {
            "matched": False,
            "command": command,
            "plugin_name": plugin_name,
            "reason": "owner_gate_unavailable",
            "owner_gate_reason": decision.reason,
        }
        raise RetryableProcessingError(
            decision.reason,
            error_type="CommandOwnerGateUnavailable",
        )

    ctx.signals["command"] = {
        "matched": False,
        "command": command,
        "plugin_name": plugin_name,
        "reason": "owner_disabled",
        "owner_gate_reason": decision.reason,
    }
    logger.info(
        "commands.owner_skipped",
        tenant_id=ctx.event.tenant_id,
        session_id=ctx.event.session_id,
        plugin_name=plugin_name,
        command=command,
        reason=decision.reason,
    )
    return False


async def dispatch_command(
    *,
    ctx: PipelineContext,
    store: CommandStore,
    service: CommandRegistryService,
    billing: BillingCoordinator | None = None,
    billing_effect_handler_enabled: bool = False,
    owner_gate: OwnerExecutionGate | None = None,
    owner_gate_timeout_seconds: float = DEFAULT_OWNER_GATE_TIMEOUT_SECONDS,
) -> CommandDispatchOutcome | None:
    event = ctx.event
    pre = ctx.pre
    ctx.signals["command"] = {"matched": False}
    if event.message.type != MessageType.TEXT:
        ctx.signals["command"]["reason"] = "non_text"
        return None
    if bool(event.metadata.get("is_self_sent")):
        ctx.signals["command"]["reason"] = "self_sent"
        return None

    command, args = _parse_command(str(event.message.content or ""))
    if not command and pre is not None:
        command, args = _parse_command(pre.cleaned_text)
    if not command and pre is not None:
        command, args = _parse_command(pre.original_text)
    explicit_slash_candidate = bool(command)
    if not command:
        command, args = _parse_bare_chinese_command_alias(str(event.message.content or ""), service)
    if not command and pre is not None:
        command, args = _parse_bare_chinese_command_alias(pre.cleaned_text, service)
    if not command and pre is not None:
        command, args = _parse_bare_chinese_command_alias(pre.original_text, service)
    if not command:
        ctx.signals["command"]["reason"] = "no_command"
        return None

    definition = service.resolve(command)
    if definition is None:
        ctx.signals["command"] = {
            "matched": False,
            "command": command,
            "reason": "unknown_command",
        }
        suppressed = _silent_group_slash_outcome(
            ctx,
            explicit_slash_candidate=explicit_slash_candidate,
            command=command,
            reason="unknown_command",
        )
        if suppressed is not None:
            return suppressed
        return None
    if not await _command_owner_allowed(
        ctx=ctx,
        command=command,
        definition=definition,
        owner_gate=owner_gate,
        owner_gate_timeout_seconds=owner_gate_timeout_seconds,
    ):
        suppressed = _silent_group_slash_outcome(
            ctx,
            explicit_slash_candidate=explicit_slash_candidate,
            command=command,
            reason=str(ctx.signals.get("command", {}).get("reason") or "owner_disabled"),
            plugin_name=definition.plugin_name,
        )
        if suppressed is not None:
            return suppressed
        return None
    if definition.should_handle is not None and not definition.should_handle(ctx):
        ctx.signals["command"] = {
            "matched": False,
            "command": command,
            "plugin_name": definition.plugin_name,
            "reason": "predicate_skipped",
        }
        suppressed = _silent_group_slash_outcome(
            ctx,
            explicit_slash_candidate=explicit_slash_candidate,
            command=command,
            reason="predicate_skipped",
            plugin_name=definition.plugin_name,
        )
        if suppressed is not None:
            return suppressed
        return None

    cfg = await store.get_config(
        event.tenant_id,
        catalog=service.catalog(),
    )
    user_commands = set(cfg.get("user_commands") or [])
    admin_commands = set(cfg.get("admin_commands") or [])
    user_enabled, admin_enabled = _enabled_command_scopes(
        command=command,
        definition=definition,
        user_commands=user_commands,
        admin_commands=admin_commands,
    )
    if definition.admin_only:
        user_enabled = False
    if not user_enabled and not admin_enabled:
        ctx.signals["command"] = {
            "matched": False,
            "command": command,
            "plugin_name": definition.plugin_name,
            "reason": "disabled",
        }
        if definition.admin_only and _is_admin(ctx, cfg):
            logger.info(
                "commands.admin_command_disabled",
                tenant_id=event.tenant_id,
                session_id=event.session_id,
                user_id=event.user_id,
                plugin_name=definition.plugin_name,
                command=command,
            )
        suppressed = _silent_group_slash_outcome(
            ctx,
            explicit_slash_candidate=explicit_slash_candidate,
            command=command,
            reason="disabled",
            plugin_name=definition.plugin_name,
        )
        if suppressed is not None:
            return suppressed
        return None
    if admin_enabled and not user_enabled and not _is_admin(ctx, cfg):
        ctx.signals["command"] = {
            "matched": True,
            "command": command,
            "plugin_name": definition.plugin_name,
            "denied": True,
            "reason": "command_denied",
        }
        suppressed = _silent_group_slash_outcome(
            ctx,
            explicit_slash_candidate=explicit_slash_candidate,
            command=command,
            reason="command_denied",
            plugin_name=definition.plugin_name,
        )
        if suppressed is not None:
            return suppressed
        return CommandDispatchOutcome(
            reply_text="你没有权限使用这个命令",
            reason="command_denied",
            command=command,
            plugin_name=definition.plugin_name,
        )

    reservation: BillingReservation | None = None
    canonical_command = definition.normalized_command()
    settle_as_effect = (
        billing_effect_handler_enabled
        or effect_handler_opt_in_enabled(
            ctx,
            effect_type="capture_credits",
            owner="commands",
        )
        or effect_handler_opt_in_enabled(
            ctx,
            effect_type="release_credits",
            owner="commands",
        )
    )
    handler_value_error = False
    try:
        ctx.extras["_command_token"] = command
        ctx.extras["_command_plugin"] = definition.plugin_name
        ctx.extras["_command_canonical"] = canonical_command
        normalized_args = _normalize_command_args(
            args,
            mentioned_me=bool(event.metadata.get("mentioned_me")),
        )
        billing_metadata = (
            definition.billing_metadata(ctx, normalized_args)
            if definition.billing_metadata is not None
            else {}
        )
        reservation = await _reserve_command_charge(
            billing,
            ctx,
            canonical_command,
            metadata=billing_metadata,
        )
        reply = await definition.handler(ctx, normalized_args)
    except CommandSkip:
        if reservation is not None and reservation.amount > 0:
            if settle_as_effect:
                ctx.extras["_billing_command_settlement_as_effect"] = True
            else:
                await _release_command_charge(billing, reservation)
            ctx.extras["_billing_command_settlement"] = "released"
        ctx.signals["command"] = {
            "matched": False,
            "command": command,
            "plugin_name": definition.plugin_name,
            "reason": "handler_skipped",
        }
        suppressed = _silent_group_slash_outcome(
            ctx,
            explicit_slash_candidate=explicit_slash_candidate,
            command=command,
            reason="handler_skipped",
            plugin_name=definition.plugin_name,
        )
        if suppressed is not None:
            return suppressed
        return None
    except ValueError as exc:
        reply = str(exc)
        handler_value_error = True
    except Exception:
        if reservation is not None and reservation.amount > 0:
            await _release_command_charge(billing, reservation)
            ctx.extras["_billing_command_settlement"] = "released"
        raise

    # The handler may have been captured before an operator disabled its
    # plugin. Re-read durable owner state after it returns and before accepting
    # its reply or settling command billing.
    try:
        owner_still_allowed = await _command_owner_allowed(
            ctx=ctx,
            command=command,
            definition=definition,
            owner_gate=owner_gate,
            owner_gate_timeout_seconds=owner_gate_timeout_seconds,
        )
    except RetryableProcessingError:
        if reservation is not None and reservation.amount > 0:
            await _release_command_charge(billing, reservation)
            ctx.extras["_billing_command_settlement"] = "released"
        raise
    if not owner_still_allowed:
        if reservation is not None and reservation.amount > 0:
            await _release_command_charge(billing, reservation)
            ctx.extras["_billing_command_settlement"] = "released"
        suppressed = _silent_group_slash_outcome(
            ctx,
            explicit_slash_candidate=explicit_slash_candidate,
            command=command,
            reason=str(ctx.signals.get("command", {}).get("reason") or "owner_disabled"),
            plugin_name=definition.plugin_name,
        )
        if suppressed is not None:
            return suppressed
        return None

    if handler_value_error or ctx.extras.get("_billing_command_force_release"):
        if reservation is not None and reservation.amount > 0:
            if settle_as_effect:
                ctx.extras["_billing_command_settlement_as_effect"] = True
            else:
                await _release_command_charge(billing, reservation)
            ctx.extras["_billing_command_settlement"] = "released"
    elif not ctx.extras.get("_billing_command_deferred"):
        if reservation is not None and reservation.amount > 0:
            if settle_as_effect:
                ctx.extras["_billing_command_settlement_as_effect"] = True
            else:
                await _capture_command_charge(billing, reservation)
            ctx.extras["_billing_command_settlement"] = "captured"

    ctx.signals["command"] = {
        "matched": True,
        "command": command,
        "canonical_command": canonical_command,
        "plugin_name": definition.plugin_name,
    }
    # The command definition came from the owner-bound registry. This is a
    # trusted adapter attribution, not a claim made by the handler reply.
    ctx.extras[RESULT_PRODUCER_OWNER_KEY] = str(definition.plugin_name or "").strip()
    logger.info(
        "commands.command_triggered",
        tenant_id=event.tenant_id,
        session_id=event.session_id,
        user_id=event.user_id,
        plugin_name=definition.plugin_name,
        command=command,
    )
    return CommandDispatchOutcome(
        reply_text=reply,
        reason=f"{definition.plugin_name}_command",
        command=command,
        plugin_name=definition.plugin_name,
    )


@dataclass
class CommandCenterHook:
    store: CommandStore
    service: CommandRegistryService
    billing: BillingCoordinator | None = None
    name: str = "commands.center"
    point: HookPoint = HookPoint.BEFORE_ROUTE
    priority: int = 10
    owner_gate: OwnerExecutionGate | None = None
    owner_gate_timeout_seconds: float = DEFAULT_OWNER_GATE_TIMEOUT_SECONDS

    async def run(self, ctx: PipelineContext) -> None:
        try:
            outcome = await dispatch_command(
                ctx=ctx,
                store=self.store,
                service=self.service,
                billing=self.billing,
                owner_gate=self.owner_gate,
                owner_gate_timeout_seconds=self.owner_gate_timeout_seconds,
            )
        except RetryableProcessingError:
            raise
        except Exception as exc:
            logger.exception(
                "commands.dispatch_failed",
                tenant_id=ctx.event.tenant_id,
                session_id=ctx.event.session_id,
                trace_id=ctx.event.trace_id,
                error=str(exc),
            )
            raise HookAbort(
                degradation_text("command_service_unavailable"),
                reason="command_service_unavailable",
            ) from exc
        if outcome is None:
            return
        if outcome.suppress_outbound:
            _apply_silent_command_suppression(ctx)
            raise HookAbort("", reason=outcome.reason)
        abort = HookAbort(outcome.reply_text, reason=outcome.reason)
        abort.bind_result_producer_owner(outcome.plugin_name)
        raise abort


@dataclass
class CommandDispatchStep:
    store: CommandStore
    service: CommandRegistryService
    billing: BillingCoordinator | None = None
    effect_handler_enabled: bool = False
    kind: str = "plugin.commands.dispatch"
    owner: str = "commands"
    name: str = "Command dispatch"
    permissions: list[str] = field(default_factory=lambda: ["commands"])
    inputs: set[str] = field(default_factory=lambda: {"event", "session", "pre"})
    outputs: set[str] = field(default_factory=lambda: {"signals.command", "result"})
    timeout_seconds: float = 5.0
    error_policy: str = "fail_closed"
    owner_gate: OwnerExecutionGate | None = None
    owner_gate_timeout_seconds: float = DEFAULT_OWNER_GATE_TIMEOUT_SECONDS

    async def run(self, ctx: PipelineContext) -> StepResult:
        try:
            outcome = await dispatch_command(
                ctx=ctx,
                store=self.store,
                service=self.service,
                billing=self.billing,
                billing_effect_handler_enabled=self.effect_handler_enabled,
                owner_gate=self.owner_gate,
                owner_gate_timeout_seconds=self.owner_gate_timeout_seconds,
            )
        except RetryableProcessingError:
            raise
        except Exception as exc:
            ctx.signals["command"] = {
                "matched": True,
                "reason": "command_service_unavailable",
                "error_class": exc.__class__.__name__,
            }
            logger.exception(
                "commands.dispatch_step_failed",
                tenant_id=ctx.event.tenant_id,
                session_id=ctx.event.session_id,
                trace_id=ctx.event.trace_id,
                error=str(exc),
            )
            return StepResult(
                action="stop",
                reason="command_service_unavailable",
                result=CapabilityResult(
                    route=RouteType.CANNED,
                    reply_text=degradation_text("command_service_unavailable"),
                ),
                finalize=True,
                skip_output_safety=True,
                route_label=RouteType.CANNED.value,
                effects=_command_billing_effects(ctx),
            )
        if outcome is None:
            reason = str(ctx.signals.get("command", {}).get("reason") or "no_command")
            return StepResult(reason=reason, effects=_command_billing_effects(ctx))
        if outcome.suppress_outbound:
            _apply_silent_command_suppression(ctx)
            return StepResult(
                action="stop",
                reason=outcome.reason,
                append_assistant_turn=False,
                publish_outbound=False,
                effects=_command_billing_effects(ctx),
            )
        return StepResult(
            action="stop",
            reason=outcome.reason,
            result=CapabilityResult(route=RouteType.CANNED, reply_text=outcome.reply_text),
            finalize=True,
            skip_output_safety=True,
            route_label=RouteType.CANNED.value,
            effects=_command_billing_effects(ctx),
        )


@dataclass
class CommandBillingAuditEffectHandler:
    """Record already-executed command billing effects without repeating writes."""

    async def __call__(
        self,
        effect: MessageEffect,
        ctx: PipelineContext,
        record: EffectCommitRecord,
    ) -> None:
        _ = record
        ctx.signals.setdefault("effects", {}).setdefault("commands", []).append(
            {
                "type": effect.type,
                "owner": effect.owner,
                "idempotency_key": effect.idempotency_key,
                "status": "audited",
            }
        )


@dataclass
class CommandBillingSettlementEffectHandler:
    billing: BillingCoordinator

    async def __call__(
        self,
        effect: MessageEffect,
        ctx: PipelineContext,
        record: EffectCommitRecord,
    ) -> None:
        _ = ctx, record
        reservation = _reservation_from_effect(effect)
        if effect.type == "capture_credits":
            await self.billing.capture(reservation)
            status = "captured"
        elif effect.type == "release_credits":
            await self.billing.release(reservation)
            status = "released"
        else:
            raise ValueError(f"unsupported command billing effect: {effect.type}")
        ctx.signals.setdefault("effects", {}).setdefault("commands", []).append(
            {
                "type": effect.type,
                "owner": effect.owner,
                "idempotency_key": effect.idempotency_key,
                "reservation_id": reservation.reservation_id,
                "amount": reservation.amount,
                "status": status,
            }
        )


def _reservation_from_effect(effect: MessageEffect) -> BillingReservation:
    payload = dict(effect.payload)
    reservation_id = str(payload.get("reservation_id") or "").strip()
    if not reservation_id:
        raise ValueError("command billing effect missing reservation_id")
    subject = BillingSubject(
        tenant_id=str(payload.get("tenant_id") or ""),
        session_id=str(payload.get("session_id") or ""),
        user_id=str(payload.get("user_id") or ""),
        display_name=str(payload.get("display_name") or ""),
    )
    metadata = payload.get("resource_metadata")
    resource = BillingResource(
        kind=str(payload.get("resource_kind") or "command"),
        operation=str(payload.get("resource_operation") or ""),
        reference=str(payload.get("resource_reference") or payload.get("trace_id") or ""),
        metadata=dict(metadata) if isinstance(metadata, dict) else {},
    )
    return BillingReservation(
        provider=str(payload.get("provider") or "credits"),
        subject=subject,
        resource=resource,
        amount=int(payload.get("amount") or 0),
        currency=str(payload.get("currency") or "credits"),
        reservation_id=reservation_id,
    )
