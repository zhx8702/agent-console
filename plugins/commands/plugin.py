from __future__ import annotations

from app.commands import CommandDefinition, CommandRegistryService
from app.orchestrator.flow import FlowStepDefinition
from app.orchestrator.owner_gate import OwnerExecutionGate
from app.plugin.base import Plugin, PluginContext, PluginMeta
from plugins.commands.hooks import (
    CommandBillingAuditEffectHandler,
    CommandBillingSettlementEffectHandler,
    CommandCenterHook,
    CommandDispatchStep,
)
from plugins.commands.router import build_commands_router
from plugins.commands.store import CommandStore


class CommandsPlugin(Plugin):
    meta = PluginMeta(
        name="commands",
        version="0.1.0",
        description="Tenant-wide command center for intercepted slash commands",
    )

    def __init__(self) -> None:
        self._store: CommandStore | None = None
        self._billing = None
        self._service = CommandRegistryService()
        self._effect_handler_enabled = False
        self._owner_gate: OwnerExecutionGate | None = None

    async def initialize(self, ctx: PluginContext) -> None:
        self._store = CommandStore(ctx.settings)
        self._billing = getattr(ctx.container, "billing", None)
        plugin_registry = getattr(ctx.container, "plugin_registry", None)
        execution_allowed = getattr(plugin_registry, "execution_allowed", None)
        self._owner_gate = execution_allowed if callable(execution_allowed) else None
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
        self._billing = None
        self._effect_handler_enabled = False
        self._owner_gate = None

    def register_definitions(
        self, definitions: list[CommandDefinition], *, owner: str = ""
    ) -> None:
        self._service.register(definitions, owner=owner)

    def unregister_owner(self, owner: str) -> int:
        return self._service.unregister_owner(owner)

    def catalog_by_owner(self) -> dict[str, list[dict[str, object]]]:
        """Expose a read-only owner catalog for plugin contract verification."""

        result: dict[str, list[dict[str, object]]] = {}
        for item in self._service.catalog():
            owner = str(item.get("owner") or item.get("plugin_name") or "").strip()
            if owner:
                result.setdefault(owner, []).append(dict(item))
        return result

    def command_tokens_by_owner(self, owner: str) -> tuple[str, ...]:
        """Return every resolvable primary command and alias owned by a plugin."""

        tokens: set[str] = set()
        for item in self.catalog_by_owner().get(str(owner or "").strip(), []):
            command = str(item.get("command") or "").strip()
            if command:
                tokens.add(command)
            aliases = item.get("aliases")
            if isinstance(aliases, list):
                tokens.update(str(alias).strip() for alias in aliases if str(alias).strip())
        return tuple(sorted(tokens))

    def get_api_router(self):
        if self._store is None:
            return None
        return build_commands_router(self._store, self._service)

    def get_pipeline_hooks(self):
        if self._store is None:
            return []
        return [
            CommandCenterHook(
                self._store,
                self._service,
                self._billing,
                owner_gate=self._owner_gate,
            )
        ]

    def get_flow_steps(self) -> list[FlowStepDefinition]:
        return [
            FlowStepDefinition(
                kind="plugin.commands.dispatch",
                owner=self.meta.name,
                name="Command dispatch",
                permissions=["commands"],
                inputs={"event", "session", "pre"},
                outputs={
                    "signals.command",
                    "result",
                    "effects.reserve_credits",
                    "effects.capture_credits",
                    "effects.release_credits",
                },
                timeout_seconds=5.0,
                error_policy="fail_closed",
            )
        ]

    def get_flow_executors(self):
        if self._store is None:
            return {}
        return {
            "plugin.commands.dispatch": CommandDispatchStep(
                self._store,
                self._service,
                self._billing,
                effect_handler_enabled=self._effect_handler_enabled,
                owner_gate=self._owner_gate,
            )
        }

    def get_effect_handlers(self):
        if self._billing is None:
            return []
        audit_handler = CommandBillingAuditEffectHandler()
        settlement_handler = CommandBillingSettlementEffectHandler(self._billing)
        return [
            ("reserve_credits", self.meta.name, audit_handler),
            ("capture_credits", self.meta.name, settlement_handler),
            ("release_credits", self.meta.name, settlement_handler),
        ]

    def get_permissions(self) -> list[str]:
        return ["commands", "hooks:pipeline", "admin_api"]

    async def get_runtime_status(self) -> dict[str, object]:
        catalog = self._service.catalog()
        admin_commands = sum(1 for item in catalog if item.get("admin_only"))
        user_commands = sum(1 for item in catalog if not item.get("admin_only"))
        return {
            "commands": len(catalog),
            "admins": 0,
            "admin_commands": admin_commands,
            "user_commands": user_commands,
            "owners": sorted(
                {str(item.get("owner") or item.get("plugin_name") or "") for item in catalog}
            ),
        }


plugin = CommandsPlugin()
