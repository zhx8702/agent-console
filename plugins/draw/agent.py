from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from app.agent.registry import AgentToolDefinition
from app.agent.scopes import GROUP_DRAW_GENERATION_SCOPE
from app.billing import BillingCoordinator, BillingReservation, BillingResource, BillingSubject
from app.channel import ChannelRegistry, ChannelSendOptions, ChannelTarget
from app.channel.identity import preferred_external_user_id
from app.common.context import get_trace_id
from app.common.ids import new_trace_id
from app.common.intent_runtime import decision_from_session
from app.common.logging import get_logger
from app.common.prompting import persona_response_language
from app.common.quote_images import QuoteImageSource, quote_image_source_from_metadata
from app.common.types import Role, Session
from plugins.draw.avatar import resolve_prompt_avatar_reference
from plugins.draw.hooks import (
    DRAW_ACCEPTED_TEXT,
    DRAW_API_ERROR_TEXT,
    DRAW_CONFIG_ERROR_TEXT,
    DRAW_CRASH_ERROR_TEXT,
    REDRAW_ACCEPTED_TEXT,
    _draw_success_text,
    _ensure_draw_storage_ready,
    _finish_persistent_operation,
    _resolve_draw_public_url,
    _resolve_persistent_operation,
    send_channel_draw_picture,
)
from plugins.draw.store import (
    DRAW_DEFAULT_QUALITY,
    DRAW_QUALITY_ERROR_TEXT,
    DrawApiError,
    DrawConfigError,
    DrawStore,
    WxbotDeliveryMedia,
    normalize_draw_quality,
)

logger = get_logger(__name__)

_DELIVERY_CONTRACT_METADATA_KEY = "_wxbot_delivery_contract"


class _DrawAgentScopeDenied(RuntimeError):
    """The draw plugin became disabled for the escaped agent-tool task."""


def build_draw_agent_tools(service: DrawAgentToolService) -> list[AgentToolDefinition]:
    return [
        AgentToolDefinition(
            scope=GROUP_DRAW_GENERATION_SCOPE,
            name="generate_group_image",
            description="为当前会话生成一张图片；微信渠道下如果提示词提到群成员头像，例如“基于群里千羽头像”，工具会自动把对应头像作为参考图。",
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "绘图提示词, 直接描述你要生成的画面内容。",
                    },
                    "quality": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": "图片质量。可选 low、medium、high；省略时使用 low。",
                    },
                },
                "required": ["prompt"],
                "additionalProperties": False,
            },
            handler=service.generate_group_image,
            metadata={"session_kinds": ["group", "private"]},
        ),
        AgentToolDefinition(
            scope=GROUP_DRAW_GENERATION_SCOPE,
            name="edit_group_image",
            description="按当前消息引用或回复的图片重绘；适合在原图旁边加人物、换背景或改风格。不要用它凭空生成一张新图。",
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "重绘提示词，直接描述要在原图上改什么。",
                    },
                    "quality": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": "图片质量。可选 low、medium、high；省略时使用 low。",
                    },
                },
                "required": ["prompt"],
                "additionalProperties": False,
            },
            handler=service.edit_group_image,
            metadata={"session_kinds": ["group", "private"]},
        ),
    ]


