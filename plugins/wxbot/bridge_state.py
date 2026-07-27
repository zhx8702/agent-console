"""Typed state and cross-capability contract shared by bridge mixins."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.social import SocialParticipationService
from app.social.store import SocialPolicyStore
from plugins.wxbot.store import WxbotStore


class WxbotBridgeState:
    """Declare state once without imposing runtime behavior on the mixins."""

    _sdk_url: str
    _tenant_id: str
    _connection_id: str
    _container: Any
    _settings: Any
    _sdk_headers: dict[str, str]
    _store: WxbotStore
    _redis: Any
    _poll_interval: float
    _send_interval: float
    _leader_retry_interval: float
    _participation_service: SocialParticipationService
    _social_policy_store: SocialPolicyStore | None
    _owners_scope_execution_allowed: Any
    _connection_activity_recorder: Callable[[str], Awaitable[Any]] | None
    _tasks: list[asyncio.Task[Any]]
    _stop: asyncio.Event
    _client: httpx.AsyncClient | None
    _sdk_online: bool
    _sdk_auth_state: str
    _sdk_auth_reason: str
    _ingest_mode: str
    _event_mode: str
    _stream_mode: str
    _leader_token: str
    _leader_refresh_task: asyncio.Task[Any] | None
    _leader_supervisor_task: asyncio.Task[Any] | None
    _status_publish_task: asyncio.Task[Any] | None
    _is_leader: bool
    _standby_logged: bool
    _cursor_reset_generation: int
    _pending_media_messages: dict[str, dict[str, Any]]
    _pending_media_resolve_offset: int
    _instance_id: str
    _reply_claim_owner: str
    _process_role: str
    _host: str
    _pid: int
    _started_at: str
    _last_self_heal_at: float
    _last_self_heal_by_reason: dict[str, float]
    _health_recurrences: dict[str, int]
    _last_cursor_observation: dict[str, int]
    _cursor_stall_count: int
    _diagnostics: dict[str, Any]

    async def _request_sdk(self, *args: Any, **kwargs: Any) -> httpx.Response:
        raise NotImplementedError

    async def _record_connection_activity(self, direction: str) -> None:
        raise NotImplementedError

    def _stream_sdk(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    @property
    def is_running(self) -> bool:
        raise NotImplementedError

    @property
    def cursor_key(self) -> str:
        raise NotImplementedError

    @property
    def legacy_cursor_key(self) -> str:
        raise NotImplementedError

    @property
    def event_cursor_key(self) -> str:
        raise NotImplementedError

    async def sdk_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        request_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def _leader_present(self) -> bool:
        raise NotImplementedError

    async def _restore_leader_key(self) -> bool:
        raise NotImplementedError

    async def _cancel_bridge_tasks(self) -> None:
        raise NotImplementedError

    async def _activate_leader(self) -> None:
        raise NotImplementedError

    def _leader_claimed(self) -> bool:
        raise NotImplementedError

    def _task_snapshot(self) -> dict[str, Any]:
        raise NotImplementedError

    def _ensure_bridge_tasks(self) -> list[str]:
        raise NotImplementedError

    async def _check_leader_health(self) -> bool:
        raise NotImplementedError

    async def _check_task_health(self) -> bool:
        raise NotImplementedError

    async def _status_watchdog(self) -> None:
        raise NotImplementedError

    async def _ingest_loop(self) -> None:
        raise NotImplementedError

    async def _event_loop(self) -> None:
        raise NotImplementedError

    async def _send_loop(self) -> None:
        raise NotImplementedError

    async def _pending_media_resolver_loop(self) -> None:
        raise NotImplementedError

    async def _cursor_reconcile_loop(self) -> None:
        raise NotImplementedError

    async def _wait_for_bus(self) -> Any:
        raise NotImplementedError

    async def _get_cursor(self) -> int:
        raise NotImplementedError

    async def _set_cursor(self, cursor: int) -> None:
        raise NotImplementedError

    async def _get_legacy_cursor(self) -> int:
        raise NotImplementedError

    async def _set_legacy_cursor(self, cursor: int) -> None:
        raise NotImplementedError

    async def _get_event_cursor(self) -> int:
        raise NotImplementedError

    async def _set_event_cursor(self, cursor: int) -> None:
        raise NotImplementedError

    async def _reconcile_ingest_cursor(
        self, cursor: int, bounds: dict[str, int] | None = None
    ) -> int:
        raise NotImplementedError

    async def _reconcile_legacy_ingest_cursor(
        self, cursor: int, bounds: dict[str, int] | None = None
    ) -> int:
        raise NotImplementedError

    async def _reconcile_event_cursor(
        self, cursor: int, bounds: dict[str, int] | None = None
    ) -> int:
        raise NotImplementedError

    async def _mark_inbound_seen(self, message_id: str) -> bool:
        raise NotImplementedError

    async def _release_inbound_seen(self, message_id: str) -> None:
        raise NotImplementedError

    @staticmethod
    def _parse_sdk_timestamp(*values: Any) -> Any:
        raise NotImplementedError

    def _is_stale_inbound_message(self, occurred_at: Any) -> bool:
        raise NotImplementedError

    def _max_inbound_message_age_seconds(self) -> int:
        raise NotImplementedError

    @staticmethod
    def _capture_allowed(payload: dict[str, Any]) -> bool:
        raise NotImplementedError

    @staticmethod
    def _record(value: Any) -> dict[str, Any]:
        raise NotImplementedError

    @staticmethod
    def _image_url(sdk_url: str, image_path: str) -> str:
        raise NotImplementedError

    def _resolve_media_variant_path(
        self,
        message: dict[str, Any],
        media: dict[str, Any],
        *,
        variant: str,
    ) -> str:
        raise NotImplementedError

    def _resolve_media_variant_url(
        self,
        message: dict[str, Any],
        media: dict[str, Any],
        *,
        variant: str,
    ) -> str:
        raise NotImplementedError

    def _apply_quote_metadata(self, metadata: dict[str, Any], quote_payload: Any) -> None:
        raise NotImplementedError

    @staticmethod
    def _apply_image_observation_metadata(metadata: dict[str, Any], msg_type: str) -> None:
        raise NotImplementedError

    async def _handle_stream_event(self, envelope: dict[str, Any], stream: str, bus: Any) -> None:
        raise NotImplementedError

    async def _record_interactive_inbound(
        self,
        *,
        session_id: str,
        message_id: str,
        content: str,
        mentioned_me: bool,
        is_self_sent: bool,
    ) -> None:
        raise NotImplementedError

    async def _record_group_observation(self, event: Any) -> None:
        raise NotImplementedError

    async def _record_stream_member_event(self, envelope: dict[str, Any]) -> None:
        raise NotImplementedError

    @staticmethod
    def _to_int(value: Any) -> int | None:
        raise NotImplementedError
