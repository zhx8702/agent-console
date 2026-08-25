from __future__ import annotations

import asyncio

from app.billing import BillingCoordinator
from app.channel import ChannelRegistry
from app.common.logging import get_logger
from app.plugin.base import Plugin, PluginContext, PluginMeta
from plugins.video.agent import VideoAgentToolService, build_video_agent_tools
from plugins.video.hooks import build_video_command_definitions
from plugins.video.store import VideoStore

logger = get_logger(__name__)


class VideoPlugin(Plugin):
    meta = PluginMeta(
        name="video",
        version="0.1.0",
        description="Slash-command and Agent video generation for wxbot chats",
    )

    def __init__(self) -> None:
        self._ctx: PluginContext | None = None
        self._store: VideoStore | None = None
        self._billing: BillingCoordinator | None = None
        self._channel_registry: ChannelRegistry | None = None
        self._agent_tool_service: VideoAgentToolService | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._lifecycle_lock = asyncio.Lock()

    def _track_background_task(self, task: asyncio.Task[None]) -> None:
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def initialize(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._store = VideoStore(ctx.settings)
        self._billing = getattr(ctx.container, "billing", None)
        self._channel_registry = getattr(ctx.container, "channel_registry", None)
        self._agent_tool_service = VideoAgentToolService(
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
            self._ctx = None
            self._store = None
            self._billing = None
            self._channel_registry = None
            self._agent_tool_service = None

    async def on_enable(self, scope=None) -> None:
        _ = scope
        self._register_commands()

    async def on_disable(self, scope=None) -> None:
        _ = scope
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
                    for task in tasks:
                        task.cancel()
            settled.result()
            self._background_tasks.difference_update(tasks)
        if cancellation_requested:
            raise asyncio.CancelledError()

    def _register_commands(self) -> None:
        if self._agent_tool_service is None or self._ctx is None:
            return
        registry = getattr(self._ctx.container, "plugin_registry", None)
        commands_plugin = (
            registry.loaded_plugins.get("commands") if registry is not None else None
        )
        register = getattr(commands_plugin, "register_definitions", None)
        if callable(register):
            register(
                build_video_command_definitions(self._agent_tool_service),
                owner=self.meta.name,
            )
        else:
            logger.warning("video.command_center_unavailable")

    async def _scope_execution_allowed(self, tenant_id: str, session_id: str = "") -> bool:
        registry = (
            getattr(self._ctx.container, "plugin_registry", None) if self._ctx else None
        )
        gate = getattr(registry, "scope_execution_allowed", None)
        if not callable(gate):
            logger.error(
                "video.scope_execution_gate_missing",
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
                "video.scope_execution_gate_error",
                tenant_id=tenant_id,
                session_id=session_id,
                error_type=exc.__class__.__name__,
            )
            return False

    def get_agent_tools(self):
        if self._agent_tool_service is None:
            return []
        return build_video_agent_tools(self._agent_tool_service)

    def get_permissions(self) -> list[str]:
        return ["network:*", "storage:plugin", "agent_tools", "commands"]

    async def get_runtime_status(self) -> dict[str, object]:
        settings = self._ctx.settings if self._ctx is not None else None
        return {
            "configured": bool(
                str(getattr(settings, "video_api_url", "") or "").strip()
                or str(getattr(settings, "draw_api_url", "") or "").strip()
            ),
            "sdk_video_delivery": bool(
                self._channel_registry is not None
                and self._channel_registry.outbound_for("wechat") is not None
                and callable(
                    getattr(
                        self._channel_registry.outbound_for("wechat"),
                        "send_file",
                        None,
                    )
                )
            ),
            "sdk_video_delivery_mode": "file",
        }


plugin = VideoPlugin()