class DrawAgentToolService:
    def __init__(
        self,
        *,
        store: DrawStore,
        channel_registry: ChannelRegistry | None,
        billing: BillingCoordinator | None = None,
        register_background_task: Callable[[asyncio.Task[None]], Awaitable[None] | None]
        | None = None,
        scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None = None,
    ) -> None:
        self._store = store
        self._channel_registry = channel_registry
        self._billing = billing
        self._register_background_task = register_background_task
        self._scope_execution_allowed = scope_execution_allowed

    async def generate_group_image(
        self, session: Session, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._accept_image_job(
            session,
            arguments,
            command="/draw" if not self._quoted_image_source(session).found else "/redraw",
            require_quote=False,
        )

    async def edit_group_image(
        self, session: Session, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._accept_image_job(
            session,
            arguments,
            command="/redraw",
            require_quote=True,
        )

    async def _accept_image_job(
        self,
        session: Session,
        arguments: dict[str, Any],
        *,
        command: str,
        require_quote: bool,
    ) -> dict[str, Any]:
        target = self._target(session)
        session_id = target.session_id
        tool_name = "edit_group_image" if command == "/redraw" else "generate_group_image"
        if not target.channel:
            raise ValueError(f"{tool_name} 缺少渠道信息")
        if (
            self._channel_registry is None
            or self._channel_registry.outbound_for(target.channel) is None
        ):
            raise ValueError("当前渠道还没有注册出站发送能力")
        if not await self._scope_allowed(session):
            raise ValueError("draw plugin is disabled for this session")
        quote = self._quoted_image_source(session)
        if require_quote and not quote.found:
            raise ValueError("当前消息没有引用图片，无法重绘")
        await self._bind_group_delivery_contract(session, target)

        prompt = str(arguments.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("prompt 不能为空")
        try:
            quality = normalize_draw_quality(arguments.get("quality", DRAW_DEFAULT_QUALITY))
        except ValueError as exc:
            raise ValueError(DRAW_QUALITY_ERROR_TEXT) from exc
        try:
            _ensure_draw_storage_ready(self._store)
        except DrawConfigError as exc:
            raise ValueError(DRAW_CONFIG_ERROR_TEXT) from exc

        tenant_id = str(getattr(session, "tenant_id", "") or "default")
        sender_name = self._sender_name(session)
        trace_id = f"{get_trace_id() or new_trace_id()}:draw-agent"
        reservation: BillingReservation | None = None
        if self._billing is not None and self._billing.provider("credits") is not None:
            reservation, cancellation_requested = await _resolve_persistent_operation(
                self._billing.reserve(
                    BillingSubject(
                        tenant_id=tenant_id,
                        session_id=self._billing_session_id(session, target),
                        user_id=self._billing_user_id(session),
                        display_name=sender_name,
                    ),
                    BillingResource(
                        kind="command",
                        operation=command,
                        reference=trace_id,
                        metadata={
                            "command": command,
                            "quality": quality,
                            "source": "agent_tool",
                        },
                    ),
                )
            )
            if cancellation_requested:
                await _finish_persistent_operation(self._billing.release(reservation))
                raise asyncio.CancelledError()

        task = asyncio.create_task(
            self._run_async_job(
                session,
                prompt=prompt,
                quality=quality,
                trace_id=trace_id,
                reservation=reservation,
                sender_name=sender_name,
            ),
            name=f"draw-agent-{session_id}",
        )
        if self._register_background_task is not None:
            maybe_awaitable = self._register_background_task(task)
            if maybe_awaitable is not None:
                _, cancellation_requested = await _resolve_persistent_operation(
                    maybe_awaitable
                )
                if cancellation_requested:
                    raise asyncio.CancelledError()

        logger.info(
            "draw.agent_tool_accepted",
            session_id=session_id,
            trace_id=trace_id,
            prompt_length=len(prompt),
            quality=quality,
            command=command,
        )
        return {
            "accepted": True,
            "async": True,
            "prompt": prompt,
            "quality": quality,
            "command": command,
            "trace_id": trace_id,
            "message": REDRAW_ACCEPTED_TEXT if command == "/redraw" else DRAW_ACCEPTED_TEXT,
        }

    async def _run_async_job(
        self,
        session: Session,
        *,
        prompt: str,
        quality: str,
        trace_id: str,
        reservation: BillingReservation | None,
        sender_name: str,
    ) -> None:
        session_id = str(getattr(session, "session_id", "") or "")
        success_committed = False
        try:
            await self._require_scope(session)
            quote = self._quoted_image_source(session)
            avatar_ref = None
            if not quote.found:
                avatar_ref = await resolve_prompt_avatar_reference(
                    self._store,
                    session_id=session_id,
                    prompt=prompt,
                    trace_id=trace_id,
                    decision=decision_from_session(session),
                )
            if quote.found:
                result = await self._store.edit_reference_image(
                    image_url=quote.image_url,
                    image_path=quote.image_path,
                    prompt=prompt,
                    trace_id=trace_id,
                    quality=quality,
                    source_label=quote.label or "quote",
                )
            elif avatar_ref is not None and (avatar_ref.image_path or avatar_ref.avatar_url):
                result = await self._store.edit_reference_image(
                    image_url=avatar_ref.avatar_url,
                    image_path=avatar_ref.image_path,
                    prompt=prompt,
                    trace_id=trace_id,
                    quality=quality,
                    source_label=avatar_ref.source_label,
                )
            else:
                result = await self._store.generate_image(
                    prompt,
                    trace_id=trace_id,
                    quality=quality,
                )
            await self._require_scope(session)
            image_url = _resolve_draw_public_url(
                self._store,
                public_path=result.public_path,
                source_url=result.source_url,
            )
            delivery_image_path = ""
            delivery_image_url = image_url
            delivery_media: WxbotDeliveryMedia | None = None
            target = self._target(session)
            stage_media = getattr(self._store, "stage_wxbot_delivery_media", None)
            if target.channel == "wechat" and callable(stage_media):
                delivery_media = stage_media(
                    result.local_path,
                    result.image_id,
                )
                delivery_image_path = delivery_media.image_path
                delivery_image_url = ""
            else:
                stage_for_delivery = getattr(
                    self._store, "stage_for_wxbot_delivery", None
                )
                if target.channel == "wechat" and callable(stage_for_delivery):
                    delivery_image_path = stage_for_delivery(
                        result.local_path,
                        result.image_id,
                    )
                    delivery_image_url = ""
            async def _capture_and_deliver() -> None:
                await self._require_scope(session)
                if self._billing is not None and reservation is not None:
                    await self._billing.capture(reservation)
                await self._require_scope(session)
                await self._enqueue_messages(
                    session,
                    text=_draw_success_text(result.image_id),
                    image_path=delivery_image_path,
                    image_url=delivery_image_url,
                    delivery_media=delivery_media,
                    trace_id=trace_id,
                )

            _, cancellation_requested = await _resolve_persistent_operation(
                _capture_and_deliver()
            )
            success_committed = True
            if cancellation_requested:
                raise asyncio.CancelledError()
            logger.info(
                "draw.agent_tool_completed",
                session_id=session_id,
                trace_id=trace_id,
                quality=quality,
            )
        except _DrawAgentScopeDenied:
            if (
                not success_committed
                and self._billing is not None
                and reservation is not None
            ):
                await _finish_persistent_operation(
                    self._billing.release(reservation)
                )
            logger.info(
                "draw.agent_tool_scope_deferred",
                session_id=session_id,
                trace_id=trace_id,
            )
        except asyncio.CancelledError:
            if (
                not success_committed
                and self._billing is not None
                and reservation is not None
            ):
                await _finish_persistent_operation(self._billing.release(reservation))
            logger.warning(
                "draw.agent_tool_cancelled",
                session_id=session_id,
                trace_id=trace_id,
                settled=success_committed,
            )
            raise
        except DrawConfigError as exc:
            async def _release_and_notify_config() -> None:
                if self._billing is not None and reservation is not None:
                    await self._billing.release(reservation)
                await self._require_scope(session)
                await self._enqueue_messages(
                    session,
                    text=DRAW_CONFIG_ERROR_TEXT,
                    trace_id=trace_id,
                )

            try:
                _, cancellation_requested = await _resolve_persistent_operation(
                    _release_and_notify_config()
                )
            except _DrawAgentScopeDenied:
                logger.info(
                    "draw.agent_tool_config_notification_scope_skipped",
                    session_id=session_id,
                    trace_id=trace_id,
                )
                return
            logger.warning(
                "draw.agent_tool_config_failed",
                session_id=session_id,
                trace_id=trace_id,
                error_class=exc.__class__.__name__,
            )
            if cancellation_requested:
                raise asyncio.CancelledError() from None
        except DrawApiError as exc:
            async def _release_and_notify_api() -> None:
                if self._billing is not None and reservation is not None:
                    await self._billing.release(reservation)
                await self._require_scope(session)
                await self._enqueue_messages(
                    session,
                    text=DRAW_API_ERROR_TEXT,
                    trace_id=trace_id,
                )

            try:
                _, cancellation_requested = await _resolve_persistent_operation(
                    _release_and_notify_api()
                )
            except _DrawAgentScopeDenied:
                logger.info(
                    "draw.agent_tool_failure_notification_scope_skipped",
                    session_id=session_id,
                    trace_id=trace_id,
                )
                return
            logger.warning(
                "draw.agent_tool_failed",
                session_id=session_id,
                trace_id=trace_id,
                error_class=exc.__class__.__name__,
            )
            if cancellation_requested:
                raise asyncio.CancelledError() from None
        except Exception:
            async def _release_and_notify_crash() -> None:
                if self._billing is not None and reservation is not None:
                    await self._billing.release(reservation)
                await self._require_scope(session)
                await self._enqueue_messages(
                    session,
                    text=DRAW_CRASH_ERROR_TEXT,
                    trace_id=trace_id,
                )

            try:
                _, cancellation_requested = await _resolve_persistent_operation(
                    _release_and_notify_crash()
                )
            except _DrawAgentScopeDenied:
                logger.info(
                    "draw.agent_tool_crash_notification_scope_skipped",
                    session_id=session_id,
                    trace_id=trace_id,
                )
                return
            logger.exception(
                "draw.agent_tool_crashed",
                session_id=session_id,
                trace_id=trace_id,
            )
            if cancellation_requested:
                raise asyncio.CancelledError() from None

    async def _enqueue_messages(
        self,
        session: Session,
        *,
        text: str,
        image_path: str = "",
        image_url: str = "",
        delivery_media: WxbotDeliveryMedia | None = None,
        trace_id: str,
    ) -> None:
        session_id = str(getattr(session, "session_id", "") or "")
        target = self._target(session)
        if self._channel_registry is None:
            raise RuntimeError("channel registry is not configured")
        outbound = self._channel_registry.require_outbound_for_target(target)
        source_message = {
            "agent_tool": "generate_group_image",
            "trace_id": trace_id,
            "session_id": session_id,
            "prompt": text,
            _DELIVERY_CONTRACT_METADATA_KEY: self._captured_delivery_contract(
                target
            ),
        }
        delivery_contract = self._task_result_delivery(target)
        if text.strip():
            await self._require_scope(session)
            command_id = f"channel-reply:{target.tenant_id}:{trace_id}:draw-agent-text"
            await outbound.send_text(
                target,
                text.strip(),
                ChannelSendOptions(
                    trace_id=trace_id,
                    source_message=source_message,
                    idempotency_key=command_id,
                    delivery_metadata={
                        "command_id": command_id,
                        "idempotency_key": command_id,
                        **delivery_contract,
                    },
                ),
            )
        if image_path.strip() or image_url.strip():
            await self._require_scope(session)
            command_id = f"channel-reply:{target.tenant_id}:{trace_id}:draw-agent-image"
            file_command_id = f"channel-reply:{target.tenant_id}:{trace_id}:draw-agent-file"
            await send_channel_draw_picture(
                outbound,
                target,
                image_path=image_path,
                image_url=image_url,
                delivery_media=delivery_media,
                trace_id=trace_id,
                source_message=source_message,
                command_id=command_id,
                file_command_id=file_command_id,
                delivery_contract=delivery_contract,
            )

    async def _scope_allowed(self, session: Session) -> bool:
        gate = self._scope_execution_allowed
        if gate is None:
            logger.error("draw.agent_tool_scope_gate_missing")
            return False
        try:
            return (
                await gate(
                    str(getattr(session, "tenant_id", "") or ""),
                    str(getattr(session, "session_id", "") or ""),
                )
                is True
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "draw.agent_tool_scope_gate_failed",
                tenant_id=str(getattr(session, "tenant_id", "") or ""),
                session_id=str(getattr(session, "session_id", "") or ""),
                error_type=exc.__class__.__name__,
            )
            return False

    async def _require_scope(self, session: Session) -> None:
        if not await self._scope_allowed(session):
            raise _DrawAgentScopeDenied("draw agent tool scope disabled")

    @staticmethod
    def _latest_user_metadata(session: Session) -> dict[str, Any]:
        for turn in reversed(list(getattr(session, "turns", []) or [])):
            if turn.role == Role.USER:
                return dict(turn.metadata or {})
        return {}

    def _quoted_image_source(self, session: Session) -> QuoteImageSource:
        return quote_image_source_from_metadata(
            self._latest_user_metadata(session),
            session=session,
        )

    def _session_name(self, session: Session) -> str:
        metadata = self._latest_user_metadata(session)
        return str(
            metadata.get("session_name")
            or getattr(session, "metadata", {}).get("session_name")
            or getattr(session, "session_id", "")
            or ""
        ).strip()

    def _sender_name(self, session: Session) -> str:
        metadata = self._latest_user_metadata(session)
        return str(metadata.get("sender_name") or "").strip()

    def _sender_wxid(self, session: Session) -> str:
        return self._billing_user_id(session)

    def _billing_user_id(self, session: Session) -> str:
        metadata = self._latest_user_metadata(session)
        return preferred_external_user_id(
            metadata.get("sender_wxid"),
            metadata.get("sender_id"),
            metadata.get("external_participant_id"),
            getattr(session, "external_participant_id", ""),
            getattr(session, "user_id", ""),
        )

    def _billing_session_id(self, session: Session, target: ChannelTarget) -> str:
        candidates = (
            target.external_conversation_id,
            getattr(session, "external_conversation_id", ""),
            (getattr(session, "metadata", {}) or {}).get("external_conversation_id"),
            (getattr(session, "metadata", {}) or {}).get("external_session_id"),
            self._latest_user_metadata(session).get("external_conversation_id"),
            self._latest_user_metadata(session).get("external_session_id"),
            getattr(session, "session_id", ""),
        )
        fallback = ""
        for value in candidates:
            session_id = str(value or "").strip()
            if not session_id:
                continue
            if not fallback:
                fallback = session_id
            if not session_id.startswith("cx1:"):
                return session_id
        return fallback

    def _reply_to_msg_svr_id(self, session: Session) -> str:
        metadata = self._latest_user_metadata(session)
        for key in ("reply_to_message_id", "msg_svr_id"):
            value = str(metadata.get(key) or "").strip()
            if value.isdigit():
                return value
        return ""

    def _current_source_message_id(self, session: Session, target: ChannelTarget) -> str:
        metadata = self._latest_user_metadata(session)
        for key in ("message_id", "source_message_id", "canonical_message_id"):
            value = str(metadata.get(key) or "").strip()
            if value:
                return value[:128]
        captured = str(
            self._captured_delivery_contract(target).get("source_message_id") or ""
        ).strip()
        if captured:
            return captured[:128]
        return str(target.reply_to_message_id or "").strip()[:128]

    def _target(self, session: Session) -> ChannelTarget:
        metadata = self._latest_user_metadata(session)
        if persona_response_language(session) == "en":
            metadata["persona_response_language"] = "en"
        session_contract = getattr(session, "metadata", {}).get(
            _DELIVERY_CONTRACT_METADATA_KEY
        )
        if (
            _DELIVERY_CONTRACT_METADATA_KEY not in metadata
            and isinstance(session_contract, dict)
        ):
            metadata[_DELIVERY_CONTRACT_METADATA_KEY] = dict(session_contract)
        session_id = str(getattr(session, "session_id", "") or "")
        raw_channel = getattr(session, "channel", "")
        channel = str(getattr(raw_channel, "value", raw_channel) or "")
        return ChannelTarget(
            tenant_id=str(getattr(session, "tenant_id", "") or "default"),
            channel=channel,
            session_id=session_id,
            adapter_id=str(
                getattr(session, "adapter_id", "") or metadata.get("adapter_id") or ""
            ),
            connection_id=str(
                getattr(session, "connection_id", "")
                or metadata.get("connection_id")
                or ""
            ),
            external_conversation_id=str(
                getattr(session, "external_conversation_id", "")
                or metadata.get("external_conversation_id")
                or session_id
            ),
            canonical_conversation_id=str(
                getattr(session, "canonical_conversation_id", "")
                or getattr(session, "conversation_id", "")
                or metadata.get("canonical_conversation_id")
                or session_id
            ),
            session_name=self._session_name(session),
            session_kind=str(
                metadata.get("session_kind")
                or getattr(session, "metadata", {}).get("session_kind")
                or ("group" if session_id.endswith("@chatroom") else "")
            ),
            user_id=str(getattr(session, "user_id", "") or ""),
            sender_id=self._sender_wxid(session),
            sender_name=self._sender_name(session),
            reply_to_message_id=self._reply_to_msg_svr_id(session),
            metadata=metadata,
        )

    async def _bind_group_delivery_contract(
        self,
        session: Session,
        target: ChannelTarget,
    ) -> None:
        if target.channel != "wechat" or not target.session_id.endswith("@chatroom"):
            return
        current_id = self._current_source_message_id(session, target)
        captured = self._captured_delivery_contract(target)
        if (
            self._complete_delivery_contract(captured)
            and str(captured.get("source_message_id") or "").strip() == current_id
            and current_id
        ):
            session.metadata[_DELIVERY_CONTRACT_METADATA_KEY] = dict(captured)
            return
        if self._channel_registry is None:
            raise RuntimeError("channel registry is not configured")
        outbound = self._channel_registry.require_outbound_for_target(target)
        capture = getattr(outbound, "capture_group_delivery_contract", None)
        if not callable(capture):
            return
        contract = await capture(
            target,
            source_message_id=current_id or target.reply_to_message_id,
            response_kind="tool_result",
        )
        if not isinstance(contract, dict) or not self._complete_delivery_contract(
            contract
        ):
            raise RuntimeError("draw_agent_async_delivery_contract_unavailable")
        session.metadata[_DELIVERY_CONTRACT_METADATA_KEY] = dict(contract)

    @staticmethod
    def _complete_delivery_contract(contract: dict[str, Any]) -> bool:
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

    @staticmethod
    def _captured_delivery_contract(target: ChannelTarget) -> dict[str, Any]:
        captured = target.metadata.get(_DELIVERY_CONTRACT_METADATA_KEY)
        return dict(captured) if isinstance(captured, dict) else {}

    @classmethod
    def _task_result_delivery(cls, target: ChannelTarget) -> dict[str, Any]:
        delivery = cls._captured_delivery_contract(target)
        source_message_id = str(
            delivery.get("source_message_id") or target.reply_to_message_id or ""
        ).strip()
        delivery.update(
            {
                "participation_status": "must_reply",
                "source_message_id": source_message_id,
                "response_kind": "tool_result",
                "speech_output_kind": "ordinary",
                "speech_class": "obligation",
                "participation_reason_codes": ["direct_tool_request"],
            }
        )
        return delivery
