from __future__ import annotations

from dataclasses import replace

import pytest

from app.agent.registry import AgentToolRegistry
from app.channel.adapters import (
    ChannelAdapterDescriptor,
    ChannelAdapterRegistration,
    ChannelProbeResult,
)
from app.channel.connections import ChannelConnectionDocument
from app.common.types import Channel, InboundEvent, Message, Session
from app.container import Container
from app.orchestrator.pipeline import PipelineContext
from app.plugin.base import Plugin, PluginContext, PluginMeta, plugin_capability_digest
from app.plugin.dependencies import resolve_plugin_dependency_graph
from app.plugin.registry import PluginRegistrationError, PluginRegistry
from app.plugin.state import PluginScopeState, PluginState, PluginStateStore


class _Plugin(Plugin):
    meta = PluginMeta(name="demo", version="1.0.0")

    async def initialize(self, ctx: PluginContext) -> None:
        _ = ctx


class _FoundationPlugin(_Plugin):
    meta = PluginMeta(name="foundation", version="1.0.0")


class _DependentPlugin(_Plugin):
    meta = PluginMeta(
        name="dependent",
        version="1.0.0",
        dependencies=["foundation>=1.0.0"],
    )


class _StateStore:
    def __init__(self) -> None:
        self.state = PluginState(
            plugin_name="demo",
            version="1.0.0",
            installed=True,
            enabled=True,
        )
        self.scope: PluginScopeState | None = None
        self.fail = False
        self.scope_calls: list[tuple[str, str, str]] = []
        self.initialized: list[str] = []
        self.initialization_acknowledged = True
        self.failures: dict[str, str] = {}

    async def get(self, plugin_name: str) -> PluginState | None:
        assert plugin_name == "demo"
        if self.fail:
            raise RuntimeError("db unavailable")
        return self.state

    async def resolve_effective_scope(
        self,
        tenant_id: str,
        session_id: str,
        plugin_name: str,
    ) -> PluginScopeState | None:
        if self.fail:
            raise RuntimeError("db unavailable")
        self.scope_calls.append((tenant_id, session_id, plugin_name))
        return self.scope

    async def mark_initialized(self, plugin_name: str, expected_version: str) -> bool:
        assert expected_version == "1.0.0"
        self.initialized.append(plugin_name)
        return self.initialization_acknowledged

    async def mark_failed(
        self,
        plugin_name: str,
        error: str,
        expected_version: str,
    ) -> None:
        assert expected_version == "1.0.0"
        self.failures[plugin_name] = error


@pytest.mark.asyncio
async def test_offline_registry_fails_closed_for_plugin_execution_by_default() -> None:
    registry = PluginRegistry()
    registry._register(_Plugin())

    await registry.initialize_all(
        PluginContext(
            container=Container(agent_tool_registry=AgentToolRegistry()),
            settings=object(),
            db_ok=False,
        )
    )

    assert registry.is_initialized("demo") is False
    assert registry.is_active("demo") is False
    assert await registry.global_execution_allowed("demo") is False
    assert (
        await registry.scope_execution_allowed(
            "demo",
            tenant_id="tenant",
            session_id="room",
        )
        is False
    )
    assert await registry.global_execution_allowed("core") is True


def _registry(store: _StateStore) -> PluginRegistry:
    registry = PluginRegistry(store)  # type: ignore[arg-type]
    registry._register(_Plugin())
    registry._active_plugins.add("demo")
    registry._initialized_plugins.add("demo")
    return registry


@pytest.mark.asyncio
async def test_global_execution_policy_is_durable_and_fail_closed() -> None:
    store = _StateStore()
    registry = _registry(store)

    assert await registry.global_execution_allowed("core") is True
    assert await registry.global_execution_allowed("channel") is True
    assert await registry.global_execution_allowed("unknown") is False
    assert await registry.global_execution_allowed("demo") is True

    store.state = replace(store.state, restart_required=True)
    assert await registry.global_execution_allowed("demo") is False
    store.state = replace(store.state, restart_required=False, enabled=False)
    assert await registry.global_execution_allowed("demo") is False
    store.state = replace(store.state, enabled=True, version="2.0.0")
    assert await registry.global_execution_allowed("demo") is False
    store.fail = True
    assert await registry.global_execution_allowed("demo") is False


