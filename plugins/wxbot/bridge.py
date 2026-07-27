"""Background bridge between cs-system and the wx-bot SDK HTTP API.

The facade preserves the historical ``SdkBridge`` API while lifecycle,
ingestion/media handling, and outbound delivery are implemented by focused
mixins with one-way dependencies on the shared bridge contract.
"""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.common.ids import new_trace_id
from app.common.logging import get_logger
from app.common.types import InboundEvent
from app.common.wxbot_auth import wxbot_sdk_headers
from app.egress.safe_http import safe_trusted_service_request, safe_trusted_service_stream
from app.social import SocialParticipationService
from app.social.store import SocialPolicyStore
from plugins.wxbot.bridge_contract import (
    _SDK_JSON_CONTENT_TYPES,
    _SDK_MAX_JSON_BYTES,
    CURSOR_LAG_THRESHOLD,
    CURSOR_RECONCILE_INTERVAL_SECONDS,
    CURSOR_REDIS_KEY_PREFIX,
    CURSOR_STALL_CHECKS,
    EVENT_CURSOR_REDIS_KEY_PREFIX,
    INBOUND_DEDUPE_KEY_PREFIX,
    LEADER_KEY_PREFIX,
    LEADER_RETRY_SECONDS,
    LEADER_TTL_SECONDS,
    LEGACY_CURSOR_REDIS_KEY_PREFIX,
    MEDIA_READY_EVENT_TYPE,
    MEMBER_EVENT_TYPES,
    REPLY_CLAIM_LEASE_SECONDS,
    REPLY_DRAIN_LIMIT,
    REPLY_MAX_ATTEMPTS,
    SELF_HEAL_COOLDOWN_SECONDS,
    SELF_HEAL_RECURRENCE_THRESHOLD,
    STATUS_KEY_PREFIX,
    STATUS_PUBLISH_INTERVAL_SECONDS,
    STATUS_TTL_SECONDS,
    _partition_key,
    _utcnow_iso,
)
from plugins.wxbot.bridge_delivery import WxbotBridgeDeliveryMixin
from plugins.wxbot.bridge_health import WxbotBridgeHealthMixin
from plugins.wxbot.bridge_media import WxbotBridgeMediaMixin
from plugins.wxbot.bridge_runtime import WxbotBridgeRuntimeMixin, read_bridge_runtime_status
from plugins.wxbot.bridge_stream import WxbotBridgeStreamMixin
from plugins.wxbot.store import WxbotStore

log = get_logger(__name__)

__all__ = [
    "CURSOR_LAG_THRESHOLD",
    "CURSOR_RECONCILE_INTERVAL_SECONDS",
    "CURSOR_REDIS_KEY_PREFIX",
    "CURSOR_STALL_CHECKS",
    "EVENT_CURSOR_REDIS_KEY_PREFIX",
    "INBOUND_DEDUPE_KEY_PREFIX",
    "LEADER_KEY_PREFIX",
    "LEADER_RETRY_SECONDS",
    "LEADER_TTL_SECONDS",
    "LEGACY_CURSOR_REDIS_KEY_PREFIX",
    "MEDIA_READY_EVENT_TYPE",
    "MEMBER_EVENT_TYPES",
    "REPLY_CLAIM_LEASE_SECONDS",
    "REPLY_DRAIN_LIMIT",
    "REPLY_MAX_ATTEMPTS",
    "SELF_HEAL_COOLDOWN_SECONDS",
    "SELF_HEAL_RECURRENCE_THRESHOLD",
    "STATUS_KEY_PREFIX",
    "STATUS_PUBLISH_INTERVAL_SECONDS",
    "STATUS_TTL_SECONDS",
    "SdkBridge",
    "WxbotSdkResponseError",
    "_partition_key",
    "read_bridge_runtime_status",
]


class WxbotSdkResponseError(RuntimeError):
    """Preserve an SDK HTTP failure so the admin facade can classify it."""

    def __init__(self, status_code: int, error_code: str = "wxbot_sdk_error") -> None:
        super().__init__(f"wxbot sdk returned HTTP {status_code}: {error_code}")
        self.status_code = int(status_code)
        self.error_code = str(error_code or "wxbot_sdk_error")[:96]


