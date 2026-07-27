"""
Repeater plugin.

Per-session opt-in repeater for group chats. When enabled, two consecutive
identical text messages will trigger one mirrored reply, with cooldown-based
dedupe to avoid repeated spam.
"""
from __future__ import annotations

from app.orchestrator.flow import FlowStepDefinition
from app.plugin.base import Plugin, PluginContext, PluginMeta
from plugins.repeater.hooks import (
    RepeaterDetectStep,
    RepeaterHook,
    RepeaterTriggerEffectHandler,
)
from plugins.repeater.router import build_repeater_router
from plugins.repeater.store import RepeaterStore


class RepeaterPlugin(Plugin):
    meta = PluginMeta(
        name="repeater",
        version="0.1.0",
        description="Per-session repeater with cooldown dedupe for group chats",
    )

    def __init__(self) -> None:
        self._store: RepeaterStore | None = None
        self._effect_handler_enabled = False

    async def initialize(self, ctx: PluginContext) -> None:
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
        self._store = None
        self._effect_handler_enabled = False

    def get_api_router(self):
        if self._store is None:
            return None
        return build_repeater_router(self._store)

    def get_pipeline_hooks(self):
        if self._store is None:
            return []
        return [RepeaterHook(self._store)]

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
