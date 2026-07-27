"""
Plugin discovery, registration, and lifecycle management.

Discovery sources (checked in order):

1. ``plugins/`` directory next to the project root — each subdirectory that
   contains a ``plugin.py`` with a module-level ``plugin`` attribute is loaded.
2. ``cs_system.plugins`` setuptools entry-point group — so plugins can be
   distributed as pip-installable packages.
"""

from __future__ import annotations

import asyncio
import copy
import importlib
import importlib.machinery
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

from packaging.version import InvalidVersion, Version

from app.agent.registry import AgentToolRegistry
from app.channel.adapters import ChannelProbeResult
from app.common.logging import get_logger
from app.plugin.admin_ui import validate_plugin_admin_ui
from app.plugin.artifacts import compute_plugin_tree_digest
from app.plugin.base import (
    PLUGIN_RESERVED_NAMES,
    Plugin,
    PluginContext,
    PluginDescriptor,
    plugin_capability_digest,
)
from app.plugin.config_schema import validate_config_schema
from app.plugin.dependencies import (
    PluginDependencyBlockedError,
    PluginDependencyGraph,
    PluginDependencyGraphError,
    parse_plugin_dependency,
    plugin_dependency_closure,
    resolve_plugin_dependency_graph,
)
from app.plugin.hooks import HookRunner
from app.plugin.runtime import GatedCapabilityEngine
from app.plugin.state import PluginStateStore

if TYPE_CHECKING:
    from fastapi import APIRouter

    from app.common.capability import CapabilityEngine
    from app.common.types import RouteType, Session
    from app.orchestrator.flow import FlowStep, FlowStepDefinition
    from app.orchestrator.pipeline import PipelineContext

logger = get_logger(__name__)

_PLUGIN_NAME_RE = re.compile(r"^[a-z0-9_]+$")
_PLUGIN_INITIALIZE_TIMEOUT_SECONDS = 60.0
_PLUGIN_LIFECYCLE_TIMEOUT_SECONDS = 30.0
_PLUGIN_SHUTDOWN_TIMEOUT_SECONDS = 20.0
_KERNEL_EXECUTION_OWNERS = frozenset({"", "core", "channel"})
_MISSING_MODULE_ATTRIBUTE = object()


def _snapshot_module_namespace(namespace: str) -> dict[str, Any]:
    prefix = f"{namespace}."
    return {
        name: module
        for name, module in sys.modules.items()
        if name == namespace or name.startswith(prefix)
    }


def _restore_module_namespace(
    namespace: str,
    snapshot: dict[str, Any],
) -> None:
    prefix = f"{namespace}."
    for name in tuple(sys.modules):
        if name == namespace or name.startswith(prefix):
            sys.modules.pop(name, None)
    sys.modules.update(snapshot)


class PluginRegistrationError(ValueError):
    """Raised when a discovered plugin cannot own its declared identity."""


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


