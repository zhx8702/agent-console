from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.agent.registry import AgentToolDefinition
from app.agent.scopes import GROUP_VIDEO_GENERATION_SCOPE
from app.billing import BillingCoordinator, BillingReservation, BillingResource, BillingSubject
from app.channel import ChannelFile, ChannelRegistry, ChannelSendOptions
from app.common.context import get_trace_id
from app.common.ids import new_trace_id
from app.common.logging import get_logger
from app.common.types import Role, Session
from plugins.draw.agent import DrawAgentToolService
from plugins.video.store import (
    VIDEO_DEFAULT_DURATION,
    VIDEO_DEFAULT_RESOLUTION,
    VIDEO_MAX_DURATION,
    VIDEO_RESOLUTIONS,
    VideoApiError,
    VideoConfigError,
    VideoResult,
    VideoStore,
)

logger = get_logger(__name__)

VIDEO_ACCEPTED_TEXT = "收到，正在生成视频。"
VIDEO_CONFIG_ERROR_TEXT = "视频生成服务还没配置好，请联系管理员。"
VIDEO_API_ERROR_TEXT = "视频生成失败了，请稍后再试。"
VIDEO_CRASH_ERROR_TEXT = "视频生成遇到内部错误，请稍后再试。"
VIDEO_DELIVERY_SUPPRESSED_TEXT = (
    "视频已生成，但当前会话未开启文件发送，视频暂未发送。请开启群文件发送后重试。"
)


class VideoDeliverySuppressed(RuntimeError):
    """The channel policy rejected the generated video's file delivery."""

    def __init__(self, reason: str, *, video_id: str = "") -> None:
        self.reason = str(reason or "channel_delivery_suppressed")
        self.video_id = str(video_id or "")
        super().__init__(self.reason)


def _latest_inbound_video_prompt(session: Session) -> str:
    """Return the current user text without asking the LLM to rewrite it."""

    for turn in reversed(list(getattr(session, "turns", []) or [])):
        raw_role = getattr(turn, "role", "")
        role = str(getattr(raw_role, "value", raw_role) or "").strip().lower()
        if role != Role.USER.value:
            continue
        metadata = dict(getattr(turn, "metadata", {}) or {})
        for key in ("wxbot_normalized_content", "original_content"):
            value = str(metadata.get(key) or "").strip()
            if value:
                return value
        value = str(getattr(turn, "content", "") or "").strip()
        if value:
            return value
    return ""


def build_video_agent_tools(service: VideoAgentToolService) -> list[AgentToolDefinition]:
    return [
        AgentToolDefinition(
            scope=GROUP_VIDEO_GENERATION_SCOPE,
            name="generate_group_video",
            description="为当前会话生成一段视频，并在生成完成后自动发送。",
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "视频提示词，描述主体、动作、镜头和风格。",
                    },
                    "duration": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 15,
                        "default": VIDEO_DEFAULT_DURATION,
                        "description": "视频时长，单位为秒。",
                    },
                    "resolution": {
                        "type": "string",
                        "enum": list(VIDEO_RESOLUTIONS),
                        "default": VIDEO_DEFAULT_RESOLUTION,
                        "description": "视频分辨率。",
                    },
                },
                "required": ["prompt"],
                "additionalProperties": False,
            },
            handler=service.generate_group_video,
            metadata={"session_kinds": ["group", "private"]},
        )
    ]


