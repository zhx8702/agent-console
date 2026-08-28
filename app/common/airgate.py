"""Small client for the AirGate standalone gateway contract.

The gateway intentionally keeps image generation OpenAI-shaped, while video
generation is an asynchronous submit-and-poll API.  Keeping that distinction
here prevents the video plugin from pretending a long-running video task is a
normal chat completion.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from app.common.safe_url import (
    OutboundURLPolicy,
    configure_http_client,
    normalize_origin,
    safe_get,
)
from app.egress.safe_http import safe_http_request

AIRGATE_MAX_JSON_BYTES = 8 * 1024 * 1024
AIRGATE_MAX_VIDEO_BYTES = 256 * 1024 * 1024
AIRGATE_JSON_CONTENT_TYPES = (
    "application/json",
    "application/problem+json",
    "text/plain",
)
AIRGATE_VIDEO_CONTENT_TYPES = (
    "video/",
    "application/octet-stream",
)


class AirgateError(RuntimeError):
    """Raised when AirGate cannot accept or finish a media request."""


@dataclass(frozen=True, slots=True)
class AirgateVideoResponse:
    request_id: str
    status: str
    video_url: str
    payload: dict[str, Any]


class AirgateClient:
    """Async AirGate client with bounded polling and response downloads."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        model: str = "",
        timeout_seconds: float = 600.0,
        poll_interval_seconds: float = 5.0,
        poll_timeout_seconds: float = 1800.0,
        key_header: str = "Authorization",
        key_prefix: str = "Bearer ",
        extra_body: str = "",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "").strip()
        self.timeout_seconds = max(0.1, float(timeout_seconds or 600.0))
        self.poll_interval_seconds = max(0.1, float(poll_interval_seconds or 5.0))
        self.poll_timeout_seconds = max(0.1, float(poll_timeout_seconds or 1800.0))
        self.key_header = str(key_header or "Authorization").strip()
        self.key_prefix = str(key_prefix or "")
        self.extra_body_raw = str(extra_body or "").strip()
        self._client = http_client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def generate_video(
        self,
        *,
        prompt: str,
        duration: int,
        resolution: str,
    ) -> AirgateVideoResponse:
        prompt = str(prompt or "").strip()
        if not prompt:
            raise AirgateError("视频提示词不能为空")
        endpoint = self._video_generation_url()
        payload = self._extra_body()
        payload.update(
            {
                "model": self.model or "grok-imagine-video-1.5-preview",
                "prompt": prompt,
                "duration": int(duration),
                "resolution": str(resolution or "720p").strip(),
            }
        )
        response = await self._request("POST", endpoint, json=payload)
        body = self._decode_json(response, "视频接口")
        request_id = self._find_first_str(
            body,
            ("request_id", "task_id", "generation_id", "id"),
        )
        video_url = self._find_first_str(
            body,
            ("video_url", "download_url", "url"),
        )
        status = self._normalize_status(
            self._find_first_str(body, ("status", "state"))
        )
        if video_url:
            return AirgateVideoResponse(
                request_id=request_id,
                status=status or "completed",
                video_url=self._resolve_media_url(endpoint, video_url),
                payload=body,
            )
        if not request_id:
            raise AirgateError("视频接口未返回 request_id")
        return await self._poll_video(
            request_id=request_id,
            status=status or "pending",
            initial_payload=body,
        )

    async def download_video(
        self,
        video_url: str,
        *,
        destination_dir: Path,
        file_stem: str,
    ) -> tuple[Path, str]:
        source_url = self._resolve_media_url(self._video_generation_url(), video_url)
        client = await self._get_client()
        try:
            response = await safe_get(
                client,
                source_url,
                headers={"Accept": "video/*,application/octet-stream"},
                max_response_bytes=AIRGATE_MAX_VIDEO_BYTES,
                allowed_content_types=AIRGATE_VIDEO_CONTENT_TYPES,
                policy=self._policy(source_url, response_kind="video"),
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AirgateError("下载生成视频失败") from exc
        content_type = str(
            response.headers.get("content-type") or "video/mp4"
        ).split(";", 1)[0].lower()
        if content_type not in AIRGATE_VIDEO_CONTENT_TYPES and not content_type.startswith(
            "video/"
        ):
            raise AirgateError("视频下载地址未返回视频内容")
        if not response.content:
            raise AirgateError("下载到的生成视频为空")
        if len(response.content) > AIRGATE_MAX_VIDEO_BYTES:
            raise AirgateError("生成视频超过大小限制")
        destination_dir.mkdir(parents=True, exist_ok=True)
        suffix = self._video_suffix(content_type, source_url)
        safe_stem = "".join(ch for ch in str(file_stem or "video") if ch.isalnum() or ch in "-_")
        path = (destination_dir / f"{safe_stem}{suffix}").resolve()
        try:
            path.relative_to(destination_dir.resolve())
        except ValueError as exc:  # pragma: no cover - defensive path guard
            raise AirgateError("视频保存路径无效") from exc
        path.write_bytes(response.content)
        return path, content_type

    async def _poll_video(
        self,
        *,
        request_id: str,
        status: str,
        initial_payload: dict[str, Any],
    ) -> AirgateVideoResponse:
        endpoint = self._video_status_url(request_id)
        deadline = time.monotonic() + self.poll_timeout_seconds
        current_status = status
        payload = initial_payload
        while True:
            if current_status in {"failed", "error", "cancelled", "canceled"}:
                message = self._find_first_str(payload, ("message", "error", "detail"))
                raise AirgateError(message or "视频生成失败")
            video_url = self._find_first_str(
                payload,
                ("video_url", "download_url", "url"),
            )
            if video_url:
                return AirgateVideoResponse(
                    request_id=request_id,
                    status=current_status or "completed",
                    video_url=self._resolve_media_url(endpoint, video_url),
                    payload=payload,
                )
            if time.monotonic() >= deadline:
                raise AirgateError("视频生成轮询超时")
            await asyncio.sleep(min(self.poll_interval_seconds, max(0.1, deadline - time.monotonic())))
            response = await self._request("GET", endpoint)
            payload = self._decode_json(response, "视频状态接口")
            current_status = self._normalize_status(
                self._find_first_str(payload, ("status", "state"))
            ) or current_status

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json: Any | None = None,
        response_kind: str = "json",
    ) -> httpx.Response:
        client = await self._get_client()
        headers = self._headers()
        try:
            response = await safe_http_request(
                client,
                method,  # type: ignore[arg-type]
                url,
                headers=headers,
                json=json,
                policy=self._policy(url, response_kind=response_kind),
            )
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as exc:
            raise AirgateError(f"AirGate 接口返回 {exc.response.status_code}") from exc
        except httpx.TimeoutException as exc:
            raise AirgateError("AirGate 请求超时") from exc
        except httpx.HTTPError as exc:
            raise AirgateError("AirGate 请求失败") from exc

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            )
        origin = normalize_origin(self.base_url)
        configure_http_client(
            self._client,
            allowed_private_origins=[self.base_url],
            origin_headers={origin: self._headers()} if origin else None,
        )
        return self._client

    def _policy(self, url: str, *, response_kind: str = "json") -> OutboundURLPolicy:
        parsed = urlparse(url)
        hostname = str(parsed.hostname or "").strip().lower().rstrip(".")
        origin = normalize_origin(self.base_url)
        return OutboundURLPolicy(
            allowed_hosts=frozenset({hostname}) if hostname else frozenset(),
            allowed_private_origins=frozenset({origin}) if origin else frozenset(),
            max_redirects=0,
            max_response_bytes=(
                AIRGATE_MAX_VIDEO_BYTES
                if response_kind == "video"
                else AIRGATE_MAX_JSON_BYTES
            ),
            timeout_seconds=self.timeout_seconds,
            allowed_response_content_types=(
                AIRGATE_VIDEO_CONTENT_TYPES
                if response_kind == "video"
                else AIRGATE_JSON_CONTENT_TYPES
            ),
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json, video/*"}
        if self.api_key and self.key_header:
            prefix = self.key_prefix
            if prefix.casefold() == "bearer":
                prefix = "Bearer "
            headers[self.key_header] = (
                f"{prefix}{self.api_key}" if prefix else self.api_key
            )
        return headers

    def _extra_body(self) -> dict[str, Any]:
        if not self.extra_body_raw:
            return {}
        try:
            value = json.loads(self.extra_body_raw)
        except json.JSONDecodeError as exc:
            raise AirgateError("VIDEO_API_EXTRA_BODY 不是合法 JSON") from exc
        if not isinstance(value, dict):
            raise AirgateError("VIDEO_API_EXTRA_BODY 必须是 JSON object")
        return dict(value)

    def _video_generation_url(self) -> str:
        raw = self.base_url.rstrip("/")
        lower = raw.lower()
        if lower.endswith("/videos/generations"):
            return raw
        if lower.endswith("/v1"):
            return f"{raw}/videos/generations"
        return f"{raw}/v1/videos/generations"

    def _video_status_url(self, request_id: str) -> str:
        endpoint = self._video_generation_url()
        suffix = "/videos/generations"
        if endpoint.lower().endswith(suffix):
            root = endpoint[: -len(suffix)]
        else:  # pragma: no cover - endpoint normalization above guarantees this
            root = endpoint.rstrip("/")
        return f"{root}/videos/{quote(str(request_id), safe='')}"

    def _decode_json(self, response: httpx.Response, label: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise AirgateError(f"{label}未返回合法 JSON") from exc
        if not isinstance(payload, dict):
            raise AirgateError(f"{label}返回格式无效")
        return payload

    @staticmethod
    def _normalize_status(value: str) -> str:
        return str(value or "").strip().lower().replace("-", "_")

    @staticmethod
    def _find_first_str(payload: Any, field_names: tuple[str, ...]) -> str:
        if isinstance(payload, dict):
            for name in field_names:
                value = payload.get(name)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for value in payload.values():
                found = AirgateClient._find_first_str(value, field_names)
                if found:
                    return found
        elif isinstance(payload, list):
            for value in payload:
                found = AirgateClient._find_first_str(value, field_names)
                if found:
                    return found
        return ""

    @staticmethod
    def _resolve_media_url(base_url: str, media_url: str) -> str:
        value = str(media_url or "").strip()
        if value.startswith(("http://", "https://")):
            return value
        return str(httpx.URL(base_url).join(value))

    @staticmethod
    def _video_suffix(content_type: str, source_url: str) -> str:
        normalized = str(content_type or "").lower()
        if "webm" in normalized or source_url.lower().split("?", 1)[0].endswith(".webm"):
            return ".webm"
        if "mov" in normalized or source_url.lower().split("?", 1)[0].endswith(".mov"):
            return ".mov"
        return ".mp4"
