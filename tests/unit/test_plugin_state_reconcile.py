from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.plugin.base import Plugin, PluginContext, PluginMeta
from app.plugin.state import (
    PLUGIN_STATUS_DISABLED,
    PLUGIN_STATUS_PENDING_RESTART,
    PluginState,
    PluginStateStore,
)


class _DiscoveredPlugin(Plugin):
    def __init__(self, name: str, version: str) -> None:
        self.meta = PluginMeta(name=name, version=version, description="Discovered plugin")

    async def initialize(self, ctx: PluginContext) -> None:
        _ = ctx


class _MemoryStateStore(PluginStateStore):
    def __init__(self, state: PluginState | None = None) -> None:
        self.states = {state.plugin_name: state} if state is not None else {}
        self.acknowledged: list[tuple[str, str]] = []

    async def get(self, plugin_name: str) -> PluginState | None:
        return self.states.get(plugin_name)

    async def create(self, state: PluginState) -> None:
        self.states.setdefault(state.plugin_name, state)

    async def update_discovered_metadata(
        self,
        plugin_name: str,
        version: str,
        metadata: dict[str, Any],
    ) -> None:
        current = self.states[plugin_name]
        self.states[plugin_name] = replace(
            current,
            metadata={**current.metadata, **metadata},
        )

    async def advance_builtin_version(
        self,
        plugin_name: str,
        *,
        expected_version: str,
        discovered_version: str,
    ) -> bool:
        current = self.states[plugin_name]
        if current.source != "builtin" or current.version != expected_version:
            return False
        self.states[plugin_name] = replace(
            current,
            version=discovered_version,
            status=PLUGIN_STATUS_PENDING_RESTART,
            restart_required=True,
            last_error="",
        )
        return True

    async def acknowledge_disabled_restart(
        self,
        plugin_name: str,
        discovered_version: str,
    ) -> None:
        self.acknowledged.append((plugin_name, discovered_version))
        current = self.states[plugin_name]
        if (
            current.installed
            and not current.enabled
            and current.restart_required
            and current.version == discovered_version
        ):
            self.states[plugin_name] = replace(
                current,
                status=PLUGIN_STATUS_DISABLED,
                restart_required=False,
                last_error="",
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source,metadata",
    [
        pytest.param(
            "local",
            {"artifact": {"package_type": "local_archive"}},
            id="local-archive-install",
        ),
        pytest.param(
            "marketplace",
            {"manifest": {"version": "2.0.0"}},
            id="disabled-upgrade",
        ),
    ],
)
async def test_reconcile_acknowledges_matching_disabled_plugin_restart(
    source: str,
    metadata: dict[str, Any],
) -> None:
    state = PluginState(
        plugin_name="demo_plugin",
        version="2.0.0",
        source=source,
        installed=True,
        enabled=False,
        status=PLUGIN_STATUS_PENDING_RESTART,
        restart_required=True,
        metadata=metadata,
    )
    store = _MemoryStateStore(state)
    plugin = _DiscoveredPlugin("demo_plugin", "2.0.0")

    reconciled = await store.reconcile({"demo_plugin": plugin})

    assert len(reconciled) == 1
    assert reconciled[0].status == PLUGIN_STATUS_DISABLED
    assert reconciled[0].restart_required is False
    assert store.acknowledged == [("demo_plugin", "2.0.0")]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["entrypoint", "install_directory"])
async def test_unapproved_external_discovery_is_quarantined(source: str) -> None:
    store = _MemoryStateStore()
    plugin = _DiscoveredPlugin("external_plugin", "1.0.0")

    reconciled = await store.reconcile(
        {"external_plugin": plugin},
        provenance={"external_plugin": source},
    )

    assert len(reconciled) == 1
    assert reconciled[0].source == source
    assert reconciled[0].installed is False
    assert reconciled[0].enabled is False
    assert reconciled[0].status == PLUGIN_STATUS_DISABLED
    assert reconciled[0].metadata["discovery_provenance"] == source


@pytest.mark.asyncio
async def test_trusted_builtin_discovery_is_enabled_on_first_reconcile() -> None:
    store = _MemoryStateStore()

    reconciled = await store.reconcile(
        {"builtin_plugin": _DiscoveredPlugin("builtin_plugin", "1.0.0")},
        provenance={"builtin_plugin": "builtin_directory"},
    )

    assert reconciled[0].source == "builtin"
    assert reconciled[0].installed is True
    assert reconciled[0].enabled is True
    assert reconciled[0].status == "active"


