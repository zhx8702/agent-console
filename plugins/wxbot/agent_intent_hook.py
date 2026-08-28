from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from app.agent.scopes import (
    FILE_ANALYSIS_SCOPE,
    GROUP_DRAW_GENERATION_SCOPE,
    GROUP_PERSONAL_MAP_SCOPE,
    GROUP_VIDEO_GENERATION_SCOPE,
    MESSAGE_EXPORT_SCOPE,
)
from app.common.logging import get_logger
from app.common.types import Channel, Role
from app.orchestrator.effect_handlers import effect_handler_opt_in_enabled
from app.orchestrator.pipeline import PipelineContext
from app.plugin.hooks import HookPoint
from app.social import ParticipationContext, ParticipationStatus, SocialParticipationService
from app.social.speech_ledger import GroupSpeechBudgetExceeded
from plugins.wxbot.channel import captured_group_delivery_contract
from plugins.wxbot.group_file_policy import (
    GroupFilePolicyReader,
    GroupFileSendDenied,
    require_group_file_send_enabled,
)
from plugins.wxbot.hook_context import (
    _agent_query_text,
    _event_mentioned_me,
    _event_policy_session_id,
    _explicit_map_generation_requested,
    _file_intent_requested,
    _message_export_requested,
    _resolve_group_agent_scope,
)
from plugins.wxbot.store import WxbotStore

logger = get_logger(__name__)

_DELIVERY_CONTRACT_METADATA_KEY = "_wxbot_delivery_contract"
_GROUP_FILE_SEND_DISABLED_REPLY = (
    "当前群尚未开启“允许群文件发送”。请先在 Web 的“群参与与行为”中开启并保存，然后再试。"
)

_OUTBOUND_FILE_TOOLS = {
    (MESSAGE_EXPORT_SCOPE, "export_history"): "export_current_messages_file",
    (FILE_ANALYSIS_SCOPE, "convert"): "convert_current_file",
    (FILE_ANALYSIS_SCOPE, "generate"): "generate_text_file",
}

_EXPLICIT_LIVE_WEB_SEARCH_RE = re.compile(
    r"(?:联网|上网|网上|网络).{0,6}(?:搜|搜索|查|查询|检索)"
    r"|(?:搜|搜索|查|查询|检索).{0,10}(?:今天|今日|最新|实时|刚刚|热点)"
    r"|(?:今天|今日|最新|实时|刚刚|热点).{0,10}(?:搜|搜索|查|查询|检索)"
)
_WEB_SEARCH_CLAUSE_SPLIT_RE = re.compile(
    r"[,，。！？!?；;\n]|(?:但是|但|不过|然而|而是|改成|改为|然后|请(?!勿))"
)
_WEB_SEARCH_NEGATION_PREFIX_RE = re.compile(
    r"(?:不要|别|无需|无须|不用|不必|不需要|禁止|请勿|切勿|"
    r"不准|不允许|不想|没必要|避免).{0,12}$"
)
_LOCAL_DATA_SEARCH_RE = re.compile(
    r"(?:群|聊天|会话|历史).{0,4}(?:消息|记录)|知识库|本地(?:文件|数据)|模型记忆"
)


