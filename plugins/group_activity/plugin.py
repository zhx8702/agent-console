from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from app.common.logging import get_logger
from app.common.types import RouteType
from app.infra.db import get_session_factory
from app.plugin.base import Plugin, PluginContext, PluginMeta
from app.social.store import SocialPolicyStore
from plugins.group_activity.hooks import GroupActivityObserveHook
from plugins.group_activity.router import build_group_activity_router
from plugins.group_activity.service import GroupActivityService
from plugins.group_activity.store import GroupActivityStore
from plugins.wxbot.channel import WxbotChannelOutbound
from plugins.wxbot.store import WxbotStore

logger = get_logger(__name__)


def _execution_owner_versions(
    registry: Any,
    *,
    group_activity_version: str,
) -> dict[str, str]:
    versions = {"group_activity": str(group_activity_version or "").strip()}
    loaded_plugins = getattr(registry, "loaded_plugins", {}) if registry is not None else {}
    if isinstance(loaded_plugins, dict):
        wxbot_plugin = loaded_plugins.get("wxbot")
        wxbot_meta = getattr(wxbot_plugin, "meta", None)
        wxbot_version = str(getattr(wxbot_meta, "version", "") or "").strip()
        if wxbot_version:
            versions["wxbot"] = wxbot_version
    return versions


async def _settle_scheduler_task(task: asyncio.Task[None]) -> None:
    """Wait for *task* to finish before propagating caller cancellation."""

    waiter = asyncio.gather(task, return_exceptions=True)
    cancellation: asyncio.CancelledError | None = None
    while not waiter.done():
        try:
            await asyncio.shield(waiter)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
    if cancellation is not None:
        raise cancellation


