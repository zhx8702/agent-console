"""HTTP client for the host local-agent sidecar."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.common.safe_url import UnsafeOutboundURLError
from app.egress.safe_http import (
    safe_http_request,
    trusted_service_policy,
    trusted_service_url,
)
from plugins.local_agent.sidecar.backends import BACKENDS

_ALLOWED_CONTENT_TYPES = (
    "application/json",
    "application/problem+json",
    "text/plain",
)


class LocalAgentClientError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class SidecarTask:
    task_id: str
    status: str
    backend: str = ""
    result_text: str = ""
    error: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> SidecarTask:
        return cls(
            task_id=str(payload.get("id") or payload.get("task_id") or ""),
            status=str(payload.get("status") or ""),
            backend=str(payload.get("backend") or ""),
            result_text=str(payload.get("result_text") or ""),
            error=str(payload.get("error") or ""),
        )


class LocalAgentClient:
    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._base_url = str(getattr(settings, "local_agent_base_url", "") or "").strip()
        self._token = str(getattr(settings, "local_agent_token", "") or "").strip()
        timeout = float(getattr(settings, "local_agent_probe_timeout_seconds", 5.0) or 5.0)
        self._timeout = max(0.5, timeout)
        self._policy = None
        if self._base_url:
            try:
                self._policy = trusted_service_policy(
                    self._base_url,
                    timeout_seconds=self._timeout,
                    max_response_bytes=2_000_000,
                    allowed_response_content_types=_ALLOWED_CONTENT_TYPES,
                )
            except UnsafeOutboundURLError as exc:
                raise LocalAgentClientError("invalid_base_url", str(exc)) from exc

    @property
    def configured(self) -> bool:
        return bool(self._base_url and self._policy is not None)

    def _headers(self) -> dict[str, str]:
        if not self._token:
            return {}
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.configured or self._policy is None:
            raise LocalAgentClientError("not_configured")
        url = trusted_service_url(self._base_url, path)
        async with httpx.AsyncClient() as client:
            response = await safe_http_request(
                client,
                "GET" if method == "GET" else "POST",
                url,
                headers=self._headers(),
                json=json,
                policy=self._policy,
            )
        if response.status_code >= 400:
            raise LocalAgentClientError(
                "sidecar_http_error",
                f"{response.status_code}: {response.text[:300]}",
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise LocalAgentClientError("invalid_sidecar_payload")
        return payload

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/health")

    async def backends(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/backends")

    async def create_task(
        self,
        *,
        backend: str,
        prompt: str,
        timeout_seconds: float | None = None,
        cwd: str = "",
        max_turns: int | None = None,
    ) -> SidecarTask:
        name = str(backend or "").strip().lower()
        if name not in BACKENDS:
            raise LocalAgentClientError("unknown_backend")
        payload: dict[str, Any] = {"backend": name, "prompt": prompt}
        if cwd:
            payload["cwd"] = cwd
        if timeout_seconds is not None:
            payload["timeout_seconds"] = float(timeout_seconds)
        if max_turns is not None:
            payload["max_turns"] = int(max_turns)
        body = await self._request("POST", "/v1/tasks", json=payload)
        task = SidecarTask.from_payload(body)
        if not task.task_id:
            raise LocalAgentClientError("invalid_sidecar_payload", "missing task id")
        return task

    async def get_task(self, task_id: str) -> SidecarTask:
        body = await self._request("GET", f"/v1/tasks/{task_id}")
        return SidecarTask.from_payload(body)


def sidecar_origin(settings: Any) -> str:
    raw = str(getattr(settings, "local_agent_base_url", "") or "").strip()
    parsed = urlsplit(raw)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return raw
