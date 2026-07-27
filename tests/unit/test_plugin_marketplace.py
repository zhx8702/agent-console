from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import HTTPException

from app.plugin.artifacts import compute_plugin_tree_digest
from app.plugin.base import (
    PLUGIN_API_VERSION,
    Plugin,
    PluginContext,
    PluginDescriptor,
    PluginMeta,
)
from app.plugin.manager import PluginManager
from app.plugin.marketplace import (
    MarketplaceManifestError,
    is_core_api_compatible,
    load_marketplace_manifest,
    permission_delta,
)
from app.plugin.state import PluginEvent, PluginScopeState, PluginState

_TEST_CAPABILITY_DIGEST = f"sha256:{'c' * 64}"


class _FakeSettings:
    def __init__(
        self,
        *,
        plugin_marketplace_path: str = "config/plugin-marketplace.yaml",
        project_root: Path | None = None,
        plugin_install_dir: str = ".runtime/plugins",
    ) -> None:
        self.project_root = project_root or Path(__file__).resolve().parents[2]
        self.plugin_marketplace_path = plugin_marketplace_path
        self.plugin_install_dir = plugin_install_dir


class _FakePluginRegistry:
    def __init__(self) -> None:
        self.loaded_plugins: dict[str, object] = {}
        self.descriptor_by_name: dict[str, PluginDescriptor] = {}
        self.config_schema_by_name: dict[str, dict[str, Any]] = {}
        self.admin_ui_by_name: dict[str, dict[str, Any]] = {}
        self.reactivated: list[str] = []
        self.deactivated: list[str] = []
        self.initialized = True
        self.active = True

    def descriptor(self, name: str) -> PluginDescriptor | None:
        return self.descriptor_by_name.get(name)

    def config_schema(self, name: str) -> dict[str, Any] | None:
        return self.config_schema_by_name.get(name)

    def admin_ui(self, name: str) -> dict[str, Any] | None:
        return self.admin_ui_by_name.get(name)

    async def global_execution_allowed(self, name: str) -> bool:
        _ = name
        return False

    def is_initialized(self, name: str) -> bool:
        _ = name
        return self.initialized

    def is_active(self, name: str) -> bool:
        _ = name
        return self.active

    async def reactivate_plugin(self, name: str, ctx: PluginContext) -> bool:
        _ = ctx
        self.reactivated.append(name)
        self.active = True
        return False

    async def deactivate_plugin(self, name: str, container: object) -> dict[str, int]:
        _ = container
        self.deactivated.append(name)
        self.active = False
        return {"hooks": 1, "agent_tools": 0, "commands": 0}


class _ManagerTestPlugin(Plugin):
    meta = PluginMeta(name="draw", version="0.1.0", description="Draw")

    async def initialize(self, ctx: PluginContext) -> None:
        _ = ctx

    def get_config_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["quiet", "normal"]},
            },
            "additionalProperties": False,
        }


class _DisabledCallbacksMustNotRunPlugin(_ManagerTestPlugin):
    def get_config_schema(self) -> dict[str, Any]:
        raise AssertionError("disabled plugin config callback must not run")

    def get_admin_ui(self) -> dict[str, Any]:
        raise AssertionError("disabled plugin admin UI callback must not run")

    def get_permissions(self) -> list[str]:
        raise AssertionError("disabled plugin permissions callback must not run")

    def get_capability_engines(self):
        raise AssertionError("disabled plugin capability callback must not run")

    async def get_runtime_status(self) -> dict[str, Any]:
        raise AssertionError("disabled plugin runtime callback must not run")


