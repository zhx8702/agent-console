from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.agent.scopes import GROUP_PERSONAL_MAP_SCOPE
from app.common.logging import get_logger
from app.common.types import Channel, Role
from app.orchestrator.effect_handlers import effect_handler_opt_in_enabled
from app.orchestrator.pipeline import PipelineContext
from app.plugin.hooks import HookPoint
from app.social import ParticipationContext, ParticipationStatus, SocialParticipationService
from app.social.speech_ledger import GroupSpeechBudgetExceeded
from plugins.wxbot.channel import captured_group_delivery_contract
from plugins.wxbot.hook_context import (
    _agent_query_text,
    _event_mentioned_me,
    _explicit_map_generation_requested,
    _resolve_group_agent_scope,
)
from plugins.wxbot.store import WxbotStore

logger = get_logger(__name__)

_DELIVERY_CONTRACT_METADATA_KEY = "_wxbot_delivery_contract"


def _complete_async_delivery_contract(contract: dict[str, object]) -> bool:
    if str(contract.get("participation_status") or "") != "must_reply":
        return False
    if not str(contract.get("source_message_id") or "").strip():
        return False
    version = contract.get("participation_policy_version")
    if isinstance(version, bool) or not isinstance(version, (int, str)):
        return False
    try:
        int(version)
    except (TypeError, ValueError):
        return False
    return isinstance(contract.get("send_revalidation_enabled"), bool)