@pytest.mark.asyncio
async def test_scope_execution_policy_uses_effective_scope_and_fails_closed() -> None:
    store = _StateStore()
    registry = _registry(store)
    store.scope = PluginScopeState(
        tenant_id="tenant",
        session_id="room",
        plugin_name="demo",
        enabled=False,
        config={},
        version=1,
        updated_at="2026-07-18T00:00:00Z",
    )

    assert (
        await registry.scope_execution_allowed(
            "demo",
            tenant_id="tenant",
            session_id="room",
        )
        is False
    )
    assert store.scope_calls == [("tenant", "room", "demo")]

    store.scope = None
    assert (
        await registry.scope_execution_allowed(
            "demo",
            tenant_id="tenant",
            session_id="room",
        )
        is True
    )
    assert (
        await registry.scope_execution_allowed(
            "demo",
            tenant_id="",
            session_id="room",
        )
        is False
    )


@pytest.mark.asyncio
async def test_reactivate_publishes_before_durable_enable_without_false_cas_failure() -> None:
    store = _StateStore()
    registry = PluginRegistry(store)  # type: ignore[arg-type]
    registry._register(_Plugin())
    container = Container(
        plugin_registry=registry,
        agent_tool_registry=AgentToolRegistry(),
    )
    ctx = PluginContext(container=container, settings=object())
    await registry.initialize_all(ctx)
    assert store.initialized == ["demo"]

    store.state = replace(store.state, enabled=False)
    await registry.deactivate_plugin("demo", container)

    assert await registry.reactivate_plugin("demo", ctx) is False
    assert registry.is_active("demo") is True
    assert store.initialized == ["demo"]


@pytest.mark.asyncio
async def test_execution_policy_recursively_gates_required_dependencies() -> None:
    class _DependencyStore:
        def __init__(self) -> None:
            self.states = {
                "foundation": PluginState(
                    plugin_name="foundation",
                    version="1.0.0",
                    installed=True,
                    enabled=True,
                ),
                "dependent": PluginState(
                    plugin_name="dependent",
                    version="1.0.0",
                    installed=True,
                    enabled=True,
                ),
            }
            self.disabled_scope_owner = ""

        async def get(self, plugin_name: str) -> PluginState | None:
            return self.states.get(plugin_name)

        async def resolve_effective_scope(
            self,
            tenant_id: str,
            session_id: str,
            plugin_name: str,
        ) -> PluginScopeState | None:
            if plugin_name != self.disabled_scope_owner:
                return None
            return PluginScopeState(
                tenant_id=tenant_id,
                session_id=session_id,
                plugin_name=plugin_name,
                enabled=False,
                config={},
                version=1,
                updated_at="2026-07-18T00:00:00Z",
            )

    store = _DependencyStore()
    registry = PluginRegistry(store)  # type: ignore[arg-type]
    registry._register(_FoundationPlugin())
    registry._register(_DependentPlugin())
    registry._active_plugins.update({"foundation", "dependent"})
    registry._initialized_plugins.update({"foundation", "dependent"})
    registry._dependency_graph = resolve_plugin_dependency_graph(
        registry.loaded_plugins
    )

    assert await registry.global_execution_allowed("dependent") is True
    store.states["foundation"] = replace(
        store.states["foundation"],
        enabled=False,
    )
    assert await registry.global_execution_allowed("dependent") is False

    store.states["foundation"] = replace(
        store.states["foundation"],
        enabled=True,
    )
    store.states["foundation"] = replace(
        store.states["foundation"],
        status="failed",
    )
    assert await registry.global_execution_allowed("dependent") is False

    store.states["foundation"] = replace(
        store.states["foundation"],
        status="active",
    )
    store.disabled_scope_owner = "foundation"
    assert (
        await registry.scope_execution_allowed(
            "dependent",
            tenant_id="tenant",
            session_id="room",
        )
        is False
    )