class GroupActivityPlugin(Plugin):
    meta = PluginMeta(
        name="group_activity",
        version="0.1.0",
        description="Group idle activity starter using group Agent skill and wxbot outbound queue",
        dependencies=["wxbot>=0.2.0"],
    )

    def __init__(self) -> None:
        self._ctx: PluginContext | None = None
        self._store: GroupActivityStore | None = None
        self._service: GroupActivityService | None = None
        self._background_tasks: dict[str, asyncio.Task[None]] = {}
        self._scheduler_wakeup = asyncio.Event()
        self._scheduler_stop = asyncio.Event()
        self._scheduler_enabled = False
        self._scheduler_busy = False
        self._execution_gate_confirmed = False
        self._execution_gate_grace_deadline = 0.0
        self._lifecycle_lock = asyncio.Lock()

    def _track_task(self, key: str, task: asyncio.Task[None]) -> None:
        self._background_tasks[key] = task

        def _cleanup(done: asyncio.Task[None]) -> None:
            if self._background_tasks.get(key) is done:
                self._background_tasks.pop(key, None)

        task.add_done_callback(_cleanup)

    def _owns_scheduler_role(self) -> bool:
        if self._ctx is None:
            return False
        return (
            str(
                getattr(self._ctx.settings, "app_process_role", "api")
                or "api"
            ).lower()
            == "scheduler"
        )

    async def initialize(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._store = GroupActivityStore(ctx.settings)
        agent_engine = dict(ctx.container.capabilities or {}).get(RouteType.AGENT)
        social_policy_store = (
            getattr(ctx.container, "social_policy_store", None)
            or SocialPolicyStore(get_session_factory())
        )
        plugin_registry = getattr(ctx.container, "plugin_registry", None)
        execution_owner_versions = _execution_owner_versions(
            plugin_registry,
            group_activity_version=self.meta.version,
        )
        owners_scope_execution_allowed = getattr(
            plugin_registry,
            "owners_scope_execution_allowed",
            None,
        )
        wxbot_store = WxbotStore(ctx.settings)
        self._service = GroupActivityService(
            store=self._store,
            settings=ctx.settings,
            agent_engine=agent_engine,
            outbound=WxbotChannelOutbound(
                wxbot_store,
                social_policy_store=social_policy_store,
            ),
            wxbot_store=wxbot_store,
            social_policy_store=social_policy_store,
            owners_scope_execution_allowed=(
                owners_scope_execution_allowed
                if callable(owners_scope_execution_allowed)
                else None
            ),
            channel_registry=getattr(ctx.container, "channel_registry", None),
            execution_owner_versions=execution_owner_versions,
        )
        if ctx.db_ok:
            await self._store.ensure_tables()
        if ctx.db_ok and self._owns_scheduler_role():
            self._scheduler_enabled = True
            await self._start_scheduler()

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            await self._stop_scheduler()
            self._background_tasks.clear()
            self._scheduler_wakeup = asyncio.Event()
            self._scheduler_stop = asyncio.Event()
            self._service = None
            self._store = None
            self._ctx = None

    async def on_enable(self, scope=None) -> None:
        _ = scope
        async with self._lifecycle_lock:
            if (
                self._ctx is not None
                and self._ctx.db_ok
                and self._service is not None
                and self._owns_scheduler_role()
                and not self._scheduler_enabled
            ):
                self._scheduler_enabled = True
                self._scheduler_wakeup.clear()
                await self._start_scheduler()

    async def on_disable(self, scope=None) -> None:
        _ = scope
        async with self._lifecycle_lock:
            await self._stop_scheduler()

    def get_api_router(self):
        if self._store is None or self._service is None:
            return None
        return build_group_activity_router(self._store, self._service)

    def get_pipeline_hooks(self):
        if self._store is None:
            return []
        return [GroupActivityObserveHook(self._store)]

    def get_permissions(self) -> list[str]:
        return ["network:wxbot", "storage:shared", "hooks:pipeline", "admin_api"]

    async def get_runtime_status(self) -> dict[str, Any]:
        return {
            "running": bool(self._background_tasks),
            "scheduler_enabled": self._scheduler_enabled,
            "tasks": sorted(self._background_tasks),
        }

    async def schedule_background(self, key: str, coro_factory: Callable[[], Awaitable[None]]) -> bool:
        existing = self._background_tasks.get(key)
        if existing is not None and not existing.done():
            return False
        task = asyncio.create_task(coro_factory(), name=f"group-activity-{key}")
        self._track_task(key, task)
        logger.info("group_activity.background_task_scheduled", key=key)
        return True

    def notify_scheduler(self) -> None:
        self._scheduler_wakeup.set()

    async def _execution_allowed(self) -> bool | None:
        registry = getattr(self._ctx.container, "plugin_registry", None) if self._ctx else None
        is_active = getattr(registry, "is_active", None)
        if callable(is_active) and not bool(is_active(self.meta.name)):
            # PluginRegistry starts us from initialize()/on_enable() just
            # before it publishes the local active flag. Do no work in that
            # activation window, but keep the generation alive for handoff.
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
                # Durable mark_initialized may still be committing after the
                # local active flag becomes visible.
                return None
            return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("group_activity.execution_gate_error", error=str(exc))
            return False

    async def _start_scheduler(self) -> None:
        existing = self._background_tasks.get("scheduler")
        if existing is not None and not existing.done():
            if not self._scheduler_stop.is_set():
                return
            try:
                await _settle_scheduler_task(existing)
            except asyncio.CancelledError:
                self._scheduler_enabled = False
                raise
            finally:
                if (
                    existing.done()
                    and self._background_tasks.get("scheduler") is existing
                ):
                    self._background_tasks.pop("scheduler", None)
        self._scheduler_stop = asyncio.Event()
        self._execution_gate_confirmed = False
        self._execution_gate_grace_deadline = 0.0
        stop_event = self._scheduler_stop
        await self.schedule_background(
            "scheduler",
            lambda: self._scheduler_loop(stop_event),
        )

    async def _stop_scheduler(self) -> None:
        self._scheduler_enabled = False
        self._scheduler_stop.set()
        self._scheduler_wakeup.set()
        task = self._background_tasks.get("scheduler")
        if task is None:
            self._scheduler_busy = False
            return
        if not task.done() and not self._scheduler_busy:
            task.cancel()
        try:
            # A busy service iteration owns a durable commit/rollback boundary.
            # Even repeated cancellation of disable/shutdown must not detach it.
            await _settle_scheduler_task(task)
        finally:
            if self._background_tasks.get("scheduler") is task:
                self._background_tasks.pop("scheduler", None)
            self._scheduler_busy = False

    async def _scheduler_loop(self, stop_event: asyncio.Event) -> None:
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
                    self._scheduler_enabled = False
                    stop_event.set()
                    break
                if self._service is not None:
                    self._scheduler_busy = True
                    try:
                        await self._service.process_due_sessions()
                    finally:
                        self._scheduler_busy = False
                if stop_event.is_set():
                    break
                try:
                    await asyncio.wait_for(self._scheduler_wakeup.wait(), timeout=60.0)
                    self._scheduler_wakeup.clear()
                except TimeoutError:
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("group_activity.scheduler_error", error=str(exc))
                if stop_event.is_set():
                    break
                await asyncio.sleep(30)


plugin = GroupActivityPlugin()
