from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import APIRouter

from app.agent.registry import AgentToolRegistry
from app.plugin.base import Plugin, PluginContext, PluginMeta
from app.plugin.dependencies import (
    PluginDependencyBlockedError,
    PluginDependencyGraphError,
    parse_plugin_dependency,
)
from app.plugin.registry import PluginRegistry
from app.plugin.state import PluginState


class _RecordingPlugin(Plugin):
    def __init__(
        self,
        name: str,
        events: list[str],
        *,
        version: str = "1.0.0",
        dependencies: list[str] | None = None,
        init_error: str = "",
    ) -> None:
        self.meta = PluginMeta(
            name=name,
            version=version,
            dependencies=list(dependencies or []),
        )
        self._events = events
        self._init_error = init_error

    async def initialize(self, ctx: PluginContext) -> None:
        _ = ctx
        self._events.append(f"init:{self.meta.name}")
        if self._init_error:
            raise RuntimeError(self._init_error)

    async def shutdown(self) -> None:
        self._events.append(f"shutdown:{self.meta.name}")


class _MutableRoutePlugin(_RecordingPlugin):
    def __init__(self, events: list[str]) -> None:
        super().__init__("mutable_route", events)
        self.path = "/stable"

    def get_api_router(self) -> APIRouter:
        router = APIRouter()

        @router.get(self.path)
        async def route() -> dict[str, bool]:
            return {"ok": True}

        return router


class _StateStore:
    def __init__(self, states: dict[str, PluginState]) -> None:
        self.states = states
        self.failed: dict[str, str] = {}
        self.initialized: list[str] = []

    async def get(self, name: str) -> PluginState | None:
        return self.states.get(name)

    async def mark_failed(
        self,
        name: str,
        reason: str,
        expected_version: str,
    ) -> None:
        _ = expected_version
        self.failed[name] = reason

    async def mark_initialized(self, name: str, expected_version: str) -> None:
        _ = expected_version
        self.initialized.append(name)


def _context() -> PluginContext:
    return PluginContext(
        container=SimpleNamespace(agent_tool_registry=AgentToolRegistry()),
        settings=object(),
        db_ok=True,
        redis_ok=True,
    )


def _register(registry: PluginRegistry, *plugins: Plugin) -> None:
    for plugin in plugins:
        registry._register(plugin)


def test_dependency_parser_supports_name_and_minimum_version() -> None:
    unconstrained = parse_plugin_dependency("wxbot", owner="reports")
    constrained = parse_plugin_dependency(" memory >= 1.2.0 ", owner="reports")

    assert unconstrained.name == "wxbot"
    assert unconstrained.minimum_version is None
    assert constrained.name == "memory"
    assert str(constrained.minimum_version) == "1.2.0"


@pytest.mark.asyncio
async def test_initialized_plugin_route_surface_cannot_drift_from_descriptor() -> None:
    events: list[str] = []
    registry = PluginRegistry(allow_offline_execution=True)
    plugin = _MutableRoutePlugin(events)
    _register(registry, plugin)

    await registry.initialize_all(_context())
    assert registry.all_api_routers()

    plugin.path = "/changed-after-initialization"
    with pytest.raises(RuntimeError, match=r"plugin descriptor drift: mutable_route\.admin_routes"):
        registry.all_api_routers()


@pytest.mark.asyncio
async def test_no_dependency_plugins_keep_registration_order_and_reverse_shutdown() -> None:
    events: list[str] = []
    registry = PluginRegistry(allow_offline_execution=True)
    _register(
        registry,
        _RecordingPlugin("alpha", events),
        _RecordingPlugin("beta", events),
    )

    await registry.initialize_all(_context())
    await registry.shutdown_all()

    assert registry.initialization_order == ("alpha", "beta")
    assert events == [
        "init:alpha",
        "init:beta",
        "shutdown:beta",
        "shutdown:alpha",
    ]


@pytest.mark.asyncio
async def test_dependencies_initialize_before_dependents_and_shutdown_after_them() -> None:
    events: list[str] = []
    registry = PluginRegistry(allow_offline_execution=True)
    _register(
        registry,
        _RecordingPlugin("feature", events, dependencies=["foundation>=1.2.0"]),
        _RecordingPlugin("foundation", events, version="1.4.0"),
    )

    await registry.initialize_all(_context())
    await registry.shutdown_all()

    assert registry.initialization_order == ("foundation", "feature")
    assert events == [
        "init:foundation",
        "init:feature",
        "shutdown:feature",
        "shutdown:foundation",
    ]


@pytest.mark.asyncio
async def test_missing_dependency_fails_preflight_before_any_initializer() -> None:
    events: list[str] = []
    registry = PluginRegistry(allow_offline_execution=True)
    _register(
        registry,
        _RecordingPlugin("independent", events),
        _RecordingPlugin("feature", events, dependencies=["missing"]),
    )

    with pytest.raises(PluginDependencyGraphError, match="missing dependency 'missing'"):
        await registry.initialize_all(_context())

    assert events == []
    assert "missing dependency 'missing'" in registry.initialization_failures["feature"]