@pytest.mark.asyncio
async def test_reconcile_keeps_enabled_plugin_pending_until_initialize_succeeds() -> None:
    state = PluginState(
        plugin_name="demo_plugin",
        version="2.0.0",
        installed=True,
        enabled=True,
        status=PLUGIN_STATUS_PENDING_RESTART,
        restart_required=True,
    )
    store = _MemoryStateStore(state)
    plugin = _DiscoveredPlugin("demo_plugin", "2.0.0")

    reconciled = await store.reconcile({"demo_plugin": plugin})

    assert reconciled[0].status == PLUGIN_STATUS_PENDING_RESTART
    assert reconciled[0].restart_required is True
    assert store.acknowledged == []


@pytest.mark.asyncio
async def test_reconcile_does_not_acknowledge_mismatched_disabled_artifact() -> None:
    state = PluginState(
        plugin_name="demo_plugin",
        version="2.0.0",
        source="local",
        installed=True,
        enabled=False,
        status=PLUGIN_STATUS_PENDING_RESTART,
        restart_required=True,
    )
    store = _MemoryStateStore(state)
    plugin = _DiscoveredPlugin("demo_plugin", "1.9.0")

    reconciled = await store.reconcile({"demo_plugin": plugin})

    assert reconciled[0].version == "2.0.0"
    assert reconciled[0].status == PLUGIN_STATUS_PENDING_RESTART
    assert reconciled[0].restart_required is True
    assert store.acknowledged == []


@pytest.mark.asyncio
async def test_old_rolling_replica_cannot_downgrade_builtin_desired_version() -> None:
    state = PluginState(
        plugin_name="demo_plugin",
        version="2.0.0",
        source="builtin",
        installed=True,
        enabled=True,
        status=PLUGIN_STATUS_PENDING_RESTART,
        restart_required=True,
        metadata={"release": "new"},
    )
    store = _MemoryStateStore(state)

    reconciled = await store.reconcile(
        {"demo_plugin": _DiscoveredPlugin("demo_plugin", "1.9.0")}
    )

    assert reconciled[0].version == "2.0.0"
    assert reconciled[0].metadata == {"release": "new"}
    assert reconciled[0].restart_required is True


@pytest.mark.asyncio
async def test_new_builtin_version_advances_desired_state_monotonically() -> None:
    state = PluginState(
        plugin_name="demo_plugin",
        version="1.9.0",
        source="builtin",
        installed=True,
        enabled=True,
        status="active",
        restart_required=False,
    )
    store = _MemoryStateStore(state)

    reconciled = await store.reconcile(
        {"demo_plugin": _DiscoveredPlugin("demo_plugin", "2.0.0")}
    )

    assert reconciled[0].version == "2.0.0"
    assert reconciled[0].status == PLUGIN_STATUS_PENDING_RESTART
    assert reconciled[0].restart_required is True
    assert store.acknowledged == []


@pytest.mark.asyncio
async def test_builtin_version_reconcile_retries_after_cas_loser() -> None:
    class _ContendedStore(_MemoryStateStore):
        def __init__(self, state: PluginState) -> None:
            super().__init__(state)
            self.attempts = 0

        async def advance_builtin_version(
            self,
            plugin_name: str,
            *,
            expected_version: str,
            discovered_version: str,
        ) -> bool:
            self.attempts += 1
            if self.attempts == 1:
                current = self.states[plugin_name]
                self.states[plugin_name] = replace(
                    current,
                    version="2.0.0",
                    status=PLUGIN_STATUS_PENDING_RESTART,
                    restart_required=True,
                )
                return False
            return await super().advance_builtin_version(
                plugin_name,
                expected_version=expected_version,
                discovered_version=discovered_version,
            )

    store = _ContendedStore(
        PluginState(
            plugin_name="demo_plugin",
            version="1.0.0",
            source="builtin",
            installed=True,
            enabled=True,
        )
    )

    reconciled = await store.reconcile(
        {"demo_plugin": _DiscoveredPlugin("demo_plugin", "3.0.0")}
    )

    assert reconciled[0].version == "3.0.0"
    assert store.attempts == 2