@pytest.mark.asyncio
async def test_database_execution_snapshot_resolves_dependencies_and_scope_in_one_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = PluginStateStore()
    captured: dict[str, object] = {}

    async def fake_fetch(sql: str, params: dict | None = None) -> list[dict]:
        captured["sql"] = sql
        captured["params"] = params or {}
        return [{"requested_count": 2, "present_count": 2, "allowed": True}]

    monkeypatch.setattr(store, "_fetch", fake_fetch)

    allowed = await store.execution_snapshot_allowed(
        {"foundation": "1.0.0", "dependent": "1.0.0"},
        tenant_id="tenant-a",
        session_id="room-a",
    )

    assert allowed is True
    assert "LEFT JOIN LATERAL" in str(captured["sql"])
    assert "state.status = 'active'" in str(captured["sql"])
    assert captured["params"] == {
        "plugin_names": ["foundation", "dependent"],
        "plugin_versions": ["1.0.0", "1.0.0"],
        "tenant_id": "tenant-a",
        "session_id": "room-a",
    }


@pytest.mark.asyncio
async def test_registry_uses_single_snapshot_for_transitive_scope_policy() -> None:
    class _SnapshotStore:
        database_execution_snapshot_enabled = True

        def __init__(self) -> None:
            self.calls: list[tuple[dict[str, str], str, str]] = []

        async def execution_snapshot_allowed(
            self,
            versions: dict[str, str],
            *,
            tenant_id: str = "",
            session_id: str = "",
        ) -> bool:
            self.calls.append((dict(versions), tenant_id, session_id))
            return True

        async def get(self, _plugin_name: str):
            raise AssertionError("snapshot path must not issue per-owner reads")

        async def resolve_effective_scope(self, *_args, **_kwargs):
            raise AssertionError("snapshot path must not issue per-owner scope reads")

    store = _SnapshotStore()
    registry = PluginRegistry(store)  # type: ignore[arg-type]
    registry._register(_FoundationPlugin())
    registry._register(_DependentPlugin())
    registry._active_plugins.update({"foundation", "dependent"})
    registry._initialized_plugins.update({"foundation", "dependent"})
    registry._dependency_graph = resolve_plugin_dependency_graph(registry.loaded_plugins)

    assert (
        await registry.scope_execution_allowed(
            "dependent",
            tenant_id="tenant-a",
            session_id="room-a",
        )
        is True
    )
    assert store.calls == [
        (
            {"foundation": "1.0.0", "dependent": "1.0.0"},
            "tenant-a",
            "room-a",
        )
    ]

    store.calls.clear()
    assert (
        await registry.owners_scope_execution_allowed(
            ("foundation", "dependent"),
            tenant_id="tenant-a",
            session_id="room-a",
        )
        is True
    )
    assert len(store.calls) == 1
    assert store.calls[0][0] == {
        "foundation": "1.0.0",
        "dependent": "1.0.0",
    }
    assert (
        await registry.owners_scope_execution_allowed(
            ("dependent", "unknown"),
            tenant_id="tenant-a",
            session_id="room-a",
        )
        is False
    )
    assert len(store.calls) == 1


@pytest.mark.asyncio
async def test_registry_uses_external_conversation_for_runtime_scope_policy() -> None:
    class _SnapshotStore:
        database_execution_snapshot_enabled = True

        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def execution_snapshot_allowed(
            self,
            _versions: dict[str, str],
            *,
            tenant_id: str = "",
            session_id: str = "",
        ) -> bool:
            self.calls.append((tenant_id, session_id))
            return True

    store = _SnapshotStore()
    registry = PluginRegistry(store)  # type: ignore[arg-type]
    registry._register(_Plugin())
    registry._active_plugins.add("demo")
    registry._initialized_plugins.add("demo")

    event = InboundEvent(
        message_id="message-1",
        tenant_id="tenant-a",
        channel=Channel.WECHAT,
        user_id="cx1:p:member",
        session_id="cx1:c:canonical@chatroom",
        external_conversation_id="external-room@chatroom",
        message=Message(content="hello"),
    )
    assert await registry.execution_allowed(
        "demo", PipelineContext(event=event, trace_id=event.trace_id)
    )

    session = Session(
        session_id=event.session_id,
        tenant_id=event.tenant_id,
        user_id=event.user_id,
        channel=event.channel,
        external_conversation_id=event.external_conversation_id,
    )
    assert await registry.session_execution_allowed("demo", session)
    assert store.calls == [
        ("tenant-a", "external-room@chatroom"),
        ("tenant-a", "external-room@chatroom"),
    ]


