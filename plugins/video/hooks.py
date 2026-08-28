from __future__ import annotations

from dataclasses import dataclass

from app.billing import BillingReservation
from app.commands import CommandDefinition
from app.common.types import MessageType
from app.orchestrator.pipeline import PipelineContext
from plugins.video.agent import VIDEO_ACCEPTED_TEXT, VideoAgentToolService
from plugins.video.store import (
    VIDEO_DEFAULT_DURATION,
    VIDEO_DEFAULT_RESOLUTION,
    VIDEO_MAX_DURATION,
    VIDEO_RESOLUTIONS,
)

VIDEO_HELP_TEXT = (
    "用法: /video [duration=1-15] [resolution=480p|720p|1080p] 提示词"
    " 或 /视频 提示词"
)
VIDEO_DURATION_ERROR_TEXT = f"视频时长必须是 1 到 {VIDEO_MAX_DURATION} 秒的整数。"
VIDEO_RESOLUTION_ERROR_TEXT = "视频分辨率只能是 480p、720p 或 1080p。"


@dataclass(frozen=True)
class VideoCommandArgs:
    duration: int
    resolution: str
    args: list[str]


def _parse_duration(value: object) -> int:
    try:
        duration = int(str(value or "").strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(VIDEO_DURATION_ERROR_TEXT) from exc
    if not 1 <= duration <= VIDEO_MAX_DURATION:
        raise ValueError(VIDEO_DURATION_ERROR_TEXT)
    return duration


def _parse_resolution(value: object) -> str:
    resolution = str(value or "").strip().lower()
    if resolution not in VIDEO_RESOLUTIONS:
        raise ValueError(VIDEO_RESOLUTION_ERROR_TEXT)
    return resolution


def _parse_video_command_args(args: list[str]) -> VideoCommandArgs:
    duration = VIDEO_DEFAULT_DURATION
    resolution = VIDEO_DEFAULT_RESOLUTION
    remaining: list[str] = []
    index = 0
    while index < len(args):
        item = str(args[index] or "").strip()
        if not item:
            index += 1
            continue
        lower = item.lower()
        if lower in {"--duration", "-d", "duration", "时长"}:
            if index + 1 >= len(args):
                raise ValueError(VIDEO_DURATION_ERROR_TEXT)
            duration = _parse_duration(args[index + 1])
            index += 2
            continue
        if lower.startswith(("--duration=", "duration=", "时长=")):
            duration = _parse_duration(item.split("=", 1)[1])
            index += 1
            continue
        if lower in {"--resolution", "-r", "resolution", "分辨率"}:
            if index + 1 >= len(args):
                raise ValueError(VIDEO_RESOLUTION_ERROR_TEXT)
            resolution = _parse_resolution(args[index + 1])
            index += 2
            continue
        if lower.startswith(("--resolution=", "resolution=", "分辨率=")):
            resolution = _parse_resolution(item.split("=", 1)[1])
            index += 1
            continue
        remaining.append(item)
        index += 1
    return VideoCommandArgs(
        duration=duration,
        resolution=resolution,
        args=remaining,
    )


def video_command_billing_metadata(
    ctx: PipelineContext,
    args: list[str],
) -> dict[str, object]:
    _ = ctx
    parsed = _parse_video_command_args(args)
    return {
        "duration": parsed.duration,
        "resolution": parsed.resolution,
    }


def _should_handle_video_command(ctx: PipelineContext) -> bool:
    event = ctx.event
    return (
        event.message.type == MessageType.TEXT
        and not bool(event.metadata.get("image_url"))
    )


async def _handle_video_command(
    service: VideoAgentToolService,
    ctx: PipelineContext,
    args: list[str],
) -> str:
    parsed = _parse_video_command_args(args)
    prompt = " ".join(str(item or "").strip() for item in parsed.args).strip()
    if not prompt:
        raise ValueError(VIDEO_HELP_TEXT)
    if ctx.session is None:
        raise ValueError("视频命令缺少当前会话信息。")

    reservation = ctx.extras.get("_billing_command_reservation")
    if not isinstance(reservation, BillingReservation):
        reservation = None
    accepted = await service.accept_video_command(
        ctx.session,
        prompt=prompt,
        duration=parsed.duration,
        resolution=parsed.resolution,
        reservation=reservation,
        trace_id=f"{ctx.trace_id}:video-command",
    )
    # The command hook must leave this reservation to the async video job. The
    # job captures it after Grok succeeds and releases it on every failure.
    ctx.extras["_billing_command_deferred"] = True
    if accepted.get("accepted_reply_enqueued"):
        # VideoAgentToolService already placed the acknowledgement in the
        # channel queue before starting the async job.  Do not publish a
        # second command reply after the job has started.
        ctx.extras["suppress_outbound"] = True
        ctx.extras["skip_assistant_turn"] = True
        return ""
    return str(accepted.get("message") or VIDEO_ACCEPTED_TEXT)


def build_video_command_definitions(
    service: VideoAgentToolService,
) -> list[CommandDefinition]:
    async def _command(ctx: PipelineContext, args: list[str]) -> str:
        return await _handle_video_command(service, ctx, args)

    return [
        CommandDefinition(
            plugin_name="video",
            command="/video",
            aliases=("/视频",),
            description="按参数生成视频并回传到当前会话",
            usage=VIDEO_HELP_TEXT,
            handler=_command,
            billing_metadata=video_command_billing_metadata,
            should_handle=_should_handle_video_command,
        )
    ]


__all__ = [
    "VIDEO_DURATION_ERROR_TEXT",
    "VIDEO_HELP_TEXT",
    "VIDEO_RESOLUTION_ERROR_TEXT",
    "VideoCommandArgs",
    "_parse_video_command_args",
    "build_video_command_definitions",
    "video_command_billing_metadata",
]