class _FakePluginStateStore:
    def __init__(self) -> None:
        self.states = {"draw": PluginState(plugin_name="draw", installed=True)}
        self.events_args: dict[str, object] | None = None
        self.scope_states_args: dict[str, object] | None = None
        self.scope_enabled_args: dict[str, object] | None = None
        self.appended_events: list[tuple[str, str, dict[str, object]]] = []

    async def get(self, plugin_name: str) -> PluginState | None:
        return self.states.get(plugin_name)

    async def list_events(
        self,
        *,
        plugin_name: str = "",
        event_type: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[PluginEvent]:
        self.events_args = {
            "plugin_name": plugin_name,
            "event_type": event_type,
            "limit": limit,
            "offset": offset,
        }
        return [
            PluginEvent(
                id=1,
                plugin_name=plugin_name or "draw",
                event_type=event_type or "install_succeeded",
                status="ok",
                actor_id="",
                actor_type="admin",
                request_id="",
                ip_address="",
                message="",
                metadata={"offset": offset},
                created_at="2026-04-28T00:00:00Z",
            )
        ]

    async def list_scope_states(
        self,
        *,
        tenant_id: str,
        session_id: str | None = None,
        plugin_name: str = "",
    ) -> list[PluginScopeState]:
        self.scope_states_args = {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "plugin_name": plugin_name,
        }
        return [
            PluginScopeState(
                tenant_id=tenant_id,
                session_id=session_id or "",
                plugin_name=plugin_name or "draw",
                enabled=True,
                config={"mode": "quiet"},
                version=1,
                updated_at="2026-04-28T00:00:00Z",
            )
        ]

    async def set_scope_enabled(
        self,
        *,
        tenant_id: str,
        session_id: str,
        plugin_name: str,
        enabled: bool,
        expected_version: int,
        config: dict[str, Any] | None = None,
    ) -> PluginScopeState:
        self.scope_enabled_args = {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "plugin_name": plugin_name,
            "enabled": enabled,
            "expected_version": expected_version,
            "config": config or {},
        }
        return PluginScopeState(
            tenant_id=tenant_id,
            session_id=session_id,
            plugin_name=plugin_name,
            enabled=enabled,
            config=config or {},
            version=expected_version + 1,
            updated_at="2026-04-28T00:00:00Z",
        )

    async def set_enabled(
        self,
        plugin_name: str,
        enabled: bool,
        *,
        restart_required: bool = False,
    ) -> PluginState | None:
        state = self.states.get(plugin_name)
        if state is None:
            return None
        next_state = replace(
            state,
            enabled=enabled,
            status="active" if enabled else "disabled",
            restart_required=restart_required,
        )
        self.states[plugin_name] = next_state
        return next_state

    async def list_states(self) -> list[PluginState]:
        return list(self.states.values())

    async def list_installed(self) -> list[PluginState]:
        return [state for state in self.states.values() if state.installed]

    async def has_pending_restart(self, *, exclude_plugin_name: str = "") -> bool:
        return any(
            state.restart_required and state.plugin_name != exclude_plugin_name
            for state in self.states.values()
        )

    async def upsert_marketplace_install(
        self,
        *,
        plugin_name: str,
        version: str,
        source: str,
        system: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> PluginState | None:
        state = PluginState(
            plugin_name=plugin_name,
            version=version,
            source=source,
            installed=True,
            enabled=False,
            system=system,
            status="pending_restart",
            restart_required=True,
            metadata=metadata or {},
        )
        self.states[plugin_name] = state
        return state

    async def mark_upgraded(
        self,
        *,
        plugin_name: str,
        version: str,
        source: str,
        metadata: dict[str, Any] | None = None,
    ) -> PluginState | None:
        state = self.states.get(plugin_name)
        if state is None:
            return None
        next_state = replace(
            state,
            version=version,
            source=source,
            status="pending_restart",
            restart_required=True,
            metadata=metadata or {},
        )
        self.states[plugin_name] = next_state
        return next_state

    async def append_event(self, plugin_name: str, event_type: str, **kwargs: object) -> None:
        self.appended_events.append((plugin_name, event_type, kwargs))


def _build_plugin_manager(
    state_store: _FakePluginStateStore,
    registry: _FakePluginRegistry | None = None,
    *,
    plugin_marketplace_path: str = "config/plugin-marketplace.yaml",
    project_root: Path | None = None,
    plugin_install_dir: str = ".runtime/plugins",
) -> PluginManager:
    ctx = PluginContext(
        container=object(),
        settings=_FakeSettings(
            plugin_marketplace_path=plugin_marketplace_path,
            project_root=project_root,
            plugin_install_dir=plugin_install_dir,
        ),
    )
    registry = registry or _FakePluginRegistry()
    registry.loaded_plugins.setdefault("draw", _ManagerTestPlugin())
    if "draw" not in registry.config_schema_by_name:
        plugin = registry.loaded_plugins["draw"]
        registry.config_schema_by_name["draw"] = plugin.get_config_schema()  # type: ignore[attr-defined]
    return PluginManager(cast(Any, registry), cast(Any, state_store), ctx)


def _write_zip(path: Path, files: dict[str, str]) -> str:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_zip_entries(
    path: Path,
    entries: list[tuple[str, str]],
    *,
    compression: int = zipfile.ZIP_STORED,
) -> str:
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _local_package_descriptor(
    *,
    name: str = "local_tool",
    version: str = "0.2.0",
    core_api: str = ">=0.1.0 <0.2.0",
    python: str = ">=3.11 <4.0",
    permissions: list[str] | None = None,
    dependencies: list[dict[str, object]] | None = None,
    capability_digest: str = _TEST_CAPABILITY_DIGEST,
) -> str:
    return json.dumps(
        {
            "schema_version": 2,
            "name": name,
            "version": version,
            "compatibility": {"core_api": core_api, "python": python},
            "permissions": permissions or [],
            "dependencies": dependencies or [],
            "capability_digest": capability_digest,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _write_local_archive_manifest(
    tmp_path: Path,
    archive_path: Path,
    checksum: str,
) -> Path:
    manifest_path = tmp_path / "plugin-marketplace.yaml"
    manifest_path.write_text(
        f"""
items:
  - name: local_tool
    version: 0.2.0
    source: local
    package:
      type: local_archive
      uri: {archive_path.relative_to(tmp_path).as_posix()}
      checksum: sha256:{checksum}
    compatibility:
      core_api: ">=0.1.0 <0.2.0"
      python: ">=3.11 <4.0"
    capability_digest: {_TEST_CAPABILITY_DIGEST}
    restart_policy: required_after_install
""",
        encoding="utf-8",
    )
    return manifest_path


def test_load_marketplace_manifest_parses_valid_item(tmp_path) -> None:
    path = tmp_path / "plugin-marketplace.yaml"
    path.write_text(
        """
items:
  - name: demo_plugin
    display_name: Demo Plugin
    version: 0.1.0
    description: Demo plugin
    source: builtin
    package:
      type: builtin
      uri: plugins/demo_plugin
    compatibility:
      core_api: ">=0.1.0 <0.2.0"
      python: ">=3.11"
    dependencies:
      - name: commands
        version: ">=0.1.0"
        required: true
    permissions:
      - id: admin_api
        level: local
        description: Admin API
    capabilities:
      routes: ["/plugins/demo_plugin"]
""",
        encoding="utf-8",
    )

    manifest = load_marketplace_manifest(path)

    assert len(manifest.items) == 1
    item = manifest.items[0]
    assert item.name == "demo_plugin"
    assert item.compatible is True
    assert item.dependencies[0].name == "commands"
    assert item.permissions[0].id == "admin_api"
    assert item.capabilities["routes"] == ["/plugins/demo_plugin"]


def test_dynamic_mutation_rejects_install_root_shared_with_builtins(
    tmp_path: Path,
) -> None:
    manager = _build_plugin_manager(
        _FakePluginStateStore(),
        project_root=tmp_path,
        plugin_install_dir="plugins",
    )

    with pytest.raises(
        HTTPException,
        match="plugin_install_dir_must_be_separate_from_builtins",
    ):
        manager._require_dynamic_mutation_allowed()


@pytest.mark.parametrize("app_env", ["prod", "production", "staging", "qa"])
def test_dynamic_mutation_fallback_fails_closed_for_production_like_aliases(
    app_env: str,
) -> None:
    manager = _build_plugin_manager(_FakePluginStateStore())
    manager.ctx.settings.app_env = app_env
    manager.ctx.settings.plugin_dynamic_mutations_enabled = True

    with pytest.raises(HTTPException) as excinfo:
        manager._require_dynamic_mutation_allowed()

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "dynamic_plugin_mutations_disabled"


def test_load_marketplace_manifest_rejects_duplicate_items(tmp_path) -> None:
    path = tmp_path / "plugin-marketplace.yaml"
    path.write_text(
        """
items:
  - name: demo_plugin
    version: 0.1.0
    package: {type: builtin, uri: plugins/demo_plugin}
    compatibility: {core_api: ">=0.1.0"}
  - name: demo_plugin
    version: 0.2.0
    package: {type: builtin, uri: plugins/demo_plugin}
    compatibility: {core_api: ">=0.1.0"}
""",
        encoding="utf-8",
    )

    with pytest.raises(MarketplaceManifestError, match="duplicate item 'demo_plugin'"):
        load_marketplace_manifest(path)


def test_load_marketplace_manifest_rejects_duplicate_dependencies(tmp_path) -> None:
    path = tmp_path / "plugin-marketplace.yaml"
    path.write_text(
        """
items:
  - name: demo_plugin
    version: 0.1.0
    package: {type: builtin, uri: plugins/demo_plugin}
    compatibility: {core_api: ">=0.1.0"}
    dependencies:
      - {name: commands, version: ">=0.1.0"}
      - {name: commands, version: ">=0.2.0"}
""",
        encoding="utf-8",
    )

    with pytest.raises(MarketplaceManifestError, match="duplicate dependency 'commands'"):
        load_marketplace_manifest(path)


def test_load_marketplace_manifest_rejects_duplicate_permissions(tmp_path) -> None:
    path = tmp_path / "plugin-marketplace.yaml"
    path.write_text(
        """
items:
  - name: demo_plugin
    version: 0.1.0
    package: {type: builtin, uri: plugins/demo_plugin}
    compatibility: {core_api: ">=0.1.0"}
    permissions:
      - admin_api
      - {id: admin_api, level: local}
""",
        encoding="utf-8",
    )

    with pytest.raises(MarketplaceManifestError, match="duplicate permission 'admin_api'"):
        load_marketplace_manifest(path)


@pytest.mark.parametrize(
    "capabilities, expected",
    [
        ("[]", "capabilities must be a mapping"),
        ('routes: "/plugins/demo_plugin"', "capability 'routes' must be a list"),
        ("routes: [42]", "capability 'routes' entry #0 must be a string"),
        ('routes: ["/plugins/demo_plugin", "/plugins/demo_plugin"]', "duplicate entry"),
    ],
)
def test_load_marketplace_manifest_rejects_invalid_capability_types(
    tmp_path,
    capabilities: str,
    expected: str,
) -> None:
    path = tmp_path / "plugin-marketplace.yaml"
    path.write_text(
        f"""
items:
  - name: demo_plugin
    version: 0.1.0
    package: {{type: builtin, uri: plugins/demo_plugin}}
    compatibility: {{core_api: ">=0.1.0"}}
    capabilities:
      {capabilities}
""",
        encoding="utf-8",
    )

    with pytest.raises(MarketplaceManifestError, match=expected):
        load_marketplace_manifest(path)


def test_load_marketplace_manifest_rejects_invalid_plugin_name(tmp_path) -> None:
    path = tmp_path / "plugin-marketplace.yaml"
    path.write_text(
        """
items:
  - name: Demo-Plugin
    version: 0.1.0
    package:
      type: builtin
    compatibility:
      core_api: ">=0.1.0"
""",
        encoding="utf-8",
    )

    with pytest.raises(MarketplaceManifestError, match="invalid_plugin_name"):
        load_marketplace_manifest(path)


def test_load_marketplace_manifest_rejects_unverified_signature_metadata(tmp_path) -> None:
    path = tmp_path / "plugin-marketplace.yaml"
    path.write_text(
        """
items:
  - name: demo_plugin
    display_name: Demo Plugin
    version: 0.1.0
    description: Demo plugin
    source: builtin
    package:
      type: builtin
      uri: plugins/demo_plugin
      signature: decorative-not-a-verifiable-signature
    compatibility:
      core_api: ">=0.1.0 <0.2.0"
      python: ">=3.11"
""",
        encoding="utf-8",
    )

    with pytest.raises(MarketplaceManifestError, match="unsupported without a trust store"):
        load_marketplace_manifest(path)


def test_load_marketplace_manifest_rejects_missing_required_fields(tmp_path) -> None:
    path = tmp_path / "plugin-marketplace.yaml"
    path.write_text("items:\n  - name: demo_plugin\n", encoding="utf-8")

    with pytest.raises(MarketplaceManifestError, match="version must be a string"):
        load_marketplace_manifest(path)


def test_load_marketplace_manifest_rejects_invalid_builtin_uri(tmp_path) -> None:
    path = tmp_path / "plugin-marketplace.yaml"
    path.write_text(
        """
items:
  - name: demo_plugin
    version: 0.1.0
    package:
      type: builtin
      uri: /tmp/demo_plugin
    compatibility:
      core_api: ">=0.1.0"
""",
        encoding="utf-8",
    )

    with pytest.raises(MarketplaceManifestError, match="builtin uri"):
        load_marketplace_manifest(path)


def test_load_marketplace_manifest_rejects_builtin_uri_for_another_plugin(
    tmp_path,
) -> None:
    path = tmp_path / "plugin-marketplace.yaml"
    path.write_text(
        """
items:
  - name: demo_plugin
    version: 0.1.0
    source: builtin
    package: {type: builtin, uri: plugins/other_plugin}
    compatibility: {core_api: ">=0.1.0"}
""",
        encoding="utf-8",
    )

    with pytest.raises(MarketplaceManifestError, match="must equal plugins/demo_plugin"):
        load_marketplace_manifest(path)


@pytest.mark.parametrize(
    ("source", "package_type"),
    [("builtin", "local_archive"), ("local", "builtin")],
)
def test_load_marketplace_manifest_rejects_source_package_type_mismatch(
    tmp_path,
    source: str,
    package_type: str,
) -> None:
    path = tmp_path / "plugin-marketplace.yaml"
    uri = "dist/demo_plugin.zip" if package_type == "local_archive" else "plugins/demo_plugin"
    path.write_text(
        f"""
items:
  - name: demo_plugin
    version: 0.1.0
    source: {source}
    package: {{type: {package_type}, uri: {uri}}}
    compatibility: {{core_api: ">=0.1.0"}}
""",
        encoding="utf-8",
    )

    with pytest.raises(MarketplaceManifestError, match=r"cannot use package\.type"):
        load_marketplace_manifest(path)


def test_core_api_compatibility_uses_plugin_api_version() -> None:
    assert is_core_api_compatible(f">={PLUGIN_API_VERSION} <0.2.0") is True
    assert is_core_api_compatible(">=9.0.0") is False


def test_permission_delta_reports_added_and_removed() -> None:
    assert permission_delta(["admin_api", "old"], ["admin_api", "network:demo"]) == {
        "added": ["network:demo"],
        "removed": ["old"],
    }


@pytest.mark.asyncio
async def test_builtin_marketplace_capabilities_come_from_runtime_descriptor() -> None:
    state_store = _FakePluginStateStore()
    registry = _FakePluginRegistry()
    registry.descriptor_by_name["draw"] = PluginDescriptor(
        name="draw",
        version="0.1.0",
        description="Draw",
        dependencies=(),
        permissions=("admin_api",),
        hooks=("before_postprocess:draw.reply",),
        agent_tools=("wxbot_group:generate_group_image",),
        commands=("/draw", "/redraw"),
        flow_steps=("plugin.draw.postprocess_result",),
        effects=("publish_media",),
        admin_routes=("GET /config",),
        storage_permissions=(),
        network_permissions=(),
    )
    manager = _build_plugin_manager(state_store, registry)

    payload = await manager.marketplace()
    draw = next(item for item in payload["items"] if item["name"] == "draw")
    installed = (await manager.installed())["plugins"][0]

    assert draw["capability_source"] == "runtime_descriptor"
    assert draw["capabilities"] == {
        "routes": ["GET /config"],
        "hooks": ["before_postprocess:draw.reply"],
        "agent_tools": ["wxbot_group:generate_group_image"],
        "commands": ["/draw", "/redraw"],
        "flow_steps": ["plugin.draw.postprocess_result"],
        "effects": ["publish_media"],
        "channel_adapters": [],
        "capability_engines": [],
        "admin_media_providers": [],
        "storage": [],
        "network": [],
    }
    assert installed["capability_source"] == "runtime_descriptor"
    assert installed["capabilities"] == draw["capabilities"]


@pytest.mark.asyncio
async def test_disabled_plugin_management_reads_cached_contract_without_callbacks() -> None:
    state_store = _FakePluginStateStore()
    state_store.states["draw"] = replace(
        state_store.states["draw"],
        enabled=False,
        status="disabled",
        metadata={
            "description": "Cached draw",
            "permissions": ["admin_api"],
            "admin_ui": {"scope": "group", "label": "Draw"},
        },
    )
    registry = _FakePluginRegistry()
    registry.loaded_plugins["draw"] = _DisabledCallbacksMustNotRunPlugin()
    registry.config_schema_by_name["draw"] = {
        "type": "object",
        "additionalProperties": False,
    }
    registry.admin_ui_by_name["draw"] = {
        "scope": "group",
        "label": "Draw",
    }
    manager = _build_plugin_manager(state_store, registry)

    schema = await manager.config_schema("draw")
    runtime = await manager.runtime("draw")
    installed = (await manager.installed())["plugins"][0]

    assert schema["schema"] == registry.config_schema_by_name["draw"]
    assert runtime["runtime_status"] == {
        "running": False,
        "execution_allowed": False,
    }
    assert installed["permissions"] == ["admin_api"]
    assert installed["admin_ui"] == {"scope": "group", "label": "Draw"}


@pytest.mark.asyncio
async def test_plugin_manager_installs_local_archive_package(tmp_path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    archive_path = dist_dir / "local_tool-0.2.0.zip"
    checksum = _write_zip(
        archive_path,
        {
            "plugin.py": "VERSION = '0.2.0'\n",
            "plugin-package.json": _local_package_descriptor(
                core_api=">=0.1.0",
                permissions=["admin_api"],
            ),
        },
    )
    manifest_path = tmp_path / "plugin-marketplace.yaml"
    manifest_path.write_text(
        f"""
items:
  - name: local_tool
    version: 0.2.0
    source: local
    package:
      type: local_archive
      uri: dist/local_tool-0.2.0.zip
      checksum: sha256:{checksum}
    compatibility:
      core_api: ">=0.1.0"
      python: ">=3.11 <4.0"
    permissions:
      - id: admin_api
    capability_digest: {_TEST_CAPABILITY_DIGEST}
    restart_policy: required_after_install
""",
        encoding="utf-8",
    )
    state_store = _FakePluginStateStore()
    manager = _build_plugin_manager(
        state_store,
        plugin_marketplace_path=str(manifest_path),
        project_root=tmp_path,
        plugin_install_dir="installed-plugins",
    )

    preview = await manager.install_preview({"name": "local_tool"})
    payload = await manager.install(
        {
            "name": "local_tool",
            "confirm_permissions": ["admin_api"],
            "confirm_restart_required": True,
        }
    )

    assert "unsupported_package_type" not in preview["warnings"]
    assert payload["plugin"]["plugin_name"] == "local_tool"
    assert payload["plugin"]["source"] == "local"
    assert payload["plugin"]["enabled"] is False
    assert payload["plugin"]["restart_required"] is True
    assert payload["plugin"]["metadata"]["manifest"]["package"]["type"] == "local_archive"
    artifact = payload["plugin"]["metadata"]["artifact"]
    assert artifact == {
        "package_type": "local_archive",
        "checksum": f"sha256:{checksum}",
        "installed_path": "installed-plugins/local_tool",
        "tree_digest": compute_plugin_tree_digest(
            tmp_path / "installed-plugins" / "local_tool"
        ),
    }
    assert (tmp_path / "installed-plugins" / "local_tool" / "plugin.py").read_text(encoding="utf-8") == "VERSION = '0.2.0'\n"
    assert [event[1] for event in state_store.appended_events] == [
        "install_requested",
        "install_succeeded",
    ]


@pytest.mark.asyncio
async def test_local_archive_validation_and_extraction_use_hashed_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = tmp_path / "local_tool.zip"
    checksum = _write_zip(
        archive_path,
        {
            "plugin.py": "VERSION = '0.2.0'\n",
            "plugin-package.json": _local_package_descriptor(),
        },
    )
    manifest_path = _write_local_archive_manifest(tmp_path, archive_path, checksum)
    manager = _build_plugin_manager(
        _FakePluginStateStore(),
        plugin_marketplace_path=str(manifest_path),
        project_root=tmp_path,
    )
    original_extract = manager._extract_local_archive

    def mutate_source_after_hash(
        snapshot_path: Path,
        target_dir: Path,
        item: object,
    ) -> None:
        assert snapshot_path != archive_path
        assert hashlib.sha256(snapshot_path.read_bytes()).hexdigest() == checksum
        archive_path.write_bytes(b"replaced after checksum")
        original_extract(snapshot_path, target_dir, item)  # type: ignore[arg-type]

    monkeypatch.setattr(manager, "_extract_local_archive", mutate_source_after_hash)

    await manager.install(
        {"name": "local_tool", "confirm_restart_required": True}
    )

    installed = tmp_path / ".runtime" / "plugins" / "local_tool" / "plugin.py"
    assert installed.read_text(encoding="utf-8") == "VERSION = '0.2.0'\n"


@pytest.mark.asyncio
async def test_local_archive_rolls_back_when_state_write_fails(tmp_path: Path) -> None:
    class FailingStateStore(_FakePluginStateStore):
        async def upsert_marketplace_install(self, **kwargs: Any) -> PluginState | None:
            _ = kwargs
            raise RuntimeError("state write failed")

    archive_path = tmp_path / "local_tool.zip"
    checksum = _write_zip(
        archive_path,
        {
            "plugin.py": "VERSION = '0.2.0'\n",
            "plugin-package.json": _local_package_descriptor(),
        },
    )
    manifest_path = _write_local_archive_manifest(tmp_path, archive_path, checksum)
    manager = _build_plugin_manager(
        FailingStateStore(),
        plugin_marketplace_path=str(manifest_path),
        project_root=tmp_path,
    )

    with pytest.raises(RuntimeError, match="state write failed"):
        await manager.install({"name": "local_tool", "confirm_restart_required": True})

    assert not (tmp_path / ".runtime" / "plugins" / "local_tool").exists()


@pytest.mark.asyncio
async def test_local_archive_keeps_new_artifact_after_ambiguous_committed_write(
    tmp_path: Path,
) -> None:
    class AmbiguousStateStore(_FakePluginStateStore):
        async def upsert_marketplace_install(self, **kwargs: Any) -> PluginState | None:
            await super().upsert_marketplace_install(**kwargs)
            raise RuntimeError("connection lost after commit")

    archive_path = tmp_path / "local_tool.zip"
    checksum = _write_zip(
        archive_path,
        {
            "plugin.py": "VERSION = '0.2.0'\n",
            "plugin-package.json": _local_package_descriptor(),
        },
    )
    manifest_path = _write_local_archive_manifest(tmp_path, archive_path, checksum)
    manager = _build_plugin_manager(
        AmbiguousStateStore(),
        plugin_marketplace_path=str(manifest_path),
        project_root=tmp_path,
    )

    with pytest.raises(RuntimeError, match="connection lost after commit"):
        await manager.install({"name": "local_tool", "confirm_restart_required": True})

    installed = tmp_path / ".runtime" / "plugins" / "local_tool" / "plugin.py"
    assert installed.read_text(encoding="utf-8") == "VERSION = '0.2.0'\n"


@pytest.mark.asyncio
async def test_plugin_manager_rejects_non_zip_local_archive(tmp_path: Path) -> None:
    archive_path = tmp_path / "local_tool.tar.gz"
    archive_path.write_bytes(b"not-a-zip")
    manifest_path = _write_local_archive_manifest(
        tmp_path,
        archive_path,
        hashlib.sha256(archive_path.read_bytes()).hexdigest(),
    )
    manager = _build_plugin_manager(
        _FakePluginStateStore(),
        plugin_marketplace_path=str(manifest_path),
        project_root=tmp_path,
    )

    with pytest.raises(HTTPException) as exc_info:
        await manager.install({"name": "local_tool", "confirm_restart_required": True})

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "invalid_local_archive_uri"


@pytest.mark.asyncio
async def test_plugin_manager_rejects_archive_without_root_plugin_py(tmp_path: Path) -> None:
    archive_path = tmp_path / "local_tool.zip"
    checksum = _write_zip(
        archive_path,
        {
            "local_tool/plugin.py": "VERSION = '0.2.0'\n",
            "plugin-package.json": _local_package_descriptor(),
        },
    )
    manifest_path = _write_local_archive_manifest(tmp_path, archive_path, checksum)
    state_store = _FakePluginStateStore()
    manager = _build_plugin_manager(
        state_store,
        plugin_marketplace_path=str(manifest_path),
        project_root=tmp_path,
    )

    with pytest.raises(HTTPException) as exc_info:
        await manager.install({"name": "local_tool", "confirm_restart_required": True})

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "local_archive_plugin_entrypoint_required"
    assert not (tmp_path / ".runtime" / "plugins" / "local_tool").exists()
    assert state_store.appended_events[-1][1] == "install_failed"


@pytest.mark.asyncio
async def test_plugin_manager_rejects_archive_without_package_descriptor(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "local_tool.zip"
    checksum = _write_zip(archive_path, {"plugin.py": "VERSION = '0.2.0'\n"})
    manifest_path = _write_local_archive_manifest(tmp_path, archive_path, checksum)
    manager = _build_plugin_manager(
        _FakePluginStateStore(),
        plugin_marketplace_path=str(manifest_path),
        project_root=tmp_path,
    )

    with pytest.raises(HTTPException) as exc_info:
        await manager.install({"name": "local_tool", "confirm_restart_required": True})

    assert exc_info.value.detail == "local_archive_descriptor_required"


@pytest.mark.parametrize(
    "descriptor",
    [
        _local_package_descriptor(name="different_plugin"),
        _local_package_descriptor(version="9.9.9"),
    ],
)
@pytest.mark.asyncio
async def test_plugin_manager_rejects_archive_identity_mismatch(
    tmp_path: Path,
    descriptor: str,
) -> None:
    archive_path = tmp_path / "local_tool.zip"
    checksum = _write_zip(
        archive_path,
        {
            "plugin.py": "VERSION = '0.2.0'\n",
            "plugin-package.json": descriptor,
        },
    )
    manifest_path = _write_local_archive_manifest(tmp_path, archive_path, checksum)
    manager = _build_plugin_manager(
        _FakePluginStateStore(),
        plugin_marketplace_path=str(manifest_path),
        project_root=tmp_path,
    )

    with pytest.raises(HTTPException) as exc_info:
        await manager.install({"name": "local_tool", "confirm_restart_required": True})

    assert exc_info.value.detail == "local_archive_identity_mismatch"
    assert not (tmp_path / ".runtime" / "plugins" / "local_tool").exists()


@pytest.mark.asyncio
async def test_plugin_manager_rejects_too_many_archive_members(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.plugin.manager._LOCAL_ARCHIVE_MAX_MEMBERS", 1)
    archive_path = tmp_path / "local_tool.zip"
    checksum = _write_zip(
        archive_path,
        {
            "plugin.py": "VERSION = '0.2.0'\n",
            "plugin-package.json": _local_package_descriptor(),
            "README.md": "demo",
        },
    )
    manifest_path = _write_local_archive_manifest(tmp_path, archive_path, checksum)
    manager = _build_plugin_manager(
        _FakePluginStateStore(),
        plugin_marketplace_path=str(manifest_path),
        project_root=tmp_path,
    )

    with pytest.raises(HTTPException) as exc_info:
        await manager.install({"name": "local_tool", "confirm_restart_required": True})

    assert exc_info.value.detail == "local_archive_member_count_exceeded"


@pytest.mark.asyncio
async def test_plugin_manager_rejects_oversized_archive_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.plugin.manager._LOCAL_ARCHIVE_MAX_MEMBER_BYTES", 256)
    archive_path = tmp_path / "local_tool.zip"
    checksum = _write_zip(
        archive_path,
        {
            "plugin.py": "x" * 257,
            "plugin-package.json": _local_package_descriptor(),
        },
    )
    manifest_path = _write_local_archive_manifest(tmp_path, archive_path, checksum)
    manager = _build_plugin_manager(
        _FakePluginStateStore(),
        plugin_marketplace_path=str(manifest_path),
        project_root=tmp_path,
    )

    with pytest.raises(HTTPException) as exc_info:
        await manager.install({"name": "local_tool", "confirm_restart_required": True})

    assert exc_info.value.detail == "local_archive_member_too_large"


@pytest.mark.asyncio
async def test_plugin_manager_rejects_excessive_total_uncompressed_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = _local_package_descriptor()
    monkeypatch.setattr(
        "app.plugin.manager._LOCAL_ARCHIVE_MAX_MEMBER_BYTES",
        len(descriptor) + 1,
    )
    monkeypatch.setattr(
        "app.plugin.manager._LOCAL_ARCHIVE_MAX_TOTAL_BYTES",
        len(descriptor) + 8,
    )
    archive_path = tmp_path / "local_tool.zip"
    checksum = _write_zip(
        archive_path,
        {
            "plugin.py": "12345",
            "plugin-package.json": descriptor,
            "data.txt": "6789",
        },
    )
    manifest_path = _write_local_archive_manifest(tmp_path, archive_path, checksum)
    manager = _build_plugin_manager(
        _FakePluginStateStore(),
        plugin_marketplace_path=str(manifest_path),
        project_root=tmp_path,
    )

    with pytest.raises(HTTPException) as exc_info:
        await manager.install({"name": "local_tool", "confirm_restart_required": True})

    assert exc_info.value.detail == "local_archive_uncompressed_size_exceeded"


@pytest.mark.asyncio
async def test_plugin_manager_rejects_excessive_compression_ratio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.plugin.manager._LOCAL_ARCHIVE_MAX_COMPRESSION_RATIO", 2.0)
    archive_path = tmp_path / "local_tool.zip"
    checksum = _write_zip_entries(
        archive_path,
        [
            ("plugin.py", "#" * 4096),
            ("plugin-package.json", _local_package_descriptor()),
        ],
        compression=zipfile.ZIP_DEFLATED,
    )
    manifest_path = _write_local_archive_manifest(tmp_path, archive_path, checksum)
    manager = _build_plugin_manager(
        _FakePluginStateStore(),
        plugin_marketplace_path=str(manifest_path),
        project_root=tmp_path,
    )

    with pytest.raises(HTTPException) as exc_info:
        await manager.install({"name": "local_tool", "confirm_restart_required": True})

    assert exc_info.value.detail == "local_archive_compression_ratio_exceeded"


@pytest.mark.asyncio
async def test_plugin_manager_rejects_duplicate_normalized_archive_paths(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "local_tool.zip"
    checksum = _write_zip_entries(
        archive_path,
        [
            ("plugin.py", "VERSION = '0.2.0'\n"),
            ("./plugin.py", "VERSION = 'shadow'\n"),
            ("plugin-package.json", _local_package_descriptor()),
        ],
    )
    manifest_path = _write_local_archive_manifest(tmp_path, archive_path, checksum)
    manager = _build_plugin_manager(
        _FakePluginStateStore(),
        plugin_marketplace_path=str(manifest_path),
        project_root=tmp_path,
    )

    with pytest.raises(HTTPException) as exc_info:
        await manager.install({"name": "local_tool", "confirm_restart_required": True})

    assert exc_info.value.detail == "duplicate_local_archive_member"


@pytest.mark.asyncio
async def test_plugin_manager_rejects_archive_symlink_member(tmp_path: Path) -> None:
    archive_path = tmp_path / "local_tool.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("plugin.py", "VERSION = '0.2.0'\n")
        archive.writestr("plugin-package.json", _local_package_descriptor())
        link = zipfile.ZipInfo("plugin-link.py")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        archive.writestr(link, "plugin.py")
    checksum = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    manifest_path = _write_local_archive_manifest(tmp_path, archive_path, checksum)
    manager = _build_plugin_manager(
        _FakePluginStateStore(),
        plugin_marketplace_path=str(manifest_path),
        project_root=tmp_path,
    )

    with pytest.raises(HTTPException) as exc_info:
        await manager.install({"name": "local_tool", "confirm_restart_required": True})

    assert exc_info.value.detail == "unsafe_local_archive_member"


@pytest.mark.asyncio
async def test_plugin_manager_rejects_file_directory_path_conflict(tmp_path: Path) -> None:
    archive_path = tmp_path / "local_tool.zip"
    checksum = _write_zip_entries(
        archive_path,
        [
            ("plugin.py", "VERSION = '0.2.0'\n"),
            ("plugin-package.json", _local_package_descriptor()),
            ("assets", "not-a-directory"),
            ("assets/icon.txt", "icon"),
        ],
    )
    manifest_path = _write_local_archive_manifest(tmp_path, archive_path, checksum)
    manager = _build_plugin_manager(
        _FakePluginStateStore(),
        plugin_marketplace_path=str(manifest_path),
        project_root=tmp_path,
    )

    with pytest.raises(HTTPException) as exc_info:
        await manager.install({"name": "local_tool", "confirm_restart_required": True})

    assert exc_info.value.detail == "conflicting_local_archive_member"


@pytest.mark.asyncio
async def test_plugin_manager_rejects_local_archive_absolute_uri(tmp_path) -> None:
    manifest_path = tmp_path / "plugin-marketplace.yaml"
    manifest_path.write_text(
        """
items:
  - name: local_tool
    version: 0.2.0
    source: local
    package:
      type: local_archive
      uri: /tmp/local_tool.zip
      checksum: sha256:abc123
    compatibility:
      core_api: ">=0.1.0 <0.2.0"
""",
        encoding="utf-8",
    )
    manager = _build_plugin_manager(_FakePluginStateStore(), plugin_marketplace_path=str(manifest_path))

    with pytest.raises(HTTPException) as exc_info:
        await manager.install({"name": "local_tool", "confirm_restart_required": True})

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "invalid_local_archive_uri"


@pytest.mark.asyncio
async def test_plugin_manager_rejects_local_archive_without_checksum(tmp_path) -> None:
    manifest_path = tmp_path / "plugin-marketplace.yaml"
    manifest_path.write_text(
        """
items:
  - name: local_tool
    version: 0.2.0
    source: local
    package:
      type: local_archive
      uri: dist/local_tool.zip
    compatibility:
      core_api: ">=0.1.0 <0.2.0"
""",
        encoding="utf-8",
    )
    manager = _build_plugin_manager(_FakePluginStateStore(), plugin_marketplace_path=str(manifest_path))

    with pytest.raises(HTTPException) as exc_info:
        await manager.install({"name": "local_tool", "confirm_restart_required": True})

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "local_archive_checksum_required"


@pytest.mark.asyncio
async def test_plugin_manager_rejects_local_archive_without_capability_digest(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "local_tool.zip"
    checksum = _write_zip(
        archive_path,
        {
            "plugin.py": "VERSION = '0.2.0'\n",
            "plugin-package.json": _local_package_descriptor(),
        },
    )
    manifest_path = _write_local_archive_manifest(tmp_path, archive_path, checksum)
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            f"    capability_digest: {_TEST_CAPABILITY_DIGEST}\n",
            "",
        ),
        encoding="utf-8",
    )
    manager = _build_plugin_manager(
        _FakePluginStateStore(),
        plugin_marketplace_path=str(manifest_path),
        project_root=tmp_path,
    )

    with pytest.raises(HTTPException) as exc_info:
        await manager.install(
            {"name": "local_tool", "confirm_restart_required": True}
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "plugin_capability_digest_required"


@pytest.mark.asyncio
async def test_plugin_manager_rejects_local_archive_capability_digest_mismatch(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "local_tool.zip"
    checksum = _write_zip(
        archive_path,
        {
            "plugin.py": "VERSION = '0.2.0'\n",
            "plugin-package.json": _local_package_descriptor(
                capability_digest=f"sha256:{'d' * 64}"
            ),
        },
    )
    manifest_path = _write_local_archive_manifest(tmp_path, archive_path, checksum)
    manager = _build_plugin_manager(
        _FakePluginStateStore(),
        plugin_marketplace_path=str(manifest_path),
        project_root=tmp_path,
    )

    with pytest.raises(HTTPException) as exc_info:
        await manager.install(
            {"name": "local_tool", "confirm_restart_required": True}
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "local_archive_capability_digest_mismatch"


@pytest.mark.asyncio
async def test_plugin_manager_rejects_local_archive_checksum_mismatch(tmp_path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    _write_zip(dist_dir / "local_tool.zip", {"plugin.py": "VERSION = '0.2.0'\n"})
    manifest_path = tmp_path / "plugin-marketplace.yaml"
    manifest_path.write_text(
        """
items:
  - name: local_tool
    version: 0.2.0
    source: local
    package:
      type: local_archive
      uri: dist/local_tool.zip
      checksum: sha256:0000000000000000000000000000000000000000000000000000000000000000
    compatibility:
      core_api: ">=0.1.0 <0.2.0"
    capability_digest: sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
""",
        encoding="utf-8",
    )
    state_store = _FakePluginStateStore()
    manager = _build_plugin_manager(state_store, plugin_marketplace_path=str(manifest_path), project_root=tmp_path)

    with pytest.raises(HTTPException) as exc_info:
        await manager.install({"name": "local_tool", "confirm_restart_required": True})

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "local_archive_checksum_mismatch"
    assert not (tmp_path / ".runtime" / "plugins" / "local_tool").exists()
    assert state_store.appended_events[-1][1] == "install_failed"


@pytest.mark.asyncio
async def test_plugin_manager_rejects_local_archive_path_traversal_member(tmp_path) -> None:
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    checksum = _write_zip(dist_dir / "local_tool.zip", {"../plugin.py": "bad\n"})
    manifest_path = tmp_path / "plugin-marketplace.yaml"
    manifest_path.write_text(
        f"""
items:
  - name: local_tool
    version: 0.2.0
    source: local
    package:
      type: local_archive
      uri: dist/local_tool.zip
      checksum: sha256:{checksum}
    compatibility:
      core_api: ">=0.1.0 <0.2.0"
    capability_digest: {_TEST_CAPABILITY_DIGEST}
""",
        encoding="utf-8",
    )
    state_store = _FakePluginStateStore()
    manager = _build_plugin_manager(state_store, plugin_marketplace_path=str(manifest_path), project_root=tmp_path)

    with pytest.raises(HTTPException) as exc_info:
        await manager.install({"name": "local_tool", "confirm_restart_required": True})

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "unsafe_local_archive_member"
    assert not (tmp_path / ".runtime" / "plugins" / "local_tool").exists()
    assert state_store.appended_events[-1][1] == "install_failed"


@pytest.mark.asyncio
async def test_plugin_manager_rejects_dependency_version_mismatch(tmp_path) -> None:
    manifest_path = tmp_path / "plugin-marketplace.yaml"
    manifest_path.write_text(
        """
items:
  - name: local_tool
    version: 0.2.0
    source: builtin
    package:
      type: builtin
      uri: plugins/local_tool
    compatibility:
      core_api: ">=0.1.0 <0.2.0"
    dependencies:
      - name: draw
        version: ">=9.0.0"
        required: true
""",
        encoding="utf-8",
    )
    state_store = _FakePluginStateStore()
    state_store.states["draw"] = replace(state_store.states["draw"], version="0.1.0", enabled=True)
    manager = _build_plugin_manager(state_store, plugin_marketplace_path=str(manifest_path))

    with pytest.raises(HTTPException) as exc_info:
        await manager.install({"name": "local_tool", "confirm_restart_required": True})

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "plugin_dependency_version_mismatch"
    assert state_store.appended_events == []


@pytest.mark.asyncio
async def test_plugin_manager_accepts_matching_dependency_version(tmp_path) -> None:
    manifest_path = tmp_path / "plugin-marketplace.yaml"
    manifest_path.write_text(
        """
items:
  - name: local_tool
    version: 0.2.0
    source: builtin
    package:
      type: builtin
      uri: plugins/local_tool
    compatibility:
      core_api: ">=0.1.0 <0.2.0"
    dependencies:
      - name: draw
        version: ">=0.1.0 <0.2.0"
        required: true
""",
        encoding="utf-8",
    )
    state_store = _FakePluginStateStore()
    state_store.states["draw"] = replace(state_store.states["draw"], version="0.1.0", enabled=True)
    manager = _build_plugin_manager(state_store, plugin_marketplace_path=str(manifest_path))

    payload = await manager.install({"name": "local_tool", "confirm_restart_required": True})

    assert payload["plugin"]["plugin_name"] == "local_tool"
    assert state_store.appended_events[-1][1] == "install_succeeded"


def test_load_marketplace_manifest_rejects_invalid_dependency_version(tmp_path) -> None:
    path = tmp_path / "plugin-marketplace.yaml"
    path.write_text(
        """
items:
  - name: local_tool
    version: 0.2.0
    package:
      type: builtin
      uri: plugins/local_tool
    compatibility:
      core_api: ">=0.1.0 <0.2.0"
    dependencies:
      - name: draw
        version: "not-a-spec"
""",
        encoding="utf-8",
    )

    with pytest.raises(MarketplaceManifestError, match=r"invalid compatibility\.version"):
        load_marketplace_manifest(path)


@pytest.mark.parametrize(
    ("fragment", "message"),
    [
        ("unknown_field: true", "unknown fields"),
        ("package:\n      type: builtin\n      unexpected: true", "unknown fields"),
        (
            "capabilities:\n      arbitrary_runtime: []",
            "unknown capability field",
        ),
        (
            "dependencies:\n      - name: draw\n        required: \"false\"",
            "required must be boolean",
        ),
    ],
)
def test_load_marketplace_manifest_rejects_ambiguous_contract_fields(
    tmp_path: Path,
    fragment: str,
    message: str,
) -> None:
    path = tmp_path / "plugin-marketplace.yaml"
    valid_package = (
        ""
        if fragment.startswith("package:")
        else "    package: {type: builtin, uri: plugins/local_tool}\n"
    )
    indented_fragment = "\n".join(
        f"    {line}" for line in fragment.splitlines()
    )
    path.write_text(
        f"""items:
  - name: local_tool
    version: 0.2.0
    compatibility:
      core_api: ">=0.1.0 <0.2.0"
{valid_package}{indented_fragment}
""",
        encoding="utf-8",
    )

    with pytest.raises(MarketplaceManifestError, match=message):
        load_marketplace_manifest(path)


def test_load_marketplace_manifest_rejects_duplicate_yaml_mapping_key(
    tmp_path: Path,
) -> None:
    path = tmp_path / "plugin-marketplace.yaml"
    path.write_text("items: []\nitems: []\n", encoding="utf-8")

    with pytest.raises(MarketplaceManifestError, match="duplicate key"):
        load_marketplace_manifest(path)


@pytest.mark.asyncio
async def test_plugin_manager_lists_events_with_filters() -> None:
    state_store = _FakePluginStateStore()
    manager = _build_plugin_manager(state_store)

    payload = await manager.events(
        plugin_name="draw",
        event_type=" install_succeeded ",
        limit=25,
        offset=2,
    )

    assert state_store.events_args == {
        "plugin_name": "draw",
        "event_type": "install_succeeded",
        "limit": 25,
        "offset": 2,
    }
    assert payload["events"][0]["plugin_name"] == "draw"
    assert payload["events"][0]["metadata"] == {"offset": 2}


@pytest.mark.asyncio
async def test_plugin_manager_lists_scope_states_with_filters() -> None:
    state_store = _FakePluginStateStore()
    manager = _build_plugin_manager(state_store)

    payload = await manager.scope_states(
        tenant_id=" demo ",
        session_id="room-1",
        plugin_name="draw",
    )

    assert state_store.scope_states_args == {
        "tenant_id": "demo",
        "session_id": "room-1",
        "plugin_name": "draw",
    }
    assert payload["items"][0]["scope"] == "session"
    assert payload["items"][0]["config"] == {"mode": "quiet"}


@pytest.mark.asyncio
async def test_plugin_manager_sets_scope_state_and_appends_event() -> None:
    state_store = _FakePluginStateStore()
    manager = _build_plugin_manager(state_store)

    payload = await manager.set_scope_state(
        "draw",
        {
            "tenant_id": " demo ",
            "session_id": " room-1 ",
            "enabled": False,
            "config": {"mode": "quiet"},
        },
        expected_version=1,
    )

    assert state_store.scope_enabled_args == {
        "tenant_id": "demo",
        "session_id": "room-1",
        "plugin_name": "draw",
        "enabled": False,
        "expected_version": 1,
        "config": {"mode": "quiet"},
    }
    assert payload["scope_state"]["enabled"] is False
    assert state_store.appended_events == [
        (
            "draw",
            "scope_disable",
            {
                "status": "ok",
                "actor_id": "",
                "request_id": "",
                "ip_address": "",
                "message": "",
                "metadata": {"tenant_id": "demo", "session_id": "room-1"},
            },
        )
    ]


@pytest.mark.asyncio
async def test_plugin_manager_appends_lifecycle_requested_and_succeeded_events() -> None:
    state_store = _FakePluginStateStore()
    registry = _FakePluginRegistry()
    manager = _build_plugin_manager(state_store, registry)

    enable_payload = await manager.enable("draw")
    disable_payload = await manager.disable("draw")

    assert registry.reactivated == ["draw"]
    assert registry.deactivated == ["draw"]
    assert enable_payload["plugin"]["enabled"] is True
    assert disable_payload["plugin"]["enabled"] is False
    assert state_store.appended_events == [
        (
            "draw",
            "enable_requested",
            {
                "status": "ok",
                "actor_id": "",
                "request_id": "",
                "ip_address": "",
                "message": "",
                "metadata": None,
            },
        ),
        (
            "draw",
            "enable_succeeded",
            {
                "status": "ok",
                "actor_id": "",
                "request_id": "",
                "ip_address": "",
                "message": "",
                "metadata": {"initialized_now": False},
            },
        ),
        (
            "draw",
            "disable_requested",
            {
                "status": "ok",
                "actor_id": "",
                "request_id": "",
                "ip_address": "",
                "message": "",
                "metadata": None,
            },
        ),
        (
            "draw",
            "disable_succeeded",
            {
                "status": "ok",
                "actor_id": "",
                "request_id": "",
                "ip_address": "",
                "message": "",
                "metadata": {
                    "cleanup": {"hooks": 1, "agent_tools": 0, "commands": 0},
                    "disable_mode": "runtime_filtered",
                },
            },
        ),
    ]


@pytest.mark.asyncio
async def test_disable_cleanup_failure_requires_restart_and_records_partial() -> None:
    class _CleanupFailureRegistry(_FakePluginRegistry):
        async def deactivate_plugin(
            self,
            name: str,
            container: object,
        ) -> dict[str, int]:
            _ = container
            self.deactivated.append(name)
            self.active = False
            return {
                "hooks": 1,
                "agent_tools": 0,
                "commands": 0,
                "cleanup_errors": 1,
            }

    state_store = _FakePluginStateStore()
    state_store.states["draw"] = replace(
        state_store.states["draw"],
        enabled=True,
    )
    registry = _CleanupFailureRegistry()
    manager = _build_plugin_manager(state_store, registry)

    payload = await manager.disable("draw")

    assert payload["plugin"]["enabled"] is False
    assert payload["restart_required"] is True
    assert payload["plugin"]["restart_required"] is True
    assert payload["disable_mode"] == "restart_required"
    assert payload["cleanup_partial"] is True
    assert [event[1] for event in state_store.appended_events] == [
        "disable_requested",
        "disable_partial",
    ]


@pytest.mark.asyncio
async def test_enable_uninitialized_plugin_stays_behind_restart_fence() -> None:
    state_store = _FakePluginStateStore()
    state_store.states["draw"] = replace(
        state_store.states["draw"],
        enabled=False,
    )
    registry = _FakePluginRegistry()
    registry.initialized = False
    manager = _build_plugin_manager(state_store, registry)

    payload = await manager.enable("draw")

    assert registry.reactivated == []
    assert payload["plugin"]["enabled"] is True
    assert payload["restart_required"] is True
    assert payload["plugin"]["restart_required"] is True


@pytest.mark.asyncio
async def test_enable_uses_installed_dependencies_after_catalog_drift(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "plugin-marketplace.yaml"
    manifest_path.write_text(
        """
items:
  - name: draw
    version: 0.1.0
    package:
      type: builtin
      uri: plugins/draw
    compatibility:
      core_api: ">=0.1.0 <0.2.0"
""",
        encoding="utf-8",
    )
    state_store = _FakePluginStateStore()
    state_store.states["draw"] = replace(
        state_store.states["draw"],
        enabled=False,
        metadata={
            "manifest": {
                "dependencies": [
                    {
                        "name": "foundation",
                        "version": ">=1.0.0",
                        "required": True,
                    }
                ]
            }
        },
    )
    manager = _build_plugin_manager(
        state_store,
        plugin_marketplace_path=str(manifest_path),
    )

    with pytest.raises(HTTPException) as exc_info:
        await manager.enable("draw")

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "plugin_dependency_not_installed"


def test_external_legacy_install_never_falls_back_to_mutable_catalog_dependencies() -> None:
    state_store = _FakePluginStateStore()
    manager = _build_plugin_manager(state_store)
    state = replace(
        state_store.states["draw"],
        source="marketplace",
        metadata={},
    )
    item = manager._load_manifest().by_name()["draw"]

    with pytest.raises(HTTPException) as excinfo:
        manager._installed_dependencies(state, fallback_item=item)

    assert excinfo.value.status_code == 409
    assert excinfo.value.detail == "plugin_dependency_contract_missing"


@pytest.mark.asyncio
async def test_enable_final_state_failure_deactivates_prepared_runtime() -> None:
    class _FailFinalEnableStore(_FakePluginStateStore):
        def __init__(self) -> None:
            super().__init__()
            self.enable_writes = 0

        async def set_enabled(
            self,
            plugin_name: str,
            enabled: bool,
            *,
            restart_required: bool = False,
        ) -> PluginState | None:
            if enabled:
                self.enable_writes += 1
                if self.enable_writes == 2:
                    raise RuntimeError("final state write failed")
            return await super().set_enabled(
                plugin_name,
                enabled,
                restart_required=restart_required,
            )

    state_store = _FailFinalEnableStore()
    state_store.states["draw"] = replace(
        state_store.states["draw"],
        enabled=False,
    )
    registry = _FakePluginRegistry()
    registry.active = False
    manager = _build_plugin_manager(state_store, registry)

    with pytest.raises(RuntimeError, match="final state write failed"):
        await manager.enable("draw")

    assert registry.reactivated == ["draw"]
    assert registry.deactivated == ["draw"]
    assert registry.active is False
    assert state_store.states["draw"].enabled is True
    assert state_store.states["draw"].restart_required is True


@pytest.mark.asyncio
async def test_plugin_manager_rejects_disable_when_enabled_dependent_exists(tmp_path) -> None:
    manifest_path = tmp_path / "plugin-marketplace.yaml"
    manifest_path.write_text(
        """
items:
  - name: draw
    version: 0.1.0
    package:
      type: builtin
      uri: plugins/draw
    compatibility:
      core_api: ">=0.1.0 <0.2.0"
  - name: demo_plugin
    version: 0.1.0
    package:
      type: builtin
      uri: plugins/demo_plugin
    compatibility:
      core_api: ">=0.1.0 <0.2.0"
    dependencies:
      - name: draw
        required: true
""",
        encoding="utf-8",
    )
    state_store = _FakePluginStateStore()
    state_store.states["demo_plugin"] = PluginState(
        plugin_name="demo_plugin",
        version="0.1.0",
        installed=True,
        enabled=True,
        metadata={
            "manifest": {
                "dependencies": [
                    {
                        "name": "draw",
                        "version": "",
                        "required": True,
                    }
                ]
            }
        },
    )
    registry = _FakePluginRegistry()
    manager = _build_plugin_manager(state_store, registry, plugin_marketplace_path=str(manifest_path))

    with pytest.raises(HTTPException) as exc_info:
        await manager.disable("draw")

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "plugin_has_enabled_dependents"
    assert registry.deactivated == []
    assert state_store.appended_events == []


@pytest.mark.asyncio
async def test_disable_uses_installed_dependency_after_catalog_removal(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "plugin-marketplace.yaml"
    manifest_path.write_text(
        """
items:
  - name: draw
    version: 0.1.0
    package:
      type: builtin
      uri: plugins/draw
    compatibility:
      core_api: ">=0.1.0 <0.2.0"
""",
        encoding="utf-8",
    )
    state_store = _FakePluginStateStore()
    state_store.states["demo_plugin"] = PluginState(
        plugin_name="demo_plugin",
        installed=True,
        enabled=True,
        metadata={
            "manifest": {
                "dependencies": [
                    {"name": "draw", "version": "", "required": True}
                ]
            }
        },
    )
    registry = _FakePluginRegistry()
    manager = _build_plugin_manager(
        state_store,
        registry,
        plugin_marketplace_path=str(manifest_path),
    )

    with pytest.raises(HTTPException) as exc_info:
        await manager.disable("draw")

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "plugin_has_enabled_dependents"
    assert registry.deactivated == []


@pytest.mark.asyncio
async def test_upgrade_rejects_enabled_dependent_version_break(tmp_path: Path) -> None:
    manifest_path = tmp_path / "plugin-marketplace.yaml"
    manifest_path.write_text(
        f"""
items:
  - name: draw
    version: 0.2.0
    source: local
    package:
      type: local_archive
      uri: draw-0.2.0.zip
      checksum: sha256:{'a' * 64}
    compatibility:
      core_api: ">=0.1.0 <0.2.0"
      python: ">=3.11"
    capability_digest: {_TEST_CAPABILITY_DIGEST}
""",
        encoding="utf-8",
    )
    state_store = _FakePluginStateStore()
    state_store.states["draw"] = replace(
        state_store.states["draw"],
        version="0.1.0",
        source="local",
        enabled=True,
    )
    state_store.states["demo_plugin"] = PluginState(
        plugin_name="demo_plugin",
        version="1.0.0",
        installed=True,
        enabled=True,
        metadata={
            "manifest": {
                "dependencies": [
                    {
                        "name": "draw",
                        "version": "<0.2.0",
                        "required": True,
                    }
                ]
            }
        },
    )
    manager = _build_plugin_manager(
        state_store,
        plugin_marketplace_path=str(manifest_path),
        project_root=tmp_path,
    )

    with pytest.raises(HTTPException) as exc_info:
        await manager.upgrade("draw", {"confirm_restart_required": True})

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "plugin_enabled_dependent_version_mismatch"
    assert state_store.states["draw"].version == "0.1.0"
    assert not (tmp_path / ".runtime" / "plugins" / "draw").exists()


@pytest.mark.asyncio
async def test_plugin_manager_allows_disable_when_dependent_is_disabled(tmp_path) -> None:
    manifest_path = tmp_path / "plugin-marketplace.yaml"
    manifest_path.write_text(
        """
items:
  - name: draw
    version: 0.1.0
    package:
      type: builtin
      uri: plugins/draw
    compatibility:
      core_api: ">=0.1.0 <0.2.0"
  - name: demo_plugin
    version: 0.1.0
    package:
      type: builtin
      uri: plugins/demo_plugin
    compatibility:
      core_api: ">=0.1.0 <0.2.0"
    dependencies:
      - name: draw
        required: true
""",
        encoding="utf-8",
    )
    state_store = _FakePluginStateStore()
    state_store.states["demo_plugin"] = PluginState(plugin_name="demo_plugin", installed=True, enabled=False)
    registry = _FakePluginRegistry()
    manager = _build_plugin_manager(state_store, registry, plugin_marketplace_path=str(manifest_path))

    payload = await manager.disable("draw")

    assert registry.deactivated == ["draw"]
    assert payload["plugin"]["enabled"] is False
    assert state_store.appended_events[-1][1] == "disable_succeeded"


@pytest.mark.asyncio
async def test_plugin_manager_rejects_enable_when_restart_is_pending() -> None:
    state_store = _FakePluginStateStore()
    state_store.states["draw"] = replace(state_store.states["draw"], restart_required=True)
    registry = _FakePluginRegistry()
    manager = _build_plugin_manager(state_store, registry)

    with pytest.raises(HTTPException) as exc_info:
        await manager.enable("draw")

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "plugin_restart_required"
    assert registry.reactivated == []
    assert state_store.appended_events == []


@pytest.mark.asyncio
async def test_plugin_manager_rejects_disable_when_restart_is_pending() -> None:
    state_store = _FakePluginStateStore()
    state_store.states["draw"] = replace(state_store.states["draw"], restart_required=True)
    registry = _FakePluginRegistry()
    manager = _build_plugin_manager(state_store, registry)

    with pytest.raises(HTTPException) as exc_info:
        await manager.disable("draw")

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "plugin_restart_required"
    assert registry.deactivated == []
    assert state_store.appended_events == []


@pytest.mark.asyncio
async def test_plugin_manager_rejects_enable_for_uninstalled_plugin() -> None:
    state_store = _FakePluginStateStore()
    state_store.states["draw"] = replace(state_store.states["draw"], installed=False)
    manager = _build_plugin_manager(state_store)

    with pytest.raises(HTTPException) as exc_info:
        await manager.enable("draw")

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "plugin_not_installed"


@pytest.mark.asyncio
async def test_plugin_manager_rejects_disable_for_uninstalled_plugin() -> None:
    state_store = _FakePluginStateStore()
    state_store.states["draw"] = replace(state_store.states["draw"], installed=False)
    manager = _build_plugin_manager(state_store)

    with pytest.raises(HTTPException) as exc_info:
        await manager.disable("draw")

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "plugin_not_installed"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["enable", "disable"])
async def test_global_lifecycle_is_disabled_when_dynamic_mutations_are_off(
    operation: str,
) -> None:
    manager = _build_plugin_manager(_FakePluginStateStore())
    manager.ctx.settings.plugin_dynamic_mutations_enabled = False

    with pytest.raises(HTTPException) as exc_info:
        await getattr(manager, operation)("draw")

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "dynamic_plugin_mutations_disabled"


@pytest.mark.asyncio
async def test_disable_recovery_accepts_durable_disabled_external_plugin_absent_from_runtime() -> None:
    state_store = _FakePluginStateStore()
    state_store.states["local_tool"] = PluginState(
        plugin_name="local_tool",
        version="0.2.0",
        source="local",
        installed=True,
        enabled=False,
        status="disabled",
        restart_required=True,
    )
    registry = _FakePluginRegistry()
    registry.active = False
    manager = _build_plugin_manager(state_store, registry)

    recovered = await manager._recover_lifecycle_response(
        "disable",
        "local_tool",
        {},
        {
            "installed": True,
            "enabled": True,
            "runtime_active": True,
            "runtime_initialized": True,
        },
    )

    assert recovered is not None
    assert recovered["plugin"]["name"] == "local_tool"
    assert recovered["plugin"]["enabled"] is False
    assert recovered["restart_required"] is True
    assert recovered["cleanup_partial"] is False
    assert registry.deactivated == []


@pytest.mark.asyncio
async def test_plugin_manager_appends_request_actor_to_scope_event() -> None:
    state_store = _FakePluginStateStore()
    manager = _build_plugin_manager(state_store)

    request = type(
        "RequestStub",
        (),
        {
            "headers": {"X-Request-ID": "req-1", "X-Admin-Actor": "ops-user"},
            "client": type("ClientStub", (), {"host": "203.0.113.9"})(),
        },
    )()

    await manager.set_scope_state(
        "draw",
        {
            "tenant_id": "demo",
            "session_id": "room-1",
            "enabled": True,
        },
        request=cast(Any, request),
        expected_version=1,
    )

    assert state_store.appended_events[-1] == (
        "draw",
        "scope_enable",
        {
            "status": "ok",
            "actor_id": "ops-user",
            "request_id": "req-1",
            "ip_address": "203.0.113.9",
            "message": "",
            "metadata": {"tenant_id": "demo", "session_id": "room-1"},
        },
    )