def _explicit_live_web_search_requested(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    for clause in _WEB_SEARCH_CLAUSE_SPLIT_RE.split(value):
        for match in _EXPLICIT_LIVE_WEB_SEARCH_RE.finditer(clause):
            if _LOCAL_DATA_SEARCH_RE.search(clause) and not re.search(
                r"(?:联网|上网|网上|网络)", match.group(0)
            ):
                continue
            if not _WEB_SEARCH_NEGATION_PREFIX_RE.search(clause[: match.start()]):
                return True
    return False


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


def _event_has_file_attachment(ctx: PipelineContext) -> bool:
    """Detect an inbound file from the normalized event without trusting paths."""

    message = getattr(ctx.event, "message", None)
    for attachment in list(getattr(message, "attachments", []) or []):
        raw_type = getattr(
            getattr(attachment, "type", None),
            "value",
            getattr(attachment, "type", ""),
        )
        if str(raw_type or "").strip().lower() == "file":
            return True
    metadata = dict(getattr(ctx.event, "metadata", {}) or {})
    media = metadata.get("media") if isinstance(metadata.get("media"), dict) else {}
    attachment = (
        metadata.get("file_attachment") if isinstance(metadata.get("file_attachment"), dict) else {}
    )
    if str(attachment.get("type") or "").strip().lower() == "file":
        return True
    return (
        str(
            metadata.get("file_name")
            or metadata.get("file_url")
            or media.get("file_name")
            or media.get("file_url")
            or ""
        ).strip()
        != ""
    )


def _event_is_group(ctx: PipelineContext) -> bool:
    metadata = dict(getattr(ctx.event, "metadata", {}) or {})
    if str(metadata.get("session_kind") or "").strip().lower() == "group":
        return True
    for value in (
        getattr(ctx.event, "external_conversation_id", ""),
        metadata.get("external_conversation_id"),
        getattr(ctx.event, "session_id", ""),
    ):
        if str(value or "").strip().endswith("@chatroom"):
            return True
    return False


def _session_has_file_attachment(ctx: PipelineContext) -> bool:
    """Return whether this session has a recent user file available to act on."""

    session = ctx.session
    if session is None:
        return False
    is_group = _event_is_group(ctx)
    requester_id = str(
        ctx.event.metadata.get("sender_wxid")
        or ctx.event.metadata.get("sender_id")
        or ctx.event.user_id
        or ""
    ).strip()
    for turn in reversed(list(getattr(session, "turns", []) or [])):
        raw_role = getattr(turn, "role", "")
        role = str(getattr(raw_role, "value", raw_role) or "").strip().lower()
        if role != "user":
            continue
        metadata = dict(getattr(turn, "metadata", {}) or {})
        if is_group and requester_id:
            file_sender = str(
                metadata.get("sender_wxid") or metadata.get("sender_id") or ""
            ).strip()
            if file_sender and file_sender != requester_id:
                continue
        media = metadata.get("media") if isinstance(metadata.get("media"), dict) else {}
        attachment = (
            metadata.get("file_attachment")
            if isinstance(metadata.get("file_attachment"), dict)
            else {}
        )
        if (
            str(metadata.get("msg_type") or "").strip().lower() == "file"
            or str(attachment.get("type") or "").strip().lower() == "file"
            or str(
                metadata.get("file_name")
                or metadata.get("file_url")
                or media.get("file_name")
                or media.get("file_url")
                or ""
            ).strip()
        ):
            return True
    return False


@dataclass
class WxbotAgentIntentHook:
    store: WxbotStore | None = None
    effect_handler_enabled: bool = False
    social_policy_store: GroupFilePolicyReader | None = None
    name: str = "wxbot.agent_intent"
    point: HookPoint = HookPoint.BEFORE_ROUTE
    priority: int = 30

    async def run(self, ctx: PipelineContext) -> None:
        if ctx.event.channel != Channel.WECHAT:
            return
        if bool(ctx.event.metadata.get("is_self_sent")):
            return
        is_group = _event_is_group(ctx)
        text = _agent_query_text(ctx)
        file_available = _event_has_file_attachment(ctx) or _session_has_file_attachment(ctx)
        file_intent = _file_intent_requested(
            text,
            has_attachment=file_available,
        )
        if file_intent.file_requested:
            ctx.extras["wxbot_file_intent"] = file_intent.as_dict()
        if is_group:
            if not _event_mentioned_me(ctx):
                return
            scope = _resolve_group_agent_scope(text)
            if (
                scope is None
                and file_intent.operation in {"inspect_incoming", "convert"}
                and file_intent.has_attachment
                and file_intent.source == "incoming_attachment"
            ):
                scope = FILE_ANALYSIS_SCOPE
            if (
                scope is None
                and file_intent.operation == "generate"
                and file_intent.delivery_required
            ):
                scope = FILE_ANALYSIS_SCOPE
        else:
            # Private sessions do not require an @ mention, but only the
            # narrowly defined file-delivery intents and explicit media
            # generation may activate tools. Group-only query scopes remain
            # unavailable in private sessions.
            detected_scope = _resolve_group_agent_scope(text)
            scope = (
                GROUP_DRAW_GENERATION_SCOPE
                if detected_scope == GROUP_DRAW_GENERATION_SCOPE
                else GROUP_VIDEO_GENERATION_SCOPE
                if detected_scope == GROUP_VIDEO_GENERATION_SCOPE
                else MESSAGE_EXPORT_SCOPE
                if _message_export_requested(text)
                else FILE_ANALYSIS_SCOPE
                if (
                    file_intent.operation in {"inspect_incoming", "convert"}
                    and file_intent.has_attachment
                    and file_intent.source == "incoming_attachment"
                )
                or (file_intent.operation == "generate" and file_intent.delivery_required)
                else None
            )
        if not scope:
            return
        router_signals = ctx.extras.setdefault("router_signals", {})
        if not isinstance(router_signals, dict):
            router_signals = {}
            ctx.extras["router_signals"] = router_signals
        group_file_send_enabled = True
        if is_group and scope in {FILE_ANALYSIS_SCOPE, MESSAGE_EXPORT_SCOPE}:
            try:
                await require_group_file_send_enabled(
                    self.social_policy_store,
                    tenant_id=ctx.event.tenant_id,
                    session_id=_event_policy_session_id(ctx),
                )
            except GroupFileSendDenied as exc:
                group_file_send_enabled = False
                ctx.extras["wxbot_file_send_denial_reason"] = exc.reason
            ctx.event.metadata["group_file_send_enabled"] = group_file_send_enabled
            if ctx.session is not None:
                ctx.session.metadata["group_file_send_enabled"] = group_file_send_enabled
        outbound_file_required = bool(
            scope == MESSAGE_EXPORT_SCOPE
            or (
                scope == FILE_ANALYSIS_SCOPE
                and file_intent.operation in {"convert", "generate", "send_existing"}
                and file_intent.delivery_required
            )
        )
        # `tool_intent_matched` describes what this hook actually knows.
        # Keep the legacy routing flag until router rules migrate to the
        # intent-specific signal; availability is still enforced downstream.
        router_signals["tool_intent_matched"] = True
        router_signals["tools_available"] = not (
            outbound_file_required and not group_file_send_enabled
        )
        if file_intent.file_requested:
            router_signals["file_intent"] = file_intent.as_dict()
        if outbound_file_required and not group_file_send_enabled:
            denial_reason = str(
                ctx.extras.get("wxbot_file_send_denial_reason") or "group_file_send_disabled"
            )
            router_signals["file_send_denied"] = denial_reason
            ctx.extras["wxbot_file_send_denial_reply"] = _GROUP_FILE_SEND_DISABLED_REPLY
            logger.info(
                "wxbot.agent_intent.file_send_denied",
                session_id=ctx.event.session_id,
                msg_svr_id=ctx.event.metadata.get("msg_svr_id"),
                scope=scope,
                reason=denial_reason,
            )
            return
        ctx.extras["agent_tool_scope"] = scope
        required_file_tool = _OUTBOUND_FILE_TOOLS.get((scope, file_intent.operation))
        if outbound_file_required and required_file_tool:
            # Keep explicit file delivery as an execution contract, not merely
            # a routing hint.  The Agent engine uses this trusted metadata to
            # prevent a model response from silently degrading to plain text.
            ctx.extras["agent_required_effect"] = {
                "type": "outbound_file",
                "scope": scope,
                "tool": required_file_tool,
                "operation": file_intent.operation,
                "format": file_intent.requested_format or "txt",
            }
            if (
                required_file_tool == "export_current_messages_file"
                and file_intent.recent_minutes is not None
            ):
                ctx.extras["agent_required_effect"]["recent_minutes"] = file_intent.recent_minutes
            if required_file_tool == "generate_text_file" and _explicit_live_web_search_requested(
                text
            ):
                ctx.extras["agent_required_effect"]["web_search_required"] = True
        if is_group:
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
            ctx.event.message_id or ctx.event.metadata.get("msg_svr_id") or ctx.trace_id or ""
        ).strip()
        policy_state = ctx.extras.get("wxbot_reply_policy")
        contract = captured_group_delivery_contract(
            source_message_id=source_message_id,
            policy_state=(policy_state if isinstance(policy_state, dict) else None),
            response_kind="tool_result",
        )
        features = ctx.extras.get("wxbot_humanization_features")
        if isinstance(features, dict):
            contract["style_eligible"] = bool(features.get("style_guard_enabled"))
        voice_profile = ctx.extras.get("wxbot_voice_profile")
        if isinstance(voice_profile, dict) and voice_profile:
            contract["voice_profile"] = dict(voice_profile)
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
            or ("group" if _event_is_group(ctx) else "private")
        )
        external_conversation_id = str(
            ctx.event.external_conversation_id
            or ctx.event.metadata.get("external_conversation_id")
            or ctx.event.session_id
            or ""
        ).strip()
        canonical_conversation_id = str(
            ctx.event.canonical_conversation_id
            or ctx.event.metadata.get("canonical_conversation_id")
            or ctx.event.session_id
            or ""
        ).strip()
        adapter_id = str(ctx.event.adapter_id or ctx.event.metadata.get("adapter_id") or "").strip()
        connection_id = str(
            ctx.event.connection_id or ctx.event.metadata.get("connection_id") or ""
        ).strip()
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
            ctx.extras["wxbot_map_progress_suppressed"] = "async_delivery_contract_unavailable"
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
            "adapter_id": adapter_id,
            "connection_id": connection_id,
            "session_id": ctx.event.session_id,
            "external_conversation_id": external_conversation_id,
            "canonical_conversation_id": canonical_conversation_id,
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
                    "adapter_id": adapter_id,
                    "connection_id": connection_id,
                    "session_id": ctx.event.session_id,
                    "external_conversation_id": external_conversation_id,
                    "canonical_conversation_id": canonical_conversation_id,
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
