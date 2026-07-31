"""Dispatch layer for committed message effects.

Existing flow steps still execute most real side effects directly. This module
provides the narrow registry and dispatcher surface needed to move those side
effects behind ``MessageEffect`` handlers incrementally.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol

from app.channel import (
    ChannelFile,
    ChannelMedia,
    ChannelOutbound,
    ChannelRegistry,
    ChannelSendOptions,
    ChannelTarget,
)
from app.common.types import SessionState, Turn, channel_id_value
from app.orchestrator.effects import (
    EFFECT_STATUS_DRY_RUN,
    EFFECT_STATUS_DUPLICATE,
    EFFECT_STATUS_RECORDED,
    EffectCommitRecord,
    EffectCommitter,
)
from app.orchestrator.flow import MessageEffect
from app.orchestrator.owner_gate import (
    DEFAULT_OWNER_GATE_TIMEOUT_SECONDS,
    OwnerExecutionGate,
    evaluate_owner_execution,
    owner_gate_failure_is_retryable,
)
from app.orchestrator.pipeline import PipelineContext
from plugins.memory.observability import build_safe_memory_profile_signal

EFFECT_HANDLER_STATUS_HANDLER_ERROR = "handler_error"
EFFECT_HANDLER_STATUS_NO_HANDLER = "no_handler"
EFFECT_HANDLER_STATUS_DISABLED = "handler_disabled"
EFFECT_HANDLER_STATUS_OWNER_SKIPPED = "owner_skipped"

EffectHandlerKey = tuple[str, str]
EFFECT_HANDLER_OPT_IN_KEYS = (
    "effect_handlers_enabled",
    "effect_handler_opt_in",
    "handler_opt_in",
    "enabled_handlers",
)


class EffectOwnerExecutionDenied(RuntimeError):
    """Signal an intentional last-hop suppression by a nested owner gate."""

    def __init__(self, owner: str, *, reason: str = "owner_execution_denied") -> None:
        self.owner = str(owner or "").strip()
        self.reason = str(reason or "owner_execution_denied").strip()
        super().__init__("effect_owner_execution_denied")


class EffectHandler(Protocol):
    async def __call__(
        self,
        effect: MessageEffect,
        ctx: PipelineContext,
        record: EffectCommitRecord,
    ) -> None: ...


@dataclass(frozen=True)
class EffectDispatchRecord:
    type: str
    owner: str
    idempotency_key: str
    payload: dict[str, object] = field(default_factory=dict)
    status: str = EFFECT_STATUS_RECORDED
    commit_status: str = EFFECT_STATUS_RECORDED
    error: str = ""
    commit_error: str = ""
    dry_run: bool = False


class EffectHandlerRegistry:
    """Registry keyed by ``MessageEffect.type`` and ``MessageEffect.owner``."""

    def __init__(self) -> None:
        self._handlers: dict[EffectHandlerKey, EffectHandler] = {}

    def register(
        self,
        effect_type: str,
        owner: str,
        handler: EffectHandler,
        *,
        replace: bool = False,
    ) -> None:
        key = _handler_key(effect_type, owner)
        if key in self._handlers and not replace:
            raise ValueError(f"effect handler already registered: {key[1]}:{key[0]}")
        self._handlers[key] = handler

    def get(self, effect_type: str, owner: str) -> EffectHandler | None:
        key = _handler_key(effect_type, owner)
        handler = self._handlers.get(key)
        if handler is not None:
            return handler
        # One-release compatibility for durable privacy intents created
        # before member erasure moved from the optional memory owner into the
        # always-on kernel compensation path.
        if key == ("forget_member", "memory"):
            return self._handlers.get(("forget_member", "core"))
        if key[0] == "enqueue_channel_reply":
            return self._handlers.get((key[0], "channel"))
        return None

    def require(self, effect_type: str, owner: str) -> EffectHandler:
        key = _handler_key(effect_type, owner)
        handler = self._handlers.get(key)
        if handler is None:
            raise LookupError(f"missing effect handler: {key[1]}:{key[0]}")
        return handler

    def list_handlers(self) -> list[dict[str, str]]:
        return [
            {
                "type": effect_type,
                "owner": owner,
                "handler": handler.__class__.__name__,
            }
            for (effect_type, owner), handler in sorted(
                self._handlers.items(),
                key=lambda item: (item[0][1], item[0][0]),
            )
        ]


def effect_handler_registry_payload(
    registry: EffectHandlerRegistry | None,
) -> dict[str, object]:
    """Return a compact, trace-safe snapshot of registered effect handlers."""

    items = registry.list_handlers() if registry is not None else []
    fallbacks = [
        {
            "type": item["type"],
            "owner": item["owner"],
            "fallback_for": "missing exact channel owner",
        }
        for item in items
        if item["type"] == "enqueue_channel_reply" and item["owner"] == "channel"
    ]
    return {
        "count": len(items),
        "owners": sorted({item["owner"] for item in items}),
        "types": sorted({item["type"] for item in items}),
        "fallbacks": fallbacks,
        "items": items,
    }


def effect_handler_opt_in_enabled(
    ctx: PipelineContext,
    *,
    effect_type: str = "",
    owner: str = "",
) -> bool:
    """Return whether an executor should defer a real side effect to a handler.

    FlowRunner wiring is intentionally landing incrementally. Accepting both a
    process-wide boolean and per-effect selectors lets plugin executors opt in
    without depending on one exact runner implementation detail.
    """

    effect_type = str(effect_type or "").strip()
    owner = str(owner or "").strip()
    candidates: list[object] = []
    effects_signal = ctx.signals.get("effects")
    if isinstance(effects_signal, dict):
        candidates.extend(effects_signal.get(key) for key in EFFECT_HANDLER_OPT_IN_KEYS)
    candidates.extend(ctx.extras.get(key) for key in EFFECT_HANDLER_OPT_IN_KEYS)
    return any(
        _opt_in_value_matches(value, owner=owner, effect_type=effect_type) for value in candidates
    )


def _set_memory_save_runtime_signal(
    ctx: PipelineContext,
    *,
    status: str,
    reason: str,
    error_type: str = "",
) -> None:
    runtime = ctx.signals.setdefault("memory", {}).setdefault("runtime", {})
    save = runtime.setdefault("save", {})
    save["status"] = status
    save["reason"] = reason
    if error_type:
        save["error_type"] = error_type.lower()[:64]
    else:
        save.pop("error_type", None)


@dataclass
class MemorySaveEffectHandler:
    """Persist ``memory:save_memory`` effects through the memory store."""

    store: Any

    async def __call__(
        self,
        effect: MessageEffect,
        ctx: PipelineContext,
        record: EffectCommitRecord,
    ) -> None:
        _ = record
        payload = dict(effect.payload)
        tenant_id = str(payload.get("tenant_id") or ctx.event.tenant_id)
        channel = str(payload.get("channel") or channel_id_value(ctx.event.channel))
        source_key = str(payload.get("source_key") or ctx.event.metadata.get("source") or "*")
        user_id = str(payload.get("user_id") or ctx.event.user_id)
        session_id = str(payload.get("session_id") or ctx.event.session_id)
        trace_id = str(payload.get("trace_id") or ctx.event.trace_id or ctx.trace_id)
        user_text = str(payload.get("user_text") or "").strip()
        if not user_text:
            if ctx.pre is not None:
                user_text = str(ctx.pre.cleaned_text or "").strip()
            if not user_text:
                user_text = str(ctx.event.message.content or "").strip()
        assistant_text = str(payload.get("assistant_text") or "").strip()
        if not assistant_text and ctx.reply is not None:
            assistant_text = str(ctx.reply.primary_text or "").strip()

        if not user_text:
            _set_memory_save_runtime_signal(
                ctx,
                status="skipped",
                reason="empty_user_text",
            )
            return

        try:
            remember_kwargs = {
                "tenant_id": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "user_id": user_id,
                "session_id": session_id,
                "user_text": user_text,
                "assistant_text": assistant_text,
                "trace_id": trace_id,
                "source_message_id": str(
                    payload.get("source_message_id") or ctx.event.message_id or ""
                ),
                "origin_session_kind": str(payload.get("origin_session_kind") or "unknown"),
                "audience_scope": str(payload.get("audience_scope") or "private"),
                "allowed_session_ids": list(payload.get("allowed_session_ids") or []),
                "sensitivity_category": str(payload.get("sensitivity_category") or "normal"),
                "expires_at": payload.get("expires_at"),
                "source_kind": str(payload.get("source_kind") or "conversation"),
            }
            if payload.get("identity_scope") is False:
                remember_kwargs["identity_scope"] = False
            profile = await self.store.remember_interaction(**remember_kwargs)
            if not isinstance(profile, dict):
                profile = {}
            if not bool(payload.get("identity_scope", True)):
                profile = _session_only_memory_profile(profile, session_id=session_id)
            ctx.extras["user_memory_profile"] = profile
            ctx.signals.setdefault("memory", {})["user_profile"] = (
                build_safe_memory_profile_signal(profile)
            )
            if ctx.session is not None:
                ctx.session.variables["user_memory"] = _memory_session_payload(
                    user_id=user_id,
                    channel=channel,
                    source_key=source_key,
                    session_id=session_id,
                    profile=profile,
                )
        except Exception as exc:
            _set_memory_save_runtime_signal(
                ctx,
                status="error",
                reason="persistence_failed",
                error_type=exc.__class__.__name__,
            )
            raise
        _set_memory_save_runtime_signal(
            ctx,
            status="success",
            reason="saved",
        )


@dataclass
class CoreAppendTurnEffectHandler:
    """Append a session turn after the effect idempotency gate is recorded."""

    session_manager: Any

    async def __call__(
        self,
        effect: MessageEffect,
        ctx: PipelineContext,
        record: EffectCommitRecord,
    ) -> None:
        _ = record
        if ctx.session is None:
            raise RuntimeError("session_required")
        payload = dict(effect.payload)
        turn_payload = payload.get("turn")
        if not isinstance(turn_payload, dict):
            raise ValueError("append turn effect missing turn payload")
        turn = Turn.model_validate(turn_payload)
        await self.session_manager.append_turn(ctx.session, turn)
        ctx.signals.setdefault("effects", {}).setdefault("session_turns", []).append(
            {
                "type": effect.type,
                "owner": effect.owner,
                "session_id": turn.session_id,
                "role": turn.role.value,
                "turn_id": turn.turn_id,
            }
        )


@dataclass
class CoreSetSessionStateEffectHandler:
    """Apply a session state transition after the effect idempotency gate."""

    session_manager: Any

    async def __call__(
        self,
        effect: MessageEffect,
        ctx: PipelineContext,
        record: EffectCommitRecord,
    ) -> None:
        _ = record
        if ctx.session is None:
            raise RuntimeError("session_required")
        payload = dict(effect.payload)
        state_value = str(payload.get("state") or "").strip()
        if not state_value:
            raise ValueError("session state effect missing state")
        new_state = SessionState(state_value)
        await self.session_manager.set_state(ctx.session, new_state)
        ctx.signals.setdefault("effects", {}).setdefault("session_states", []).append(
            {
                "type": effect.type,
                "owner": effect.owner,
                "session_id": ctx.session.session_id,
                "state": new_state.value,
            }
        )


@dataclass
class ChannelReplyEffectHandler:
    """Enqueue a channel reply through the registered outbound provider."""

    channel_registry: ChannelRegistry
    default_channel: str = ""
    owner_gate: OwnerExecutionGate | None = None
    owner_gate_timeout_seconds: float = DEFAULT_OWNER_GATE_TIMEOUT_SECONDS

    async def __call__(
        self,
        effect: MessageEffect,
        ctx: PipelineContext,
        record: EffectCommitRecord,
    ) -> None:
        _ = record
        payload = dict(effect.payload)
        if _reply_effect_already_enqueued(payload):
            return

        target = _channel_target_from_effect(payload, ctx, default_channel=self.default_channel)
        body = _reply_effect_body(payload)
        file = _reply_effect_file(payload)
        media = _reply_effect_media(payload)
        options = _channel_send_options_from_effect(payload, ctx, effect)
        outbound = await self._gated_outbound(target, ctx)

        if file is not None:
            result = await outbound.send_file(target, file, options)
        elif body:
            result = await outbound.send_text(target, body, options)
        elif media is not None:
            result = await outbound.send_image(target, media, options)
        else:
            raise ValueError("channel reply effect missing text body or supported media/file")

        if (
            file is not None
            and bool(dict(payload.get("delivery") or {}).get("must_deliver_file"))
            and bool(result.metadata.get("suppressed"))
        ):
            raise RuntimeError("channel_file_delivery_suppressed")

        ctx.signals.setdefault("effects", {}).setdefault("channel_replies", []).append(
            {
                "type": effect.type,
                "owner": effect.owner,
                "channel": target.channel,
                "message_id": result.message_id,
                "provider": result.provider,
                "metadata": dict(result.metadata),
            }
        )

    async def _gated_outbound(
        self,
        target: ChannelTarget,
        ctx: PipelineContext,
    ) -> ChannelOutbound:
        if self.owner_gate is None:
            return self.channel_registry.require_outbound_for_target(target)
        for _attempt in range(3):
            binding_owner = str(self.channel_registry.owner_for_target(target) or "").strip()
            if binding_owner:
                decision = await evaluate_owner_execution(
                    self.owner_gate,
                    binding_owner,
                    ctx,
                    timeout_seconds=self.owner_gate_timeout_seconds,
                )
                if not decision.allowed:
                    raise EffectOwnerExecutionDenied(
                        binding_owner,
                        reason=decision.reason,
                    )
            if binding_owner == str(self.channel_registry.owner_for_target(target) or "").strip():
                return self.channel_registry.require_outbound_for_target(target)
        raise EffectOwnerExecutionDenied(
            binding_owner,
            reason="channel_binding_changed_during_gate",
        )


@dataclass
class CorePublishOutboundEffectHandler:
    """Publish generic outbound replies after effect commit succeeds."""

    bus: Any
    default_stream: str = ""

    async def __call__(
        self,
        effect: MessageEffect,
        ctx: PipelineContext,
        record: EffectCommitRecord,
    ) -> None:
        _ = record
        payload = dict(effect.payload)
        outbound_payload = payload.get("payload")
        if not isinstance(outbound_payload, dict):
            raise ValueError("publish_outbound effect missing payload")
        stream = _first_non_empty(payload.get("stream"), self.default_stream)
        if not stream:
            raise ValueError("publish_outbound effect missing stream")
        tenant_id = _first_non_empty(
            payload.get("tenant_id"),
            outbound_payload.get("tenant_id"),
            ctx.event.tenant_id,
        )
        session_id = _first_non_empty(
            payload.get("session_id"),
            outbound_payload.get("session_id"),
            ctx.event.session_id,
        )
        if not tenant_id or not session_id:
            raise ValueError("publish_outbound effect missing tenant/session scope")
        partition_key = f"{tenant_id}:{session_id}"
        supplied_partition_key = _first_non_empty(payload.get("partition_key"))
        if supplied_partition_key and supplied_partition_key != partition_key:
            raise ValueError("publish_outbound effect partition scope mismatch")
        message_id = await self.bus.publish(
            stream,
            dict(outbound_payload),
            partition_key=partition_key,
        )
        ctx.signals.setdefault("effects", {}).setdefault("published_outbound", []).append(
            {
                "type": effect.type,
                "owner": effect.owner,
                "stream": stream,
                "partition_key": partition_key,
                "message_id": str(message_id or ""),
            }
        )


@dataclass
class WxbotReplyEffectHandler:
    """Compatibility handler for legacy ``enqueue_wxbot_reply`` effects."""

    channel_handler: ChannelReplyEffectHandler

    async def __call__(
        self,
        effect: MessageEffect,
        ctx: PipelineContext,
        record: EffectCommitRecord,
    ) -> None:
        payload = dict(effect.payload)
        if _reply_effect_already_enqueued(payload):
            return
        normalized = MessageEffect(
            type="enqueue_channel_reply",
            owner=effect.owner,
            payload={"channel": "wechat", **payload},
            idempotency_key=effect.idempotency_key,
            producer_owner=effect.producer_owner,
        )
        await self.channel_handler(normalized, ctx, record)


class EffectDispatcher:
    """Commit effects for idempotency before invoking registered handlers."""

    def __init__(
        self,
        registry: EffectHandlerRegistry,
        committer: EffectCommitter,
        *,
        enabled_handlers: object = True,
        owner_gate: OwnerExecutionGate | None = None,
        owner_gate_timeout_seconds: float = DEFAULT_OWNER_GATE_TIMEOUT_SECONDS,
    ) -> None:
        self._registry = registry
        self._committer = committer
        self._enabled_handlers = enabled_handlers
        self._owner_gate = owner_gate
        self._owner_gate_timeout_seconds = owner_gate_timeout_seconds

    async def dispatch(
        self,
        effect: MessageEffect,
        ctx: PipelineContext,
        *,
        sequence: int = 0,
        dry_run: bool = False,
    ) -> EffectDispatchRecord:
        record = await self._committer.commit(
            effect,
            ctx,
            sequence=sequence,
            dry_run=dry_run,
        )
        if record.status == EFFECT_STATUS_DUPLICATE:
            return _dispatch_record(record, status=EFFECT_STATUS_DUPLICATE)
        if dry_run or record.status == EFFECT_STATUS_DRY_RUN:
            return _dispatch_record(record, status=EFFECT_STATUS_DRY_RUN)

        normalized = MessageEffect(
            type=record.type,
            owner=record.owner,
            payload=dict(record.payload),
            idempotency_key=record.idempotency_key,
            producer_owner=str(effect.producer_owner or record.owner),
        )
        if not _opt_in_value_matches(
            self._enabled_handlers,
            owner=normalized.owner,
            effect_type=normalized.type,
        ):
            return _dispatch_record(
                record,
                status=EFFECT_HANDLER_STATUS_DISABLED,
            )
        gate_owners = tuple(
            dict.fromkeys((normalized.producer_owner, normalized.owner))
        )
        for gate_owner in gate_owners:
            decision = await evaluate_owner_execution(
                self._owner_gate,
                gate_owner,
                ctx,
                timeout_seconds=self._owner_gate_timeout_seconds,
            )
            if decision.allowed:
                continue
            return _dispatch_record(
                record,
                status=(
                    EFFECT_HANDLER_STATUS_HANDLER_ERROR
                    if owner_gate_failure_is_retryable(decision.reason)
                    else EFFECT_HANDLER_STATUS_OWNER_SKIPPED
                ),
                error=decision.reason,
            )
        handler = self._registry.get(normalized.type, normalized.owner)
        if handler is None:
            return _dispatch_record(
                record,
                status=EFFECT_HANDLER_STATUS_NO_HANDLER,
            )
        try:
            await handler(normalized, ctx, record)
        except EffectOwnerExecutionDenied as exc:
            return _dispatch_record(
                record,
                status=EFFECT_HANDLER_STATUS_OWNER_SKIPPED,
                error=exc.reason,
            )
        except Exception as exc:
            error = str(exc).strip() or exc.__class__.__name__
            return _dispatch_record(
                record,
                status=EFFECT_HANDLER_STATUS_HANDLER_ERROR,
                error=error,
            )
        return _dispatch_record(record, status=EFFECT_STATUS_RECORDED)

    async def dispatch_all(
        self,
        effects: Iterable[MessageEffect],
        ctx: PipelineContext,
        *,
        base_sequence: int = 0,
        dry_run: bool = False,
    ) -> list[EffectDispatchRecord]:
        results: list[EffectDispatchRecord] = []
        for index, effect in enumerate(effects):
            results.append(
                await self.dispatch(
                    effect,
                    ctx,
                    sequence=base_sequence + index,
                    dry_run=dry_run,
                )
            )
        return results


def _handler_key(effect_type: str, owner: str) -> EffectHandlerKey:
    normalized_type = str(effect_type or "").strip()
    normalized_owner = str(owner or "").strip()
    if not normalized_type:
        raise ValueError("effect type cannot be empty")
    if not normalized_owner:
        raise ValueError("effect owner cannot be empty")
    return normalized_type, normalized_owner


def _opt_in_value_matches(value: object, *, owner: str, effect_type: str) -> bool:
    if value is True:
        return True
    if value is None or value is False or value == "":
        return False
    selectors = _effect_handler_selectors(owner=owner, effect_type=effect_type)
    if isinstance(value, str):
        return any(item in selectors for item in _selector_items(value))
    if isinstance(value, dict):
        return any(bool(value.get(selector)) for selector in selectors)
    if isinstance(value, Iterable):
        return any(
            selector in selectors for item in value for selector in _selector_items(str(item))
        )
    return False


def _selector_items(value: str) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    return [item.strip() for chunk in raw.split(",") for item in chunk.split() if item.strip()]


def _effect_handler_selectors(*, owner: str, effect_type: str) -> set[str]:
    selectors = {"*"}
    if owner:
        selectors.add(owner)
    if effect_type:
        selectors.add(effect_type)
    if owner and effect_type:
        selectors.update(
            {
                f"{owner}:{effect_type}",
                f"{owner}.{effect_type}",
                f"{owner}/{effect_type}",
            }
        )
    return selectors


def register_memory_save_handler(
    registry: EffectHandlerRegistry,
    store: Any,
    *,
    replace: bool = False,
) -> MemorySaveEffectHandler:
    handler = MemorySaveEffectHandler(store)
    registry.register("save_memory", "memory", handler, replace=replace)
    return handler


def register_core_publish_outbound_handler(
    registry: EffectHandlerRegistry,
    bus: Any,
    *,
    default_stream: str = "",
    replace: bool = False,
) -> CorePublishOutboundEffectHandler:
    handler = CorePublishOutboundEffectHandler(bus, default_stream=default_stream)
    registry.register("publish_outbound", "core", handler, replace=replace)
    return handler


def register_core_session_effect_handlers(
    registry: EffectHandlerRegistry,
    session_manager: Any,
    *,
    replace: bool = False,
) -> tuple[CoreAppendTurnEffectHandler, CoreSetSessionStateEffectHandler]:
    append_handler = CoreAppendTurnEffectHandler(session_manager)
    state_handler = CoreSetSessionStateEffectHandler(session_manager)
    registry.register("append_user_turn", "core", append_handler, replace=replace)
    registry.register("append_assistant_turn", "core", append_handler, replace=replace)
    registry.register("set_session_state", "core", state_handler, replace=replace)
    return append_handler, state_handler


def register_channel_reply_handlers(
    registry: EffectHandlerRegistry,
    channel_registry: ChannelRegistry,
    *,
    owner: str,
    replace: bool = False,
    owner_gate: OwnerExecutionGate | None = None,
    owner_gate_timeout_seconds: float = DEFAULT_OWNER_GATE_TIMEOUT_SECONDS,
) -> ChannelReplyEffectHandler:
    handler = ChannelReplyEffectHandler(
        channel_registry,
        owner_gate=owner_gate,
        owner_gate_timeout_seconds=owner_gate_timeout_seconds,
    )
    registry.register("enqueue_channel_reply", owner, handler, replace=replace)
    registry.register(
        "enqueue_wxbot_reply",
        owner,
        WxbotReplyEffectHandler(
            ChannelReplyEffectHandler(
                channel_registry,
                default_channel="wechat",
                owner_gate=owner_gate,
                owner_gate_timeout_seconds=owner_gate_timeout_seconds,
            )
        ),
        replace=replace,
    )
    return handler


def _dispatch_record(
    record: EffectCommitRecord,
    *,
    status: str,
    error: str = "",
) -> EffectDispatchRecord:
    return EffectDispatchRecord(
        type=record.type,
        owner=record.owner,
        idempotency_key=record.idempotency_key,
        payload=dict(record.payload),
        status=status,
        commit_status=record.status,
        error=error,
        commit_error=record.error,
        dry_run=record.dry_run,
    )


def _reply_effect_already_enqueued(payload: dict[str, Any]) -> bool:
    if bool(payload.get("already_enqueued")):
        return True
    # First-round wxbot flow effects were audit markers emitted after the
    # inline store enqueue had already happened.
    return bool(payload.get("queued_count")) and not (
        payload.get("body")
        or payload.get("text")
        or payload.get("media")
        or payload.get("messages")
    )


def _channel_target_from_effect(
    payload: dict[str, Any],
    ctx: PipelineContext,
    *,
    default_channel: str = "",
) -> ChannelTarget:
    target_payload = payload.get("target")
    if not isinstance(target_payload, dict):
        target_payload = {}
    metadata = {
        **dict(getattr(ctx.event, "metadata", {}) or {}),
        **(
            dict(target_payload.get("metadata"))
            if isinstance(target_payload.get("metadata"), dict)
            else {}
        ),
    }
    channel = _first_non_empty(
        payload.get("channel"),
        target_payload.get("channel"),
        default_channel,
        getattr(ctx.event.channel, "value", ctx.event.channel),
    )
    session_id = _first_non_empty(
        payload.get("session_id"),
        target_payload.get("session_id"),
        ctx.event.session_id,
    )
    event_session_id = str(ctx.event.session_id or "").strip()
    effect_targets_another_session = bool(session_id and session_id != event_session_id)
    fallback_external_conversation_id = (
        session_id
        if effect_targets_another_session
        else
        _first_non_empty(
            ctx.event.external_conversation_id,
            metadata.get("external_conversation_id"),
            session_id,
        )
    )
    fallback_canonical_conversation_id = (
        session_id
        if effect_targets_another_session
        else
        _first_non_empty(
            ctx.event.canonical_conversation_id,
            metadata.get("canonical_conversation_id"),
            session_id,
        )
    )
    return ChannelTarget(
        tenant_id=_first_non_empty(
            payload.get("tenant_id"),
            target_payload.get("tenant_id"),
            ctx.event.tenant_id,
        ),
        channel=channel,
        adapter_id=_first_non_empty(
            payload.get("adapter_id"),
            target_payload.get("adapter_id"),
            ctx.event.adapter_id,
            metadata.get("adapter_id"),
        ),
        connection_id=_first_non_empty(
            payload.get("connection_id"),
            target_payload.get("connection_id"),
            ctx.event.connection_id,
            metadata.get("connection_id"),
        ),
        session_id=session_id,
        external_conversation_id=_first_non_empty(
            payload.get("external_conversation_id"),
            target_payload.get("external_conversation_id"),
            fallback_external_conversation_id,
        ),
        canonical_conversation_id=_first_non_empty(
            payload.get("canonical_conversation_id"),
            target_payload.get("canonical_conversation_id"),
            fallback_canonical_conversation_id,
        ),
        external_participant_id=_first_non_empty(
            payload.get("external_participant_id"),
            target_payload.get("external_participant_id"),
            ctx.event.external_participant_id,
            metadata.get("external_participant_id"),
            ctx.event.user_id,
        ),
        canonical_participant_id=_first_non_empty(
            payload.get("canonical_participant_id"),
            target_payload.get("canonical_participant_id"),
            ctx.event.canonical_participant_id,
            metadata.get("canonical_participant_id"),
            ctx.event.user_id,
        ),
        session_name=_first_non_empty(
            payload.get("session_name"),
            target_payload.get("session_name"),
            metadata.get("session_name"),
        ),
        session_kind=_first_non_empty(
            payload.get("session_kind"),
            target_payload.get("session_kind"),
            metadata.get("session_kind"),
            "group" if str(ctx.event.session_id or "").endswith("@chatroom") else "private",
        ),
        user_id=_first_non_empty(
            payload.get("user_id"),
            target_payload.get("user_id"),
            ctx.event.user_id,
        ),
        sender_id=_first_non_empty(
            payload.get("sender_id"),
            payload.get("sender_wxid"),
            target_payload.get("sender_id"),
            target_payload.get("sender_wxid"),
            metadata.get("sender_id"),
            metadata.get("sender_wxid"),
            ctx.event.user_id,
        ),
        sender_name=_first_non_empty(
            payload.get("sender_name"),
            target_payload.get("sender_name"),
            metadata.get("sender_name"),
        ),
        reply_to_message_id=_first_non_empty(
            payload.get("reply_to_message_id"),
            payload.get("reply_to_msg_svr_id"),
            target_payload.get("reply_to_message_id"),
            target_payload.get("reply_to_msg_svr_id"),
            metadata.get("reply_to_message_id"),
            metadata.get("msg_svr_id"),
            ctx.event.message_id,
        ),
        metadata=metadata,
    )


def _channel_send_options_from_effect(
    payload: dict[str, Any],
    ctx: PipelineContext,
    effect: MessageEffect,
) -> ChannelSendOptions:
    delivery = payload.get("delivery")
    if not isinstance(delivery, dict):
        delivery = {}
    source_message = payload.get("source_message")
    if not isinstance(source_message, dict):
        source_message = ctx.event.model_dump(mode="json")
    mention_sender = payload.get("mention_sender", delivery.get("mention_sender"))
    if not isinstance(mention_sender, bool):
        mention_sender = None
    return ChannelSendOptions(
        trace_id=_first_non_empty(payload.get("trace_id"), ctx.event.trace_id, ctx.trace_id),
        mention_sender=mention_sender,
        reply_to_message_id=_first_non_empty(
            payload.get("reply_to_message_id"),
            payload.get("reply_to_msg_svr_id"),
            delivery.get("reply_to_message_id"),
            delivery.get("reply_to_msg_svr_id"),
        ),
        source_message=dict(source_message),
        delivery_metadata=dict(delivery),
        idempotency_key=_first_non_empty(
            payload.get("command_id"),
            payload.get("idempotency_key"),
            delivery.get("command_id"),
            delivery.get("idempotency_key"),
            effect.idempotency_key,
        ),
    )


def _reply_effect_body(payload: dict[str, Any]) -> str:
    body = payload.get("body")
    if isinstance(body, str):
        return body.strip()
    if isinstance(body, dict):
        return _first_non_empty(
            body.get("text"),
            body.get("content"),
            body.get("body"),
        ).strip()
    return _first_non_empty(payload.get("text"), payload.get("reply_text")).strip()


def _reply_effect_media(payload: dict[str, Any]) -> ChannelMedia | None:
    media = payload.get("media")
    if not isinstance(media, dict):
        media = {}
    image_path = _first_non_empty(
        media.get("image_path"),
        media.get("path"),
        payload.get("image_path"),
    )
    image_url = _first_non_empty(
        media.get("image_url"),
        media.get("url"),
        payload.get("image_url"),
    )
    if not image_path and not image_url:
        return None
    return ChannelMedia(image_path=image_path, image_url=image_url)


def _reply_effect_file(payload: dict[str, Any]) -> ChannelFile | None:
    file_payload = payload.get("file")
    if not isinstance(file_payload, dict):
        file_payload = {}
    file_path = _first_non_empty(
        file_payload.get("file_path"),
        payload.get("file_path"),
    )
    if not file_path:
        return None
    file_size_value = file_payload.get("file_size", payload.get("file_size"))
    file_size: int | None = None
    if file_size_value not in (None, ""):
        if isinstance(file_size_value, bool):
            raise ValueError("channel reply file_size must be a non-negative integer")
        try:
            file_size = int(file_size_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "channel reply file_size must be a non-negative integer"
            ) from exc
        if file_size < 0:
            raise ValueError("channel reply file_size must be a non-negative integer")
    return ChannelFile(
        file_path=file_path,
        file_name=_first_non_empty(
            file_payload.get("file_name"),
            payload.get("file_name"),
        ),
        file_size=file_size,
        file_md5=_first_non_empty(
            file_payload.get("file_md5"),
            payload.get("file_md5"),
        ),
        file_sha256=_first_non_empty(
            file_payload.get("file_sha256"),
            payload.get("file_sha256"),
        ),
    )


def _first_non_empty(*values: object) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _memory_session_payload(
    *,
    user_id: str,
    channel: str,
    source_key: str,
    session_id: str,
    profile: dict[str, Any],
) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "channel": channel,
        "source_key": source_key,
        "session_id": session_id,
        "short_term": profile.get("short_term_memory") or "",
        "session_summary": profile.get("session_summary") or "",
        "open_items": _json_safe(profile.get("open_items") or []),
        "decisions": _json_safe(profile.get("decisions") or []),
        "recent_turns": _json_safe(profile.get("recent_turns") or []),
        "last_compacted_at": _json_safe(profile.get("last_compacted_at")),
        "summary_version": profile.get("summary_version") or 1,
        "long_term": profile.get("long_term_memory") or "",
        "manual_notes": profile.get("manual_notes") or "",
        "identity_manual_notes": profile.get("identity_manual_notes") or "",
        "session_manual_notes": profile.get("session_manual_notes") or "",
        "message_count": profile.get("message_count") or 0,
        "identity_message_count": profile.get("identity_message_count") or 0,
        "session_message_count": profile.get("session_message_count") or 0,
        "imported_message_count": profile.get("imported_message_count") or 0,
        "last_session_id": profile.get("last_session_id") or "",
        "identity_profile": _json_safe(profile.get("identity_profile") or {}),
        "session_profile": _json_safe(profile.get("session_profile") or {}),
        "memory_items": _json_safe(profile.get("memory_items") or {}),
    }


def _session_only_memory_profile(
    profile: dict[str, Any],
    *,
    session_id: str,
) -> dict[str, Any]:
    filtered = dict(profile)
    filtered["long_term_memory"] = ""
    filtered["identity_manual_notes"] = ""
    filtered["manual_notes"] = str(filtered.get("session_manual_notes") or "")
    filtered["identity_message_count"] = 0
    filtered["audience_scope"] = "group_session_only"
    memory_items = dict(filtered.get("memory_items") or {})
    memory_items["identity"] = []
    memory_items["session"] = [
        item
        for item in (memory_items.get("session") or [])
        if isinstance(item, dict) and str(item.get("session_id") or session_id) == session_id
    ]
    filtered["memory_items"] = memory_items
    filtered["relevant_memory_items"] = [
        item
        for item in (filtered.get("relevant_memory_items") or [])
        if isinstance(item, dict)
        and str(item.get("scope_type") or "") == "session"
        and str(item.get("session_id") or "") == session_id
    ]
    filtered["relevant_graph_facts"] = []
    filtered["relevant_graph_episodes"] = []
    return filtered


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value
