"""Builtin plugin that probes host grok / Codex and runs them asynchronously."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

from app.channel import ChannelRegistry
from app.common.logging import get_logger
from app.plugin.base import Plugin, PluginContext, PluginMeta
from plugins.local_agent.client import LocalAgentClient, LocalAgentClientError
from plugins.local_agent.hooks import build_local_agent_command_definitions
from plugins.local_agent.overflow import LocalAgentOverflowHook, LocalAgentOverflowRetryHook
from plugins.local_agent.probe import LocalAgentProbe
from plugins.local_agent.router import build_local_agent_router
from plugins.local_agent.store import LocalAgentStore
from plugins.local_agent.worker import drain_queued_jobs

logger = get_logger(__name__)


class LocalAgentPlugin(Plugin):
    meta = PluginMeta(
        name="local_agent",
        version="0.1.0",
        description="Probe host grok / Codex CLIs, run /grok /codex, and overflow long prompts locally",
    )

    def __init__(self) -> None:
        self._ctx: PluginContext | None = None
        self._store: LocalAgentStore | None = None
        self._client: LocalAgentClient | None = None
        self._probe: LocalAgentProbe | None = None
        self._channel_registry: ChannelRegistry | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._accept_jobs = False
        self._worker_owner = f"local-agent-{uuid.uuid4().hex[:12]}"

    async def initialize(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._store = LocalAgentStore(ctx.settings)
        try:
            self._client = LocalAgentClient(ctx.settings)
        except LocalAgentClientError as exc:
            logger.warning("local_agent.client_init_failed", error=exc.code)
            self._client = LocalAgentClient(
                SimpleNamespace(
                    local_agent_base_url="",
                    local_agent_token="",
                    local_agent_probe_timeout_seconds=5.0,
                )
            )
        self._probe = LocalAgentProbe(self._client, ctx.settings)
        self._channel_registry = getattr(ctx.container, "channel_registry", None)
        if ctx.db_ok:
            await self._store.ensure_tables()
        self._accept_jobs = True
        self._register_commands()
        self._ensure_worker()

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            self._accept_jobs = False
            await self._stop_worker()
            self._ctx = None
            self._store = None
            self._client = None
            self._probe = None
            self._channel_registry = None

    async def on_enable(self, scope=None) -> None:
        _ = scope
        self._accept_jobs = True
        self._register_commands()
        self._ensure_worker()

    async def on_disable(self, scope=None) -> None:
        _ = scope
        async with self._lifecycle_lock:
            self._accept_jobs = False
            await self._stop_worker()

    def _register_commands(self) -> None:
        if self._store is None or self._probe is None or self._ctx is None:
            return
        registry = getattr(self._ctx.container, "plugin_registry", None)
        commands_plugin = registry.loaded_plugins.get("commands") if registry is not None else None
        register = getattr(commands_plugin, "register_definitions", None)
        if callable(register):
            register(
                build_local_agent_command_definitions(
                    self._store,
                    self._probe,
                    self._channel_registry,
                    self._scope_execution_allowed,
                ),
                owner=self.meta.name,
            )
        else:
            logger.warning("local_agent.command_center_unavailable")

    def get_api_router(self):
        if self._store is None or self._probe is None:
            return None
        return build_local_agent_router(self._store, self._probe)

    def get_pipeline_hooks(self):
        if self._store is None or self._probe is None or self._ctx is None:
            return []
        return [
            LocalAgentOverflowHook(self._store, self._probe, self._ctx.settings),
            LocalAgentOverflowRetryHook(self._store, self._probe, self._ctx.settings),
        ]

    def get_permissions(self) -> list[str]:
        return [
            "commands",
            "hooks:pipeline",
            "admin_api",
            "network:local_agent",
            "storage:plugin",
        ]

    def get_config_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "overflow_enabled": {
                    "type": "boolean",
                    "default": True,
                    "description": "超长文本或上下文溢出时自动转本机 grok/Codex",
                },
                "overflow_min_chars": {
                    "type": "integer",
                    "minimum": 1000,
                    "maximum": 200000,
                    "default": 24000,
                    "description": "自动溢出的最小拼装提示词字符数",
                },
                "overflow_backend": {
                    "type": "string",
                    "enum": ["auto", "codex", "grok"],
                    "default": "auto",
                    "description": "auto 优先 Codex，不可用再走 grok",
                },
            },
            "additionalProperties": False,
        }

    def get_admin_ui(self) -> dict[str, Any]:
        return {
            "scope": "global",
            "label": "本机 CLI Agent",
            "summary": "探针发现宿主机 grok / Codex。可用 /grok /codex，超长请求也会自动转到本机。",
        }

    async def get_runtime_status(self) -> dict[str, Any]:
        if self._probe is None:
            return {"configured": False, "backends": {}}
        snapshot = await self._probe.snapshot()
        return snapshot.as_dict()

    async def _scope_execution_allowed(
        self,
        tenant_id: str,
        session_id: str = "",
    ) -> bool:
        registry = getattr(self._ctx.container, "plugin_registry", None) if self._ctx else None
        gate = getattr(registry, "scope_execution_allowed", None)
        if not callable(gate):
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
                "local_agent.scope_execution_gate_error",
                tenant_id=tenant_id,
                session_id=session_id,
                error_type=exc.__class__.__name__,
            )
            return False

    def _worker_roles(self) -> set[str]:
        if self._ctx is None:
            return set()
        raw = str(
            getattr(self._ctx.settings, "local_agent_worker_roles", "scheduler") or "scheduler"
        )
        return {item.strip().lower() for item in raw.split(",") if item.strip()}

    def _should_run_worker(self) -> bool:
        if self._ctx is None or self._store is None or self._client is None:
            return False
        if not self._accept_jobs or not self._ctx.db_ok:
            return False
        role = str(getattr(self._ctx.settings, "app_process_role", "api") or "api").strip().lower()
        return role in self._worker_roles()

    def _ensure_worker(self) -> None:
        if not self._should_run_worker():
            return
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._worker_task = asyncio.create_task(self._worker_loop(), name="local-agent-worker")

    async def _stop_worker(self) -> None:
        task = self._worker_task
        if task is None:
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if self._worker_task is task:
            self._worker_task = None

    async def _worker_loop(self) -> None:
        assert self._ctx is not None
        interval = max(
            0.5,
            float(getattr(self._ctx.settings, "local_agent_job_poll_interval_seconds", 2.0) or 2.0),
        )
        while self._accept_jobs and self._should_run_worker():
            try:
                await self.drain_queued_tasks(worker_id=self._worker_owner)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "local_agent.worker_loop_error",
                    error_type=exc.__class__.__name__,
                )
            await asyncio.sleep(interval)

    async def drain_queued_tasks(
        self,
        *,
        worker_id: str = "",
        scope_execution_allowed: Any | None = None,
    ) -> dict[str, int]:
        if self._store is None or self._client is None or self._channel_registry is None:
            return {"claimed": 0, "processed": 0, "failed": 0}
        return await drain_queued_jobs(
            store=self._store,
            client=self._client,
            channel_registry=self._channel_registry,
            worker_id=worker_id or self._worker_owner,
            settings=self._store.settings,
            scope_execution_allowed=scope_execution_allowed or self._scope_execution_allowed,
        )


plugin = LocalAgentPlugin()