@dataclass
class WxbotAgentIntentHook:
    store: WxbotStore | None = None
    effect_handler_enabled: bool = False
    name: str = "wxbot.agent_intent"
    point: HookPoint = HookPoint.BEFORE_ROUTE
    priority: int = 30

    async def run(self, ctx: PipelineContext) -> None:
        if ctx.event.channel != Channel.WECHAT:
            return
        if not str(ctx.event.session_id or "").endswith("@chatroom"):
            return
        if bool(ctx.event.metadata.get("is_self_sent")):
            return
        if not _event_mentioned_me(ctx):
            return
        text = _agent_query_text(ctx)
        scope = _resolve_group_agent_scope(text)
        if not scope:
            return
        router_signals = ctx.extras.setdefault("router_signals", {})
        if not isinstance(router_signals, dict):
            router_signals = {}
            ctx.extras["router_signals"] = router_signals
        router_signals["tools_available"] = True
        ctx.extras["agent_tool_scope"] = scope
        self._capture_async_delivery_contract(ctx)
        if scope == GROUP_PERSONAL_MAP_SCOPE and _explicit_map_generation_requested(text):
            await self._enqueue_map_progress(
                ctx,
                effect_only=(
                    self.effect_handler_enabled
                    or effect_handler_opt_in_enabled(
                        ctx,
                        effect_type="enqueue_channel_reply",
                        owner="wxbot",
                    )
                ),
            )
        logger.info(
            "wxbot.agent_intent.detected",
            session_id=ctx.event.session_id,
            msg_svr_id=ctx.event.metadata.get("msg_svr_id"),
            text_length=len(text),
            scope=scope,
        )

    @staticmethod
    def _capture_async_delivery_contract(ctx: PipelineContext) -> dict[str, object]:
        source_message_id = str(
            ctx.event.message_id
            or ctx.event.metadata.get("msg_svr_id")
            or ctx.trace_id
            or ""
        ).strip()
        policy_state = ctx.extras.get("wxbot_reply_policy")
        contract = captured_group_delivery_contract(
            source_message_id=source_message_id,
            policy_state=(policy_state if isinstance(policy_state, dict) else None),
            response_kind="tool_result",
        )
        ctx.extras["wxbot_async_delivery_contract"] = dict(contract)
        ctx.event.metadata[_DELIVERY_CONTRACT_METADATA_KEY] = dict(contract)
        if ctx.session is None:
            return contract
        ctx.session.metadata[_DELIVERY_CONTRACT_METADATA_KEY] = dict(contract)
        for turn in reversed(ctx.session.turns):
            if turn.role is not Role.USER:
                continue
            turn.metadata[_DELIVERY_CONTRACT_METADATA_KEY] = dict(contract)
            break
        return contract

    async def _enqueue_map_progress(
        self,
        ctx: PipelineContext,
        *,
        effect_only: bool = False,
    ) -> None:
        if self.store is None:
            return
        source_message_id = str(
            ctx.event.message_id or ctx.event.metadata.get("msg_svr_id") or ctx.trace_id or ""
        ).strip()
        command_id = f"wxbot-progress:{ctx.event.tenant_id}:{source_message_id}:amap-map"
        session_kind = str(
            ctx.event.metadata.get("session_kind")
            or ("group" if str(ctx.event.session_id or "").endswith("@chatroom") else "private")
        )
        mention_sender = False
        feature_state = ctx.extras.get("wxbot_humanization_features")
        speech_budget_enabled = bool(
            isinstance(feature_state, dict) and feature_state.get("speech_budget_enabled")
        )
        duplicate_guard_enabled = bool(
            isinstance(feature_state, dict) and feature_state.get("duplicate_guard_enabled")
        )
        captured_value = ctx.extras.get("wxbot_async_delivery_contract")
        captured = dict(captured_value) if isinstance(captured_value, dict) else {}
        if not _complete_async_delivery_contract(captured):
            ctx.extras["wxbot_map_progress_suppressed"] = (
                "async_delivery_contract_unavailable"
            )
            logger.warning(
                "wxbot.agent_map_progress.contract_unavailable",
                session_id=ctx.event.session_id,
                trace_id=ctx.trace_id,
            )
            return
        timing_context = ParticipationContext(
            tenant_id=ctx.event.tenant_id,
            session_id=ctx.event.session_id,
            message_id=source_message_id,
            now=datetime.now(UTC),
            response_kind="tool_progress",
        )
        not_before, expires_at = SocialParticipationService().timing_for(
            timing_context,
            ParticipationStatus.MUST_REPLY,
        )
        delivery = {
            "channel": "wechat",
            "command_id": command_id,
            "idempotency_key": command_id,
            "tenant_id": ctx.event.tenant_id,
            "session_id": ctx.event.session_id,
            "session_name": str(ctx.event.metadata.get("session_name") or ""),
            "session_kind": session_kind,
            "sender_name": str(ctx.event.metadata.get("sender_name") or ""),
            "sender_wxid": str(ctx.event.metadata.get("sender_wxid") or ""),
            "mention_sender": mention_sender,
            "reply_to_msg_svr_id": str(ctx.event.metadata.get("msg_svr_id") or ""),
            **captured,
            "speech_output_kind": "ordinary",
            "speech_class": "obligation",
            "speech_budget_enabled": speech_budget_enabled,
            "duplicate_guard_enabled": duplicate_guard_enabled,
            "response_kind": "tool_progress",
            "not_before": not_before.isoformat(),
            "expires_at": expires_at.isoformat(),
        }
        if effect_only:
            ctx.extras.setdefault("wxbot_map_progress_effect_items", []).append(
                {
                    "tenant_id": ctx.event.tenant_id,
                    "channel": "wechat",
                    "session_id": ctx.event.session_id,
                    "session_name": str(ctx.event.metadata.get("session_name") or ""),
                    "session_kind": session_kind,
                    "user_id": ctx.event.user_id,
                    "sender_name": str(ctx.event.metadata.get("sender_name") or ""),
                    "sender_wxid": str(ctx.event.metadata.get("sender_wxid") or ""),
                    "reply_to_msg_svr_id": str(ctx.event.metadata.get("msg_svr_id") or ""),
                    "body": {
                        "type": "text",
                        "text": "收到，正在生成高德地图，请稍后。",
                    },
                    "trace_id": ctx.trace_id,
                    "mention_sender": mention_sender,
                    "source_message": ctx.event.model_dump(mode="json"),
                    "delivery": delivery,
                    "command_id": command_id,
                }
            )
            ctx.extras["suppress_outbound"] = True
            ctx.extras["wxbot_map_progress_enqueued"] = True
            logger.info(
                "wxbot.agent_map_progress.effect_enqueued",
                session_id=ctx.event.session_id,
                trace_id=ctx.trace_id,
                command_id=command_id,
            )
            return
        try:
            await self.store.enqueue_reply(
                tenant_id=ctx.event.tenant_id,
                session_id=ctx.event.session_id,
                session_name=str(ctx.event.metadata.get("session_name") or ""),
                sender_name=str(ctx.event.metadata.get("sender_name") or ""),
                sender_wxid=str(ctx.event.metadata.get("sender_wxid") or ""),
                reply_text="收到，正在生成高德地图，请稍后。",
                trace_id=ctx.trace_id,
                msg_type="text",
                mention_sender=mention_sender,
                reply_to_msg_svr_id=str(ctx.event.metadata.get("msg_svr_id") or ""),
                session_kind=session_kind,
                source_message=ctx.event.model_dump(mode="json"),
                delivery=delivery,
                command_id=command_id,
            )
        except GroupSpeechBudgetExceeded as exc:
            ctx.extras["wxbot_map_progress_suppressed"] = exc.reason
            logger.info(
                "wxbot.agent_map_progress.speech_budget_suppressed",
                session_id=ctx.event.session_id,
                reason=exc.reason,
            )
            return
        ctx.extras["suppress_outbound"] = True
        ctx.extras["wxbot_map_progress_enqueued"] = True
        logger.info(
            "wxbot.agent_map_progress.enqueued",
            session_id=ctx.event.session_id,
            trace_id=ctx.trace_id,
            command_id=command_id,
        )
