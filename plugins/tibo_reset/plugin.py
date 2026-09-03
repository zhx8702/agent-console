from __future__ import annotations

import asyncio
from typing import Any

from app.common.logging import get_logger
from app.orchestrator.flow import FlowStepDefinition, StepResult
from app.orchestrator.pipeline import PipelineContext
from app.plugin.base import Plugin, PluginContext, PluginMeta
from app.plugin.hooks import HookPoint
from plugins.tibo_reset.client import TiboResetClient
from plugins.tibo_reset.hooks import TiboResetIntentHook, TiboResetIntentStep
from plugins.tibo_reset.router import build_tibo_reset_router
from plugins.tibo_reset.service import TiboResetService
from plugins.tibo_reset.store import TiboResetStore
from plugins.wxbot.channel import WxbotChannelOutbound
from plugins.wxbot.store import WxbotStore

logger = get_logger(__name__)


class _DisabledTiboResetIntentHook:
    name = "tibo_reset.intent"
    point = HookPoint.BEFORE_ROUTE
    priority = 18

    async def run(self, ctx: PipelineContext) -> None:
        _ = ctx


class _DisabledTiboResetIntentStep:
    kind = "plugin.tibo_reset.intent"
    owner = "tibo_reset"

    async def run(self, ctx: PipelineContext) -> StepResult:
        _ = ctx
        return StepResult(reason="tibo_reset_disabled")


class _DisabledTiboResetService:
    """Administrative read facade that never performs external polling."""

    def __init__(self, store: TiboResetStore) -> None:
        self._store = store

    async def status(self) -> dict[str, Any]:
        return {"configured_enabled": False, "running": False}

    async def stats(self, *, timezone_name: str | None = None) -> dict[str, Any]:
        return await self._store.reset_stats(timezone_name=timezone_name)

    async def poll_once(self) -> dict[str, Any]:
        return {"status": "disabled"}


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


