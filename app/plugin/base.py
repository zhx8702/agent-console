"""
Plugin base class and metadata.

Every plugin is a Python package under ``plugins/`` (or installed via pip
with an ``cs_system.plugins`` entry-point group).  It must expose a
module-level ``plugin`` attribute that is an instance of a :class:`Plugin`
subclass.

Minimal example (``plugins/my_feature/plugin.py``)::

    from app.plugin import Plugin, PluginMeta

    class MyPlugin(Plugin):
        meta = PluginMeta(name="my_feature", version="0.1.0",
                          description="Does something useful")

        async def initialize(self, ctx):
            ...

    plugin = MyPlugin()
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

PLUGIN_API_VERSION = "0.1.0"
PLUGIN_RESERVED_NAMES = frozenset({"core", "channel"})

if TYPE_CHECKING:
    from fastapi import APIRouter

    from app.agent.registry import AgentToolDefinition
    from app.channel.adapters import ChannelAdapterRegistration
    from app.common.capability import CapabilityEngine
    from app.common.types import RouteType
    from app.orchestrator.flow import FlowStep, FlowStepDefinition
    from app.plugin.hooks import PipelineHook


@dataclass(frozen=True)
class PluginMeta:
    name: str
    version: str = "0.1.0"
    description: str = ""
    # Runtime dependencies use ``name`` or a minimum version constraint such
    # as ``name>=1.2.0``.  The registry validates the complete selected graph
    # before invoking any plugin initializer.
    dependencies: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PluginDescriptor:
    """Immutable inventory of every capability a loaded plugin contributes.

    Executable contribution getters remain factories. The registry snapshots
    their metadata after initialization and exposes only this descriptor to
    authorization, observability and marketplace consumers.
    """

    name: str
    version: str
    description: str
    dependencies: tuple[str, ...]
    permissions: tuple[str, ...]
    hooks: tuple[str, ...]
    agent_tools: tuple[str, ...]
    commands: tuple[str, ...]
    flow_steps: tuple[str, ...]
    effects: tuple[str, ...]
    admin_routes: tuple[str, ...]
    storage_permissions: tuple[str, ...]
    network_permissions: tuple[str, ...]
    channel_adapters: tuple[str, ...] = ()
    capability_engines: tuple[str, ...] = ()
    admin_media_providers: tuple[str, ...] = ()

    @classmethod
    def from_plugin(
        cls,
        plugin: Plugin,
        *,
        command_tokens: tuple[str, ...] = (),
    ) -> PluginDescriptor:
        permissions = tuple(
            sorted(
                {
                    str(item).strip()
                    for item in plugin.get_permissions()
                    if str(item).strip()
                }
            )
        )
        hooks = tuple(
            sorted(
                {
                    (
                        f"{getattr(getattr(item, 'point', ''), 'value', getattr(item, 'point', ''))}:"
                        f"{getattr(item, 'name', '')}"
                    )
                    for item in plugin.get_pipeline_hooks()
                }
            )
        )
        tools = tuple(
            sorted(
                {
                    f"{getattr(item, 'scope', '')}:{getattr(item, 'name', '')}"
                    for item in plugin.get_agent_tools()
                }
            )
        )
        router = plugin.get_api_router()
        routes: set[str] = set()
        if router is not None:
            for route in getattr(router, "routes", ()):
                path = str(getattr(route, "path", "") or "")
                for method in sorted(getattr(route, "methods", ()) or ()):
                    routes.add(f"{str(method).upper()} {path}")
        admin_media_provider = plugin.get_admin_media_event_provider()
        return cls(
            name=plugin.meta.name,
            version=plugin.meta.version,
            description=plugin.meta.description,
            dependencies=tuple(plugin.meta.dependencies),
            permissions=permissions,
            hooks=hooks,
            agent_tools=tools,
            commands=tuple(
                sorted(
                    {
                        str(item).strip()
                        for item in command_tokens
                        if str(item).strip()
                    }
                )
            ),
            flow_steps=tuple(sorted({str(item.kind) for item in plugin.get_flow_steps()})),
            effects=tuple(sorted({str(item[0]) for item in plugin.get_effect_handlers()})),
            admin_routes=tuple(sorted(routes)),
            storage_permissions=tuple(
                item for item in permissions if item.startswith("storage:")
            ),
            network_permissions=tuple(
                item for item in permissions if item.startswith("network:")
            ),
            channel_adapters=tuple(
                sorted(
                    {
                        registration.descriptor.adapter_id
                        for registration in plugin.get_channel_adapters()
                    }
                )
            ),
            capability_engines=tuple(
                sorted(
                    {
                        str(getattr(route_type, "value", route_type))
                        for route_type in plugin.get_capability_engines()
                    }
                )
            ),
            admin_media_providers=(
                (
                    str(
                        getattr(admin_media_provider, "name", "")
                        or plugin.meta.name
                    ),
                )
                if admin_media_provider is not None
                else ()
            ),
        )

    @property
    def routes(self) -> tuple[str, ...]:
        """Exact method/path pairs exposed by the initialized plugin router."""

        return self.admin_routes

    @property
    def storage(self) -> tuple[str, ...]:
        return self.storage_permissions

    @property
    def network(self) -> tuple[str, ...]:
        return self.network_permissions

    def as_capabilities(self) -> dict[str, list[str]]:
        """Return the complete, JSON-safe runtime capability contract."""

        return {
            "routes": list(self.admin_routes),
            "hooks": list(self.hooks),
            "agent_tools": list(self.agent_tools),
            "commands": list(self.commands),
            "flow_steps": list(self.flow_steps),
            "effects": list(self.effects),
            "storage": list(self.storage_permissions),
            "network": list(self.network_permissions),
            "channel_adapters": list(self.channel_adapters),
            "capability_engines": list(self.capability_engines),
            "admin_media_providers": list(self.admin_media_providers),
        }


def plugin_capability_digest(descriptor: PluginDescriptor) -> str:
    """Return the canonical digest of every executable capability identity."""

    payload = json.dumps(
        descriptor.as_capabilities(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


@dataclass(frozen=True)
class PluginScope:
    tenant_id: str = ""
    session_id: str = ""


@dataclass
class PluginContext:
    """Passed to :meth:`Plugin.initialize` so the plugin can access shared
    services without importing the concrete container."""

    container: Any
    settings: Any
    db_ok: bool = False
    redis_ok: bool = False


class Plugin(ABC):
    """Base class all plugins must inherit from."""

    meta: PluginMeta

    @abstractmethod
    async def initialize(self, ctx: PluginContext) -> None:
        """Called once during startup after the core container is assembled."""

    async def shutdown(self) -> None:  # noqa: B027
        """Called on graceful shutdown.  Override if you hold resources."""

    def get_capability_engines(self) -> dict[RouteType, CapabilityEngine]:
        """Return extra capability engines to register in the orchestrator."""
        return {}

    def get_api_router(self) -> APIRouter | None:
        """Return a FastAPI router to mount under ``/plugins/{name}/``."""
        return None

    def get_api_default_tenant_id(self) -> str:
        """Return the tenant implicitly targeted by tenantless API routes.

        Most plugins must keep this empty and declare tenant IDs explicitly.
        Legacy adapters whose API is deliberately bound to one configured
        tenant may opt in so session-only routes remain scope-gated.
        """

        return ""

    def get_pipeline_hooks(self) -> list[PipelineHook]:
        """Return hooks to inject into the orchestrator pipeline."""
        return []

    def get_agent_tools(self) -> list[AgentToolDefinition]:
        """Return Agent tools to register into the shared agent tool registry."""
        return []

    def get_flow_steps(self) -> list[FlowStepDefinition]:
        """Return message flow steps contributed by this plugin."""
        return []

    def get_flow_executors(self) -> dict[str, FlowStep]:
        """Return runtime executors keyed by message flow step kind."""
        return {}

    def get_effect_handlers(self) -> list[tuple[str, str, Any]]:
        """Return effect handlers as ``(effect_type, owner, handler)`` tuples."""
        return []

    def get_admin_media_event_provider(self) -> Any | None:
        """Return a provider for normalized admin media events."""
        return None

    def get_channel_adapters(self) -> list[ChannelAdapterRegistration]:
        """Return message-platform adapter registrations contributed by this plugin."""
        return []

    def get_config_schema(self) -> dict[str, Any]:
        """Return the bounded local JSON Schema subset for scope config.

        The contract is validated by :mod:`app.plugin.config_schema`.  Remote
        references and schema combinators are deliberately unsupported.  An
        empty object means that the plugin accepts no configurable values.
        """
        return {}

    async def get_runtime_status(self) -> dict[str, Any]:
        return {}

    def get_admin_ui(self) -> dict[str, Any]:
        return {}

    def get_permissions(self) -> list[str]:
        return []

    def build_descriptor(
        self,
        *,
        command_tokens: tuple[str, ...] = (),
    ) -> PluginDescriptor:
        return PluginDescriptor.from_plugin(self, command_tokens=command_tokens)

    async def on_enable(self, scope: PluginScope | None = None) -> None:
        _ = scope

    async def on_disable(self, scope: PluginScope | None = None) -> None:
        _ = scope