class VideoAgentToolService(DrawAgentToolService):
    """Async video tool; delivery/permission fencing is shared with draw."""

    def __init__(
        self,
        *,
        store: VideoStore,
        channel_registry: ChannelRegistry | None,
        billing: BillingCoordinator | None = None,
        register_background_task: Callable[[asyncio.Task[None]], Awaitable[None] | None]
        | None = None,
        scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None = None,
    ) -> None:
        super().__init__(
            store=store,  # type: ignore[arg-type]
            channel_registry=channel_registry,
            billing=billing,
            register_background_task=register_background_task,
            scope_execution_allowed=scope_execution_allowed,
        )

    async def generate_group_video(
        self, session: Session, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        inbound_prompt = _latest_inbound_video_prompt(session)
        if inbound_prompt:
            # Keep the effective API/audit argument equal to the received
            # message. The model still chooses duration/resolution, but it
            # must not rewrite the user's video description.
            arguments["prompt"] = inbound_prompt
        prompt = inbound_prompt or str(
            arguments.get("prompt") or ""
        ).strip()
        if not prompt:
            raise ValueError("prompt 不能为空")
        try:
            duration = int(arguments.get("duration", VIDEO_DEFAULT_DURATION))
        except (TypeError, ValueError) as exc:
            raise ValueError("duration 必须是整数") from exc
        resolution = str(
            arguments.get("resolution", VIDEO_DEFAULT_RESOLUTION)
            or VIDEO_DEFAULT_RESOLUTION
        ).strip().lower()
        if not 1 <= duration <= VIDEO_MAX_DURATION:
            raise ValueError(
                f"duration 必须在 1 到 {VIDEO_MAX_DURATION} 秒之间"
            )
        if resolution not in VIDEO_RESOLUTIONS:
            raise ValueError("resolution 只能是 480p、720p 或 1080p")

        return await self._accept_video_job(
            session,
            prompt=prompt,
            duration=duration,
            resolution=resolution,
            source="agent_tool",
        )

    async def accept_video_command(
        self,
        session: Session,
        *,
        prompt: str,
        duration: int,
        resolution: str,
        reservation: BillingReservation | None,
        trace_id: str,
    ) -> dict[str, Any]:
        """Accept a slash-command request using the command hook's reservation."""

        return await self._accept_video_job(
            session,
            prompt=prompt,
            duration=duration,
            resolution=resolution,
            reservation=reservation,
            reserve_billing=False,
            trace_id=trace_id,
            source="command",
        )

    async def _accept_video_job(
        self,
        session: Session,
        *,
        prompt: str,
        duration: int,
        resolution: str,
        reservation: BillingReservation | None = None,
        reserve_billing: bool = True,
        trace_id: str = "",
        source: str = "agent_tool",
    ) -> dict[str, Any]:
        target = self._target(session)
        session_id = target.session_id
        if not target.channel:
            raise ValueError("generate_group_video 缺少渠道信息")
        if target.session_kind not in {"", "group", "private"} and not session_id.endswith(
            "@chatroom"
        ):
            raise ValueError("generate_group_video 仅支持微信私聊或群聊会话")
        if self._channel_registry is None:
            raise ValueError("当前渠道还没有注册出站发送能力")
        outbound = self._channel_registry.outbound_for_target(target)
        if outbound is None or not callable(getattr(outbound, "send_file", None)):
            raise ValueError("当前渠道的 SDK 还不支持文件发送")
        if not await self._scope_allowed(session):
            raise ValueError("video plugin is disabled for this session")
        await self._bind_group_delivery_contract(session, target)

        tenant_id = str(getattr(session, "tenant_id", "") or "default")
        user_id = str(getattr(session, "user_id", "") or "")
        sender_name = self._sender_name(session)
        trace_id = str(trace_id or f"{get_trace_id() or new_trace_id()}:{source}")
        billing = self._billing
        if reserve_billing and billing is not None and billing.provider("credits") is not None:
            reservation = await billing.reserve(
                BillingSubject(
                    tenant_id=tenant_id,
                    session_id=target.external_conversation_id or session_id,
                    user_id=user_id,
                    display_name=sender_name,
                ),
                BillingResource(
                    kind="command",
                    operation="/video",
                    reference=trace_id,
                    metadata={
                        "command": "/video",
                        "duration": duration,
                        "resolution": resolution,
                        "source": "agent_tool",
                    },
                ),
            )

        # Put the acceptance message in the channel queue before starting the
        # background job.  A fast provider failure must never overtake the
        # user's acknowledgement because the job is allowed to finish before
        # the outer orchestrator publishes its normal tool reply.
        try:
            await self._enqueue_messages(
                session,
                text=VIDEO_ACCEPTED_TEXT,
                trace_id=trace_id,
                source=source,
                message_kind="accepted",
            )
        except Exception:
            if reserve_billing and billing is not None and reservation is not None:
                await billing.release(reservation)
            raise

        try:
            task = asyncio.create_task(
                self._run_async_job(
                    session,
                    prompt=prompt,
                    duration=duration,
                    resolution=resolution,
                    trace_id=trace_id,
                    reservation=reservation,
                    source=source,
                ),
                name=f"video-{source}-{session_id}",
            )
        except Exception:
            if reserve_billing and billing is not None and reservation is not None:
                await billing.release(reservation)
            raise
        if self._register_background_task is not None:
            maybe_awaitable = self._register_background_task(task)
            if maybe_awaitable is not None:
                await maybe_awaitable
        return {
            "accepted": True,
            "async": True,
            "prompt": prompt,
            "duration": duration,
            "resolution": resolution,
            "command": "/video",
            "trace_id": trace_id,
            "message": VIDEO_ACCEPTED_TEXT,
            "accepted_reply_enqueued": True,
            "self_enqueued_reply": True,
            "suppress_final_reply": True,
            "suppress_outbound": True,
        }

    async def _run_async_job(
        self,
        session: Session,
        *,
        prompt: str,
        duration: int,
        resolution: str,
        trace_id: str,
        reservation: BillingReservation | None,
        source: str = "agent_tool",
    ) -> None:
        billing = self._billing
        success_committed = False
        try:
            await self._require_scope(session)
            result = await self._store.generate_video(  # type: ignore[union-attr]
                prompt,
                duration=duration,
                resolution=resolution,
            )
            await self._require_scope(session)
            await self._enqueue_messages(
                session,
                result=result,
                trace_id=trace_id,
                source=source,
            )
            # Capture only after the channel accepts the file. A group-file
            # policy denial returns a suppressed result without a queue row.
            if billing is not None and reservation is not None:
                await billing.capture(reservation)
            success_committed = True
            logger.info(
                "video.generation_completed",
                session_id=session.session_id,
                trace_id=trace_id,
                video_id=result.video_id,
                source=source,
            )
        except asyncio.CancelledError:
            if not success_committed and billing is not None and reservation is not None:
                await billing.release(reservation)
            raise
        except VideoDeliverySuppressed as exc:
            await self._release_and_notify(
                session,
                reservation=reservation,
                text=VIDEO_DELIVERY_SUPPRESSED_TEXT,
                trace_id=trace_id,
                source=source,
            )
            logger.warning(
                "video.delivery_suppressed",
                session_id=session.session_id,
                trace_id=trace_id,
                source=source,
                video_id=exc.video_id,
                reason=exc.reason,
            )
        except VideoConfigError as exc:
            logger.warning(
                "video.generation_failed",
                session_id=session.session_id,
                trace_id=trace_id,
                source=source,
                error_class=exc.__class__.__name__,
                error=str(exc),
            )
            await self._release_and_notify(
                session,
                reservation=reservation,
                text=VIDEO_CONFIG_ERROR_TEXT,
                trace_id=trace_id,
                source=source,
            )
        except VideoApiError as exc:
            logger.warning(
                "video.generation_failed",
                session_id=session.session_id,
                trace_id=trace_id,
                source=source,
                error_class=exc.__class__.__name__,
                error=str(exc),
            )
            await self._release_and_notify(
                session,
                reservation=reservation,
                text=VIDEO_API_ERROR_TEXT,
                trace_id=trace_id,
                source=source,
            )
        except Exception:
            await self._release_and_notify(
                session,
                reservation=reservation,
                text=VIDEO_CRASH_ERROR_TEXT,
                trace_id=trace_id,
                source=source,
            )
            logger.exception(
                "video.agent_tool_crashed",
                session_id=session.session_id,
                trace_id=trace_id,
            )

    async def _release_and_notify(
        self,
        session: Session,
        *,
        reservation: BillingReservation | None,
        text: str,
        trace_id: str,
        source: str = "agent_tool",
    ) -> None:
        if self._billing is not None and reservation is not None:
            await self._billing.release(reservation)
        try:
            await self._require_scope(session)
            await self._enqueue_messages(
                session,
                text=text,
                trace_id=trace_id,
                source=source,
                message_kind="failure",
            )
        except Exception:
            logger.warning(
                "video.agent_tool_failure_notification_failed",
                session_id=session.session_id,
                trace_id=trace_id,
            )

    async def _enqueue_messages(
        self,
        session: Session,
        *,
        trace_id: str,
        text: str = "",
        result: VideoResult | None = None,
        source: str = "agent_tool",
        message_kind: str = "text",
    ) -> None:
        target = self._target(session)
        if self._channel_registry is None:
            raise RuntimeError("channel registry is not configured")
        outbound = self._channel_registry.require_outbound_for_target(target)
        delivery_contract = self._task_result_delivery(target)
        source_message = {
            "agent_tool": "generate_group_video" if source == "agent_tool" else "",
            "command": "/video" if source == "command" else "",
            "video_source": source,
            "trace_id": trace_id,
            "session_id": target.session_id,
        }
        idempotency_prefix = "video-agent" if source == "agent_tool" else "video-command"
        if text.strip():
            safe_message_kind = "".join(
                ch for ch in str(message_kind or "text") if ch.isalnum() or ch in "-_"
            ) or "text"
            command_id = (
                f"channel-reply:{target.tenant_id}:{trace_id}:"
                f"{idempotency_prefix}-{safe_message_kind}"
            )
            text_result = await outbound.send_text(
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
            self._raise_if_suppressed(text_result, message_kind=message_kind)
        if result is None:
            return
        send_file = getattr(outbound, "send_file", None)
        if not callable(send_file):
            raise RuntimeError("当前渠道的 SDK 还不支持文件发送")
        file_path = Path(result.local_path)
        file_size = file_path.stat().st_size
        command_id = f"channel-reply:{target.tenant_id}:{trace_id}:{idempotency_prefix}-video"
        file_result = await send_file(
            target,
            ChannelFile(
                file_path=str(file_path),
                file_name=file_path.name,
                file_size=file_size,
            ),
            ChannelSendOptions(
                trace_id=trace_id,
                source_message=source_message,
                idempotency_key=command_id,
                delivery_metadata={
                    "command_id": command_id,
                    "idempotency_key": command_id,
                    "video_id": result.video_id,
                    **delivery_contract,
                },
            ),
        )
        self._raise_if_suppressed(
            file_result,
            message_kind="video_file",
            video_id=result.video_id,
        )

    @staticmethod
    def _raise_if_suppressed(
        result: object,
        *,
        message_kind: str,
        video_id: str = "",
    ) -> None:
        metadata = getattr(result, "metadata", None)
        if not isinstance(metadata, dict) or not bool(metadata.get("suppressed")):
            return
        raise VideoDeliverySuppressed(
            str(metadata.get("reason") or f"{message_kind}_suppressed"),
            video_id=video_id,
        )