class _GatedAdminMediaEventProvider:
    """Last-hop scope gate for providers captured by the admin router."""

    def __init__(self, registry: PluginRegistry, owner: str, delegate: Any) -> None:
        self._registry = registry
        self._owner = owner
        self._delegate = delegate
        self.name = str(getattr(delegate, "name", "") or owner)
        self.owner = owner

    async def list_recent_media_events(
        self,
        *,
        tenant_id: str,
        limit: int = 50,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not await self._registry.scope_execution_allowed(
            self._owner,
            tenant_id=tenant_id,
            session_id=str(session_id or ""),
        ):
            return []
        return await self._delegate.list_recent_media_events(
            tenant_id=tenant_id,
            limit=limit,
            session_id=session_id,
        )

    def project_recent_message(
        self,
        item: dict[str, Any],
        tenant_id: str,
    ) -> dict[str, Any]:
        """Expose the delegate's read-only admin response sanitizer."""

        projector = getattr(self._delegate, "project_recent_message", None)
        if not callable(projector):
            return item
        return projector(item, tenant_id)


class _GatedChannelAdapterRegistration:
    """Last-hop gate for adapter factories and probes held by live catalogs."""

    def __init__(self, registry: PluginRegistry, owner: str, delegate: Any) -> None:
        self._registry = registry
        self._owner = owner
        self._delegate = delegate
        self.descriptor = delegate.descriptor

    @property
    def provider_factory(self) -> Any | None:
        """Expose the registration capability without bypassing the scope gate."""
        return self.create_provider if self._delegate.provider_factory is not None else None

    @property
    def probe(self) -> Any | None:
        """Expose the probe capability without leaking the ungated delegate."""
        return self.probe_connection if self._delegate.probe is not None else None

    async def create_provider(self, connection: Any) -> Any:
        if not await self._execution_allowed(connection):
            raise RuntimeError(
                f"channel adapter plugin runtime disabled: {self._owner}"
            )
        return await self._delegate.create_provider(connection)

    async def probe_connection(
        self,
        connection: Any,
        *,
        timeout_seconds: float = 10.0,
    ) -> ChannelProbeResult:
        if not await self._execution_allowed(connection):
            return ChannelProbeResult(
                ok=False,
                status="unavailable",
                error_code="plugin_runtime_disabled",
            )
        return await self._delegate.probe_connection(
            connection,
            timeout_seconds=timeout_seconds,
        )

    async def _execution_allowed(self, connection: Any) -> bool:
        return await self._registry.scope_execution_allowed(
            self._owner,
            tenant_id=str(getattr(connection, "tenant_id", "") or "").strip(),
            session_id="",
        )


class PluginRegistry:
    """Central registry for all loaded plugins."""

    def __init__(
        self,
        state_store: PluginStateStore | None = None,
        *,
        allow_offline_execution: bool = False,
    ) -> None:
        self._plugins: dict[str, Plugin] = {}
        self._active_plugins: set[str] = set()
        self._hook_runner = HookRunner(owner_gate=self.execution_allowed)
        self._hooks_registered: set[str] = set()
        self._agent_tools_registered: set[str] = set()
        self._initialized_plugins: set[str] = set()
        self._initialization_order: list[str] = []
        self._initialization_failures: dict[str, str] = {}
        self._dependency_graph: PluginDependencyGraph | None = None
        self._descriptors: dict[str, PluginDescriptor] = {}
        self._config_schemas: dict[str, dict[str, Any]] = {}
        self._admin_ui_documents: dict[str, dict[str, Any]] = {}
        self._discovery_provenance: dict[str, str] = {}
        self._pending_directory_plugins: dict[str, tuple[Path, str]] = {}
        self._pending_entrypoints: dict[str, tuple[Any, str]] = {}
        self._runtime_container: Any | None = None
        self._state_store = state_store
        self._allow_offline_execution = bool(allow_offline_execution)
        self._initialized = False

    # -- discovery -----------------------------------------------------------

    def discover_directory(
        self,
        plugins_dir: Path | str,
        *,
        trusted_builtin: bool = True,
    ) -> int:
        plugins_dir = Path(plugins_dir)
        if not plugins_dir.is_dir():
            logger.info("plugin.dir_not_found", path=str(plugins_dir))
            return 0

        loaded = 0
        for candidate in sorted(plugins_dir.iterdir()):
            if not candidate.is_dir():
                continue
            plugin_py = candidate / "plugin.py"
            if not plugin_py.exists():
                continue
            try:
                if not _PLUGIN_NAME_RE.fullmatch(candidate.name):
                    raise PluginRegistrationError(
                        f"invalid plugin directory name: {candidate.name!r}"
                    )
                if trusted_builtin:
                    self._load_and_register_file(
                        plugin_py,
                        candidate.name,
                        source=(
                            "builtin_directory"
                            if trusted_builtin
                            else "install_directory"
                        ),
                        source_detail=str(plugin_py.resolve()),
                        expected_name=candidate.name,
                    )
                else:
                    version = self._external_directory_version(candidate)
                    self._queue_external_discovery(
                        candidate.name,
                        self._pending_directory_plugins,
                        (plugin_py.resolve(), version),
                    )
                loaded += 1
            except Exception as exc:
                logger.error(
                    "plugin.load_failed",
                    path=str(plugin_py),
                    error=str(exc),
                )
        return loaded

    def discover_entrypoints(self) -> int:
        try:
            from importlib.metadata import entry_points
        except ImportError:
            return 0

        eps = entry_points()
        group = (
            eps.select(group="cs_system.plugins")
            if hasattr(eps, "select")
            else eps.get("cs_system.plugins", [])
        )

        loaded = 0
        for ep in sorted(group, key=lambda item: (str(item.name), str(item.value))):
            try:
                name = str(ep.name or "").strip()
                if not _PLUGIN_NAME_RE.fullmatch(name):
                    raise PluginRegistrationError(
                        f"invalid entry point plugin name: {name!r}"
                    )
                if name in PLUGIN_RESERVED_NAMES:
                    raise PluginRegistrationError(
                        f"reserved kernel owner cannot be registered as a plugin: {name!r}"
                    )
                version = str(
                    getattr(getattr(ep, "dist", None), "version", "") or ""
                ).strip()
                try:
                    Version(version)
                except InvalidVersion as exc:
                    raise PluginRegistrationError(
                        f"entry point {name!r} has no valid static distribution version"
                    ) from exc
                self._queue_external_discovery(
                    name,
                    self._pending_entrypoints,
                    (ep, version),
                )
                loaded += 1
            except Exception as exc:
                logger.error(
                    "plugin.entrypoint_failed",
                    name=ep.name,
                    error=str(exc),
                )
        return loaded

    # -- lifecycle -----------------------------------------------------------

    async def reconcile_state(self) -> None:
        if self._state_store is None:
            return
        await self._state_store.ensure_tables()
        acknowledge_uninstalled = getattr(
            self._state_store,
            "acknowledge_uninstalled_restarts",
            None,
        )
        if callable(acknowledge_uninstalled):
            await acknowledge_uninstalled()
        states = await self._state_store.reconcile(
            self._plugins,
            provenance=self._discovery_provenance,
        )
        loaded = await self._load_approved_external_plugins()
        if loaded:
            states = await self._state_store.reconcile(
                self._plugins,
                provenance=self._discovery_provenance,
            )
        self._active_plugins = {
            state.plugin_name for state in states if state.installed and state.enabled
        }

    async def initialize_all(self, ctx: PluginContext) -> None:
        self._runtime_container = ctx.container
        selected_names = await self._selected_plugin_names()
        for name in selected_names:
            self._active_plugins.discard(name)
            self._initialization_failures.pop(name, None)

        try:
            graph = resolve_plugin_dependency_graph(self._plugins, selected_names)
        except PluginDependencyGraphError as exc:
            self._dependency_graph = None
            for name, reason in exc.failures.items():
                await self._record_initialization_failure(name, reason)
            logger.error("plugin.dependency_graph_invalid", error=str(exc))
            raise

        self._dependency_graph = graph
        for name in graph.order:
            blocked_reason = self._dependency_block_reason(name, graph)
            if blocked_reason:
                await self._record_initialization_failure(name, blocked_reason)
                logger.error("plugin.init_blocked", name=name, error=blocked_reason)
                continue
            try:
                self._initialization_failures.pop(name, None)
                await self._initialize_plugin_unchecked(name, ctx)
            except Exception as exc:
                reason = str(exc) or type(exc).__name__
                await self._record_initialization_failure(name, reason)
                logger.error("plugin.init_failed", name=name, error=reason)
        self._initialized = True

    async def initialize_plugin(self, name: str, ctx: PluginContext) -> None:
        self._runtime_container = ctx.container
        if name not in self._plugins:
            raise KeyError(name)
        selected_names = plugin_dependency_closure(self._plugins, (name,))
        graph = resolve_plugin_dependency_graph(self._plugins, selected_names)
        blocked_reason = self._dependency_block_reason(name, graph)
        if blocked_reason:
            raise PluginDependencyBlockedError(blocked_reason)
        await self._initialize_plugin_unchecked(name, ctx)

    async def _initialize_plugin_unchecked(self, name: str, ctx: PluginContext) -> None:
        plugin = self._plugins[name]
        was_initialized = name in self._initialized_plugins
        try:
            if not was_initialized:
                await asyncio.wait_for(
                    plugin.initialize(ctx),
                    timeout=_PLUGIN_INITIALIZE_TIMEOUT_SECONDS,
                )
                config_schema = plugin.get_config_schema()
                admin_ui = plugin.get_admin_ui()
                validate_config_schema(config_schema)
                validate_plugin_admin_ui(admin_ui)
                self._config_schemas[name] = copy.deepcopy(config_schema)
                self._admin_ui_documents[name] = copy.deepcopy(admin_ui)
                descriptor = plugin.build_descriptor(
                    command_tokens=self._command_tokens_by_owner(name)
                )
                if descriptor.name != name:
                    raise ValueError(
                        f"plugin descriptor name mismatch: {name!r} != {descriptor.name!r}"
                    )
                await self._validate_durable_package_contract(name, descriptor)
                self._descriptors[name] = descriptor
                self._initialized_plugins.add(name)
                self._initialization_order.append(name)
            self._register_plugin_hooks(name, plugin)
            self._register_plugin_agent_tools(name, plugin, ctx.container)
            self._validate_runtime_descriptor(name, container=ctx.container)
            self._active_plugins.add(name)
            self._initialization_failures.pop(name, None)
            if self._state_store is not None:
                acknowledged = await self._state_store.mark_initialized(
                    name, plugin.meta.version
                )
                if acknowledged is False:
                    raise RuntimeError("plugin_state_changed_during_initialization")
            logger.info("plugin.initialized", name=name, version=plugin.meta.version)
        except BaseException:
            await self._rollback_failed_activation(
                name,
                plugin,
                ctx.container,
                discard_initialization=not was_initialized,
            )
            await self._cleanup_failed_activation(name, plugin)
            raise

    async def deactivate_plugin(self, name: str, container: Any) -> dict[str, int]:
        removed_hooks = self._hook_runner.unregister_owner(name)
        self._hooks_registered.discard(name)
        removed_tools = 0
        registry = getattr(container, "agent_tool_registry", None)
        if isinstance(registry, AgentToolRegistry):
            removed_tools = registry.unregister_owner(name)
        self._agent_tools_registered.discard(name)
        removed_commands = 0
        commands_plugin = self._plugins.get("commands")
        unregister = getattr(commands_plugin, "unregister_owner", None)
        if callable(unregister):
            removed_commands = int(unregister(name))
        self._active_plugins.discard(name)
        plugin = self._plugins.get(name)
        if plugin is not None:
            try:
                await asyncio.wait_for(
                    plugin.on_disable(),
                    timeout=_PLUGIN_LIFECYCLE_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # The execution gate is already closed.  Keep the plugin
                # inactive even when its best-effort resource cleanup fails.
                logger.exception("plugin.disable_cleanup_failed", name=name)
                return {
                    "hooks": removed_hooks,
                    "agent_tools": removed_tools,
                    "commands": removed_commands,
                    "cleanup_errors": 1,
                }
        return {
            "hooks": removed_hooks,
            "agent_tools": removed_tools,
            "commands": removed_commands,
            "cleanup_errors": 0,
        }

    async def reactivate_plugin(self, name: str, ctx: PluginContext) -> bool:
        self._runtime_container = ctx.container
        if name not in self._initialized_plugins:
            await self.initialize_plugin(name, ctx)
            return True
        selected_names = plugin_dependency_closure(self._plugins, (name,))
        graph = resolve_plugin_dependency_graph(self._plugins, selected_names)
        blocked_reason = self._dependency_block_reason(name, graph)
        if blocked_reason:
            raise PluginDependencyBlockedError(blocked_reason)
        plugin = self._plugins[name]
        try:
            await asyncio.wait_for(
                plugin.on_enable(),
                timeout=_PLUGIN_LIFECYCLE_TIMEOUT_SECONDS,
            )
            self._register_plugin_hooks(name, plugin)
            self._register_plugin_agent_tools(name, plugin, ctx.container)
            self._validate_runtime_descriptor(name, container=ctx.container)
            self._active_plugins.add(name)
            return False
        except BaseException:
            await self._rollback_failed_activation(
                name,
                plugin,
                ctx.container,
                discard_initialization=False,
            )
            await self._cleanup_failed_activation(name, plugin)
            raise

    async def shutdown_all(self) -> None:
        for name in reversed(self._initialization_order):
            try:
                await asyncio.wait_for(
                    self._plugins[name].shutdown(),
                    timeout=_PLUGIN_SHUTDOWN_TIMEOUT_SECONDS,
                )
                logger.info("plugin.shutdown", name=name)
            except Exception as exc:
                logger.error("plugin.shutdown_failed", name=name, error=str(exc))

    # -- accessors -----------------------------------------------------------

    @property
    def hook_runner(self) -> HookRunner:
        return self._hook_runner

    def all_capability_engines(self) -> dict[RouteType, CapabilityEngine]:
        merged: dict[RouteType, CapabilityEngine] = {}
        owners: dict[RouteType, str] = {}
        for name, plugin in self._plugins.items():
            if name in self._active_plugins:
                engines = plugin.get_capability_engines()
                self._assert_descriptor_values(
                    name,
                    "capability_engines",
                    {
                        str(getattr(route_type, "value", route_type))
                        for route_type in engines
                    },
                )
                for route_type, engine in engines.items():
                    previous_owner = owners.get(route_type)
                    if previous_owner is not None:
                        raise RuntimeError(
                            "duplicate plugin capability engine: "
                            f"{route_type} ({previous_owner}, {name})"
                        )
                    owners[route_type] = name
                    merged[route_type] = GatedCapabilityEngine(
                        name,
                        engine,
                        self.session_execution_allowed,
                    )
        return merged

    def all_api_routers(self) -> list[tuple[str, APIRouter]]:
        result = []
        for name, plugin in self._plugins.items():
            if name not in self._active_plugins:
                continue
            router = plugin.get_api_router()
            routes: set[str] = set()
            if router is not None:
                for route in getattr(router, "routes", ()):
                    path = str(getattr(route, "path", "") or "")
                    for method in sorted(getattr(route, "methods", ()) or ()):
                        routes.add(f"{str(method).upper()} {path}")
            self._assert_descriptor_values(name, "admin_routes", routes)
            if router is not None:
                result.append((name, router))
        return result

    def all_flow_steps(self) -> list[FlowStepDefinition]:
        result: list[FlowStepDefinition] = []
        seen: dict[str, str] = {}
        for name, plugin in self._plugins.items():
            if name not in self._active_plugins:
                continue
            steps = plugin.get_flow_steps()
            self._assert_descriptor_values(
                name,
                "flow_steps",
                {str(item.kind) for item in steps},
            )
            for step in steps:
                kind = str(step.kind or "").strip()
                owner = str(step.owner or "").strip()
                if owner != name:
                    raise RuntimeError(
                        "plugin flow step owner mismatch: "
                        f"{kind or '<empty>'} expected={name!r} actual={owner!r}"
                    )
                previous_owner = seen.get(kind)
                if previous_owner is not None:
                    raise RuntimeError(
                        "duplicate plugin flow step: "
                        f"{kind} ({previous_owner}, {name})"
                    )
                seen[kind] = name
            result.extend(steps)
        return result

    def all_flow_executors(self) -> dict[str, FlowStep]:
        result: dict[str, FlowStep] = {}
        owners: dict[str, str] = {}
        for name, plugin in self._plugins.items():
            if name not in self._active_plugins:
                continue
            executors = plugin.get_flow_executors()
            self._assert_descriptor_values(
                name,
                "flow_steps",
                {str(kind) for kind in executors},
            )
            for kind, executor in executors.items():
                if kind in result:
                    raise RuntimeError(
                        "duplicate plugin flow executor: "
                        f"{kind} ({owners[kind]}, {name})"
                    )
                executor_owner = str(getattr(executor, "owner", name) or "").strip()
                if executor_owner != name:
                    raise RuntimeError(
                        "plugin flow executor owner mismatch: "
                        f"{kind} expected={name!r} actual={executor_owner!r}"
                    )
                result[kind] = executor
                owners[kind] = name
        return result

    def all_effect_handlers(self) -> list[tuple[str, str, Any]]:
        result: list[tuple[str, str, Any]] = []
        for name, plugin in self._plugins.items():
            if name not in self._active_plugins:
                continue
            handlers = plugin.get_effect_handlers()
            invalid_owners = sorted(
                {
                    str(item[1])
                    for item in handlers
                    if str(item[1]).strip() != name
                }
            )
            if invalid_owners:
                raise RuntimeError(
                    f"plugin descriptor drift: {name}.effects owners: "
                    f"expected={name!r} actual={invalid_owners!r}"
                )
            self._assert_descriptor_values(
                name,
                "effects",
                {str(item[0]) for item in handlers},
            )
            result.extend(handlers)
        return result

    def all_admin_media_event_providers(self) -> list[Any]:
        result: list[Any] = []
        for name, plugin in self._plugins.items():
            if name not in self._active_plugins:
                continue
            provider = plugin.get_admin_media_event_provider()
            self._assert_descriptor_values(
                name,
                "admin_media_providers",
                (
                    {str(getattr(provider, "name", "") or name)}
                    if provider is not None
                    else set()
                ),
            )
            if provider is not None:
                result.append(_GatedAdminMediaEventProvider(self, name, provider))
        return result

    def all_channel_adapters(self) -> list[Any]:
        """Return initialized adapter registrations from active plugins."""

        result: list[Any] = []
        seen: dict[str, str] = {}
        for name, plugin in self._plugins.items():
            if name not in self._active_plugins or name not in self._initialized_plugins:
                continue
            registrations = list(plugin.get_channel_adapters())
            actual_ids = {
                str(getattr(getattr(item, "descriptor", None), "adapter_id", "") or "")
                for item in registrations
            }
            self._assert_descriptor_values(name, "channel_adapters", actual_ids)
            for registration in registrations:
                adapter_id = str(
                    getattr(getattr(registration, "descriptor", None), "adapter_id", "")
                    or ""
                ).strip()
                if not adapter_id:
                    raise RuntimeError(
                        f"plugin channel adapter missing stable id: {name}"
                    )
                previous_owner = seen.get(adapter_id)
                if previous_owner is not None:
                    raise RuntimeError(
                        "duplicate channel adapter registration: "
                        f"{adapter_id} ({previous_owner}, {name})"
                    )
                seen[adapter_id] = name
                result.append(
                    _GatedChannelAdapterRegistration(self, name, registration)
                )
        return result

    def all_permissions(self) -> dict[str, set[str]]:
        for name in sorted(self._active_plugins & self._initialized_plugins):
            self._validate_runtime_descriptor(name)
        return {
            name: set(self._descriptors[name].permissions)
            for name in self._plugins
            if name in self._active_plugins
            and name in self._descriptors
        }

    def descriptor(self, name: str) -> PluginDescriptor | None:
        plugin_name = str(name or "").strip()
        if (
            plugin_name in self._active_plugins
            and plugin_name in self._initialized_plugins
        ):
            self._validate_runtime_descriptor(plugin_name)
        return self._descriptors.get(plugin_name)

    def config_schema(self, name: str) -> dict[str, Any] | None:
        schema = self._config_schemas.get(str(name or "").strip())
        return copy.deepcopy(schema) if schema is not None else None

    def admin_ui(self, name: str) -> dict[str, Any] | None:
        document = self._admin_ui_documents.get(str(name or "").strip())
        return copy.deepcopy(document) if document is not None else None

    def api_default_tenant_id(self, name: str) -> str:
        """Resolve an active plugin's explicit tenantless-API compatibility scope."""

        plugin_name = str(name or "").strip()
        if (
            plugin_name not in self._active_plugins
            or plugin_name not in self._initialized_plugins
        ):
            return ""
        plugin = self._plugins.get(plugin_name)
        if plugin is None:
            return ""
        return str(plugin.get_api_default_tenant_id() or "").strip()

    @property
    def descriptors(self) -> dict[str, PluginDescriptor]:
        for name in sorted(self._active_plugins & self._initialized_plugins):
            self._validate_runtime_descriptor(name)
        return dict(self._descriptors)

    @property
    def loaded_plugins(self) -> dict[str, Plugin]:
        return dict(self._plugins)

    def is_active(self, name: str) -> bool:
        """Return whether a discovered plugin is currently enabled.

        Lifecycle consumers must use this public query instead of coupling to
        the registry's internal bookkeeping sets.
        """

        return str(name or "").strip() in self._active_plugins

    def is_initialized(self, name: str) -> bool:
        """Return whether a plugin completed runtime initialization."""

        return str(name or "").strip() in self._initialized_plugins

    @property
    def initialization_failures(self) -> dict[str, str]:
        return dict(self._initialization_failures)

    @property
    def initialization_order(self) -> tuple[str, ...]:
        return tuple(self._initialization_order)

    @property
    def summary(self) -> list[dict[str, Any]]:
        return [
            {
                "name": p.meta.name,
                "version": p.meta.version,
                "description": p.meta.description,
            }
            for p in self._plugins.values()
            if p.meta.name in self._active_plugins
        ]

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _external_directory_version(candidate: Path) -> str:
        """Read an installed package identity without importing plugin code."""

        descriptor_path = candidate / "plugin-package.json"
        try:
            raw = json.loads(
                descriptor_path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise PluginRegistrationError(
                f"external plugin package identity is invalid: {descriptor_path}"
            ) from exc
        if not isinstance(raw, dict) or raw.get("schema_version") not in {1, 2}:
            raise PluginRegistrationError(
                f"external plugin package identity is invalid: {descriptor_path}"
            )
        name = str(raw.get("name") or "").strip()
        version = str(raw.get("version") or "").strip()
        if name != candidate.name:
            raise PluginRegistrationError(
                "external plugin package identity mismatch: "
                f"directory={candidate.name!r} descriptor={name!r}"
            )
        try:
            Version(version)
        except InvalidVersion as exc:
            raise PluginRegistrationError(
                f"external plugin package version is invalid: {version!r}"
            ) from exc
        return version

    def _queue_external_discovery(
        self,
        name: str,
        target: dict[str, tuple[Any, str]],
        candidate: tuple[Any, str],
    ) -> None:
        if name in self._plugins:
            raise PluginRegistrationError(
                f"duplicate plugin registration: {name} from external discovery"
            )
        if (
            name in self._pending_directory_plugins
            or name in self._pending_entrypoints
        ):
            raise PluginRegistrationError(
                f"duplicate pending plugin discovery: {name}"
            )
        target[name] = candidate
        logger.info(
            "plugin.external_discovery_deferred",
            name=name,
            version=candidate[1],
        )

    async def _load_approved_external_plugins(self) -> int:
        """Import only external package generations approved in durable state.

        Discovery itself is deliberately data-only.  Python module top-level
        code is not executed until the state store confirms an installed row
        for the exact statically observed package/distribution version.
        """

        store = self._state_store
        if store is None:
            return 0
        loaded = 0
        for name, (plugin_path, version) in sorted(
            tuple(self._pending_directory_plugins.items())
        ):
            state = await store.get(name)
            if not self._external_directory_generation_is_trusted(
                state,
                name=name,
                version=version,
                plugin_path=plugin_path,
            ):
                logger.info(
                    "plugin.external_discovery_quarantined",
                    name=name,
                    version=version,
                )
                continue
            if not bool(getattr(state, "enabled", False)):
                if bool(getattr(state, "restart_required", False)):
                    await store.acknowledge_disabled_restart(name, version)
                logger.info(
                    "plugin.external_discovery_disabled",
                    name=name,
                    version=version,
                )
                continue
            try:
                self._load_and_register_file(
                    plugin_path,
                    name,
                    source="install_directory",
                    source_detail=str(plugin_path),
                    expected_name=name,
                    expected_version=version,
                )
                self._pending_directory_plugins.pop(name, None)
                loaded += 1
            except Exception as exc:
                logger.error(
                    "plugin.external_load_failed",
                    name=name,
                    error=str(exc),
                )

        for name, (entrypoint, version) in sorted(
            tuple(self._pending_entrypoints.items())
        ):
            state = await store.get(name)
            if not self._external_entrypoint_generation_is_trusted(
                state,
                name=name,
                version=version,
            ):
                logger.info(
                    "plugin.external_discovery_quarantined",
                    name=name,
                    version=version,
                )
                continue
            if not bool(getattr(state, "enabled", False)):
                if bool(getattr(state, "restart_required", False)):
                    await store.acknowledge_disabled_restart(name, version)
                logger.info(
                    "plugin.external_discovery_disabled",
                    name=name,
                    version=version,
                )
                continue
            try:
                plugin = self._load_entrypoint(
                    entrypoint,
                    expected_version=version,
                )
                self._register(
                    plugin,
                    source="entrypoint",
                    source_detail=name,
                    expected_name=name,
                )
                self._pending_entrypoints.pop(name, None)
                loaded += 1
            except Exception as exc:
                logger.error(
                    "plugin.external_load_failed",
                    name=name,
                    error=str(exc),
                )
        return loaded

    @staticmethod
    def _external_manifest_package_matches(
        state: Any,
        *,
        name: str,
        version: str,
        package_type: str,
    ) -> bool:
        metadata = getattr(state, "metadata", None)
        if not isinstance(metadata, dict):
            return False
        manifest = metadata.get("manifest")
        artifact = metadata.get("artifact")
        if not isinstance(manifest, dict) or not isinstance(artifact, dict):
            return False
        package = manifest.get("package")
        if not isinstance(package, dict):
            return False
        package_checksum = str(package.get("checksum") or "").strip().lower()
        artifact_checksum = str(artifact.get("checksum") or "").strip().lower()
        return bool(
            state is not None
            and bool(getattr(state, "installed", False))
            and str(getattr(state, "version", "") or "") == version
            and str(manifest.get("name") or "") == name
            and str(manifest.get("version") or "") == version
            and str(package.get("type") or "") == package_type
            and str(artifact.get("package_type") or "") == package_type
            and bool(package_checksum)
            and package_checksum == artifact_checksum
        )

    @classmethod
    def _external_directory_generation_is_trusted(
        cls,
        state: Any,
        *,
        name: str,
        version: str,
        plugin_path: Path,
    ) -> bool:
        if str(getattr(state, "source", "") or "") not in {
            "local",
            "marketplace",
        }:
            return False
        if not cls._external_manifest_package_matches(
            state,
            name=name,
            version=version,
            package_type="local_archive",
        ):
            return False
        metadata = getattr(state, "metadata", {})
        artifact = metadata.get("artifact") if isinstance(metadata, dict) else None
        expected_tree_digest = (
            str(artifact.get("tree_digest") or "").strip().lower()
            if isinstance(artifact, dict)
            else ""
        )
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_tree_digest):
            return False
        try:
            actual_tree_digest = compute_plugin_tree_digest(plugin_path.parent)
        except (OSError, ValueError):
            return False
        return actual_tree_digest == expected_tree_digest

    @classmethod
    def _external_entrypoint_generation_is_trusted(
        cls,
        state: Any,
        *,
        name: str,
        version: str,
    ) -> bool:
        if str(getattr(state, "source", "") or "") not in {
            "marketplace",
            "entrypoint",
        }:
            return False
        return cls._external_manifest_package_matches(
            state,
            name=name,
            version=version,
            package_type="wheel",
        )

    @staticmethod
    def _load_entrypoint(entrypoint: Any, *, expected_version: str) -> Plugin:
        plugin_cls = entrypoint.load()
        plugin = plugin_cls() if isinstance(plugin_cls, type) else plugin_cls
        if not isinstance(plugin, Plugin):
            raise PluginRegistrationError(
                f"entry point {entrypoint.name!r} did not expose a Plugin instance"
            )
        if expected_version and str(plugin.meta.version) != expected_version:
            raise PluginRegistrationError(
                "entry point runtime version mismatch: "
                f"approved={expected_version!r} actual={plugin.meta.version!r}"
            )
        return plugin

    def _register(
        self,
        plugin: Plugin,
        *,
        source: str = "manual",
        source_detail: str = "",
        expected_name: str = "",
    ) -> None:
        if not isinstance(plugin, Plugin):
            raise PluginRegistrationError(
                f"plugin source {source!r} did not expose a Plugin instance"
            )
        name = plugin.meta.name
        if not isinstance(name, str) or not _PLUGIN_NAME_RE.fullmatch(name):
            raise PluginRegistrationError(f"invalid plugin name from {source}: {name!r}")
        if name in PLUGIN_RESERVED_NAMES:
            raise PluginRegistrationError(
                f"reserved kernel owner cannot be registered as a plugin: {name!r}"
            )
        if expected_name and name != expected_name:
            raise PluginRegistrationError(
                "plugin identity mismatch: "
                f"source={source!r} expected={expected_name!r} actual={name!r}"
            )
        try:
            Version(str(plugin.meta.version or ""))
        except InvalidVersion as exc:
            raise PluginRegistrationError(
                f"invalid plugin version from {source}: {plugin.meta.version!r}"
            ) from exc
        dependencies = plugin.meta.dependencies
        if not isinstance(dependencies, (list, tuple)) or not all(
            isinstance(item, str) and item.strip() for item in dependencies
        ):
            raise PluginRegistrationError(
                f"plugin dependencies must be non-empty strings: {name}"
            )
        if name in self._plugins:
            raise PluginRegistrationError(
                f"duplicate plugin registration: {name} from {source}"
            )
        self._plugins[name] = plugin
        self._discovery_provenance[name] = str(source or "manual").strip()
        if self._state_store is None and self._allow_offline_execution:
            self._active_plugins.add(name)
        logger.info(
            "plugin.registered",
            name=name,
            version=plugin.meta.version,
            description=plugin.meta.description,
            source=source,
            source_detail=source_detail,
        )

    def _register_plugin_hooks(self, name: str, plugin: Plugin) -> None:
        if name in self._hooks_registered:
            return
        hooks = plugin.get_pipeline_hooks()
        self._assert_descriptor_values(
            name,
            "hooks",
            {
                (
                    f"{getattr(getattr(item, 'point', ''), 'value', getattr(item, 'point', ''))}:"
                    f"{getattr(item, 'name', '')}"
                )
                for item in hooks
            },
        )
        for hook in hooks:
            self._hook_runner.register(hook, owner=name)
        self._hooks_registered.add(name)

    def _register_plugin_agent_tools(self, name: str, plugin: Plugin, container: Any) -> None:
        if name in self._agent_tools_registered:
            return
        registry = getattr(container, "agent_tool_registry", None)
        if registry is None:
            return
        if not isinstance(registry, AgentToolRegistry):
            logger.warning("plugin.agent_tool_registry_invalid", plugin=name)
            return
        tools = plugin.get_agent_tools()
        self._assert_descriptor_values(
            name,
            "agent_tools",
            {
                f"{getattr(item, 'scope', '')}:{getattr(item, 'name', '')}"
                for item in tools
            },
        )
        if not tools:
            self._agent_tools_registered.add(name)
            return
        count = registry.register_many(list(tools), owner=name)
        logger.info("plugin.agent_tools_registered", plugin=name, count=count)
        self._agent_tools_registered.add(name)

    def _validate_runtime_descriptor(
        self,
        name: str,
        *,
        container: Any | None = None,
    ) -> None:
        plugin = self._plugins.get(name)
        if plugin is None:
            raise RuntimeError(f"plugin missing for descriptor validation: {name}")
        descriptor = self._descriptors.get(name)
        if descriptor is None:
            raise RuntimeError(f"plugin descriptor missing after initialization: {name}")

        if (
            plugin.meta.name != descriptor.name
            or plugin.meta.version != descriptor.version
            or plugin.meta.description != descriptor.description
            or tuple(plugin.meta.dependencies) != descriptor.dependencies
        ):
            raise RuntimeError(f"plugin descriptor drift: {name}.metadata")

        permissions = {
            str(item).strip()
            for item in plugin.get_permissions()
            if str(item).strip()
        }
        self._assert_descriptor_values(name, "permissions", permissions)
        self._assert_descriptor_values(
            name,
            "storage_permissions",
            {item for item in permissions if item.startswith("storage:")},
        )
        self._assert_descriptor_values(
            name,
            "network_permissions",
            {item for item in permissions if item.startswith("network:")},
        )

        hooks = plugin.get_pipeline_hooks()
        self._assert_descriptor_values(
            name,
            "hooks",
            {
                (
                    f"{getattr(getattr(item, 'point', ''), 'value', getattr(item, 'point', ''))}:"
                    f"{getattr(item, 'name', '')}"
                )
                for item in hooks
            },
        )
        tools = plugin.get_agent_tools()
        self._assert_descriptor_values(
            name,
            "agent_tools",
            {
                f"{getattr(item, 'scope', '')}:{getattr(item, 'name', '')}"
                for item in tools
            },
        )
        self._assert_registered_agent_tools(
            name,
            container=container if container is not None else self._runtime_container,
        )

        router = plugin.get_api_router()
        routes: set[str] = set()
        if router is not None:
            for route in getattr(router, "routes", ()):
                path = str(getattr(route, "path", "") or "")
                for method in sorted(getattr(route, "methods", ()) or ()):
                    routes.add(f"{str(method).upper()} {path}")
        self._assert_descriptor_values(name, "admin_routes", routes)

        steps = {str(item.kind) for item in plugin.get_flow_steps()}
        self._assert_descriptor_values(name, "flow_steps", steps)
        self._assert_descriptor_values(
            name,
            "flow_steps",
            {str(kind) for kind in plugin.get_flow_executors()},
        )

        handlers = plugin.get_effect_handlers()
        invalid_owners = sorted(
            {
                str(item[1])
                for item in handlers
                if str(item[1]).strip() != name
            }
        )
        if invalid_owners:
            raise RuntimeError(
                f"plugin descriptor drift: {name}.effects owners: "
                f"expected={name!r} actual={invalid_owners!r}"
            )
        self._assert_descriptor_values(
            name,
            "effects",
            {str(item[0]) for item in handlers},
        )
        self._assert_descriptor_values(
            name,
            "commands",
            set(self._command_tokens_by_owner(name)),
        )
        self._assert_descriptor_values(
            name,
            "channel_adapters",
            {
                str(getattr(getattr(item, "descriptor", None), "adapter_id", "") or "")
                for item in plugin.get_channel_adapters()
            },
        )
        self._assert_descriptor_values(
            name,
            "capability_engines",
            {
                str(getattr(route_type, "value", route_type))
                for route_type in plugin.get_capability_engines()
            },
        )
        admin_media_provider = plugin.get_admin_media_event_provider()
        self._assert_descriptor_values(
            name,
            "admin_media_providers",
            (
                {
                    str(
                        getattr(admin_media_provider, "name", "")
                        or name
                    )
                }
                if admin_media_provider is not None
                else set()
            ),
        )

    async def _validate_durable_package_contract(
        self,
        name: str,
        descriptor: PluginDescriptor,
    ) -> None:
        """Fence installed package declarations against runtime metadata.

        This is an integrity/audit fence for trusted in-process code, not a
        Python sandbox.  The install manifest is retained under
        ``metadata.manifest`` and must agree with the initialized descriptor
        before any contribution is published.
        """

        store = self._state_store
        if store is None:
            return
        state = await store.get(name)
        if state is None:
            raise RuntimeError(f"plugin durable state missing: {name}")
        manifest = state.metadata.get("manifest")
        if not isinstance(manifest, dict):
            if state.source != "builtin":
                raise RuntimeError(f"plugin package contract missing: {name}")
            return
        if str(manifest.get("name") or "") != descriptor.name:
            raise RuntimeError(f"plugin package name drift: {name}")
        if str(manifest.get("version") or "") != descriptor.version:
            raise RuntimeError(f"plugin package version drift: {name}")
        declared_capability_digest = str(
            manifest.get("capability_digest") or ""
        ).strip().lower()
        if declared_capability_digest != plugin_capability_digest(descriptor):
            raise RuntimeError(f"plugin package capability digest drift: {name}")

        raw_permissions = manifest.get("permissions") or []
        if not isinstance(raw_permissions, list):
            raise RuntimeError(f"plugin package permissions invalid: {name}")
        manifest_permissions = {
            str(item.get("id") if isinstance(item, dict) else item).strip()
            for item in raw_permissions
        }
        if manifest_permissions != set(descriptor.permissions):
            raise RuntimeError(f"plugin package permission drift: {name}")

        raw_dependencies = manifest.get("dependencies") or []
        if not isinstance(raw_dependencies, list):
            raise RuntimeError(f"plugin package dependencies invalid: {name}")
        manifest_dependencies = {
            (
                str(item.get("name") or "").strip()
                + str(item.get("version") or "").replace(" ", "")
            )
            for item in raw_dependencies
            if isinstance(item, dict) and bool(item.get("required", True))
        }
        runtime_dependencies = {
            parse_plugin_dependency(value, owner=name).label
            for value in descriptor.dependencies
        }
        if manifest_dependencies != runtime_dependencies:
            raise RuntimeError(f"plugin package dependency drift: {name}")

        manifest_schema = manifest.get("config_schema") or {}
        if manifest_schema != (self._config_schemas.get(name) or {}):
            raise RuntimeError(f"plugin package config schema drift: {name}")

        declared_capabilities = manifest.get("capabilities") or {}
        if not isinstance(declared_capabilities, dict):
            raise RuntimeError(f"plugin package capabilities invalid: {name}")
        runtime_capabilities = {
            "routes": [f"/plugins/{name}"] if descriptor.admin_routes else [],
            "hooks": sorted({item.split(":", 1)[0] for item in descriptor.hooks}),
            "agent_tools": sorted(
                {item.split(":", 1)[1] for item in descriptor.agent_tools}
            ),
            "commands": sorted(descriptor.commands),
        }
        for field, actual in runtime_capabilities.items():
            declared = declared_capabilities.get(field) or []
            if not isinstance(declared, list) or sorted(declared) != actual:
                raise RuntimeError(
                    f"plugin package capability drift: {name}.{field}"
                )

    def _assert_registered_agent_tools(
        self,
        name: str,
        *,
        container: Any | None,
    ) -> None:
        descriptor = self._descriptors.get(name)
        if descriptor is None:
            raise RuntimeError(f"plugin descriptor missing after initialization: {name}")
        registry = getattr(container, "agent_tool_registry", None)
        if registry is None:
            if descriptor.agent_tools:
                raise RuntimeError(
                    f"plugin descriptor drift: {name}.agent_tools: registry unavailable"
                )
            return
        if not isinstance(registry, AgentToolRegistry):
            raise RuntimeError(
                f"plugin descriptor drift: {name}.agent_tools: invalid registry"
            )
        actual = {
            f"{item.get('scope', '')}:{item.get('name', '')}"
            for item in registry.catalog_by_owner().get(name, [])
        }
        self._assert_descriptor_values(name, "agent_tools", actual)

    def _command_tokens_by_owner(self, name: str) -> tuple[str, ...]:
        commands_plugin = self._plugins.get("commands")
        lookup = getattr(commands_plugin, "command_tokens_by_owner", None)
        if not callable(lookup):
            return ()
        tokens = lookup(name)
        return tuple(
            sorted(
                {
                    str(item).strip()
                    for item in tokens
                    if str(item).strip()
                }
            )
        )

    def _assert_descriptor_values(
        self,
        name: str,
        field_name: str,
        actual: set[str],
    ) -> None:
        descriptor = self._descriptors.get(name)
        if descriptor is None:
            raise RuntimeError(f"plugin descriptor missing after initialization: {name}")
        expected = set(getattr(descriptor, field_name))
        if actual != expected:
            raise RuntimeError(
                f"plugin descriptor drift: {name}.{field_name}: "
                f"expected={sorted(expected)!r} actual={sorted(actual)!r}"
            )

    async def _selected_plugin_names(self) -> set[str]:
        if self._state_store is None:
            return set(self._plugins) if self._allow_offline_execution else set()

        selected: set[str] = set()
        for name in self._plugins:
            state = await self._state_store.get(name)
            if state is None or (state.installed and state.enabled):
                selected.add(name)
                continue
            self._active_plugins.discard(name)
            logger.info("plugin.skipped_by_state", name=name, status=state.status)
        return selected

    def _dependency_block_reason(
        self,
        name: str,
        graph: PluginDependencyGraph,
    ) -> str:
        for dependency in graph.requirements.get(name, ()):
            failure = self._initialization_failures.get(dependency.name)
            if failure:
                return f"dependency {dependency.name!r} failed initialization: {failure}"
            if dependency.name not in self._initialized_plugins:
                return f"dependency {dependency.name!r} is not initialized"
            if dependency.name not in self._active_plugins:
                return f"dependency {dependency.name!r} is not active"
        return ""

    async def global_execution_allowed(self, owner: str) -> bool:
        """Return whether an owner may execute in this process right now.

        A configured durable state store is authoritative.  This makes a
        disable performed by one API/worker replica effective at the next
        contribution boundary in every other replica, even though their
        in-memory contribution catalogs are intentionally immutable until a
        restart. Unknown non-core owners fail closed instead of accidentally
        acquiring kernel privileges through an ownership typo.
        """

        name = str(owner or "").strip()
        if name in _KERNEL_EXECUTION_OWNERS:
            return True
        store = self._state_store
        try:
            execution_owners = self._execution_owner_closure(name)
            for execution_owner in execution_owners:
                if (
                    execution_owner not in self._plugins
                    or execution_owner not in self._active_plugins
                    or execution_owner not in self._initialized_plugins
                ):
                    return False
                if store is None:
                    if not self._allow_offline_execution:
                        return False
                    continue
            if store is None:
                return True
            snapshot = getattr(store, "execution_snapshot_allowed", None)
            if callable(snapshot) and bool(
                getattr(store, "database_execution_snapshot_enabled", False)
            ):
                return (
                    await snapshot(
                        {
                            execution_owner: self._plugins[execution_owner].meta.version
                            for execution_owner in execution_owners
                        }
                    )
                    is True
                )
            for execution_owner in execution_owners:
                state = await store.get(execution_owner)
                if not bool(
                    state is not None
                    and state.installed
                    and state.enabled
                    and state.status == "active"
                    and not state.restart_required
                    and state.version
                    == self._plugins[execution_owner].meta.version
                ):
                    return False
            return True
        except Exception as exc:
            logger.error(
                "plugin.global_execution_policy_failed",
                plugin=name,
                error=type(exc).__name__,
            )
            return False

    async def scope_execution_allowed(
        self,
        owner: str,
        *,
        tenant_id: str,
        session_id: str = "",
    ) -> bool:
        """Apply global state and the session-over-tenant scope override."""

        name = str(owner or "").strip()
        if name in _KERNEL_EXECUTION_OWNERS:
            return True
        execution_owners = self._execution_owner_closure(name)
        if any(
            execution_owner not in self._plugins
            or execution_owner not in self._active_plugins
            or execution_owner not in self._initialized_plugins
            for execution_owner in execution_owners
        ):
            return False
        store = self._state_store
        if store is None:
            return self._allow_offline_execution
        tenant = str(tenant_id or "").strip()
        session = str(session_id or "").strip()
        if not tenant:
            return False
        try:
            snapshot = getattr(store, "execution_snapshot_allowed", None)
            if callable(snapshot) and bool(
                getattr(store, "database_execution_snapshot_enabled", False)
            ):
                return (
                    await snapshot(
                        {
                            execution_owner: self._plugins[execution_owner].meta.version
                            for execution_owner in execution_owners
                        },
                        tenant_id=tenant,
                        session_id=session,
                    )
                    is True
                )
            for execution_owner in execution_owners:
                state = await store.get(execution_owner)
                if not bool(
                    state is not None
                    and state.installed
                    and state.enabled
                    and state.status == "active"
                    and not state.restart_required
                    and state.version
                    == self._plugins[execution_owner].meta.version
                ):
                    return False
                scope = await store.resolve_effective_scope(
                    tenant_id=tenant,
                    session_id=session,
                    plugin_name=execution_owner,
                )
                if scope is not None and not bool(scope.enabled):
                    return False
            return True
        except Exception as exc:
            logger.error(
                "plugin.scope_execution_policy_failed",
                plugin=name,
                error=type(exc).__name__,
            )
            return False

    async def owners_scope_execution_allowed(
        self,
        owners: tuple[str, ...] | list[str] | set[str],
        *,
        tenant_id: str,
        session_id: str = "",
    ) -> bool:
        """Atomically gate a boundary that consumes multiple plugin owners.

        Cross-plugin capability ports must not authorize each owner from
        different database snapshots.  The database-backed state store can
        evaluate the union of their dependency closures in one statement.
        """

        requested = tuple(
            dict.fromkeys(str(owner or "").strip() for owner in owners)
        )
        plugin_owners = tuple(
            owner for owner in requested if owner not in _KERNEL_EXECUTION_OWNERS
        )
        if not plugin_owners:
            return bool(requested)
        tenant = str(tenant_id or "").strip()
        session = str(session_id or "").strip()
        if not tenant:
            return False

        execution_owner_set: set[str] = set()
        for owner in plugin_owners:
            execution_owner_set.update(self._execution_owner_closure(owner))
        if any(owner not in self._plugins for owner in execution_owner_set):
            return False
        graph = self._dependency_graph
        execution_owners = (
            tuple(owner for owner in graph.order if owner in execution_owner_set)
            if graph is not None
            else tuple(sorted(execution_owner_set))
        )
        if not execution_owners or set(execution_owners) != execution_owner_set:
            return False
        if any(
            execution_owner not in self._plugins
            or execution_owner not in self._active_plugins
            or execution_owner not in self._initialized_plugins
            for execution_owner in execution_owners
        ):
            return False

        store = self._state_store
        if store is None:
            return self._allow_offline_execution
        try:
            snapshot = getattr(store, "execution_snapshot_allowed", None)
            if callable(snapshot) and bool(
                getattr(store, "database_execution_snapshot_enabled", False)
            ):
                return (
                    await snapshot(
                        {
                            execution_owner: self._plugins[execution_owner].meta.version
                            for execution_owner in execution_owners
                        },
                        tenant_id=tenant,
                        session_id=session,
                    )
                    is True
                )
            for execution_owner in execution_owners:
                state = await store.get(execution_owner)
                if not bool(
                    state is not None
                    and state.installed
                    and state.enabled
                    and state.status == "active"
                    and not state.restart_required
                    and state.version
                    == self._plugins[execution_owner].meta.version
                ):
                    return False
                scope = await store.resolve_effective_scope(
                    tenant_id=tenant,
                    session_id=session,
                    plugin_name=execution_owner,
                )
                if scope is not None and not bool(scope.enabled):
                    return False
            return True
        except Exception as exc:
            logger.error(
                "plugin.multi_owner_scope_execution_policy_failed",
                owners=plugin_owners,
                error=type(exc).__name__,
            )
            return False

    async def session_execution_allowed(self, owner: str, session: Session) -> bool:
        """Adapter used by capability engines and agent-tool boundaries."""

        return await self.scope_execution_allowed(
            owner,
            tenant_id=str(session.tenant_id or ""),
            session_id=str(
                session.external_conversation_id or session.session_id or ""
            ),
        )

    async def execution_allowed(self, owner: str, ctx: PipelineContext) -> bool:
        """Strict adapter for retry-aware pipeline/effect boundaries.

        Unlike control-plane boolean probes, durable-store errors propagate so
        ``evaluate_owner_execution`` can classify them as transient instead of
        terminally skipping a prepared side effect.
        """

        name = str(owner or "").strip()
        if name in _KERNEL_EXECUTION_OWNERS:
            return True
        event = ctx.event
        store = self._state_store
        execution_owners = self._execution_owner_closure(name)
        if any(
            execution_owner not in self._plugins
            or execution_owner not in self._active_plugins
            or execution_owner not in self._initialized_plugins
            for execution_owner in execution_owners
        ):
            return False
        if store is None:
            return self._allow_offline_execution
        tenant_id = str(event.tenant_id or "").strip()
        if not tenant_id:
            return False
        scope_session_id = str(
            event.external_conversation_id
            or dict(event.metadata or {}).get("external_conversation_id")
            or event.session_id
            or ""
        ).strip()
        snapshot = getattr(store, "execution_snapshot_allowed", None)
        if callable(snapshot) and bool(
            getattr(store, "database_execution_snapshot_enabled", False)
        ):
            return (
                await snapshot(
                    {
                        execution_owner: self._plugins[execution_owner].meta.version
                        for execution_owner in execution_owners
                    },
                    tenant_id=tenant_id,
                    session_id=scope_session_id,
                )
                is True
            )
        for execution_owner in execution_owners:
            state = await store.get(execution_owner)
            if not bool(
                state is not None
                and state.installed
                and state.enabled
                and state.status == "active"
                and not state.restart_required
                and state.version == self._plugins[execution_owner].meta.version
            ):
                return False
            scope = await store.resolve_effective_scope(
                tenant_id=tenant_id,
                session_id=scope_session_id,
                plugin_name=execution_owner,
            )
            if scope is not None and not bool(scope.enabled):
                return False
        return True

    def _execution_owner_closure(self, name: str) -> tuple[str, ...]:
        """Return owner plus required transitive dependencies, dependency first."""

        if name not in self._plugins:
            return (name,)
        graph = self._dependency_graph
        if graph is None:
            return (name,)
        selected = plugin_dependency_closure(self._plugins, (name,))
        ordered = tuple(owner for owner in graph.order if owner in selected)
        return ordered if ordered else (name,)

    async def _rollback_failed_activation(
        self,
        name: str,
        plugin: Plugin,
        container: Any,
        *,
        discard_initialization: bool,
    ) -> None:
        """Remove every owner-indexed contribution after a failed publish."""

        self._active_plugins.discard(name)
        self._hook_runner.unregister_owner(name)
        self._hooks_registered.discard(name)
        registry = getattr(container, "agent_tool_registry", None)
        if isinstance(registry, AgentToolRegistry):
            registry.unregister_owner(name)
        self._agent_tools_registered.discard(name)
        commands_plugin = self._plugins.get("commands")
        unregister = getattr(commands_plugin, "unregister_owner", None)
        if callable(unregister):
            unregister(name)
        if not discard_initialization:
            return
        self._descriptors.pop(name, None)
        self._config_schemas.pop(name, None)
        self._admin_ui_documents.pop(name, None)
        self._initialized_plugins.discard(name)
        self._initialization_order = [item for item in self._initialization_order if item != name]

    async def _cleanup_failed_activation(self, name: str, plugin: Plugin) -> None:
        """Best-effort resource cleanup after initialize/on_enable publication fails."""

        try:
            await asyncio.wait_for(
                plugin.on_disable(),
                timeout=_PLUGIN_LIFECYCLE_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("plugin.failed_activation_cleanup_failed", name=name)
        try:
            await asyncio.wait_for(
                plugin.shutdown(),
                timeout=_PLUGIN_SHUTDOWN_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.error(
                "plugin.activation_rollback_failed",
                name=name,
                error=str(exc),
            )

    async def _record_initialization_failure(self, name: str, reason: str) -> None:
        self._active_plugins.discard(name)
        self._initialization_failures[name] = reason
        if self._state_store is not None:
            await self._state_store.mark_failed(
                name,
                reason,
                self._plugins[name].meta.version,
            )

    def _load_and_register_file(
        self,
        path: Path,
        package_name: str,
        *,
        source: str,
        source_detail: str,
        expected_name: str,
        expected_version: str = "",
    ) -> None:
        namespace = f"plugins.{package_name}"
        module_snapshot = _snapshot_module_namespace(namespace)
        root_package = importlib.import_module("plugins")
        previous_package_attribute = getattr(
            root_package,
            package_name,
            _MISSING_MODULE_ATTRIBUTE,
        )
        try:
            plugin = self._load_from_file(
                path,
                package_name,
                expected_version=expected_version,
            )
            self._register(
                plugin,
                source=source,
                source_detail=source_detail,
                expected_name=expected_name,
            )
        except BaseException:
            _restore_module_namespace(namespace, module_snapshot)
            if previous_package_attribute is _MISSING_MODULE_ATTRIBUTE:
                if hasattr(root_package, package_name):
                    delattr(root_package, package_name)
            else:
                setattr(root_package, package_name, previous_package_attribute)
            raise

    @staticmethod
    def _load_from_file(
        path: Path,
        package_name: str,
        *,
        expected_version: str = "",
    ) -> Plugin:
        path = path.resolve()
        package_dir = path.parent
        package_module_name = f"plugins.{package_name}"
        module_name = f"plugins.{package_name}.plugin"
        previous_modules = _snapshot_module_namespace(package_module_name)
        root_package = importlib.import_module("plugins")
        previous_package_attribute = getattr(
            root_package,
            package_name,
            _MISSING_MODULE_ATTRIBUTE,
        )
        existing_package_module = sys.modules.get(package_module_name)
        previous_plugin_attribute = (
            getattr(
                existing_package_module,
                "plugin",
                _MISSING_MODULE_ATTRIBUTE,
            )
            if existing_package_module is not None
            else _MISSING_MODULE_ATTRIBUTE
        )
        try:
            package_module = sys.modules.get(package_module_name)
            if package_module is None:
                init_path = package_dir / "__init__.py"
                if init_path.is_file():
                    package_spec = importlib.util.spec_from_file_location(
                        package_module_name,
                        init_path,
                        submodule_search_locations=[str(package_dir)],
                    )
                    if package_spec is None or package_spec.loader is None:
                        raise ImportError(
                            f"cannot create package spec for {package_dir}"
                        )
                    package_module = importlib.util.module_from_spec(package_spec)
                    sys.modules[package_module_name] = package_module
                    setattr(root_package, package_name, package_module)
                    package_spec.loader.exec_module(package_module)
                else:
                    package_module = ModuleType(package_module_name)
                    package_spec = importlib.machinery.ModuleSpec(
                        package_module_name,
                        loader=None,
                        is_package=True,
                    )
                    package_spec.submodule_search_locations = [str(package_dir)]
                    package_module.__file__ = str(package_dir)
                    package_module.__package__ = package_module_name
                    package_module.__path__ = [str(package_dir)]
                    package_module.__spec__ = package_spec
                    sys.modules[package_module_name] = package_module
                    setattr(root_package, package_name, package_module)
            else:
                package_locations = {
                    Path(str(location)).resolve()
                    for location in getattr(package_module, "__path__", ())
                }
                if package_dir not in package_locations:
                    raise PluginRegistrationError(
                        "plugin package namespace collision: "
                        f"{package_module_name!r} is not rooted at {package_dir}"
                    )

            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot create spec for {path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            package_module.plugin = module
            plugin = getattr(module, "plugin", None)
            if plugin is None:
                raise AttributeError(f"{path} has no module-level 'plugin' attribute")
            if not isinstance(plugin, Plugin):
                raise TypeError(
                    f"{path}: 'plugin' must be a Plugin instance, "
                    f"got {type(plugin).__name__}"
                )
            if str(plugin.meta.name or "").strip() != package_name:
                raise PluginRegistrationError(
                    "plugin identity mismatch: "
                    f"expected={package_name!r} actual={plugin.meta.name!r}"
                )
            if expected_version and str(plugin.meta.version or "") != expected_version:
                raise PluginRegistrationError(
                    "external plugin runtime version mismatch: "
                    f"approved={expected_version!r} actual={plugin.meta.version!r}"
                )
            return plugin
        except BaseException:
            _restore_module_namespace(package_module_name, previous_modules)
            if previous_package_attribute is _MISSING_MODULE_ATTRIBUTE:
                current = getattr(
                    root_package,
                    package_name,
                    _MISSING_MODULE_ATTRIBUTE,
                )
                if (
                    current is not _MISSING_MODULE_ATTRIBUTE
                    and package_module_name not in sys.modules
                ):
                    delattr(root_package, package_name)
            else:
                setattr(root_package, package_name, previous_package_attribute)
            restored_package = sys.modules.get(package_module_name)
            if restored_package is not None:
                if previous_plugin_attribute is _MISSING_MODULE_ATTRIBUTE:
                    if hasattr(restored_package, "plugin"):
                        delattr(restored_package, "plugin")
                else:
                    restored_package.plugin = previous_plugin_attribute
            raise
