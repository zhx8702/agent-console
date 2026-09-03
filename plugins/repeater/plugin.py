"""
Repeater plugin.

Per-session opt-in repeater for group chats. When enabled, two consecutive
identical text messages will trigger one mirrored reply, with cooldown-based
dedupe to avoid repeated spam.
"""
from __future__ import annotations

import asyncio

from app.common.logging import get_logger
from app.orchestrator.flow import FlowStepDefinition
from app.plugin.base import Plugin, PluginContext, PluginMeta
from plugins.repeater.hooks import (
    RepeaterDetectStep,
    RepeaterHook,
    RepeaterTriggerEffectHandler,
)
from plugins.repeater.router import build_repeater_router
from plugins.repeater.store import RepeaterStore

logger = get_logger(__name__)


class RepeaterPlugin(Plugin):
    meta = PluginMeta(
        name="repeater",
        version="0.1.0",
        description="Per-session repeater with cooldown dedupe for group chats",
    )

    def __init__(self) -> None:
        self._ctx: PluginContext | None = None
        self._store: RepeaterStore | None = None
        self._effect_handler_enabled = False

    async def initialize(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._store = RepeaterStore(ctx.settings)
        self._effect_handler_enabled = any(
            bool(getattr(ctx.settings, name, False))
            for name in (
                "orchestrator_flow_effect_handler_enabled",
                "orchestrator_flow_effect_handlers_enabled",
                "orchestrator_flow_effect_dispatch_enabled",
            )
        )
        await self._store.ensure_tables()

    async def shutdown(self) -> None:
        self._ctx = None
        self._store = None
        self._effect_handler_enabled = False

    async def _scope_execution_allowed(
        self,
        tenant_id: str,
        session_id: str = "",
    ) -> bool:
        registry = getattr(self._ctx.container, "plugin_registry", None) if self._ctx else None
        gate = getattr(registry, "scope_execution_allowed", None)
        if not callable(gate):
            logger.error(
                "repeater.scope_execution_gate_missing",
                tenant_id=tenant_id,
                session_id=session_id,
            )
            return False
        try:
            return (
                await gate(
                    self.meta.name,
                    tenant_id=str(tenant_id or "").strip(),
                    session_id=str(session_id or "").strip(),
                )
                is True
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("repeater.scope_execution_gate_error", error=str(exc))
            return False

    def get_api_router(self):
        if self._store is None:
            return None
        return build_repeater_router(self._store)

    def get_pipeline_hooks(self):
        if self._store is None:
            return []
        return [
            RepeaterHook(
                self._store,
                scope_execution_allowed=self._scope_execution_allowed,
            )
        ]

    def get_flow_steps(self) -> list[FlowStepDefinition]:
        return [
            FlowStepDefinition(
                kind="plugin.repeater.detect",
                owner=self.meta.name,
                name="Detect group repeater",
                permissions=["storage:shared"],
                inputs={"event", "session", "pre"},
                outputs={"signals.repeater", "result", "effects.record_repeater_trigger"},
                timeout_seconds=1.0,
                error_policy="fail_open",
            )
        ]

    def get_flow_executors(self):
        if self._store is None:
            return {}
        return {
            "plugin.repeater.detect": RepeaterDetectStep(
                self._store,
                effect_handler_enabled=self._effect_handler_enabled,
                scope_execution_allowed=self._scope_execution_allowed,
            )
        }

    def get_effect_handlers(self):
        if self._store is None:
            return []
        return [
            (
                "record_repeater_trigger",
                self.meta.name,
                RepeaterTriggerEffectHandler(self._store),
            )
        ]

    def get_permissions(self) -> list[str]:
        return ["storage:shared", "hooks:pipeline", "admin_api"]

    def get_admin_ui(self) -> dict[str, object]:
        return {
            "scope": "group",
            "label": "群复读",
            "summary": "按群启用复读检测与响应策略。",
        }

plugin = RepeaterPlugin()