class _MediaProvider:
    name = "demo-media"

    def __init__(self) -> None:
        self.calls = 0

    async def list_recent_media_events(
        self,
        *,
        tenant_id: str,
        limit: int,
        session_id: str | None,
    ) -> list[dict[str, object]]:
        self.calls += 1
        return [{"tenant_id": tenant_id, "limit": limit, "session_id": session_id}]


class _MediaPlugin(_Plugin):
    def __init__(self, provider: _MediaProvider) -> None:
        self.provider = provider

    def get_admin_media_event_provider(self) -> _MediaProvider:
        return self.provider


@pytest.mark.asyncio
async def test_channel_adapter_probe_and_factory_are_scope_gated_after_capture() -> None:
    calls = {"probe": 0, "factory": 0}

    async def probe(_connection: ChannelConnectionDocument) -> ChannelProbeResult:
        calls["probe"] += 1
        return ChannelProbeResult(ok=True, status="online")

    def factory(_connection: ChannelConnectionDocument) -> object:
        calls["factory"] += 1
        return object()

    registration = ChannelAdapterRegistration(
        descriptor=ChannelAdapterDescriptor(
            adapter_id="demo-adapter",
            display_name="Demo",
            channel="demo",
        ),
        provider_factory=factory,  # type: ignore[arg-type]
        probe=probe,
    )

    class _AdapterPlugin(_Plugin):
        def get_channel_adapters(self) -> list[ChannelAdapterRegistration]:
            return [registration]

    store = _StateStore()
    registry = PluginRegistry(store)  # type: ignore[arg-type]
    plugin = _AdapterPlugin()
    registry._register(plugin)
    registry._active_plugins.add("demo")
    registry._initialized_plugins.add("demo")
    registry._descriptors["demo"] = plugin.build_descriptor()
    captured = registry.all_channel_adapters()[0]
    connection = ChannelConnectionDocument(
        tenant_id="tenant",
        connection_id="demo-connection",
        adapter_id="demo-adapter",
        display_name="Demo",
        desired_state="enabled",
        effective_state="online",
        secret_status="not_required",
        version=1,
        priority=100,
        required_for_launch=False,
    )
    store.scope = PluginScopeState(
        tenant_id="tenant",
        session_id="",
        plugin_name="demo",
        enabled=False,
        config={},
        version=1,
        updated_at="2026-07-18T00:00:00Z",
    )

    assert captured.probe is not None
    assert captured.provider_factory is not None

    result = await captured.probe(connection)
    with pytest.raises(RuntimeError, match="plugin runtime disabled"):
        await captured.provider_factory(connection)

    assert result.error_code == "plugin_runtime_disabled"
    assert calls == {"probe": 0, "factory": 0}


def test_channel_adapter_capability_proxies_preserve_missing_capabilities() -> None:
    registration = ChannelAdapterRegistration(
        descriptor=ChannelAdapterDescriptor(
            adapter_id="metadata-only",
            display_name="Metadata only",
            channel="demo",
        )
    )

    class _AdapterPlugin(_Plugin):
        def get_channel_adapters(self) -> list[ChannelAdapterRegistration]:
            return [registration]

    registry = PluginRegistry(_StateStore())  # type: ignore[arg-type]
    plugin = _AdapterPlugin()
    registry._register(plugin)
    registry._active_plugins.add("demo")
    registry._initialized_plugins.add("demo")
    registry._descriptors["demo"] = plugin.build_descriptor()

    captured = registry.all_channel_adapters()[0]

    assert captured.provider_factory is None
    assert captured.probe is None


@pytest.mark.asyncio
async def test_admin_media_provider_is_scope_gated_after_capture() -> None:
    store = _StateStore()
    provider = _MediaProvider()
    registry = PluginRegistry(store)  # type: ignore[arg-type]
    plugin = _MediaPlugin(provider)
    registry._register(plugin)
    registry._descriptors["demo"] = plugin.build_descriptor()
    registry._active_plugins.add("demo")
    registry._initialized_plugins.add("demo")
    proxy = registry.all_admin_media_event_providers()[0]

    store.scope = PluginScopeState(
        tenant_id="tenant",
        session_id="room",
        plugin_name="demo",
        enabled=False,
        config={},
        version=1,
        updated_at="2026-07-18T00:00:00Z",
    )
    assert (
        await proxy.list_recent_media_events(
            tenant_id="tenant",
            limit=10,
            session_id="room",
        )
        == []
    )
    assert provider.calls == 0