class SdkBridge(
    WxbotBridgeRuntimeMixin,
    WxbotBridgeHealthMixin,
    WxbotBridgeStreamMixin,
    WxbotBridgeMediaMixin,
    WxbotBridgeDeliveryMixin,
):
    def __init__(
        self,
        sdk_url: str,
        tenant_id: str,
        container: Any,
        settings: Any,
        store: WxbotStore,
        redis: Any,
        *,
        poll_interval: float = 3.0,
        send_interval: float = 2.0,
        leader_retry_interval: float = LEADER_RETRY_SECONDS,
        participation_service: SocialParticipationService | None = None,
        social_policy_store: SocialPolicyStore | None = None,
        connection_id: str = "",
        sdk_headers: dict[str, str] | None = None,
        owners_scope_execution_allowed: (
            Callable[[dict[str, str], str, str], Awaitable[bool]] | None
        ) = None,
        connection_activity_recorder: Callable[[str], Awaitable[Any]] | None = None,
    ) -> None:
        self._sdk_url = sdk_url.rstrip("/")
        self._tenant_id = tenant_id
        self._connection_id = str(connection_id or "").strip()
        self._container = container
        self._settings = settings
        self._sdk_headers = (
            dict(sdk_headers) if sdk_headers is not None else wxbot_sdk_headers(settings)
        )
        self._store = store
        self._redis = redis
        self._poll_interval = poll_interval
        self._send_interval = send_interval
        self._leader_retry_interval = max(0.1, leader_retry_interval)
        self._participation_service = participation_service or SocialParticipationService()
        self._social_policy_store = social_policy_store
        self._owners_scope_execution_allowed = owners_scope_execution_allowed
        self._connection_activity_recorder = connection_activity_recorder
        self._tasks: list[asyncio.Task[Any]] = []
        self._stop = asyncio.Event()
        self._client: httpx.AsyncClient | None = None
        self._sdk_online = False
        self._sdk_auth_state = "unknown"
        self._sdk_auth_reason = ""
        self._ingest_mode = "starting"
        self._event_mode = "starting"
        self._stream_mode = "unknown"
        self._leader_token = new_trace_id()
        self._leader_refresh_task: asyncio.Task[Any] | None = None
        self._leader_supervisor_task: asyncio.Task[Any] | None = None
        self._status_publish_task: asyncio.Task[Any] | None = None
        self._is_leader = False
        self._standby_logged = False
        self._cursor_reset_generation = 0
        self._pending_media_messages: dict[str, dict[str, Any]] = {}
        self._pending_media_resolve_offset = 0
        self._instance_id = str(
            getattr(settings, "resolved_worker_instance_id", None)
            or f"{socket.gethostname()}-{os.getpid()}"
        )
        self._reply_claim_owner = f"{self._instance_id}:{self._leader_token}"[:128]
        self._process_role = str(
            getattr(settings, "app_process_role", "wxbot_bridge") or "wxbot_bridge"
        )
        self._host = socket.gethostname()
        self._pid = os.getpid()
        self._started_at = _utcnow_iso()
        self._last_self_heal_at = 0.0
        self._last_self_heal_by_reason: dict[str, float] = {}
        self._health_recurrences: dict[str, int] = {}
        self._last_cursor_observation: dict[str, int] = {}
        self._cursor_stall_count = 0
        self._diagnostics: dict[str, Any] = {}

    async def _record_connection_activity(self, direction: str) -> None:
        recorder = self._connection_activity_recorder
        if recorder is None:
            return
        try:
            await recorder(direction)
        except Exception as exc:
            # Telemetry must never turn an accepted inbound message or a final
            # delivery acknowledgement into a retryable transport failure.
            log.warning(
                "wxbot.bridge.connection_activity_record_failed",
                tenant_id=self._tenant_id,
                connection_id=self._connection_id,
                direction=direction,
                error_class=exc.__class__.__name__,
            )

    async def _record_group_member_seen(self, event: InboundEvent) -> None:
        """Maintain the authorization projection without weakening ingestion durability."""

        if not str(event.session_id or "").endswith("@chatroom") or bool(
            event.metadata.get("is_self_sent")
        ):
            return
        recorder = getattr(self._store, "record_group_member_seen", None)
        if not callable(recorder):
            return
        try:
            await recorder(
                tenant_id=self._tenant_id,
                session_id=event.session_id,
                user_wxid=event.user_id,
                user_name=str(event.metadata.get("sender_name") or ""),
            )
        except Exception as exc:
            # Membership is an authorization projection, not the durable
            # inbound contract. A projection outage must not drop the message.
            log.warning(
                "wxbot.bridge.group_membership_update_failed",
                session_id=event.session_id,
                user_wxid=event.user_id,
                error=str(exc),
            )

    async def _record_group_observation(self, event: InboundEvent) -> None:
        """Extend the split stream mixin with the membership projection update."""

        await super()._record_group_observation(event)
        await self._record_group_member_seen(event)

    async def _request_sdk(self, *args: Any, **kwargs: Any) -> httpx.Response:
        return await safe_trusted_service_request(*args, **kwargs)

    def _stream_sdk(self, *args: Any, **kwargs: Any) -> Any:
        return safe_trusted_service_stream(*args, **kwargs)

    async def sdk_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        request_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=10,
                trust_env=False,
            )
        normalized_method = str(method or "").upper()
        if normalized_method not in {"GET", "POST"}:
            raise ValueError("unsupported wxbot sdk method")
        try:
            resp = await safe_trusted_service_request(
                self._client,
                normalized_method,
                self._sdk_url,
                path,
                params=params,
                json=json_body,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    **self._sdk_headers,
                    **(request_headers or {}),
                },
                timeout_seconds=10.0,
                max_response_bytes=_SDK_MAX_JSON_BYTES,
                allowed_response_content_types=_SDK_JSON_CONTENT_TYPES,
            )
        except httpx.ConnectError:
            self._sdk_online = False
            raise
        if resp.status_code >= 400:
            error_code = "wxbot_sdk_error"
            try:
                error_payload = resp.json()
                if isinstance(error_payload, dict):
                    error_code = str(error_payload.get("error") or error_code)
            except (TypeError, ValueError):
                pass
            raise WxbotSdkResponseError(resp.status_code, error_code)
        self._sdk_online = True
        data = resp.json()
        if isinstance(data, dict) and path == "/status":
            auth_active = data.get("auth_active")
            if isinstance(auth_active, bool):
                self._sdk_auth_state = "ok" if auth_active else "inactive"
                if auth_active:
                    self._sdk_auth_reason = ""
        if isinstance(data, dict):
            return data
        return {"data": data}
