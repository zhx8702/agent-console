"""
WeChat bot channel adapter plugin.

Bridges cs-system with wx-bot SDK running on Windows alongside WeChat.
The SDK exposes a local HTTP API; this plugin polls it for inbound
messages and pushes replies back via the same API.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from app.channel.adapters import (
    WECHAT_SDK_DESCRIPTOR,
    ChannelAdapterCatalog,
    ChannelAdapterRegistration,
    ChannelProbeResult,
)
from app.channel.connections import ChannelConnectionStore
from app.channel.identity import (
    canonical_conversation_id,
    canonical_message_id,
    canonical_participant_id,
)
from app.common.logging import get_logger
from app.common.types import Channel, InboundEvent
from app.infra.db import get_session_factory
from app.infra.redis_client import get_redis
from app.orchestrator.effect_handlers import (
    ChannelReplyEffectHandler,
    WxbotReplyEffectHandler,
)
from app.orchestrator.flow import FlowStepDefinition
from app.plugin.base import Plugin, PluginContext, PluginMeta
from app.social.feedback import NaturalFeedbackService
from app.social.store import SocialPolicyStore
from plugins.memory.store import MemoryStore
from plugins.wxbot.agent_tools import (
    WxbotAgentToolService,
    build_wxbot_group_agent_tools,
    build_wxbot_group_plugin_status_agent_tools,
)
from plugins.wxbot.bridge_runtime import read_bridge_runtime_status
from plugins.wxbot.channel import WxbotChannelOutbound
from plugins.wxbot.commands import build_wxbot_command_definitions
from plugins.wxbot.effects import WxbotSdkTriggerConfigEffectHandler
from plugins.wxbot.group_context import (
    WxbotGroupContextHook,
    WxbotGroupContextLoadStep,
    WxbotGroupSummaryService,
)
from plugins.wxbot.hooks import (
    WxbotAgentIntentHook,
    WxbotAgentScopeEnrichStep,
    WxbotInboundNormalizeHook,
    WxbotNormalizeEventStep,
    WxbotOutboundPolicyStep,
    WxbotReplyPolicyHook,
    WxbotReplyPolicyStep,
    WxbotReplyQueueHook,
    WxbotUserBanGateStep,
    WxbotUserBanPreCommandStep,
    WxbotVoiceProfileEnrichStep,
    WxbotVoiceProfileHook,
)
from plugins.wxbot.reports import (
    WxbotReportService,
    resolve_due_period,
    seconds_to_next_subscription_fire,
    should_defer_report_job_retry,
)
from plugins.wxbot.router import _client_safe_media_payload, build_wxbot_router
from plugins.wxbot.self_review import (
    WxbotSelfReviewService,
    resolve_self_review_due_period,
    seconds_to_next_self_review_fire,
)
from plugins.wxbot.store import WxbotStore, normalize_wxbot_event_connection_id

logger = get_logger(__name__)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _read_string(record: dict[str, Any], key: str) -> str:
    value = record.get(key)
    return value.strip() if isinstance(value, str) else ""


def _parse_time_ms(value: Any) -> int | None:
    if isinstance(value, int | float):
        return int(value * 1000)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


class WxbotAdminMediaEventProvider:
    name = "wxbot"

    def __init__(self, store: WxbotStore) -> None:
        self._store = store

    async def list_recent_media_events(
        self,
        *,
        tenant_id: str,
        limit: int = 50,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        connection_id = normalize_wxbot_event_connection_id(
            str(getattr(self._store.settings, "channel_connection_id", "") or "")
        )
        rows = await self._store.list_media_ready_events(
            tenant_id,
            limit=limit,
            connection_id=connection_id,
        )
        if session_id:
            rows = [
                row
                for row in rows
                if session_id
                in {
                    str(row.get("session_id") or ""),
                    (
                        canonical_conversation_id(
                            str(row.get("connection_id") or connection_id),
                            str(row.get("session_id") or ""),
                        )
                        if str(row.get("session_id") or "")
                        else ""
                    ),
                }
            ]
        return [
            self.project_recent_message(self._to_admin_media_event(row, tenant_id), tenant_id)
            for row in rows[:limit]
        ]

    def project_recent_message(
        self,
        item: dict[str, Any],
        tenant_id: str,
    ) -> dict[str, Any]:
        """Replace SDK media locators with tenant-scoped signed media IDs."""

        return _client_safe_media_payload(item, self._store, tenant_id=tenant_id)

    def _to_admin_media_event(self, row: dict[str, Any], tenant_id: str) -> dict[str, Any]:
        event_payload = _as_dict(row.get("payload"))
        event_message = _as_dict(event_payload.get("message"))
        event_media = _as_dict(event_payload.get("media"))
        event_raw = _as_dict(event_payload.get("raw"))
        external_message_id = str(
            row.get("message_id")
            or _read_string(event_message, "id")
            or row.get("sdk_event_id")
            or ""
        )
        connection_id = normalize_wxbot_event_connection_id(
            str(
                row.get("connection_id")
                or getattr(self._store.settings, "channel_connection_id", "")
                or ""
            )
        )
        external_session_id = str(row.get("session_id") or "")
        external_user_id = str(row.get("sender_wxid") or "")
        message_id = canonical_message_id(connection_id, external_message_id)
        session_id = (
            canonical_conversation_id(connection_id, external_session_id)
            if external_session_id
            else ""
        )
        user_id = (
            canonical_participant_id(connection_id, external_user_id)
            if external_user_id
            else ""
        )
        media_url = str(row.get("media_url") or "") or _read_string(event_media, "image_url") or _read_string(event_message, "image_url")
        media_path = str(row.get("media_path") or "") or _read_string(event_media, "image_path") or _read_string(event_message, "image_path")
        media_type = str(row.get("media_type") or "") or "image"
        msg_type = str(row.get("msg_type") or "") or media_type
        event_id = str(row.get("stream_event_id") or row.get("sdk_event_id") or message_id or "unknown")
        created_ts_ms = _parse_time_ms(row.get("created_ts")) or _parse_time_ms(row.get("received_at"))
        if created_ts_ms is None:
            created_raw = row.get("created_ts")
            try:
                created_ts_ms = int(created_raw) * 1000
            except (TypeError, ValueError):
                created_ts_ms = None
        variants = _as_dict(event_media.get("variants")) or _as_dict(event_message.get("image_variants"))
        payload = {
            "admin_event_source": "media_event",
            "media_event_owner": "wxbot",
            "message_id": message_id,
            "external_message_id": external_message_id,
            "connection_id": connection_id,
            "tenant_id": tenant_id,
            "channel": "wechat",
            "user_id": user_id,
            "external_participant_id": external_user_id,
            "session_id": session_id,
            "external_conversation_id": external_session_id,
            "message": {
                "type": msg_type,
                "content": _read_string(event_message, "text") or "[图片]",
                "attachments": [
                    {
                        "type": media_type,
                        "image_url": media_url,
                        "image_path": media_path,
                        "image_variants": variants,
                        "variants": variants,
                    }
                ],
            },
            "received_at": row.get("received_at") or "",
            "metadata": {
                "source": "wxbot",
                "sdk_source": "wxbot-sdk",
                "sdk_event_id": row.get("sdk_event_id"),
                "sdk_event_type": row.get("event_type") or "message.media.ready",
                "connection_id": connection_id,
                "external_message_id": external_message_id,
                "external_conversation_id": external_session_id,
                "external_participant_id": external_user_id,
                "session_name": row.get("session_name") or "",
                "sender_wxid": row.get("sender_wxid") or "",
                "sender_name": row.get("sender_name") or "",
                "msg_svr_id": external_message_id,
                "session_kind": "group" if external_session_id.endswith("@chatroom") else "private",
                "media_status": _read_string(event_media, "status") or "ready",
                "media": {
                    **event_media,
                    "image_url": media_url,
                    "image_path": media_path,
                },
                "raw": {
                    **event_raw,
                    "image_variants": _as_dict(event_raw.get("image_variants")) or variants,
                },
                "image_observation": {
                    "current_image_found": True,
                    "quote_image_found": False,
                    "attachment_count": 1,
                    "quote_attachment_count": 0,
                    "media_status": _read_string(event_media, "status") or "ready",
                    "failure_reason": "",
                    "skip_reason": "",
                },
            },
            "media": {
                **event_media,
                "image_url": media_url,
                "image_path": media_path,
            },
            "raw": event_raw,
            "media_ready_event": row,
        }
        return {
            "id": f"media:wxbot:{event_id}",
            "source": "media_event",
            "owner": "wxbot",
            "stream_key": "media_events",
            "stream": "admin:media_events",
            "tenant_id": tenant_id,
            "session_id": session_id,
            "user_id": user_id,
            "trace_id": None,
            "channel": "wechat",
            "attempts": 0,
            "reason": row.get("event_type") or "message.media.ready",
            "origin_stream": None,
            "origin_id": event_id,
            "created_ts_ms": created_ts_ms,
            "headers": {
                "source": "media_event",
                "owner": "wxbot",
                "stream_event_id": event_id,
            },
            "payload": payload,
        }


class _OwnerGatedMemoryFeedbackStore(MemoryStore):
    """Memory feedback store with a fresh wxbot+memory snapshot boundary.

    Corrections perform candidate reads and then invalidate a selected item.
    The base store calls the explicit pre-mutation hook inside the same
    member-scoped transaction, immediately before its row-locked UPDATE.  The
    inherited ``forget_member`` intentionally stays ungated because a member
    privacy deletion is a compensating operation that must remain available
    while ordinary memory execution is disabled.
    """

    def __init__(
        self,
        settings: Any,
        *,
        llm_service: Any = None,
        vector_store: Any = None,
        owners_scope_execution_allowed: Callable[
            [tuple[str, ...], str, str],
            Awaitable[bool],
        ],
    ) -> None:
        super().__init__(
            settings,
            llm_service=llm_service,
            vector_store=vector_store,
        )
        self._owners_scope_execution_allowed = owners_scope_execution_allowed

    async def _require_memory_scope(
        self,
        tenant_id: str,
        session_id: str,
    ) -> None:
        if (
            await self._owners_scope_execution_allowed(
                ("wxbot", "memory"),
                tenant_id=str(tenant_id or "").strip(),
                session_id=str(session_id or "").strip(),
            )
            is not True
        ):
            raise RuntimeError("memory_plugin_runtime_disabled")

    async def resolve_member_fact_correction(
        self,
        *,
        tenant_id: str,
        session_id: str,
        user_id: str,
        correction_text: str,
        idempotency_key: str,
    ) -> Any:
        await self._require_memory_scope(tenant_id, session_id)
        return await super().resolve_member_fact_correction(
            tenant_id=tenant_id,
            session_id=session_id,
            user_id=user_id,
            correction_text=correction_text,
            idempotency_key=idempotency_key,
        )

    async def _before_member_fact_correction_mutation(
        self,
        *,
        tenant_id: str,
        session_id: str,
        user_id: str,
    ) -> None:
        _ = user_id
        await self._require_memory_scope(tenant_id, session_id)


class WxbotPlugin(Plugin):
    meta = PluginMeta(
        name="wxbot",
        version="0.2.0",
        description="WeChat bot SDK bridge — polls inbound messages, dispatches replies",
    )

    def __init__(self) -> None:
        self._store: WxbotStore | None = None
        self._ctx: PluginContext | None = None
        self._agent_tool_service: WxbotAgentToolService | None = None
        self._channel_outbound: WxbotChannelOutbound | None = None
        self._effect_handler_enabled = False
        self._background_tasks: dict[str, asyncio.Task[None]] = {}
        self._background_enabled = False
        self._background_stop = asyncio.Event()
        self._critical_tasks: set[asyncio.Task[None]] = set()
        self._execution_gate_confirmed = False
        self._execution_gate_grace_deadline = 0.0
        self._lifecycle_lock = asyncio.Lock()
        self._report_service: WxbotReportService | None = None
        self._self_review_service: WxbotSelfReviewService | None = None
        self._group_summary_service: WxbotGroupSummaryService | None = None
        self._social_policy_store: SocialPolicyStore | None = None
        self._natural_feedback_service: NaturalFeedbackService | None = None
        self._report_scheduler_wakeup = asyncio.Event()
        self._self_review_scheduler_wakeup = asyncio.Event()

    def _track_task(self, key: str, task: asyncio.Task[None]) -> None:
        self._background_tasks[key] = task

        def _cleanup(done: asyncio.Task[None]) -> None:
            if self._background_tasks.get(key) is done:
                self._background_tasks.pop(key, None)
            self._critical_tasks.discard(done)
            if done.cancelled():
                return
            failure = done.exception()
            if failure is not None:
                logger.error(
                    "wxbot.background_task_failed",
                    key=key,
                    error_type=failure.__class__.__name__,
                )

        task.add_done_callback(_cleanup)

    def get_channel_adapters(self) -> list[ChannelAdapterRegistration]:
        def provider_factory(_connection: object) -> WxbotChannelOutbound:
            if self._channel_outbound is None:
                raise RuntimeError("wxbot channel adapter is not initialized")
            return self._channel_outbound

        async def probe_connection(connection: Any) -> ChannelProbeResult:
            if self._store is None or self._ctx is None:
                return ChannelProbeResult(
                    ok=False,
                    status="unavailable",
                    error_code="wxbot_adapter_not_initialized",
                )
            status = await read_bridge_runtime_status(
                get_redis(),
                self._store,
                self._ctx.settings,
                str(connection.tenant_id),
                connection_id=str(connection.connection_id),
            )
            ok = bool(
                status.get("running")
                and status.get("sdk_online")
                and str(status.get("sdk_auth_state") or "").lower() == "ok"
            )
            error_code = ""
            if not ok:
                error_code = str(
                    status.get("sdk_auth_reason")
                    or (
                        "wxbot_bridge_not_running"
                        if not status.get("running")
                        else "wxbot_sdk_unavailable"
                    )
                )[:96]
            return ChannelProbeResult(
                ok=ok,
                status="ready" if ok else "blocked",
                error_code=error_code,
            )

        return [
            ChannelAdapterRegistration(
                descriptor=WECHAT_SDK_DESCRIPTOR,
                provider_factory=provider_factory,
                probe=probe_connection,
            )
        ]

    async def initialize(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._background_stop = asyncio.Event()
        self._background_enabled = True
        self._store = WxbotStore(ctx.settings)
        policy_store = getattr(ctx.container, "social_policy_store", None)
        if policy_store is None:
            policy_store = SocialPolicyStore(get_session_factory())
        self._social_policy_store = policy_store
        memory_feedback_store = _OwnerGatedMemoryFeedbackStore(
            ctx.settings,
            llm_service=getattr(ctx.container, "llm_service", None),
            vector_store=getattr(ctx.container, "vector_store", None),
            owners_scope_execution_allowed=self._owners_scope_execution_allowed,
        )
        self._natural_feedback_service = NaturalFeedbackService(
            policy_store,
            memory=memory_feedback_store,
        )
        self._channel_outbound = WxbotChannelOutbound(
            self._store,
            social_policy_store=self._social_policy_store,
            connection_store=ChannelConnectionStore(
                get_session_factory(),
                ChannelAdapterCatalog(self.get_channel_adapters()),
            ),
        )
        self._effect_handler_enabled = any(
            bool(getattr(ctx.settings, name, False))
            for name in (
                "orchestrator_flow_effect_handler_enabled",
                "orchestrator_flow_effect_handlers_enabled",
                "orchestrator_flow_effect_dispatch_enabled",
            )
        )
        self._register_channel()
        self._agent_tool_service = WxbotAgentToolService(
            ctx.settings,
            wxbot_store=self._store,
            data_owner_scope_execution_allowed=self._owner_scope_execution_allowed,
            data_owners_scope_execution_allowed=self._owners_scope_execution_allowed,
        )
        self._report_service = WxbotReportService(
            self._store,
            ctx.container,
            scope_execution_allowed=self._scope_execution_allowed,
        )
        self._self_review_service = WxbotSelfReviewService(
            self._store,
            ctx.container,
            scope_execution_allowed=self._scope_execution_allowed,
        )
        llm_service = getattr(ctx.container, "llm_service", None)
        self._group_summary_service = (
            WxbotGroupSummaryService(self._store, llm_service, ctx.settings)
            if llm_service is not None
            else None
        )
        self._register_commands()
        if ctx.db_ok:
            await self._store.ensure_tables()
            await self._store.fail_stale_report_jobs()
            await self._store.fail_stale_self_review_jobs()
        await self._start_subscription_schedulers()

    async def on_enable(self, scope=None) -> None:
        _ = scope
        async with self._lifecycle_lock:
            if not self._background_enabled and self._background_tasks:
                await self._stop_background_tasks()
            if not self._background_enabled:
                self._background_stop = asyncio.Event()
            self._background_enabled = True
            self._register_channel()
            self._register_commands()
            await self._start_subscription_schedulers()

    def _register_commands(self) -> None:
        if self._ctx is None or self._agent_tool_service is None:
            return
        registry = getattr(self._ctx.container, "plugin_registry", None)
        commands_plugin = registry.loaded_plugins.get("commands") if registry is not None else None
        register = getattr(commands_plugin, "register_definitions", None)
        if callable(register):
            register(
                build_wxbot_command_definitions(self._agent_tool_service, self._store),
                owner=self.meta.name,
            )
        else:
            logger.warning("wxbot.command_center_unavailable")

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            self._unregister_channel()
            await self._stop_background_tasks()
            self._background_tasks.clear()
            self._critical_tasks.clear()
            self._background_stop = asyncio.Event()
            self._report_scheduler_wakeup = asyncio.Event()
            self._self_review_scheduler_wakeup = asyncio.Event()
            self._agent_tool_service = None
            self._channel_outbound = None
            self._effect_handler_enabled = False
            self._report_service = None
            self._self_review_service = None
            self._group_summary_service = None
            self._social_policy_store = None
            self._natural_feedback_service = None
            self._store = None
            self._ctx = None

    async def on_disable(self, scope=None) -> None:
        _ = scope
        async with self._lifecycle_lock:
            self._unregister_channel()
            await self._stop_background_tasks()

    def _owns_subscription_scheduler_role(self) -> bool:
        return bool(
            self._ctx is not None
            and self._ctx.db_ok
            and str(
                getattr(self._ctx.settings, "app_process_role", "api") or "api"
            ).lower()
            == "scheduler"
        )

    async def _start_subscription_schedulers(self) -> None:
        if not self._background_enabled or not self._owns_subscription_scheduler_role():
            return
        scheduler_keys = {
            "report-subscription-scheduler",
            "self-review-subscription-scheduler",
        }
        if not any(
            key in scheduler_keys and not task.done()
            for key, task in self._background_tasks.items()
        ):
            self._execution_gate_confirmed = False
            self._execution_gate_grace_deadline = 0.0
        self._report_scheduler_wakeup.clear()
        self._self_review_scheduler_wakeup.clear()
        stop_event = self._background_stop
        await self.schedule_background(
            "report-subscription-scheduler",
            lambda: self._report_subscription_scheduler_loop(stop_event),
        )
        await self.schedule_background(
            "self-review-subscription-scheduler",
            lambda: self._self_review_subscription_scheduler_loop(stop_event),
        )

    async def _stop_background_tasks(self) -> None:
        self._background_enabled = False
        self._background_stop.set()
        self._report_scheduler_wakeup.set()
        self._self_review_scheduler_wakeup.set()
        settlement = asyncio.create_task(
            self._settle_background_tasks(),
            name="wxbot-background-settlement",
        )
        cancellation_requested = False
        while not settlement.done():
            try:
                await asyncio.shield(settlement)
            except asyncio.CancelledError:
                # A lifecycle timeout or repeated caller cancellation must not
                # detach accepted delivery/persistence jobs from the plugin
                # resources they still use. Finish settlement, then preserve
                # the caller's cancellation contract.
                cancellation_requested = True
        if cancellation_requested:
            if not settlement.cancelled():
                settlement.exception()
            raise asyncio.CancelledError()
        settlement.result()

    async def _settle_background_tasks(self) -> None:
        while self._background_tasks:
            tasks = tuple(self._background_tasks.values())
            critical = tuple(task for task in tasks if task in self._critical_tasks)
            cancellable = tuple(task for task in tasks if task not in self._critical_tasks)
            for task in cancellable:
                if not task.done():
                    task.cancel()
            if cancellable:
                await asyncio.gather(*cancellable, return_exceptions=True)
            if critical:
                # Delivery and scheduler persistence sections must resolve
                # their durable state before plugin resources are released.
                await asyncio.gather(*critical, return_exceptions=True)
            for key, task in tuple(self._background_tasks.items()):
                if task in tasks:
                    self._background_tasks.pop(key, None)

    async def _await_persistent_section(self, operation: Awaitable[Any]) -> Any:
        task = asyncio.current_task()
        if task is not None:
            self._critical_tasks.add(task)
        operation_task = asyncio.ensure_future(operation)
        cancellation_requested = False
        try:
            while not operation_task.done():
                try:
                    await asyncio.shield(operation_task)
                except asyncio.CancelledError:
                    # Repeated cancellation (for example a lifecycle timeout
                    # racing shutdown) must not strand an SDK send between the
                    # remote side effect and its durable acknowledgement.
                    cancellation_requested = True
            if cancellation_requested:
                # Observe any inner exception without replacing the caller's
                # cancellation contract.
                if not operation_task.cancelled():
                    operation_task.exception()
                raise asyncio.CancelledError()
            return operation_task.result()
        finally:
            if task is not None:
                self._critical_tasks.discard(task)

    async def _execution_allowed(self) -> bool | None:
        registry = getattr(self._ctx.container, "plugin_registry", None) if self._ctx else None
        is_active = getattr(registry, "is_active", None)
        if callable(is_active) and not bool(is_active(self.meta.name)):
            return None
        if callable(is_active) and self._execution_gate_grace_deadline <= 0:
            self._execution_gate_grace_deadline = (
                asyncio.get_running_loop().time() + 5.0
            )
        gate = getattr(registry, "global_execution_allowed", None)
        if not callable(gate):
            return True
        try:
            allowed = await gate(self.meta.name) is True
            if allowed:
                self._execution_gate_confirmed = True
                return True
            if (
                callable(is_active)
                and not self._execution_gate_confirmed
                and asyncio.get_running_loop().time()
                < self._execution_gate_grace_deadline
            ):
                return None
            return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("wxbot.execution_gate_error", error=str(exc))
            return False

    async def _scope_execution_allowed(
        self,
        tenant_id: str,
        session_id: str = "",
    ) -> bool:
        return await self._owner_scope_execution_allowed(
            self.meta.name,
            tenant_id=tenant_id,
            session_id=session_id,
        )

    async def _owner_scope_execution_allowed(
        self,
        owner: str,
        *,
        tenant_id: str,
        session_id: str = "",
    ) -> bool:
        normalized_owner = str(owner or "").strip()
        if not normalized_owner:
            return False
        registry = getattr(self._ctx.container, "plugin_registry", None) if self._ctx else None
        gate = getattr(registry, "scope_execution_allowed", None)
        if not callable(gate):
            logger.error(
                "wxbot.owner_scope_execution_gate_missing",
                owner=normalized_owner,
                tenant_id=tenant_id,
                session_id=session_id,
            )
            return False
        try:
            return (
                await gate(
                    normalized_owner,
                    tenant_id=str(tenant_id or ""),
                    session_id=str(session_id or ""),
                )
                is True
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "wxbot.owner_scope_execution_gate_error",
                owner=normalized_owner,
                tenant_id=tenant_id,
                session_id=session_id,
                error_type=exc.__class__.__name__,
            )
            return False

    async def _owners_scope_execution_allowed(
        self,
        owners: tuple[str, ...],
        *,
        tenant_id: str,
        session_id: str = "",
    ) -> bool:
        normalized_owners = tuple(
            dict.fromkeys(str(owner or "").strip() for owner in owners)
        )
        if not normalized_owners:
            return False
        registry = getattr(self._ctx.container, "plugin_registry", None) if self._ctx else None
        gate = getattr(registry, "owners_scope_execution_allowed", None)
        if not callable(gate):
            logger.error(
                "wxbot.owners_scope_execution_gate_missing",
                owners=list(normalized_owners),
                tenant_id=tenant_id,
                session_id=session_id,
            )
            return False
        try:
            return (
                await gate(
                    normalized_owners,
                    tenant_id=str(tenant_id or ""),
                    session_id=str(session_id or ""),
                )
                is True
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "wxbot.owners_scope_execution_gate_error",
                owners=list(normalized_owners),
                tenant_id=tenant_id,
                session_id=session_id,
                error_type=exc.__class__.__name__,
            )
            return False

    def _unregister_channel(self) -> None:
        if self._ctx is None:
            return
        channel_registry = getattr(self._ctx.container, "channel_registry", None)
        unregister_owner = getattr(channel_registry, "unregister_owner", None)
        if callable(unregister_owner):
            unregister_owner(self.meta.name)

    def _register_channel(self) -> None:
        if self._ctx is None or self._channel_outbound is None:
            return
        channel_registry = getattr(self._ctx.container, "channel_registry", None)
        register_outbound = getattr(channel_registry, "register_outbound", None)
        if callable(register_outbound):
            register_outbound("wechat", self._channel_outbound, owner=self.meta.name)
        register_adapter_outbound = getattr(
            channel_registry,
            "register_adapter_outbound",
            None,
        )
        if callable(register_adapter_outbound):
            register_adapter_outbound(
                WECHAT_SDK_DESCRIPTOR.adapter_id,
                self._channel_outbound,
                channel=WECHAT_SDK_DESCRIPTOR.channel,
                owner=self.meta.name,
            )

    def get_api_router(self):
        if self._store is None or self._ctx is None:
            return None
        return build_wxbot_router(
            self._store,
            self._ctx.container,
            bridge=None,
            scheduler=self,
            report_service=self._report_service,
            self_review_service=self._self_review_service,
            agent_store=getattr(self._ctx.container, "agent_store", None),
            scope_execution_allowed=self._scope_execution_allowed,
            owners_scope_execution_allowed=self._owners_scope_execution_allowed,
        )

    def get_api_default_tenant_id(self) -> str:
        if self._store is None:
            return ""
        return str(
            getattr(self._store.settings, "wxbot_default_tenant_id", "default")
            or "default"
        ).strip()

    def get_pipeline_hooks(self):
        if self._store is None:
            return []
        return [
            WxbotInboundNormalizeHook(),
            WxbotReplyPolicyHook(
                self._store,
                social_policy_store=self._social_policy_store,
                natural_feedback_service=self._natural_feedback_service,
            ),
            WxbotAgentIntentHook(self._store),
            WxbotGroupContextHook(
                self._store,
                self._ctx.settings if self._ctx else self._store.settings,
                self._social_policy_store,
            ),
            WxbotVoiceProfileHook(),
            WxbotReplyQueueHook(
                self._store,
                social_policy_store=self._social_policy_store,
            ),
        ]

    def get_agent_tools(self):
        if self._agent_tool_service is None:
            return []
        return [
            *build_wxbot_group_agent_tools(self._agent_tool_service),
            *build_wxbot_group_plugin_status_agent_tools(self._agent_tool_service),
        ]

    def get_profile_report_builder(self):
        if self._agent_tool_service is None:
            return None
        return self._agent_tool_service.build_group_member_profile_report

    def get_group_membership_authorizer(self):
        if self._store is None:
            return None
        return self._store.is_group_member

    def get_flow_steps(self) -> list[FlowStepDefinition]:
        return [
            FlowStepDefinition(
                kind="plugin.wxbot.normalize_event",
                owner=self.meta.name,
                name="Normalize WeChat event",
                permissions=["hooks:pipeline"],
                inputs={"event"},
                outputs={"signals.channel.wechat.normalized"},
                timeout_seconds=1.0,
                error_policy="fail_closed",
            ),
            FlowStepDefinition(
                kind="plugin.wxbot.user_ban_pre_command",
                owner=self.meta.name,
                name="WeChat user ban pre-command guard",
                permissions=["storage:shared", "hooks:pipeline"],
                inputs={"event", "session", "pre"},
                outputs={"signals.channel.wechat.user_ban_pre_command"},
                timeout_seconds=1.0,
                error_policy="fail_closed",
            ),
            FlowStepDefinition(
                kind="plugin.wxbot.user_ban_gate",
                owner=self.meta.name,
                name="WeChat user ban gate",
                permissions=["storage:shared", "hooks:pipeline"],
                inputs={"event", "session", "pre"},
                outputs={"signals.channel.wechat.user_ban"},
                timeout_seconds=1.0,
                error_policy="fail_closed",
            ),
            FlowStepDefinition(
                kind="plugin.wxbot.reply_policy",
                owner=self.meta.name,
                name="WeChat reply policy",
                permissions=["storage:shared", "hooks:pipeline"],
                inputs={"event", "session", "pre"},
                outputs={
                    "signals.reply_policy",
                    "signals.channel.wechat.reply_policy",
                    "signals.participation",
                    "signals.channel.wechat.participation",
                },
                timeout_seconds=4.0,
                error_policy="fail_closed",
            ),
            FlowStepDefinition(
                kind="plugin.wxbot.agent_scope_enrich",
                owner=self.meta.name,
                name="WeChat group agent scope enrich",
                permissions=["storage:shared", "agent_tools", "hooks:pipeline"],
                inputs={"event", "session", "pre"},
                outputs={
                    "effects.enqueue_channel_reply",
                    "signals.agent.tool_scope",
                    "signals.router.tools_available",
                },
                timeout_seconds=1.5,
                error_policy="fail_open",
            ),
            FlowStepDefinition(
                kind="plugin.wxbot.group_context_load",
                owner=self.meta.name,
                name="Load durable WeChat group context",
                permissions=["storage:shared", "hooks:pipeline"],
                inputs={"event", "session", "pre"},
                outputs={"signals.channel.wechat.group_context"},
                timeout_seconds=2.0,
                error_policy="fail_open",
            ),
            FlowStepDefinition(
                kind="plugin.wxbot.outbound_policy",
                owner=self.meta.name,
                name="WeChat outbound policy",
                permissions=["storage:shared", "hooks:pipeline"],
                inputs={"event", "session", "pre", "reply"},
                outputs={
                    "signals.channel.wechat.outbound_policy",
                    "effects.enqueue_channel_reply",
                    "effects.enqueue_wxbot_reply",
                },
                timeout_seconds=2.0,
                error_policy="fail_closed",
            ),
            FlowStepDefinition(
                kind="plugin.wxbot.voice_profile_enrich",
                owner=self.meta.name,
                name="WeChat VoiceProfile enrich",
                permissions=["storage:shared", "hooks:pipeline"],
                inputs={"event", "session"},
                outputs={"signals.channel.wechat.voice_profile"},
                timeout_seconds=1.0,
                error_policy="fail_closed",
            ),
        ]

    def get_flow_executors(self):
        if self._store is None:
            return {}
        return {
            "plugin.wxbot.normalize_event": WxbotNormalizeEventStep(),
            "plugin.wxbot.user_ban_pre_command": WxbotUserBanPreCommandStep(self._store),
            "plugin.wxbot.user_ban_gate": WxbotUserBanGateStep(self._store),
            "plugin.wxbot.reply_policy": WxbotReplyPolicyStep(
                self._store,
                social_policy_store=self._social_policy_store,
                natural_feedback_service=self._natural_feedback_service,
            ),
            "plugin.wxbot.agent_scope_enrich": WxbotAgentScopeEnrichStep(
                self._store,
                effect_handler_enabled=self._effect_handler_enabled,
            ),
            "plugin.wxbot.voice_profile_enrich": WxbotVoiceProfileEnrichStep(),
            "plugin.wxbot.group_context_load": WxbotGroupContextLoadStep(
                WxbotGroupContextHook(
                    self._store,
                    self._ctx.settings if self._ctx else self._store.settings,
                    self._social_policy_store,
                )
            ),
            "plugin.wxbot.outbound_policy": WxbotOutboundPolicyStep(
                self._store,
                effect_handler_enabled=self._effect_handler_enabled,
                social_policy_store=self._social_policy_store,
            ),
        }

    def get_effect_handlers(self):
        if self._ctx is None or self._store is None:
            return []
        handlers = [
            (
                "sdk_trigger_config",
                self.meta.name,
                WxbotSdkTriggerConfigEffectHandler(self._store),
            )
        ]
        channel_registry = getattr(self._ctx.container, "channel_registry", None)
        if channel_registry is None:
            return handlers
        channel_handler = ChannelReplyEffectHandler(channel_registry)
        wxbot_handler = WxbotReplyEffectHandler(
            ChannelReplyEffectHandler(channel_registry, default_channel="wechat")
        )
        handlers.extend([
            ("enqueue_channel_reply", self.meta.name, channel_handler),
            ("enqueue_wxbot_reply", self.meta.name, wxbot_handler),
        ])
        return handlers

    def get_admin_media_event_provider(self):
        if self._store is None:
            return None
        return WxbotAdminMediaEventProvider(self._store)

    def get_permissions(self) -> list[str]:
        return [
            "network:wxbot",
            "storage:shared",
            "agent_tools",
            "commands",
            "hooks:pipeline",
            "admin_api",
        ]

    async def get_runtime_status(self) -> dict[str, object]:
        if self._store is None:
            return {"bridge": {"running": False}, "pending": 0, "sessions": 0}
        tenant_id = str(getattr(self._ctx.settings, "tenant_demo_id", "demo") if self._ctx else "demo")
        queue = await self._store.reply_queue_stats(tenant_id)
        pending = int(queue.get("pending") or 0)
        running = bool(self._background_tasks)
        return {
            "running": running,
            "sdk_online": False,
            "ingest_mode": "plugin-runtime",
            "bridge": {
                "running": running,
                "tasks": sorted(self._background_tasks),
            },
            "pending": pending,
            "reply_queue": queue,
            "sessions": 0,
            "agent_tools": [tool.name for tool in self.get_agent_tools()],
        }

    async def schedule_background(self, key: str, coro_factory: Callable[[], Awaitable[None]]) -> bool:
        if not self._background_enabled:
            logger.info("wxbot.background_task_skipped_disabled", key=key)
            return False
        existing = self._background_tasks.get(key)
        if existing is not None and not existing.done():
            return False
        task = asyncio.create_task(coro_factory(), name=f"wxbot-{key}")
        self._track_task(key, task)
        logger.info("wxbot.background_task_scheduled", key=key)
        return True

    @staticmethod
    def _group_observation_metadata(event: InboundEvent) -> dict[str, Any]:
        source = dict(event.metadata or {})
        keys = {
            "source",
            "sdk_source",
            "sdk_stream_id",
            "sdk_event_id",
            "sdk_event_type",
            "msg_svr_id",
            "mentioned_me",
            "at_wxids",
            "mention_mode",
            "bot_mentioned",
            "bot_addressed",
            "bot_mention_position",
            "bot_mention_names",
            "bot_normalized_content",
            "wxbot_normalized_content",
            "bot_wxid",
            "quote",
            "quote_text",
            "quote_image_path",
            "quote_image_url",
            "image_observation",
            "media_status",
            "occurred_at",
            "occurred_ts",
        }
        metadata = {key: source[key] for key in keys if key in source}
        if "quote_text" in metadata:
            metadata["quote_text"] = str(metadata["quote_text"] or "")[:2000]
        return metadata

    async def record_group_observation(self, event: InboundEvent) -> bool:
        if (
            self._store is None
            or event.channel != Channel.WECHAT
            or not str(event.session_id or "").endswith("@chatroom")
        ):
            return False
        occurred_ts = event.metadata.get("occurred_ts")
        if not occurred_ts:
            occurred_ts = int(event.received_at.timestamp())
        message_type = getattr(event.message.type, "value", event.message.type)
        return await self._store.record_group_observation(
            tenant_id=event.tenant_id,
            session_id=event.session_id,
            message_id=event.message_id,
            session_name=str(event.metadata.get("session_name") or ""),
            sender_wxid=str(event.metadata.get("sender_wxid") or event.user_id or ""),
            sender_name=str(event.metadata.get("sender_name") or ""),
            msg_type=str(message_type or "text"),
            content=str(event.message.content or ""),
            mentioned_me=bool(event.metadata.get("bot_mentioned") or event.metadata.get("mentioned_me")),
            bot_addressed=bool(
                event.metadata.get("bot_addressed")
                if event.metadata.get("bot_addressed") is not None
                else event.metadata.get("mentioned_me")
            ),
            is_self_sent=bool(event.metadata.get("is_self_sent")),
            occurred_ts=int(occurred_ts or 0),
            metadata=self._group_observation_metadata(event),
            summary_debounce_seconds=float(
                getattr(self._store.settings, "wxbot_group_summary_debounce_seconds", 5.0)
                or 5.0
            ),
        )

    async def drain_group_summary_jobs(
        self,
        *,
        limit: int = 1,
        worker_id: str = "",
        scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None = None,
    ) -> dict[str, int]:
        if self._group_summary_service is None:
            return {"claimed": 0, "succeeded": 0, "failed": 0}
        result = {"claimed": 0, "succeeded": 0, "failed": 0}
        for _ in range(max(1, min(int(limit or 1), 20))):
            current = await self._group_summary_service.drain_once(
                worker_id=worker_id or "wxbot-group-summary",
                scope_execution_allowed=(
                    scope_execution_allowed or self._scope_execution_allowed
                ),
            )
            for key in result:
                result[key] += int(current.get(key, 0) or 0)
            if not current.get("claimed"):
                break
        return result

    def notify_report_scheduler(self) -> None:
        self._report_scheduler_wakeup.set()

    def notify_self_review_scheduler(self) -> None:
        self._self_review_scheduler_wakeup.set()

    async def _process_due_report_subscriptions(self) -> None:
        if self._store is None or self._report_service is None:
            return
        tenant_id = str(getattr(self._store.settings, "wxbot_default_tenant_id", "default") or "default")
        queued_jobs = await self._store.list_report_deliveries_to_reconcile(
            tenant_id,
            limit=100,
        )
        for queued_job in queued_jobs:
            queued_session_id = str(queued_job.get("session_id") or "")
            if not await self._scope_execution_allowed(
                tenant_id,
                queued_session_id,
            ):
                continue
            await self.schedule_background(
                f"report-reconcile-{queued_job['id']}",
                lambda job_id=int(queued_job["id"]): self._reconcile_report_job(
                    job_id
                ),
            )
        subscriptions = await self._store.list_enabled_report_subscriptions(tenant_id)
        for sub in subscriptions:
            session_id = str(sub["session_id"])
            if not await self._scope_execution_allowed(tenant_id, session_id):
                continue
            for report_type in ("daily", "weekly", "monthly"):
                due = resolve_due_period(sub, report_type)
                if not due:
                    continue
                period_key, period_label = due
                job = await self._store.get_or_create_report_job(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    session_name=str(sub.get("session_name") or sub["session_id"]),
                    report_type=report_type,
                    period_key=period_key,
                    period_label=period_label,
                )
                status = str(job.get("status") or "pending")
                delivery_status = str(job.get("delivery_status") or "pending")
                if status == "completed":
                    if delivery_status in {"pending", "failed"}:
                        await self.schedule_background(
                            f"report-send-{job['id']}",
                            lambda job_id=int(job["id"]): self._send_report_job(job_id),
                        )
                    continue
                if status == "running":
                    await self.schedule_background(
                        f"report-job-{job['id']}",
                        lambda job_id=int(job["id"]): self._run_and_send_due_report_job(
                            job_id
                        ),
                    )
                    continue
                if should_defer_report_job_retry(job):
                    payload = job.get("report_payload") if isinstance(job, dict) else {}
                    retry_after = payload.get("retry_after") if isinstance(payload, dict) else ""
                    logger.info(
                        "wxbot.report_job_backoff_active",
                        job_id=job.get("id"),
                        current_stage=job.get("current_stage"),
                        retry_after=retry_after,
                        error=job.get("error"),
                    )
                    continue
                reset_kwargs: dict[str, Any] = {
                    "status": "pending",
                    "current_stage": "queued",
                    "msg_count": 0,
                    "result_text": "",
                }
                if status != "failed":
                    reset_kwargs["report_payload"] = {}
                    reset_kwargs["error"] = ""
                reset = await self._store.update_report_job(
                    int(job["id"]),
                    expected_run_attempt=int(job.get("run_attempt") or 0),
                    expected_status=status,
                    **reset_kwargs,
                )
                if not reset:
                    continue
                await self.schedule_background(
                    f"report-job-{job['id']}",
                    lambda job_id=int(job["id"]): self._run_and_send_due_report_job(job_id),
                )

    async def _process_due_self_review_subscriptions(self) -> None:
        if self._store is None or self._self_review_service is None:
            return
        tenant_id = str(getattr(self._store.settings, "wxbot_default_tenant_id", "default") or "default")
        subscriptions = await self._store.list_enabled_self_review_subscriptions(tenant_id)
        for sub in subscriptions:
            session_id = str(sub["session_id"])
            if not await self._scope_execution_allowed(tenant_id, session_id):
                continue
            due = resolve_self_review_due_period(sub)
            if not due:
                continue
            period_key, period_label = due
            job = await self._store.get_or_create_self_review_job(
                tenant_id=tenant_id,
                session_id=session_id,
                session_name=str(sub.get("session_name") or sub["session_id"]),
                period_key=period_key,
                period_label=period_label,
            )
            status = str(job.get("status") or "pending")
            if status == "completed":
                continue
            if status == "running":
                await self.schedule_background(
                    f"self-review-job-{job['id']}",
                    lambda job_id=int(job["id"]): self._run_due_self_review_job(
                        job_id
                    ),
                )
                continue
            reset = await self._store.update_self_review_job(
                int(job["id"]),
                status="pending",
                current_stage="queued",
                msg_count=0,
                result_text="",
                review_payload={},
                kb_doc_title=f"[{(sub.get('session_name') or sub['session_id'])!s}] 自我迭代复盘 · {period_label}",
                error="",
                expected_run_attempt=int(job.get("run_attempt") or 0),
                expected_status=status,
            )
            if not reset:
                continue
            await self.schedule_background(
                f"self-review-job-{job['id']}",
                lambda job_id=int(job["id"]): self._run_due_self_review_job(job_id),
            )

    async def _run_and_send_due_report_job(self, job_id: int) -> None:
        if self._report_service is None or self._store is None:
            return
        job = await self._store.get_report_job(job_id)
        if not job or not await self._scope_execution_allowed(
            str(job.get("tenant_id") or ""),
            str(job.get("session_id") or ""),
        ):
            return
        await self._report_service.run_report_job(job_id)
        job = await self._store.get_report_job(job_id)
        if not job or str(job.get("status") or "") != "completed":
            return
        if str(job.get("delivery_status") or "pending") not in {
            "pending",
            "failed",
        }:
            return
        await self._send_report_job(job_id)

    async def _send_report_job(self, job_id: int) -> None:
        if self._report_service is None or self._store is None:
            return
        job = await self._store.get_report_job(job_id)
        if not job or not await self._scope_execution_allowed(
            str(job.get("tenant_id") or ""),
            str(job.get("session_id") or ""),
        ):
            return
        await self._await_persistent_section(
            self._report_service.send_report_job(job_id)
        )
        refreshed = await self._store.get_report_job(job_id)
        # Wake immediately only when an SDK row now needs acknowledgement.
        # Waking after a failed claim (network outage, speech guard, etc.)
        # creates a zero-delay failed -> sending -> failed reclaim loop.  The
        # normal scheduler interval provides bounded retry backoff instead.
        if str((refreshed or {}).get("delivery_status") or "") == "queued":
            self.notify_report_scheduler()

    async def _reconcile_report_job(self, job_id: int) -> None:
        if self._report_service is None or self._store is None:
            return
        job = await self._store.get_report_job(job_id)
        if not job or not await self._scope_execution_allowed(
            str(job.get("tenant_id") or ""),
            str(job.get("session_id") or ""),
        ):
            return
        await self._await_persistent_section(
            self._report_service.reconcile_report_delivery(job_id)
        )

    async def _run_due_self_review_job(self, job_id: int) -> None:
        if self._self_review_service is None or self._store is None:
            return
        job = await self._store.get_self_review_job(job_id)
        if not job or not await self._scope_execution_allowed(
            str(job.get("tenant_id") or ""),
            str(job.get("session_id") or ""),
        ):
            return
        await self._self_review_service.run_self_review_job(job_id)

    async def _report_subscription_scheduler_loop(
        self,
        stop_event: asyncio.Event,
    ) -> None:
        if self._store is None:
            return
        tenant_id = str(getattr(self._store.settings, "wxbot_default_tenant_id", "default") or "default")
        while not stop_event.is_set():
            try:
                execution_allowed = await self._execution_allowed()
                if execution_allowed is None:
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=0.05)
                    except TimeoutError:
                        continue
                    continue
                if not execution_allowed:
                    self._background_enabled = False
                    stop_event.set()
                    break
                await self._await_persistent_section(
                    self._process_due_report_subscriptions()
                )
                if stop_event.is_set():
                    break
                subscriptions = await self._store.list_enabled_report_subscriptions(tenant_id)
                queued_deliveries = await self._store.list_report_deliveries_to_reconcile(
                    tenant_id,
                    limit=1,
                )
                timeout = (
                    5.0
                    if queued_deliveries
                    else min(seconds_to_next_subscription_fire(subscriptions), 300.0)
                )
                try:
                    await asyncio.wait_for(self._report_scheduler_wakeup.wait(), timeout=timeout)
                    self._report_scheduler_wakeup.clear()
                except TimeoutError:
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("wxbot.report_subscription_scheduler_error", error=str(exc))
                if stop_event.is_set():
                    break
                await asyncio.sleep(30)

    async def _self_review_subscription_scheduler_loop(
        self,
        stop_event: asyncio.Event,
    ) -> None:
        if self._store is None:
            return
        tenant_id = str(getattr(self._store.settings, "wxbot_default_tenant_id", "default") or "default")
        while not stop_event.is_set():
            try:
                execution_allowed = await self._execution_allowed()
                if execution_allowed is None:
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=0.05)
                    except TimeoutError:
                        continue
                    continue
                if not execution_allowed:
                    self._background_enabled = False
                    stop_event.set()
                    break
                await self._await_persistent_section(
                    self._process_due_self_review_subscriptions()
                )
                if stop_event.is_set():
                    break
                subscriptions = await self._store.list_enabled_self_review_subscriptions(tenant_id)
                timeout = min(seconds_to_next_self_review_fire(subscriptions), 300.0)
                try:
                    await asyncio.wait_for(self._self_review_scheduler_wakeup.wait(), timeout=timeout)
                    self._self_review_scheduler_wakeup.clear()
                except TimeoutError:
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("wxbot.self_review_subscription_scheduler_error", error=str(exc))
                if stop_event.is_set():
                    break
                await asyncio.sleep(30)


plugin = WxbotPlugin()
