from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.common.airgate import AirgateClient, AirgateError

VIDEO_DEFAULT_MODEL = "grok-imagine-video-1.5-preview"
VIDEO_DEFAULT_DURATION = 6
VIDEO_DEFAULT_RESOLUTION = "720p"
VIDEO_RESOLUTIONS = ("480p", "720p", "1080p")
VIDEO_MAX_DURATION = 15


class VideoError(RuntimeError):
    """Base error for the video generation plugin."""


class VideoConfigError(VideoError):
    """Raised when the video provider or storage is not configured."""


class VideoApiError(VideoError):
    """Raised when the video provider rejects or cannot finish a task."""


@dataclass(frozen=True, slots=True)
class VideoResult:
    video_id: str
    prompt: str
    local_path: str
    media_type: str
    source_url: str = ""


class VideoStore:
    """AirGate video submit/poll/download facade used by the Agent tool."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._storage_dir = self._resolve_storage_dir(
            str(
                getattr(
                    settings,
                    "video_storage_dir",
                    "/mnt/c/Users/Public/agent-console-video",
                )
                or ""
            )
        )
        self._outbound_dir = self._resolve_storage_dir(
            str(
                getattr(
                    settings,
                    "wxbot_outbound_file_dir",
                    "/data/wxbot-outbound",
                )
                or "/data/wxbot-outbound"
            )
        )
        self._client: AirgateClient | None = None

    async def initialize(self) -> None:
        # Storage is validated immediately before a generation request.  A
        # missing VIDEO_API_URL should not prevent the rest of the bot from
        # starting; the tool returns a precise configuration error instead.
        return None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def generate_video(
        self,
        prompt: str,
        *,
        duration: int = VIDEO_DEFAULT_DURATION,
        resolution: str = VIDEO_DEFAULT_RESOLUTION,
    ) -> VideoResult:
        prompt = str(prompt or "").strip()
        if not prompt:
            raise VideoApiError("视频提示词不能为空")
        try:
            normalized_duration = int(duration)
        except (TypeError, ValueError) as exc:
            raise VideoApiError("duration 必须是整数") from exc
        if not 1 <= normalized_duration <= VIDEO_MAX_DURATION:
            raise VideoApiError(f"duration 必须在 1 到 {VIDEO_MAX_DURATION} 秒之间")
        normalized_resolution = str(resolution or VIDEO_DEFAULT_RESOLUTION).strip().lower()
        if normalized_resolution not in VIDEO_RESOLUTIONS:
            raise VideoApiError("resolution 只能是 480p、720p 或 1080p")
        self._ensure_storage_dir()
        self._ensure_outbound_dir()
        client = self._get_client()
        try:
            generated = await client.generate_video(
                prompt=prompt,
                duration=normalized_duration,
                resolution=normalized_resolution,
            )
            video_id = generated.request_id or f"video_{uuid4().hex}"
            path, media_type = await client.download_video(
                generated.video_url,
                destination_dir=self._storage_dir,
                file_stem=self._file_stem(video_id),
            )
            delivery_path = self._stage_for_file_delivery(path, video_id)
        except AirgateError as exc:
            raise VideoApiError(str(exc)) from exc
        return VideoResult(
            video_id=video_id,
            prompt=prompt,
            local_path=str(delivery_path),
            media_type=media_type,
            source_url=generated.video_url,
        )

    def _get_client(self) -> AirgateClient:
        if self._client is not None:
            return self._client
        api_url = str(getattr(self.settings, "video_api_url", "") or "").strip()
        if not api_url:
            api_url = self._base_url_from_draw_setting(
                str(getattr(self.settings, "draw_api_url", "") or "").strip()
            )
        if not api_url:
            raise VideoConfigError("未配置 VIDEO_API_URL")
        api_key = str(getattr(self.settings, "video_api_key", "") or "").strip()
        if not api_key:
            api_key = str(getattr(self.settings, "draw_api_key", "") or "").strip()
        self._client = AirgateClient(
            base_url=api_url,
            api_key=api_key,
            model=str(
                getattr(self.settings, "video_api_model", VIDEO_DEFAULT_MODEL)
                or VIDEO_DEFAULT_MODEL
            ).strip(),
            timeout_seconds=float(
                getattr(self.settings, "video_api_timeout_seconds", 600.0) or 600.0
            ),
            poll_interval_seconds=float(
                getattr(self.settings, "video_api_poll_interval_seconds", 5.0) or 5.0
            ),
            poll_timeout_seconds=float(
                getattr(self.settings, "video_api_poll_timeout_seconds", 1800.0)
                or 1800.0
            ),
            key_header=str(
                getattr(self.settings, "video_api_key_header", "Authorization")
                or "Authorization"
            ),
            key_prefix=str(getattr(self.settings, "video_api_key_prefix", "Bearer ") or ""),
            extra_body=str(getattr(self.settings, "video_api_extra_body", "") or ""),
        )
        return self._client

    def _ensure_storage_dir(self) -> None:
        try:
            self._storage_dir.mkdir(parents=True, exist_ok=True)
            probe = self._storage_dir / f".write-test-{uuid4().hex}"
            probe.write_text("", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except OSError as exc:
            raise VideoConfigError(
                "VIDEO_STORAGE_DIR 不可写，请检查视频缓存目录配置"
            ) from exc

    def _ensure_outbound_dir(self) -> None:
        try:
            self._outbound_dir.mkdir(parents=True, exist_ok=True)
            if self._outbound_dir.is_symlink() or not self._outbound_dir.is_dir():
                raise OSError("outbound directory is not a regular directory")
        except OSError as exc:
            raise VideoConfigError(
                "WXBOT_OUTBOUND_FILE_DIR 不可写，视频需要通过 SDK 文件发送"
            ) from exc

    def _stage_for_file_delivery(self, source: Path, video_id: str) -> Path:
        try:
            max_bytes = int(
                getattr(self.settings, "wxbot_outbound_file_max_bytes", 100 * 1024 * 1024)
                or 100 * 1024 * 1024
            )
            size = source.stat().st_size
            if size > max_bytes:
                raise VideoConfigError(
                    f"视频文件超过文件发送大小限制（{max_bytes} 字节）"
                )
            destination = self._outbound_dir / f"{self._file_stem(video_id)}{source.suffix}"
            temp = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
            shutil.copy2(source, temp)
            os.replace(temp, destination)
            return destination
        except VideoConfigError:
            raise
        except OSError as exc:
            raise VideoConfigError(
                "视频已生成，但无法复制到 SDK 文件发送目录"
            ) from exc

    def _resolve_storage_dir(self, raw_path: str) -> Path:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = Path(getattr(self.settings, "project_root", Path.cwd())) / candidate
        return candidate.resolve()

    @staticmethod
    def _base_url_from_draw_setting(value: str) -> str:
        raw = str(value or "").rstrip("/")
        lower = raw.lower()
        for suffix in ("/images/generations", "/image/generations"):
            if lower.endswith(suffix):
                return raw[: -len(suffix)]
        return raw

    @staticmethod
    def _file_stem(video_id: str) -> str:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        safe_id = "".join(ch for ch in str(video_id or "") if ch.isalnum() or ch in "-_")
        return f"vid_{timestamp}_{safe_id[-24:] or uuid4().hex[:8]}"