@pytest.mark.asyncio
async def test_version_mismatch_fails_preflight_before_any_initializer() -> None:
    events: list[str] = []
    registry = PluginRegistry(allow_offline_execution=True)
    _register(
        registry,
        _RecordingPlugin("feature", events, dependencies=["foundation>=2.0.0"]),
        _RecordingPlugin("foundation", events, version="1.9.9"),
    )

    with pytest.raises(
        PluginDependencyGraphError,
        match=r"dependency 'foundation' requires >=2\.0\.0, found 1\.9\.9",
    ):
        await registry.initialize_all(_context())

    assert events == []


@pytest.mark.asyncio
async def test_invalid_dependency_spec_fails_preflight_before_any_initializer() -> None:
    events: list[str] = []
    registry = PluginRegistry(allow_offline_execution=True)
    _register(
        registry,
        _RecordingPlugin("independent", events),
        _RecordingPlugin("feature", events, dependencies=["foundation==1.0.0"]),
        _RecordingPlugin("foundation", events),
    )

    with pytest.raises(
        PluginDependencyGraphError,
        match="expected 'name' or 'name>=minimum-version'",
    ):
        await registry.initialize_all(_context())

    assert events == []


@pytest.mark.asyncio
async def test_cycle_and_its_downstream_are_recorded_before_initialization() -> None:
    events: list[str] = []
    registry = PluginRegistry(allow_offline_execution=True)
    _register(
        registry,
        _RecordingPlugin("a", events, dependencies=["b"]),
        _RecordingPlugin("b", events, dependencies=["a"]),
        _RecordingPlugin("downstream", events, dependencies=["a"]),
    )

    with pytest.raises(PluginDependencyGraphError, match="dependency cycle detected"):
        await registry.initialize_all(_context())

    assert events == []
    assert "dependency cycle detected" in registry.initialization_failures["a"]
    assert "dependency cycle detected" in registry.initialization_failures["b"]
    assert (
        registry.initialization_failures["downstream"]
        == "dependency 'a' has an invalid dependency graph"
    )


@pytest.mark.asyncio
async def test_failed_dependency_blocks_transitive_dependents_and_records_causes() -> None:
    events: list[str] = []
    state_store = _StateStore(
        {
            name: PluginState(plugin_name=name)
            for name in ("leaf", "middle", "foundation", "independent")
        }
    )
    registry = PluginRegistry(state_store)  # type: ignore[arg-type]
    _register(
        registry,
        _RecordingPlugin("leaf", events, dependencies=["middle"]),
        _RecordingPlugin("middle", events, dependencies=["foundation"]),
        _RecordingPlugin("foundation", events, init_error="foundation exploded"),
        _RecordingPlugin("independent", events),
    )

    await registry.initialize_all(_context())
    await registry.shutdown_all()

    assert events == [
        "init:foundation",
        "shutdown:foundation",
        "init:independent",
        "shutdown:independent",
    ]
    assert state_store.initialized == ["independent"]
    assert state_store.failed["foundation"] == "foundation exploded"
    assert state_store.failed["middle"] == (
        "dependency 'foundation' failed initialization: foundation exploded"
    )
    assert state_store.failed["leaf"] == (
        "dependency 'middle' failed initialization: "
        "dependency 'foundation' failed initialization: foundation exploded"
    )


@pytest.mark.asyncio
async def test_enabled_plugin_cannot_use_disabled_dependency() -> None:
    events: list[str] = []
    state_store = _StateStore(
        {
            "foundation": PluginState(
                plugin_name="foundation", enabled=False, status="disabled"
            ),
            "feature": PluginState(plugin_name="feature"),
        }
    )
    registry = PluginRegistry(state_store)  # type: ignore[arg-type]
    _register(
        registry,
        _RecordingPlugin("foundation", events),
        _RecordingPlugin("feature", events, dependencies=["foundation"]),
    )

    with pytest.raises(
        PluginDependencyGraphError,
        match="dependency 'foundation' is not enabled for initialization",
    ):
        await registry.initialize_all(_context())

    assert events == []
    assert "not enabled" in state_store.failed["feature"]


@pytest.mark.asyncio
async def test_direct_initialization_requires_initialized_active_dependency() -> None:
    events: list[str] = []
    registry = PluginRegistry(allow_offline_execution=True)
    _register(
        registry,
        _RecordingPlugin("feature", events, dependencies=["foundation"]),
        _RecordingPlugin("foundation", events),
    )

    with pytest.raises(
        PluginDependencyBlockedError,
        match="dependency 'foundation' is not initialized",
    ):
        await registry.initialize_plugin("feature", _context())

    assert events == []