class TiboResetPlugin(Plugin):
    meta = PluginMeta(
        name="tibo_reset",
        version="0.2.3",
        description="Persist confirmed Codex reset history, answer group queries, and forward new posts",
        dependencies=["wxbot>=0.2.0"],
    )

    def __init__(self) -> None:
        self._ctx: PluginContext | None = None
        self._store: TiboResetStore | None = None
        self._client: TiboResetClient | None = None
        self._service: TiboResetService | None = None
        self._scheduler_task: asyncio.Task[None] | None = None
        self._scheduler_enabled = False
        self._scheduler_busy = False
        self._scheduler_wakeup = asyncio.Event()
        self._scheduler_stop = asyncio.Event()
        self._execution_gate_confirmed = False
        self._execution_gate_grace_deadline = 0.0
        self._lifecycle_lock = asyncio.Lock()
        # Directly assembled instances keep historical lifecycle behavior.
        # initialize() treats plugin_state as the run switch and the API URL
        # as configuration, not a second enablement lock.
        self._configured_enabled = True
        self._api_url_configured = False

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
        api_url = str(getattr(ctx.settings, "tibo_reset_api_url", "") or "").strip()
        self._api_url_configured = bool(api_url)
        self._configured_enabled = self._api_url_configured
        self._store = TiboResetStore(ctx.settings)
        if ctx.db_ok:
            await self._store.ensure_tables()
        if not self._api_url_configured:
            self._service = _DisabledTiboResetService(self._store)  # type: ignore[assignment]
            return
        self._client = TiboResetClient(
            api_url,
            timeout_seconds=float(
                getattr(ctx.settings, "tibo_reset_request_timeout_seconds", 15.0)
            ),
        )
        plugin_registry = getattr(ctx.container, "plugin_registry", None)
        scope_execution_allowed = getattr(
            plugin_registry,
            "scope_execution_allowed",
            None,
        )
        self._service = TiboResetService(
            store=self._store,
            client=self._client,
            outbound=WxbotChannelOutbound(WxbotStore(ctx.settings)),
            scope_execution_allowed=(
                scope_execution_allowed
                if callable(scope_execution_allowed)
                else None
            ),
            channel_registry=getattr(ctx.container, "channel_registry", None),
            connection_id=str(
                getattr(ctx.settings, "channel_connection_id", "") or ""
            ),
        )
        if ctx.db_ok and self._owns_scheduler_role():
            self._scheduler_enabled = True
            await self._start_scheduler()

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            await self._stop_scheduler()
            if self._client is not None:
                await self._client.close()
            self._service = None
            self._client = None
            self._store = None
            self._ctx = None
            self._configured_enabled = False
            self._api_url_configured = False
            self._scheduler_wakeup = asyncio.Event()
            self._scheduler_stop = asyncio.Event()

    async def on_enable(self, scope=None) -> None:
        _ = scope
        async with self._lifecycle_lock:
            if (
                self._ctx is not None
                and self._configured_enabled
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
        if self._store is None or self._service is None or self._ctx is None:
            return None
        return build_tibo_reset_router(self._store, self._service, self._ctx.settings)

    def get_pipeline_hooks(self):
        if self._store is None:
            return []
        if not self._configured_enabled:
            return [_DisabledTiboResetIntentHook()]
        return [TiboResetIntentHook(self._store)]

    def get_flow_steps(self) -> list[FlowStepDefinition]:
        return [
            FlowStepDefinition(
                kind="plugin.tibo_reset.intent",
                owner=self.meta.name,
                name="Answer Tibo reset questions",
                permissions=["storage:plugin", "hooks:pipeline"],
                inputs={"event", "session", "pre"},
                outputs={"signals.tibo_reset", "result"},
                timeout_seconds=2.0,
                error_policy="fail_open",
            )
        ]

    def get_flow_executors(self):
        if self._store is None or not self._configured_enabled:
            return {
                "plugin.tibo_reset.intent": _DisabledTiboResetIntentStep(),
            }
        return {
            "plugin.tibo_reset.intent": TiboResetIntentStep(self._store),
        }

    def get_permissions(self) -> list[str]:
        return ["network:tibo-reset", "storage:plugin", "hooks:pipeline", "admin_api"]

    def get_config_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_name": {
                    "type": "string",
                    "maxLength": 128,
                    "description": "Display name for the target WeChat group",
                }
            },
            "additionalProperties": False,
        }

    def get_admin_ui(self) -> dict[str, Any]:
        return {
            "scope": "group",
            "label": "Codex Tibo 重置提醒",
            "summary": (
                "按群启用后会保留已确认的重置记录、回答群里的相关提问，并原样转发新确认条目。"
                "缺少接口地址时显示未配置，不会当成未安装。"
            ),
        }

    async def get_runtime_status(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "configured_enabled": self._configured_enabled,
            "api_url_configured": self._api_url_configured,
            "running": bool(self._scheduler_task and not self._scheduler_task.done()),
            "scheduler_enabled": self._scheduler_enabled,
            "poll_interval_seconds": self._poll_interval(),
        }
        if self._store is not None and self._ctx is not None and self._ctx.db_ok:
            try:
                payload.update(await self._store.runtime_status())
                payload["stats"] = await self._store.reset_stats()
            except Exception as exc:
                payload["status_error"] = str(exc)
        return payload

    async def _start_scheduler(self) -> None:
        existing = self._scheduler_task
        if existing is not None and not existing.done():
            if not self._scheduler_stop.is_set():
                return
            try:
                await _settle_scheduler_task(existing)
            except asyncio.CancelledError:
                self._scheduler_enabled = False
                raise
            finally:
                if existing.done() and self._scheduler_task is existing:
                    self._scheduler_task = None
        self._scheduler_stop = asyncio.Event()
        self._execution_gate_confirmed = False
        self._execution_gate_grace_deadline = 0.0
        stop_event = self._scheduler_stop
        self._scheduler_task = asyncio.create_task(
            self._scheduler_loop(stop_event),
            name="tibo-reset-scheduler",
        )

    async def _stop_scheduler(self) -> None:
        self._scheduler_enabled = False
        self._scheduler_stop.set()
        self._scheduler_wakeup.set()
        task = self._scheduler_task
        if task is None:
            self._scheduler_busy = False
            return
        if not task.done() and not self._scheduler_busy:
            task.cancel()
        try:
            # poll_once owns durable cursor/feed updates. Even repeated
            # cancellation of disable/shutdown must not detach that boundary.
            await _settle_scheduler_task(task)
        finally:
            if self._scheduler_task is task:
                self._scheduler_task = None
            self._scheduler_busy = False

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
            logger.warning("tibo_reset.execution_gate_error", error=str(exc))
            return False

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
                        await self._service.poll_once()
                    finally:
                        self._scheduler_busy = False
                if stop_event.is_set():
                    break
                try:
                    await asyncio.wait_for(
                        self._scheduler_wakeup.wait(),
                        timeout=self._poll_interval(),
                    )
                    self._scheduler_wakeup.clear()
                except TimeoutError:
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("tibo_reset.scheduler_error", error=str(exc))
                if stop_event.is_set():
                    break
                await asyncio.sleep(min(60.0, self._poll_interval()))

    def _poll_interval(self) -> float:
        settings = self._ctx.settings if self._ctx is not None else None
        return max(
            30.0,
            float(getattr(settings, "tibo_reset_poll_interval_seconds", 300.0) or 300.0),
        )


plugin = TiboResetPlugin()
