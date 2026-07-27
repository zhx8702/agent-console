"""
Credits / interaction plugin.

Migrated from wx-bot's interaction system. Provides:
- Per-tenant, per-session credit balances
- Cost-per-chat deduction before LLM calls
- Daily check-in rewards with streak bonuses
- Credit transfer between users
- Admin grant/adjust API
- Pipeline hook: blocks reply if user has insufficient balance
"""
from __future__ import annotations

import asyncio

from app.common.logging import get_logger
from app.orchestrator.flow import FlowStepDefinition
from app.plugin.base import Plugin, PluginContext, PluginMeta
from plugins.credits.billing import CreditsBillingProvider
from plugins.credits.hooks import (
    CreditAuditEffectHandler,
    CreditAutoCheckinHook,
    CreditDeductionHook,
    CreditNaturalLanguageHook,
    CreditQueryCommandStep,
    CreditReserveStep,
    CreditSettlementEffectHandler,
    CreditSettlementHook,
    CreditSettleStep,
    build_credit_command_definitions,
)
from plugins.credits.router import build_credits_router
from plugins.credits.store import CreditStore

logger = get_logger(__name__)


class CreditsPlugin(Plugin):
    meta = PluginMeta(
        name="credits",
        version="0.1.0",
        description="Credit balance, check-in rewards, and per-chat cost deduction",
    )

    def __init__(self) -> None:
        self._store: CreditStore | None = None
        self._ctx: PluginContext | None = None
        self._effect_handler_enabled = False

    async def initialize(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._store = CreditStore(ctx.settings)
        self._effect_handler_enabled = any(
            bool(getattr(ctx.settings, name, False))
            for name in (
                "orchestrator_flow_effect_handler_enabled",
                "orchestrator_flow_effect_handlers_enabled",
                "orchestrator_flow_effect_dispatch_enabled",
            )
        )
        await self._store.ensure_tables()
        billing = getattr(ctx.container, "billing", None)
        if billing is not None:
            provider = CreditsBillingProvider(
                self._store,
                scope_execution_allowed=self._scope_execution_allowed,
            )
            billing.register_provider(
                provider,
                owner=self.meta.name,
                scope_execution_allowed=self._scope_execution_allowed,
            )
        self._register_commands()

    async def _scope_execution_allowed(
        self,
        tenant_id: str,
        session_id: str = "",
    ) -> bool:
        registry = getattr(self._ctx.container, "plugin_registry", None) if self._ctx else None
        gate = getattr(registry, "scope_execution_allowed", None)
        if not callable(gate):
            logger.error(
                "credits.scope_execution_gate_missing",
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
            logger.warning(
                "credits.scope_execution_gate_error",
                tenant_id=tenant_id,
                session_id=session_id,
                error_type=exc.__class__.__name__,
            )
            return False

    async def shutdown(self) -> None:
        self._store = None
        self._ctx = None
        self._effect_handler_enabled = False

    async def on_enable(self, scope=None) -> None:
        _ = scope
        self._register_commands()

    def _register_commands(self) -> None:
        if self._store is None or self._ctx is None:
            return
        registry = getattr(self._ctx.container, "plugin_registry", None)
        commands_plugin = registry.loaded_plugins.get("commands") if registry is not None else None
        register = getattr(commands_plugin, "register_definitions", None)
        if callable(register):
            register(build_credit_command_definitions(self._store), owner=self.meta.name)
        else:
            logger.warning("credits.command_center_unavailable")

    def get_api_router(self):
        if self._store is None:
            return None
        return build_credits_router(self._store)

    def get_pipeline_hooks(self):
        if self._store is None:
            return []
        return [
            CreditAutoCheckinHook(self._store),
            CreditNaturalLanguageHook(self._store),
            CreditDeductionHook(self._store),
            CreditSettlementHook(self._store),
        ]

    def get_flow_steps(self) -> list[FlowStepDefinition]:
        return [
            FlowStepDefinition(
                kind="plugin.credits.query_command",
                owner=self.meta.name,
                name="Credit query command",
                permissions=["storage:shared"],
                inputs={"event", "session", "pre", "route"},
                outputs={"signals.credits.query", "result", "effects.auto_checkin"},
                timeout_seconds=1.5,
                error_policy="fail_closed",
            ),
            FlowStepDefinition(
                kind="plugin.credits.reserve",
                owner=self.meta.name,
                name="Reserve credits",
                permissions=["storage:shared"],
                inputs={"event", "session", "pre", "route"},
                outputs={"signals.billing.reservation", "effects.reserve_credits"},
                timeout_seconds=2.0,
                error_policy="fail_closed",
            ),
            FlowStepDefinition(
                kind="plugin.credits.settle",
                owner=self.meta.name,
                name="Settle credits",
                permissions=["storage:shared"],
                inputs={"event", "session", "route", "result", "signals.billing.reservation"},
                outputs={"effects.capture_credits", "effects.release_credits"},
                timeout_seconds=2.0,
                error_policy="fail_closed",
            ),
        ]

    def get_flow_executors(self):
        if self._store is None:
            return {}
        return {
            "plugin.credits.query_command": CreditQueryCommandStep(self._store),
            "plugin.credits.reserve": CreditReserveStep(self._store),
            "plugin.credits.settle": CreditSettleStep(
                self._store,
                effect_handler_enabled=self._effect_handler_enabled,
            ),
        }

    def get_effect_handlers(self):
        if self._store is None:
            return []
        audit_handler = CreditAuditEffectHandler()
        settlement_handler = CreditSettlementEffectHandler(self._store)
        return [
            ("auto_checkin", self.meta.name, audit_handler),
            ("reserve_credits", self.meta.name, audit_handler),
            ("capture_credits", self.meta.name, settlement_handler),
            ("release_credits", self.meta.name, settlement_handler),
        ]

    def get_permissions(self) -> list[str]:
        return ["storage:shared", "commands", "hooks:pipeline", "admin_api"]

    def get_admin_ui(self) -> dict[str, object]:
        return {
            "scope": "group",
            "label": "群积分",
            "summary": "按群启用积分、签到与结算能力。",
        }

plugin = CreditsPlugin()
