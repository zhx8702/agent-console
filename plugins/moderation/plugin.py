"""
Content moderation plugin.

Migrated from wx-bot's moderation system. Extends cs-system's built-in
safety service with:
- Per-tenant, per-session keyword management (CRUD via admin API)
- Moderation event logging and audit trail
- Optional webhook notifications for flagged messages
- Pipeline hook that checks inbound messages against session-specific keywords
"""
from __future__ import annotations

import asyncio

from app.orchestrator.flow import FlowStepDefinition
from app.plugin.base import Plugin, PluginContext, PluginMeta
from plugins.moderation.hooks import (
    ModerationAppendReminderHook,
    ModerationAuditEffectHandler,
    ModerationAuditHook,
    ModerationDecorateOutputStep,
    ModerationEnforceInputStep,
    ModerationInspectInputStep,
    ModerationReplaceReminderHook,
)
from plugins.moderation.router import build_moderation_router
from plugins.moderation.store import ModerationStore


class ModerationPlugin(Plugin):
    meta = PluginMeta(
        name="moderation",
        version="0.1.0",
        description="Per-session keyword moderation, audit logging, and webhook alerts",
    )

    def __init__(self) -> None:
        self._store: ModerationStore | None = None
        self._ctx: PluginContext | None = None
        self._effect_handler_enabled = False

    async def initialize(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._store = ModerationStore(ctx.settings)
        self._store.scope_execution_allowed = self._scope_execution_allowed
        self._effect_handler_enabled = any(
            bool(getattr(ctx.settings, name, False))
            for name in (
                "orchestrator_flow_effect_handler_enabled",
                "orchestrator_flow_effect_handlers_enabled",
                "orchestrator_flow_effect_dispatch_enabled",
            )
        )
        await self._store.ensure_tables()

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
        except Exception:
            return False

    async def shutdown(self) -> None:
        self._store = None
        self._ctx = None
        self._effect_handler_enabled = False

    def get_api_router(self):
        if self._store is None:
            return None
        return build_moderation_router(self._store)

    def get_pipeline_hooks(self):
        if self._store is None:
            return []
        return [
            ModerationAuditHook(self._store),
            ModerationReplaceReminderHook(self._store),
            ModerationAppendReminderHook(self._store),
        ]

    def get_flow_steps(self) -> list[FlowStepDefinition]:
        return [
            FlowStepDefinition(
                kind="plugin.moderation.inspect_input",
                owner=self.meta.name,
                name="Inspect input moderation",
                permissions=["storage:shared"],
                inputs={"event", "session", "pre"},
                outputs={"signals.moderation.input", "effects.write_audit_event"},
                timeout_seconds=1.5,
                error_policy="fail_open",
            ),
            FlowStepDefinition(
                kind="plugin.moderation.enforce_input",
                owner=self.meta.name,
                name="Enforce input moderation",
                permissions=["storage:shared"],
                inputs={"event", "session", "pre", "route", "signals.moderation.input"},
                outputs={"result"},
                timeout_seconds=1.0,
                error_policy="fail_closed",
            ),
            FlowStepDefinition(
                kind="plugin.moderation.decorate_output",
                owner=self.meta.name,
                name="Decorate moderated output",
                permissions=["storage:shared"],
                inputs={"event", "session", "result", "signals.moderation.input"},
                outputs={"result"},
                timeout_seconds=1.0,
                error_policy="fail_open",
            ),
        ]

    def get_flow_executors(self):
        if self._store is None:
            return {}
        return {
            "plugin.moderation.inspect_input": ModerationInspectInputStep(
                self._store,
                effect_handler_enabled=self._effect_handler_enabled,
            ),
            "plugin.moderation.enforce_input": ModerationEnforceInputStep(self._store),
            "plugin.moderation.decorate_output": ModerationDecorateOutputStep(self._store),
        }

    def get_effect_handlers(self):
        if self._store is None:
            return []
        return [
            ("write_audit_event", self.meta.name, ModerationAuditEffectHandler(self._store))
        ]

    def get_permissions(self) -> list[str]:
        return ["network:webhook", "storage:shared", "hooks:pipeline", "admin_api"]

    def get_admin_ui(self) -> dict[str, object]:
        return {
            "scope": "group",
            "label": "群审核",
            "summary": "按群启用内容检查、拦截和审计。",
        }

plugin = ModerationPlugin()