@pytest.mark.asyncio
async def test_disabled_restart_acknowledgement_is_guarded_in_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def execute(sql: str, params: dict[str, Any] | None = None) -> None:
        captured["sql"] = sql
        captured["params"] = params

    store = PluginStateStore()
    monkeypatch.setattr(store, "_execute", execute)

    await store.acknowledge_disabled_restart("demo_plugin", "2.0.0")

    sql = str(captured["sql"])
    assert "installed = TRUE" in sql
    assert "enabled = FALSE" in sql
    assert "restart_required = TRUE" in sql
    assert "version = :discovered_version" in sql
    assert captured["params"] == {
        "plugin_name": "demo_plugin",
        "discovered_version": "2.0.0",
        "status": PLUGIN_STATUS_DISABLED,
    }


@pytest.mark.asyncio
async def test_uninstalled_restart_acknowledgement_clears_orphaned_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def execute(sql: str, params: dict[str, Any] | None = None) -> None:
        captured["sql"] = sql
        captured["params"] = params

    store = PluginStateStore()
    monkeypatch.setattr(store, "_execute", execute)

    await store.acknowledge_uninstalled_restarts()

    sql = str(captured["sql"])
    assert "installed = FALSE" in sql
    assert "enabled = FALSE" in sql
    assert "restart_required = TRUE" in sql
    assert "restart_required = FALSE" in sql
    assert captured["params"] == {"status": PLUGIN_STATUS_DISABLED}


@pytest.mark.asyncio
async def test_discovery_metadata_update_preserves_install_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def execute(sql: str, params: dict[str, Any] | None = None) -> None:
        captured["sql"] = sql
        captured["params"] = params

    store = PluginStateStore()
    monkeypatch.setattr(store, "_execute", execute)

    await store.update_discovered_metadata(
        "demo_plugin",
        "2.0.0",
        {"description": "runtime metadata"},
    )

    sql = str(captured["sql"])
    assert "COALESCE(metadata_json" in sql
    assert "|| CAST(:metadata_json AS JSONB)" in sql
    assert '"description": "runtime metadata"' in captured["params"]["metadata_json"]


@pytest.mark.asyncio
async def test_mark_initialized_is_fenced_by_enabled_state_and_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fetch(sql: str, params: dict[str, Any] | None = None):
        captured["sql"] = sql
        captured["params"] = params
        return [{"plugin_name": "demo_plugin"}]

    store = PluginStateStore()
    monkeypatch.setattr(store, "_fetch", fetch)

    assert await store.mark_initialized("demo_plugin", "2.0.0") is True

    sql = str(captured["sql"])
    assert "installed = TRUE" in sql
    assert "enabled = TRUE" in sql
    assert "version = :expected_version" in sql
    assert "RETURNING plugin_name" in sql
    assert captured["params"]["expected_version"] == "2.0.0"


@pytest.mark.asyncio
async def test_resolve_effective_scope_prefers_session_then_falls_back_to_tenant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'plugin-scope-resolution.db'}"
    )
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE plugin_scope_state (
                    tenant_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    plugin_name TEXT NOT NULL,
                    enabled BOOLEAN NOT NULL,
                    config_json TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, session_id, plugin_name)
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                INSERT INTO plugin_scope_state (
                    tenant_id, session_id, plugin_name, enabled,
                    config_json, version, updated_at
                ) VALUES
                    ('tenant-a', '', 'demo_plugin', TRUE, '{"level":"tenant"}', 2, 'tenant'),
                    ('tenant-a', 'room-1', 'demo_plugin', FALSE, '{"level":"session"}', 3, 'session')
                """
            )
        )
    monkeypatch.setattr("app.plugin.state.get_engine", lambda: engine)
    store = PluginStateStore()

    try:
        session_scope = await store.resolve_effective_scope(
            tenant_id="tenant-a",
            session_id="room-1",
            plugin_name="demo_plugin",
        )
        tenant_scope = await store.resolve_effective_scope(
            tenant_id="tenant-a",
            session_id="room-without-override",
            plugin_name="demo_plugin",
        )
    finally:
        await engine.dispose()

    assert session_scope is not None
    assert session_scope.session_id == "room-1"
    assert session_scope.enabled is False
    assert session_scope.config == {"level": "session"}
    assert session_scope.version == 3
    assert tenant_scope is not None
    assert tenant_scope.session_id == ""
    assert tenant_scope.enabled is True
    assert tenant_scope.config == {"level": "tenant"}
    assert tenant_scope.version == 2
