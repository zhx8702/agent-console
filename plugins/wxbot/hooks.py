"""WeChat pipeline integration facade and lightweight orchestration steps.

Large policy, intent, and reply-queue implementations live in focused modules;
the historical imports from ``plugins.wxbot.hooks`` remain stable.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.channel import get_reply_policy_override
from app.common.logging import get_logger
from app.common.types import (
    CapabilityResult,
    Channel,
    MessageType,
    RouteType,
    channel_id_value,
)
from app.infra.metrics import WXBOT_REPLY_COALESCE_SECONDS, WXBOT_REPLY_SUPPRESSED
from app.orchestrator.effect_handlers import effect_handler_opt_in_enabled
from app.orchestrator.flow import MessageEffect, StepResult
from app.orchestrator.pipeline import PipelineContext
from app.plugin.hooks import HookAbort, HookPoint
from app.social import ParticipationDecision, ParticipationStatus
from app.social.feedback import NaturalFeedbackService
from app.social.store import SocialPolicyStore
from plugins.wxbot.agent_intent_hook import WxbotAgentIntentHook
from plugins.wxbot.hook_context import (
    _BAN_SAFE_COMMANDS,
    _apply_voice_profile_to_session,
    _event_command_token,
    _event_mentioned_me,
    _event_sender_wxid,
    _record_participation_decision,
    _strip_group_mention_prefix,
    _sync_wxbot_reply_policy_signal,
)
from plugins.wxbot.reply_policy_hook import WxbotReplyPolicyHook
from plugins.wxbot.reply_queue_hook import WxbotReplyQueueHook as _WxbotReplyQueueHook
from plugins.wxbot.store import WxbotStore

logger = get_logger(__name__)


class _ClaimedReplyStoreProxy:
    """Delegate store access while suppressing the legacy duplicate claim."""

    def __init__(self, target: WxbotStore, policy: dict[str, object]) -> None:
        self._target = target
        self._policy = dict(policy)

    def __getattr__(self, name: str):
        return getattr(self._target, name)

    async def get_session_policy(
        self,
        _tenant_id: str,
        _session_id: str,
    ) -> dict[str, object]:
        return dict(self._policy)

    async def claim_interactive_reply(self, **_kwargs: object) -> bool:
        return True


@dataclass
class WxbotReplyQueueHook(_WxbotReplyQueueHook):
    """Apply coalescing and adaptive fencing around the split queue hook."""

    async def run(self, ctx: PipelineContext) -> None:
        if ctx.event.channel != Channel.WECHAT or ctx.reply is None:
            await super().run(ctx)
            return
        result_metadata = dict(ctx.result.metadata or {}) if ctx.result is not None else {}
        if bool(
            result_metadata.get("suppress_outbound") or result_metadata.get("suppress_final_reply")
        ):
            await super().run(ctx)
            return
        is_group = str(ctx.event.session_id or "").endswith("@chatroom")
        is_command_reply = bool(ctx.extras.get("_command_token"))
        reply_override = get_reply_policy_override(ctx.extras)
        force_send = bool(
            is_command_reply
            or reply_override.get("force_send")
            or ctx.extras.get("wxbot_force_send")
        )
        policy_state = ctx.extras.get("wxbot_reply_policy")
        participation_state = ctx.extras.get("wxbot_participation")
        participation_approved = bool(
            isinstance(participation_state, dict)
            and participation_state.get("status")
            in {
                ParticipationStatus.MUST_REPLY.value,
                ParticipationStatus.MAY_REPLY.value,
                ParticipationStatus.DEFER.value,
            }
        )
        # Contracted participation decisions use the bounded semantic
        # revalidation in the bridge.  The legacy latest-message cursor is too
        # coarse here: unrelated chatter must not erase a direct @ obligation.
        if (
            not is_group
            or force_send
            or participation_approved
            or not isinstance(policy_state, dict)
            or policy_state.get("allowed") is False
        ):
            await super().run(ctx)
            return

        mentioned_me = bool(policy_state.get("mentioned_me"))
        if not mentioned_me:
            configured_coalesce = policy_state.get("effective_coalesce_window_ms")
            coalesce_seconds = max(
                0.0,
                float(
                    configured_coalesce
                    if configured_coalesce is not None
                    else getattr(
                        getattr(self.store, "settings", None),
                        "wxbot_group_reply_coalesce_window_ms",
                        250,
                    )
                )
                / 1000.0,
            )
            if coalesce_seconds:
                WXBOT_REPLY_COALESCE_SECONDS.observe(coalesce_seconds)
                await asyncio.sleep(coalesce_seconds)

        claim_reply = getattr(self.store, "claim_interactive_reply", None)
        if not callable(claim_reply):
            await super().run(ctx)
            return
        source_message_id = str(
            ctx.event.message_id or ctx.event.metadata.get("msg_svr_id") or ctx.trace_id or ""
        ).strip()
        configured_cooldown = policy_state.get("effective_reply_cooldown_seconds")
        cooldown = 0.0
        if not mentioned_me:
            cooldown = float(
                configured_cooldown
                if configured_cooldown is not None
                else getattr(
                    getattr(self.store, "settings", None),
                    "wxbot_group_reply_cooldown_seconds",
                    1.0,
                )
            )
        configured_adaptive = policy_state.get("effective_adaptive_cooldown_enabled")
        claimed = await claim_reply(
            tenant_id=ctx.event.tenant_id,
            session_id=ctx.event.session_id,
            message_id=source_message_id,
            cooldown_seconds=cooldown,
            adaptive_cooldown=bool(
                configured_adaptive
                if configured_adaptive is not None
                else getattr(
                    getattr(self.store, "settings", None),
                    "wxbot_group_reply_adaptive_cooldown_enabled",
                    True,
                )
            ),
            adaptive_max_seconds=float(
                getattr(
                    getattr(self.store, "settings", None),
                    "wxbot_group_reply_adaptive_cooldown_max_seconds",
                    8.0,
                )
                or 0.0
            ),
        )
        if not claimed:
            ctx.extras["suppress_outbound"] = True
            ctx.extras["skip_assistant_turn"] = True
            ctx.extras["wxbot_reply_stale"] = True
            WXBOT_REPLY_SUPPRESSED.labels(reason="superseded_or_cooldown").inc()
            logger.info(
                "wxbot.reply_queue.superseded_or_cooled_down",
                session_id=ctx.event.session_id,
                message_id=source_message_id,
                cooldown_seconds=cooldown,
            )
            return

        # The focused implementation still contains its legacy fixed-cooldown
        # claim. Delegate through a proxy so this atomic adaptive claim is the
        # single send fence for the response.
        cached_policy = {
            **policy_state,
            "effective_mode": str(policy_state.get("reply_mode") or "off"),
            "trigger_keywords": list(policy_state.get("keywords") or []),
        }
        delegate = _WxbotReplyQueueHook(
            store=_ClaimedReplyStoreProxy(self.store, cached_policy),  # type: ignore[arg-type]
            effect_only=self.effect_only,
            social_policy_store=self.social_policy_store,
            name=self.name,
            point=self.point,
            priority=self.priority,
        )
        await delegate.run(ctx)


__all__ = [
    "WxbotAgentIntentHook",
    "WxbotAgentScopeEnrichStep",
    "WxbotInboundNormalizeHook",
    "WxbotNormalizeEventStep",
    "WxbotOutboundPolicyStep",
    "WxbotReplyPolicyHook",
    "WxbotReplyPolicyStep",
    "WxbotReplyQueueHook",
    "WxbotUserBanGateStep",
    "WxbotUserBanPreCommandStep",
    "WxbotVoiceProfileEnrichStep",
    "WxbotVoiceProfileHook",
]


@dataclass
class WxbotInboundNormalizeHook:
    name: str = "wxbot.inbound_normalize"
    point: HookPoint = HookPoint.BEFORE_PREPROCESS
    priority: int = 5

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
        if not _event_mentioned_me(ctx):
            return

        original = str(event.message.content or "")
        event.metadata.setdefault("bot_mentioned", True)
        event.metadata.setdefault(
            "bot_addressed",
            str(event.metadata.get("bot_mention_position") or "") != "inline",
        )
        sdk_normalized = str(event.metadata.get("bot_normalized_content") or "").strip()
        stripped = sdk_normalized or _strip_group_mention_prefix(original)
        if not stripped or stripped == original:
            return

        event.metadata.setdefault("wxbot_original_content", original)
        event.metadata["wxbot_normalized_content"] = stripped
        event.message.content = stripped
        logger.info(
            "wxbot.inbound_normalized",
            session_id=event.session_id,
            msg_svr_id=event.metadata.get("msg_svr_id"),
            original_length=len(original),
            normalized_length=len(stripped),
        )


def _sync_wxbot_normalize_signal(ctx: PipelineContext) -> dict[str, object]:
    signal = {
        "normalized": bool(ctx.event.metadata.get("wxbot_normalized_content")),
        "original_content": str(ctx.event.metadata.get("wxbot_original_content") or ""),
        "normalized_content": str(ctx.event.metadata.get("wxbot_normalized_content") or ""),
    }
    ctx.signals.setdefault("channel", {}).setdefault("wechat", {})["normalized"] = signal
    return signal


def _sync_wxbot_agent_scope_signal(ctx: PipelineContext) -> dict[str, object]:
    router_signals = ctx.extras.get("router_signals")
    tool_intent_matched = (
        bool(router_signals.get("tool_intent_matched"))
        if isinstance(router_signals, dict)
        else False
    )
    tools_available = (
        bool(router_signals.get("tools_available")) if isinstance(router_signals, dict) else False
    )
    signal = {
        "tool_scope": str(ctx.extras.get("agent_tool_scope") or ""),
        "tool_intent_matched": tool_intent_matched,
        "tools_available": tools_available,
        "map_progress_enqueued": bool(ctx.extras.get("wxbot_map_progress_enqueued")),
    }
    ctx.signals["agent"] = {
        **dict(ctx.signals.get("agent") or {}),
        **({"tool_scope": signal["tool_scope"]} if signal["tool_scope"] else {}),
    }
    ctx.signals.setdefault("router", {})["tool_intent_matched"] = tool_intent_matched
    ctx.signals.setdefault("router", {})["tools_available"] = tools_available
    ctx.signals.setdefault("channel", {}).setdefault("wechat", {})["agent_scope"] = signal
    return signal


def _sync_wxbot_user_ban_signal(
    ctx: PipelineContext,
    *,
    active: bool,
    reason: str,
    ban: dict[str, object] | None = None,
) -> dict[str, object]:
    signal = {
        "active": active,
        "reason": reason,
        "session_id": ctx.event.session_id,
        "user_wxid": _event_sender_wxid(ctx),
    }
    if ban:
        signal["ban_id"] = ban.get("id")
        signal["expires_at"] = str(ban.get("expires_at") or "")
    ctx.signals.setdefault("channel", {}).setdefault("wechat", {})["user_ban"] = signal
    return signal


def _is_wxbot_group_user_event(ctx: PipelineContext) -> bool:
    return (
        ctx.event.channel == Channel.WECHAT
        and str(ctx.event.session_id or "").endswith("@chatroom")
        and not bool(ctx.event.metadata.get("is_self_sent"))
    )


def _sync_wxbot_outbound_policy_signal(ctx: PipelineContext) -> dict[str, object]:
    queued_count = int(ctx.extras.get("wxbot_reply_queued_count") or 0)
    signal = {
        "suppress_outbound": bool(ctx.extras.get("suppress_outbound")),
        "skip_assistant_turn": bool(ctx.extras.get("skip_assistant_turn")),
        "queued": queued_count > 0,
        "queued_count": queued_count,
    }
    ctx.signals.setdefault("channel", {}).setdefault("wechat", {})["outbound_policy"] = signal
    return signal


def _wxbot_enqueue_effects(ctx: PipelineContext, signal: dict[str, object]) -> list[MessageEffect]:
    queued_count = int(signal.get("queued_count") or 0)
    if queued_count <= 0:
        return []
    pending = ctx.extras.get("wxbot_reply_effect_items")
    if isinstance(pending, list) and pending:
        effects: list[MessageEffect] = []
        for index, item in enumerate(pending):
            if not isinstance(item, dict):
                continue
            payload = dict(item)
            command_id = str(payload.get("command_id") or "").strip()
            effects.append(
                MessageEffect(
                    type="enqueue_channel_reply",
                    owner="wxbot",
                    payload=payload,
                    idempotency_key=command_id
                    or (
                        "channel:enqueue_reply:"
                        f"{ctx.event.tenant_id}:{channel_id_value(ctx.event.channel)}:"
                        f"{ctx.event.session_id}:{ctx.event.trace_id}:{index}"
                    ),
                )
            )
        return effects
    payload = {
        "tenant_id": ctx.event.tenant_id,
        "session_id": ctx.event.session_id,
        "user_id": ctx.event.user_id,
        "channel": channel_id_value(ctx.event.channel),
        "queued_count": queued_count,
        "trace_id": ctx.event.trace_id,
        "already_enqueued": True,
    }
    return [
        MessageEffect(
            type="enqueue_channel_reply",
            owner="wxbot",
            payload=dict(payload),
            idempotency_key=(
                "channel:enqueue_reply:"
                f"{ctx.event.tenant_id}:{channel_id_value(ctx.event.channel)}:"
                f"{ctx.event.session_id}:{ctx.event.trace_id}"
            ),
        ),
        MessageEffect(
            type="enqueue_wxbot_reply",
            owner="wxbot",
            payload=dict(payload),
            idempotency_key=(
                "wxbot:enqueue_reply:"
                f"{ctx.event.tenant_id}:{ctx.event.session_id}:{ctx.event.trace_id}"
            ),
        ),
    ]


def _wxbot_map_progress_effects(ctx: PipelineContext) -> list[MessageEffect]:
    pending = ctx.extras.get("wxbot_map_progress_effect_items")
    if not isinstance(pending, list) or not pending:
        return []
    effects: list[MessageEffect] = []
    for index, item in enumerate(pending):
        if not isinstance(item, dict):
            continue
        payload = dict(item)
        command_id = str(payload.get("command_id") or "").strip()
        effects.append(
            MessageEffect(
                type="enqueue_channel_reply",
                owner="wxbot",
                payload=payload,
                idempotency_key=command_id
                or (
                    "channel:map_progress:"
                    f"{ctx.event.tenant_id}:{channel_id_value(ctx.event.channel)}:"
                    f"{ctx.event.session_id}:{ctx.event.trace_id}:{index}"
                ),
            )
        )
    return effects


@dataclass
class WxbotNormalizeEventStep:
    kind: str = "plugin.wxbot.normalize_event"
    owner: str = "wxbot"
    name: str = "Normalize WeChat event"
    permissions: list[str] = field(default_factory=lambda: ["hooks:pipeline"])
    inputs: set[str] = field(default_factory=lambda: {"event"})
    outputs: set[str] = field(default_factory=lambda: {"signals.channel.wechat.normalized"})
    timeout_seconds: float = 1.0
    error_policy: str = "fail_closed"

    async def run(self, ctx: PipelineContext) -> StepResult:
        await WxbotInboundNormalizeHook().run(ctx)
        signal = _sync_wxbot_normalize_signal(ctx)
        return StepResult(reason="normalized" if signal["normalized"] else "not_normalized")


@dataclass
class WxbotVoiceProfileHook:
    """Merge the public group VoiceProfile after persona enrichment."""

    name: str = "wxbot.voice_profile"
    point: HookPoint = HookPoint.BEFORE_CAPABILITY
    # PersonaSkillHook is priority 40. Lower priorities run first, so this must
    # run later to keep the group profile from being overwritten.
    priority: int = 80

    async def run(self, ctx: PipelineContext) -> None:
        if ctx.event.channel != Channel.WECHAT or not str(ctx.event.session_id or "").endswith(
            "@chatroom"
        ):
            return
        _apply_voice_profile_to_session(ctx)


@dataclass
class WxbotVoiceProfileEnrichStep:
    kind: str = "plugin.wxbot.voice_profile_enrich"
    owner: str = "wxbot"
    name: str = "WeChat VoiceProfile enrich"
    permissions: list[str] = field(default_factory=lambda: ["storage:shared", "hooks:pipeline"])
    inputs: set[str] = field(default_factory=lambda: {"event", "session"})
    outputs: set[str] = field(default_factory=lambda: {"signals.channel.wechat.voice_profile"})
    timeout_seconds: float = 1.0
    error_policy: str = "fail_closed"

    async def run(self, ctx: PipelineContext) -> StepResult:
        signal = _apply_voice_profile_to_session(ctx)
        return StepResult(reason=str(signal.get("reason") or "voice_profile_not_configured"))


@dataclass
class WxbotReplyPolicyStep:
    store: WxbotStore
    social_policy_store: SocialPolicyStore | None = field(
        default=None,
        repr=False,
    )
    natural_feedback_service: NaturalFeedbackService | None = field(
        default=None,
        repr=False,
    )
    kind: str = "plugin.wxbot.reply_policy"
    owner: str = "wxbot"
    name: str = "WeChat reply policy"
    permissions: list[str] = field(default_factory=lambda: ["storage:shared", "hooks:pipeline"])
    inputs: set[str] = field(default_factory=lambda: {"event", "session", "pre"})
    outputs: set[str] = field(
        default_factory=lambda: {
            "signals.reply_policy",
            "signals.channel.wechat.reply_policy",
            "signals.participation",
            "signals.channel.wechat.participation",
        }
    )
    # Four bounded lookups may run serially for quoted soft candidates.
    timeout_seconds: float = 4.0
    error_policy: str = "fail_closed"

    async def run(self, ctx: PipelineContext) -> StepResult:
        try:
            await WxbotReplyPolicyHook(
                self.store,
                social_policy_store=self.social_policy_store,
                natural_feedback_service=self.natural_feedback_service,
            ).run(ctx)
        except HookAbort as exc:
            signal = _sync_wxbot_reply_policy_signal(ctx)
            return StepResult(
                action="stop",
                reason=exc.reason,
                result=CapabilityResult(route=RouteType.CANNED, reply_text=exc.reply_text),
                finalize=True,
                skip_output_safety=True,
                append_assistant_turn=False if ctx.extras.get("skip_assistant_turn") else None,
                publish_outbound=False if ctx.extras.get("suppress_outbound") else None,
                route_label=RouteType.CANNED.value,
            )
        except Exception as exc:
            reason = "wxbot_reply_policy_unavailable"
            ctx.extras["interaction_mode"] = "observed"
            ctx.event.metadata["reply_allowed"] = False
            ctx.extras["wxbot_reply_policy"] = {
                "session_id": ctx.event.session_id,
                "reply_mode": "unavailable",
                "keywords": [],
                "mentioned_me": _event_mentioned_me(ctx),
                "allowed": False,
                "reason": reason,
            }
            ctx.extras["suppress_outbound"] = True
            ctx.extras["skip_assistant_turn"] = True
            ctx.extras["skip_state_transition"] = True
            _record_participation_decision(
                ctx,
                ParticipationDecision(
                    status=ParticipationStatus.OBSERVE_ONLY,
                    score=0,
                    reason_codes=(reason,),
                ),
            )
            logger.exception(
                "wxbot.reply_policy.step_failed_closed",
                session_id=ctx.event.session_id,
                msg_svr_id=ctx.event.metadata.get("msg_svr_id"),
                error_class=exc.__class__.__name__,
            )
            _sync_wxbot_reply_policy_signal(ctx)
            return StepResult(
                action="stop",
                reason=reason,
                result=CapabilityResult(route=RouteType.CANNED, reply_text=""),
                finalize=True,
                skip_output_safety=True,
                append_assistant_turn=False,
                publish_outbound=False,
                route_label=RouteType.CANNED.value,
            )
        signal = _sync_wxbot_reply_policy_signal(ctx)
        return StepResult(reason=str(signal.get("reason") or "allowed"))


@dataclass
class WxbotUserBanPreCommandStep:
    store: WxbotStore
    kind: str = "plugin.wxbot.user_ban_pre_command"
    owner: str = "wxbot"
    name: str = "WeChat user ban pre-command guard"
    permissions: list[str] = field(default_factory=lambda: ["storage:shared", "hooks:pipeline"])
    inputs: set[str] = field(default_factory=lambda: {"event", "session", "pre"})
    outputs: set[str] = field(
        default_factory=lambda: {"signals.channel.wechat.user_ban_pre_command"}
    )
    timeout_seconds: float = 1.0
    error_policy: str = "fail_closed"

    async def run(self, ctx: PipelineContext) -> StepResult:
        if not _is_wxbot_group_user_event(ctx):
            _sync_wxbot_user_ban_signal(ctx, active=False, reason="not_group")
            return StepResult(reason="not_group")
        command = _event_command_token(ctx)
        if not command:
            _sync_wxbot_user_ban_signal(ctx, active=False, reason="no_command")
            return StepResult(reason="no_command")
        if command in _BAN_SAFE_COMMANDS:
            _sync_wxbot_user_ban_signal(ctx, active=False, reason="ban_safe_command")
            return StepResult(reason="ban_safe_command")
        sender_wxid = _event_sender_wxid(ctx)
        if not sender_wxid:
            _sync_wxbot_user_ban_signal(ctx, active=False, reason="missing_sender_wxid")
            return StepResult(reason="missing_sender_wxid")
        ban = await self.store.get_active_user_ban(
            ctx.event.tenant_id, ctx.event.session_id, sender_wxid
        )
        if not ban:
            _sync_wxbot_user_ban_signal(ctx, active=False, reason="not_banned")
            return StepResult(reason="not_banned")
        ctx.extras["suppress_outbound"] = True
        ctx.extras["skip_assistant_turn"] = True
        ctx.extras["skip_state_transition"] = True
        signal = _sync_wxbot_user_ban_signal(ctx, active=True, reason="command_blocked", ban=ban)
        logger.info(
            "wxbot.user_ban.hit",
            tenant_id=ctx.event.tenant_id,
            session_id=ctx.event.session_id,
            user_wxid=sender_wxid,
            trace_id=ctx.event.trace_id,
            command=command,
            reason=signal["reason"],
        )
        return StepResult(
            action="stop",
            reason="wxbot_user_ban_command_blocked",
            append_assistant_turn=False,
            publish_outbound=False,
        )


@dataclass
class WxbotUserBanGateStep:
    store: WxbotStore
    kind: str = "plugin.wxbot.user_ban_gate"
    owner: str = "wxbot"
    name: str = "WeChat user ban gate"
    permissions: list[str] = field(default_factory=lambda: ["storage:shared", "hooks:pipeline"])
    inputs: set[str] = field(default_factory=lambda: {"event", "session", "pre"})
    outputs: set[str] = field(default_factory=lambda: {"signals.channel.wechat.user_ban"})
    timeout_seconds: float = 1.0
    error_policy: str = "fail_closed"

    async def run(self, ctx: PipelineContext) -> StepResult:
        if not _is_wxbot_group_user_event(ctx):
            _sync_wxbot_user_ban_signal(ctx, active=False, reason="not_group")
            return StepResult(reason="not_group")
        if _event_command_token(ctx):
            _sync_wxbot_user_ban_signal(ctx, active=False, reason="command_already_handled")
            return StepResult(reason="command")
        sender_wxid = _event_sender_wxid(ctx)
        if not sender_wxid:
            _sync_wxbot_user_ban_signal(ctx, active=False, reason="missing_sender_wxid")
            return StepResult(reason="missing_sender_wxid")
        ban = await self.store.get_active_user_ban(
            ctx.event.tenant_id, ctx.event.session_id, sender_wxid
        )
        if not ban:
            _sync_wxbot_user_ban_signal(ctx, active=False, reason="not_banned")
            return StepResult(reason="not_banned")
        ctx.extras["suppress_outbound"] = True
        ctx.extras["skip_assistant_turn"] = True
        ctx.extras["skip_state_transition"] = True
        signal = _sync_wxbot_user_ban_signal(ctx, active=True, reason="message_blocked", ban=ban)
        logger.info(
            "wxbot.user_ban.hit",
            tenant_id=ctx.event.tenant_id,
            session_id=ctx.event.session_id,
            user_wxid=sender_wxid,
            trace_id=ctx.event.trace_id,
            reason=signal["reason"],
        )
        return StepResult(
            action="stop",
            reason="wxbot_user_ban_blocked",
            append_assistant_turn=False,
            publish_outbound=False,
        )


@dataclass
class WxbotAgentScopeEnrichStep:
    store: WxbotStore | None = None
    effect_handler_enabled: bool = False
    social_policy_store: SocialPolicyStore | None = field(
        default=None,
        repr=False,
    )
    kind: str = "plugin.wxbot.agent_scope_enrich"
    owner: str = "wxbot"
    name: str = "WeChat group agent scope enrich"
    permissions: list[str] = field(
        default_factory=lambda: ["storage:shared", "agent_tools", "hooks:pipeline"]
    )
    inputs: set[str] = field(default_factory=lambda: {"event", "session", "pre"})
    outputs: set[str] = field(
        default_factory=lambda: {
            "effects.enqueue_channel_reply",
            "result",
            "signals.agent.tool_scope",
            "signals.router.tool_intent_matched",
            "signals.router.tools_available",
        }
    )
    timeout_seconds: float = 1.5
    error_policy: str = "fail_open"

    async def run(self, ctx: PipelineContext) -> StepResult:
        await WxbotAgentIntentHook(
            self.store,
            effect_handler_enabled=self.effect_handler_enabled,
            social_policy_store=self.social_policy_store,
        ).run(ctx)
        signal = _sync_wxbot_agent_scope_signal(ctx)
        effects = _wxbot_map_progress_effects(ctx)
        denial_reply = str(
            ctx.extras.get("wxbot_file_send_denial_reply") or ""
        ).strip()
        if denial_reply:
            denial_reason = str(
                ctx.extras.get("wxbot_file_send_denial_reason")
                or "group_file_send_disabled"
            )
            return StepResult(
                action="stop",
                reason="wxbot_group_file_send_denied",
                result=CapabilityResult(
                    route=RouteType.CANNED,
                    reply_text=denial_reply,
                    metadata={
                        "wxbot_file_send_denied": True,
                        "wxbot_file_send_denial_reason": denial_reason,
                    },
                ),
                finalize=True,
                skip_output_safety=True,
                route_label=RouteType.CANNED.value,
                effects=effects,
            )
        return StepResult(
            reason="enriched" if signal["tool_scope"] else "not_matched",
            effects=effects,
        )


@dataclass
class WxbotOutboundPolicyStep:
    store: WxbotStore
    effect_handler_enabled: bool = False
    social_policy_store: SocialPolicyStore | None = field(
        default=None,
        repr=False,
    )
    kind: str = "plugin.wxbot.outbound_policy"
    owner: str = "wxbot"
    name: str = "WeChat outbound policy"
    permissions: list[str] = field(default_factory=lambda: ["storage:shared", "hooks:pipeline"])
    inputs: set[str] = field(default_factory=lambda: {"event", "session", "pre", "reply"})
    outputs: set[str] = field(
        default_factory=lambda: {
            "signals.channel.wechat.outbound_policy",
            "effects.enqueue_channel_reply",
            "effects.enqueue_wxbot_reply",
        }
    )
    timeout_seconds: float = 2.0
    error_policy: str = "fail_closed"

    async def run(self, ctx: PipelineContext) -> StepResult:
        effect_only = (
            self.effect_handler_enabled
            or effect_handler_opt_in_enabled(
                ctx,
                effect_type="enqueue_channel_reply",
                owner="wxbot",
            )
            or effect_handler_opt_in_enabled(
                ctx,
                effect_type="enqueue_wxbot_reply",
                owner="wxbot",
            )
        )
        await WxbotReplyQueueHook(
            self.store,
            effect_only=effect_only,
            social_policy_store=self.social_policy_store,
        ).run(ctx)
        signal = _sync_wxbot_outbound_policy_signal(ctx)
        return StepResult(
            reason="queued" if signal.get("queued") else "not_queued",
            publish_outbound=False if ctx.extras.get("suppress_outbound") else None,
            append_assistant_turn=False if ctx.extras.get("skip_assistant_turn") else None,
            effects=_wxbot_enqueue_effects(ctx, signal),
        )
