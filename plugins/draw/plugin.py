from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.billing import BillingCoordinator
from app.channel import ChannelRegistry
from app.common.logging import get_logger
from app.orchestrator.flow import FlowStepDefinition
from app.plugin.base import Plugin, PluginContext, PluginMeta
from plugins.draw.agent import DrawAgentToolService, build_draw_agent_tools
from plugins.draw.hooks import (
    DrawPostprocessResultStep,
    DrawPublishMediaEffectHandler,
    DrawReplyHook,
    build_draw_command_definitions,
    drain_queued_draw_tasks,
    recover_stale_draw_tasks,
)
from plugins.draw.router import build_draw_router
from plugins.draw.store import DrawStore

logger = get_logger(__name__)


class DrawPlugin(Plugin):
    meta = PluginMeta(
        name="draw",
        version="0.1.0",
        description="Slash-command image generation for wxbot chats",
    )

    def __init__(self) -> None:
        self._store: DrawStore | None = None
        self._billing: BillingCoordinator | None = None
        self._channel_registry: ChannelRegistry | None = None
        self._agent_tool_service: DrawAgentToolService | None = None
        self._ctx: PluginContext | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._lifecycle_lock = asyncio.Lock()

    def _track_background_task(self, task: asyncio.Task[None]) -> None:
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def initialize(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._store = DrawStore(ctx.settings)
        self._billing = getattr(ctx.container, "billing", None)
        self._channel_registry = getattr(ctx.container, "channel_registry", None)
        self._agent_tool_service = DrawAgentToolService(
            store=self._store,
            channel_registry=self._channel_registry,
            billing=self._billing,
            register_background_task=self._track_background_task,
            scope_execution_allowed=self._scope_execution_allowed,
        )
        await self._store.initialize()
        self._register_commands()

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            await self._drain_background_tasks()
            if self._store is not None:
                await self._store.close()
            self._store = None
            self._billing = None
            self._channel_registry = None
            self._agent_tool_service = None
            self._ctx = None

    async def on_enable(self, scope=None) -> None:
        _ = scope
        async with self._lifecycle_lock:
            await self._drain_background_tasks()
            self._register_commands()

    async def on_disable(self, scope=None) -> None:
        _ = scope
        # Draw jobs are finite and persist billing/task state around provider
        # calls. Contributions have already been unpublished by the registry,
        # so drain accepted work instead of interrupting those critical writes.
        async with self._lifecycle_lock:
            await self._drain_background_tasks()

    async def _drain_background_tasks(self) -> None:
        cancellation_requested = False
        while self._background_tasks:
            tasks = tuple(self._background_tasks)
            settled = asyncio.gather(*tasks, return_exceptions=True)
            while not settled.done():
                try:
                    await asyncio.shield(settled)
                except asyncio.CancelledError:
                    cancellation_requested = True
                    # Normal disable drains accepted work. A lifecycle timeout
                    # instead asks each cancellation-safe job to settle, then
                    # waits for that settlement so no task survives store close.
                    for task in tasks:
                        task.cancel()
            settled.result()
            self._background_tasks.difference_update(tasks)
        if cancellation_requested:
            raise asyncio.CancelledError()

    def _register_commands(self) -> None:
        if self._store is None or self._ctx is None:
            return
        registry = getattr(self._ctx.container, "plugin_registry", None)
        commands_plugin = registry.loaded_plugins.get("commands") if registry is not None else None
        register = getattr(commands_plugin, "register_definitions", None)
        if callable(register):
            register(
                build_draw_command_definitions(
                    self._store,
                    self._billing,
                    self._channel_registry,
                    self._track_background_task,
                    self._scope_execution_allowed,
                ),
                owner=self.meta.name,
            )
        else:
            logger.warning("draw.command_center_unavailable")

    def get_api_router(self):
        if self._store is None:
            return None
        return build_draw_router(
            self._store,
            channel_registry=self._channel_registry,
            billing=self._billing,
            register_background_task=self._track_background_task,
            recover_stale_tasks=self.recover_stale_tasks,
            scope_execution_allowed=self._scope_execution_allowed,
        )

    async def _scope_execution_allowed(
        self,
        tenant_id: str,
        session_id: str = "",
    ) -> bool:
        registry = getattr(self._ctx.container, "plugin_registry", None) if self._ctx else None
        gate = getattr(registry, "scope_execution_allowed", None)
        if not callable(gate):
            logger.error(
                "draw.scope_execution_gate_missing",
                tenant_id=tenant_id,
                session_id=session_id,
            )
            return False
        try:
            return (
                await gate(
                    self.meta.name,
                    tenant_id=str(tenant_id or ""),
                    session_id=str(session_id or ""),
                )
                is True
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "draw.scope_execution_gate_error",
                tenant_id=tenant_id,
                session_id=session_id,
                error_type=exc.__class__.__name__,
            )
            return False

    def get_pipeline_hooks(self):
        if self._store is None:
            return []
        return [
            DrawReplyHook(),
        ]

    def get_agent_tools(self):
        if self._agent_tool_service is None:
            return []
        return build_draw_agent_tools(self._agent_tool_service)

    def get_flow_steps(self) -> list[FlowStepDefinition]:
        return [
            FlowStepDefinition(
                kind="plugin.draw.postprocess_result",
                owner=self.meta.name,
                name="Postprocess draw result",
                permissions=["storage:plugin"],
                inputs={"event", "session", "result"},
                outputs={
                    "signals.draw.result",
                    "effects.enqueue_channel_reply",
                    "effects.publish_media",
                },
                timeout_seconds=1.0,
                error_policy="fail_open",
            )
        ]

    def get_flow_executors(self):
        effect_reply_enabled = False
        if self._ctx is not None:
            effect_reply_enabled = (
                bool(getattr(self._ctx.settings, "orchestrator_flow_runtime_enabled", False))
                and bool(
                    getattr(
                        self._ctx.settings,
                        "orchestrator_flow_effect_handlers_enabled",
                        False,
                    )
                )
            )
        return {
            "plugin.draw.postprocess_result": DrawPostprocessResultStep(
                channel_reply_effects_enabled=effect_reply_enabled,
            )
        }

    def get_effect_handlers(self):
        return [("publish_media", self.meta.name, DrawPublishMediaEffectHandler())]

    def get_permissions(self) -> list[str]:
        return [
            "network:*",
            "storage:plugin",
            "agent_tools",
            "commands",
            "hooks:pipeline",
            "admin_api",
        ]

    async def recover_stale_tasks(
        self,
        *,
        stale_seconds: float | None = None,
        limit: int = 50,
        worker_id: str = "",
        scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None = None,
    ) -> dict[str, int]:
        _ = worker_id
        if self._store is None or self._channel_registry is None:
            return {"recovered": 0, "callbacks_sent": 0, "callback_failed": 0}
        if stale_seconds is None:
            stale_seconds = float(
                getattr(self._store.settings, "draw_task_stale_seconds", 3600.0) or 3600.0
            )
        return await recover_stale_draw_tasks(
            store=self._store,
            channel_registry=self._channel_registry,
            stale_seconds=stale_seconds,
            limit=limit,
            scope_execution_allowed=(
                scope_execution_allowed or self._scope_execution_allowed
            ),
        )

    async def drain_queued_tasks(
        self,
        *,
        worker_id: str = "",
        batch_size: int | None = None,
        lock_ttl_seconds: float | None = None,
        auto_retry_enabled: bool | None = None,
        max_retries: int | None = None,
        retry_backoff_seconds: float | None = None,
        scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None = None,
    ) -> dict[str, int]:
        if self._store is None or self._channel_registry is None:
            return {"claimed": 0, "completed": 0, "failed": 0, "auto_retried": 0}
        settings = self._store.settings
        if batch_size is None:
            batch_size = int(getattr(settings, "draw_task_queue_batch_size", 5) or 5)
        if lock_ttl_seconds is None:
            lock_ttl_seconds = float(getattr(settings, "draw_task_lock_ttl_seconds", 900.0) or 900.0)
        if auto_retry_enabled is None:
            auto_retry_enabled = bool(getattr(settings, "draw_task_auto_retry_enabled", False))
        if max_retries is None:
            max_retries = int(getattr(settings, "draw_task_max_retries", 3) or 0)
        if retry_backoff_seconds is None:
            retry_backoff_seconds = float(
                getattr(settings, "draw_task_retry_backoff_seconds", 0.0) or 0.0
            )
        return await drain_queued_draw_tasks(
            store=self._store,
            channel_registry=self._channel_registry,
            billing=self._billing,
            worker_id=worker_id,
            batch_size=batch_size,
            lock_ttl_seconds=lock_ttl_seconds,
            auto_retry_enabled=auto_retry_enabled,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            scope_execution_allowed=(
                scope_execution_allowed or self._scope_execution_allowed
            ),
        )

    async def get_runtime_status(self) -> dict[str, object]:
        if self._store is None:
            return {"configured": False, "storage": {}, "commands": []}
        settings = self._store.settings
        storage_dir = Path(str(getattr(settings, "draw_storage_dir", "") or ""))
        return {
            "configured": bool(str(getattr(settings, "draw_api_url", "") or "").strip()),
            "fallback_configured": bool(str(getattr(settings, "draw_fallback_api_url", "") or "").strip()),
            "storage": {
                "dir": str(storage_dir),
                "exists": storage_dir.exists(),
                "images": len(self._store.list_images(limit=200)),
            },
            "commands": ["/draw", "/画图", "/redraw", "/重绘"],
            "agent_tools": [tool.name for tool in self.get_agent_tools()],
        }


plugin = DrawPlugin()
