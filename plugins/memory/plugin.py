"""
User memory plugin.

Maintains per-user short-term and long-term memory keyed by
tenant/channel/source/user_id so channel adapters like wx-bot can preserve
continuity by wxid even inside shared group sessions.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping

from app.common.logging import get_logger
from app.infra.db import get_session_factory
from app.infra.metrics import MEMORY_GOVERNANCE_EVENTS
from app.orchestrator.effect_handlers import MemorySaveEffectHandler
from app.orchestrator.flow import FlowStepDefinition
from app.plugin.base import Plugin, PluginContext, PluginMeta
from app.social.effects import MemberMemoryForgetEffectHandler
from app.social.store import SocialPolicyStore
from plugins.memory.hooks import (
    MemoryContextHook,
    MemoryControlHook,
    MemoryControlStep,
    MemoryLoadStep,
    MemoryPersistenceHook,
    MemorySaveStep,
)
from plugins.memory.router import build_memory_router
from plugins.memory.store import MemoryStore

logger = get_logger(__name__)


class MemoryPlugin(Plugin):
    meta = PluginMeta(
        name="memory",
        version="0.1.0",
        description="Per-user short-term and long-term memory keyed by channel/source/wxid",
    )

    def __init__(self) -> None:
        self._store: MemoryStore | None = None
        self._ctx: PluginContext | None = None
        self._effect_handler_enabled = False
        self._governance_task: asyncio.Task[None] | None = None

    async def initialize(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._store = MemoryStore(
            ctx.settings,
            llm_service=getattr(ctx.container, "llm_service", None),
            vector_store=getattr(ctx.container, "vector_store", None),
        )
        self._store.runtime_scope_gates_required = True
        self._store.scope_execution_allowed = self._scope_execution_allowed
        self._store.history_scope_execution_allowed = self._wxbot_scope_execution_allowed
        self._store.combined_history_scope_execution_allowed = (
            self._memory_wxbot_scope_execution_allowed
        )
        self._effect_handler_enabled = any(
            bool(getattr(ctx.settings, name, False))
            for name in (
                "orchestrator_flow_effect_handler_enabled",
                "orchestrator_flow_effect_handlers_enabled",
                "orchestrator_flow_effect_dispatch_enabled",
            )
        )
        await self._store.ensure_tables()
        if bool(getattr(ctx.settings, "memory_governance_auto_cleanup_enabled", True)):
            self._governance_task = asyncio.create_task(
                self._governance_loop(),
                name="memory-governance-cleanup",
            )

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

    async def _wxbot_scope_execution_allowed(
        self,
        tenant_id: str,
        session_id: str = "",
    ) -> bool:
        return await self._owner_scope_execution_allowed(
            "wxbot",
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
        registry = getattr(self._ctx.container, "plugin_registry", None) if self._ctx else None
        gate = getattr(registry, "scope_execution_allowed", None)
        if not callable(gate):
            return False
        try:
            return (
                await gate(
                    owner,
                    tenant_id=str(tenant_id or ""),
                    session_id=str(session_id or ""),
                )
                is True
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def _memory_wxbot_scope_execution_allowed(
        self,
        tenant_id: str,
        session_id: str = "",
    ) -> bool:
        registry = getattr(self._ctx.container, "plugin_registry", None) if self._ctx else None
        gate = getattr(registry, "owners_scope_execution_allowed", None)
        if not callable(gate):
            return False
        try:
            return (
                await gate(
                    (self.meta.name, "wxbot"),
                    tenant_id=str(tenant_id or ""),
                    session_id=str(session_id or ""),
                )
                is True
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def shutdown(self) -> None:
        if self._governance_task is not None:
            self._governance_task.cancel()
            await asyncio.gather(self._governance_task, return_exceptions=True)
            self._governance_task = None
        self._store = None
        self._ctx = None
        self._effect_handler_enabled = False

    async def _governance_loop(self) -> None:
        while self._store is not None and self._ctx is not None:
            try:
                await self._store.run_governance_cleanup(dry_run=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The next interval retries; startup and message handling must
                # remain available when a maintenance pass fails.
                MEMORY_GOVERNANCE_EVENTS.labels(
                    action="cleanup", result="failure"
                ).inc()
                logger.exception(
                    "memory.governance_cleanup_failed",
                    error_type=exc.__class__.__name__,
                )
            interval = float(
                getattr(self._ctx.settings, "memory_governance_interval_seconds", 86_400.0)
                or 86_400.0
            )
            await asyncio.sleep(max(60.0, interval))

    def get_api_router(self):
        if self._store is None:
            return None
        return build_memory_router(
            self._store,
            profile_report_builder=self._profile_report_builder(),
            scope_execution_allowed=self._scope_execution_allowed,
            history_scope_execution_allowed=self._wxbot_scope_execution_allowed,
            combined_scope_execution_allowed=self._memory_wxbot_scope_execution_allowed,
            group_membership_authorizer=self._group_membership_authorizer(),
        )

    def _profile_report_builder(self):
        if self._ctx is None:
            return None

        async def build(session, arguments):
            tenant_id = str(getattr(session, "tenant_id", "") or "").strip()
            session_id = str(getattr(session, "session_id", "") or "").strip()
            if not await self._memory_wxbot_scope_execution_allowed(tenant_id, session_id):
                raise RuntimeError("memory/wxbot plugin runtime disabled for profile report")

            # Resolve the provider at call time.  Capturing a bound method here
            # would keep a stopped wxbot service reachable after lifecycle
            # teardown and bypass its current owner state.
            registry = getattr(self._ctx.container, "plugin_registry", None) if self._ctx else None
            plugins = getattr(registry, "loaded_plugins", {}) if registry is not None else {}
            wxbot_plugin = plugins.get("wxbot") if isinstance(plugins, Mapping) else None
            get_builder = getattr(wxbot_plugin, "get_profile_report_builder", None)
            builder = get_builder() if callable(get_builder) else None
            if not callable(builder):
                raise RuntimeError("profile report builder unavailable")
            report = await builder(session, arguments)

            if not await self._memory_wxbot_scope_execution_allowed(tenant_id, session_id):
                raise RuntimeError("memory/wxbot plugin runtime disabled for profile report")
            return report

        return build

    def _group_membership_authorizer(self):
        if self._ctx is None:
            return None

        async def authorize(tenant_id: str, session_id: str, user_id: str) -> bool:
            if not await self._memory_wxbot_scope_execution_allowed(tenant_id, session_id):
                return False

            # Resolve the provider for every call so a stopped or reloaded
            # wxbot plugin cannot remain reachable through a captured store
            # method.
            registry = getattr(self._ctx.container, "plugin_registry", None) if self._ctx else None
            plugins = getattr(registry, "loaded_plugins", {}) if registry is not None else {}
            wxbot_plugin = plugins.get("wxbot") if isinstance(plugins, Mapping) else None
            get_authorizer = getattr(wxbot_plugin, "get_group_membership_authorizer", None)
            authorizer = get_authorizer() if callable(get_authorizer) else None
            if not callable(authorizer):
                return False
            try:
                allowed = await authorizer(
                    str(tenant_id or ""),
                    str(session_id or ""),
                    str(user_id or ""),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                return False
            if allowed is not True:
                return False
            return await self._memory_wxbot_scope_execution_allowed(tenant_id, session_id)

        return authorize

    def get_pipeline_hooks(self):
        if self._store is None:
            return []
        return [
            MemoryControlHook(self._store),
            MemoryContextHook(self._store),
            MemoryPersistenceHook(self._store),
        ]

    def get_flow_steps(self) -> list[FlowStepDefinition]:
        return [
            FlowStepDefinition(
                kind="plugin.memory.control_intents",
                owner=self.meta.name,
                name="Handle memory control intents",
                permissions=["storage:shared"],
                inputs={"event", "session", "pre"},
                outputs={"signals.memory_control", "result"},
                timeout_seconds=2.0,
                error_policy="fail_closed",
                optional=True,
            ),
            FlowStepDefinition(
                kind="plugin.memory.load",
                owner=self.meta.name,
                name="Load user memory",
                permissions=["storage:shared"],
                inputs={"event", "session", "pre", "route"},
                outputs={"signals.memory.user_profile"},
                timeout_seconds=3.5,
                error_policy="fail_open",
            ),
            FlowStepDefinition(
                kind="plugin.memory.save",
                owner=self.meta.name,
                name="Save user memory",
                permissions=["storage:shared"],
                inputs={"event", "session", "pre", "reply"},
                outputs={"effects.save_memory"},
                timeout_seconds=2.0,
                error_policy="fail_open",
            ),
        ]

    def get_flow_executors(self):
        if self._store is None:
            return {}
        return {
            "plugin.memory.control_intents": MemoryControlStep(self._store),
            "plugin.memory.load": MemoryLoadStep(self._store),
            "plugin.memory.save": MemorySaveStep(
                self._store,
                effect_handler_enabled=self._effect_handler_enabled,
            ),
        }

    def get_effect_handlers(self):
        if self._store is None:
            return []
        return [
            ("save_memory", self.meta.name, MemorySaveEffectHandler(self._store)),
            (
                "forget_member",
                self.meta.name,
                MemberMemoryForgetEffectHandler(
                    self._store,
                    SocialPolicyStore(get_session_factory()),
                ),
            ),
        ]

    async def drain_extraction_jobs(
        self,
        *,
        limit: int | None = None,
        worker_id: str | None = None,
        scope_allowlist: str | None = None,
        scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None = None,
    ) -> dict[str, int]:
        if self._store is None:
            return {"claimed": 0, "succeeded": 0, "failed": 0, "dead": 0}
        return await self._store.drain_llm_extraction_jobs(
            limit=limit,
            worker_id=worker_id,
            scope_allowlist=scope_allowlist,
            scope_execution_allowed=scope_execution_allowed,
        )

    def get_permissions(self) -> list[str]:
        return ["network:wxbot", "storage:shared", "hooks:pipeline", "admin_api"]


plugin = MemoryPlugin()