class _InvalidSchemaPlugin(_Plugin):
    def __init__(self) -> None:
        self.shutdown_called = False

    def get_config_schema(self) -> dict[str, object]:
        return {"type": "object", "$ref": "https://invalid.example/schema"}

    async def shutdown(self) -> None:
        self.shutdown_called = True


@pytest.mark.asyncio
async def test_invalid_contribution_contract_rolls_back_initialization() -> None:
    plugin = _InvalidSchemaPlugin()
    registry = PluginRegistry(allow_offline_execution=True)
    registry._register(plugin)

    await registry.initialize_all(
        PluginContext(
            container=Container(agent_tool_registry=AgentToolRegistry()),
            settings=object(),
        )
    )

    assert registry.is_active("demo") is False
    assert registry.is_initialized("demo") is False
    assert plugin.shutdown_called is True
    assert "demo" in registry.initialization_failures


def _package_manifest(**overrides: object) -> dict[str, object]:
    descriptor = _Plugin().build_descriptor()
    manifest: dict[str, object] = {
        "name": "demo",
        "version": "1.0.0",
        "permissions": [],
        "dependencies": [],
        "config_schema": {},
        "capabilities": {
            "routes": [],
            "hooks": [],
            "agent_tools": [],
            "commands": [],
        },
        "capability_digest": plugin_capability_digest(descriptor),
    }
    manifest.update(overrides)
    return manifest


@pytest.mark.asyncio
async def test_local_package_contract_must_match_runtime_before_publish() -> None:
    store = _StateStore()
    store.state = replace(
        store.state,
        source="local",
        metadata={"manifest": _package_manifest()},
    )
    registry = PluginRegistry(store)  # type: ignore[arg-type]
    registry._register(_Plugin())

    await registry.initialize_all(
        PluginContext(
            container=Container(agent_tool_registry=AgentToolRegistry()),
            settings=object(),
        )
    )

    assert registry.is_active("demo") is True
    assert store.initialized == ["demo"]


@pytest.mark.asyncio
async def test_local_package_contract_drift_rolls_back_before_publish() -> None:
    store = _StateStore()
    store.state = replace(
        store.state,
        source="local",
        metadata={"manifest": _package_manifest(version="9.9.9")},
    )
    registry = PluginRegistry(store)  # type: ignore[arg-type]
    registry._register(_Plugin())

    await registry.initialize_all(
        PluginContext(
            container=Container(agent_tool_registry=AgentToolRegistry()),
            settings=object(),
        )
    )

    assert registry.is_active("demo") is False
    assert registry.is_initialized("demo") is False
    assert store.initialized == []
    assert "version drift" in store.failures["demo"]


@pytest.mark.asyncio
async def test_initialization_state_cas_loss_rolls_back_local_activation() -> None:
    store = _StateStore()
    store.initialization_acknowledged = False
    registry = PluginRegistry(store)  # type: ignore[arg-type]
    registry._register(_Plugin())

    await registry.initialize_all(
        PluginContext(
            container=Container(agent_tool_registry=AgentToolRegistry()),
            settings=object(),
        )
    )

    assert registry.is_active("demo") is False
    assert registry.is_initialized("demo") is False
    assert store.failures["demo"] == "plugin_state_changed_during_initialization"


def test_registration_rejects_identity_drift_and_duplicate_ownership() -> None:
    registry = PluginRegistry()
    with pytest.raises(PluginRegistrationError, match="identity mismatch"):
        registry._register(_Plugin(), expected_name="other")

    registry._register(_Plugin())
    with pytest.raises(PluginRegistrationError, match="duplicate plugin registration"):
        registry._register(_Plugin())


def test_registration_rejects_reserved_kernel_owner() -> None:
    class ReservedPlugin(_Plugin):
        meta = PluginMeta(name="core", version="1.0.0")

    with pytest.raises(PluginRegistrationError, match="reserved kernel owner"):
        PluginRegistry()._register(ReservedPlugin())
