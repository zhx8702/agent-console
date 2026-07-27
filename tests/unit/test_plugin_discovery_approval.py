from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from app.plugin.artifacts import compute_plugin_tree_digest
from app.plugin.base import Plugin, PluginContext, PluginMeta
from app.plugin.registry import PluginRegistry
from app.plugin.state import PluginState


class _DiscoveryStore:
    def __init__(self, state: PluginState | None = None) -> None:
        self.state = state

    async def ensure_tables(self) -> None:
        return None

    async def get(self, plugin_name: str) -> PluginState | None:
        if self.state is not None and self.state.plugin_name == plugin_name:
            return self.state
        return None

    async def reconcile(
        self,
        plugins: dict[str, Plugin],
        *,
        provenance: dict[str, str] | None = None,
    ) -> list[PluginState]:
        _ = provenance
        if self.state is None or self.state.plugin_name not in plugins:
            return []
        return [self.state]

    async def acknowledge_disabled_restart(
        self,
        plugin_name: str,
        discovered_version: str,
    ) -> None:
        if (
            self.state is not None
            and self.state.plugin_name == plugin_name
            and self.state.version == discovered_version
        ):
            self.state = replace(
                self.state,
                restart_required=False,
                status="disabled",
            )


def _write_external_candidate(
    root: Path,
    *,
    name: str,
    version: str,
) -> tuple[Path, Path]:
    candidate = root / name
    candidate.mkdir(parents=True)
    marker = root / f"{name}.imported"
    (candidate / "plugin-package.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "name": name,
                "version": version,
            }
        ),
        encoding="utf-8",
    )
    (candidate / "plugin.py").write_text(
        "\n".join(
            (
                "from pathlib import Path",
                "from app.plugin.base import Plugin, PluginContext, PluginMeta",
                f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')",
                "class ExternalPlugin(Plugin):",
                f"    meta = PluginMeta(name={name!r}, version={version!r})",
                "    async def initialize(self, ctx: PluginContext) -> None:",
                "        _ = ctx",
                "plugin = ExternalPlugin()",
            )
        ),
        encoding="utf-8",
    )
    return candidate, marker


def _directory_state(
    candidate: Path,
    *,
    name: str,
    version: str,
    state_version: str | None = None,
    source: str = "local",
    enabled: bool = True,
    restart_required: bool = False,
) -> PluginState:
    checksum = f"sha256:{'a' * 64}"
    return PluginState(
        plugin_name=name,
        version=state_version or version,
        source=source,
        installed=True,
        enabled=enabled,
        restart_required=restart_required,
        metadata={
            "manifest": {
                "name": name,
                "version": version,
                "package": {
                    "type": "local_archive",
                    "checksum": checksum,
                },
            },
            "artifact": {
                "package_type": "local_archive",
                "checksum": checksum,
                "tree_digest": compute_plugin_tree_digest(candidate),
            },
        },
    )


@pytest.mark.asyncio
async def test_external_directory_is_not_imported_before_durable_approval(
    tmp_path: Path,
) -> None:
    name = "external_approval_test"
    candidate, marker = _write_external_candidate(
        tmp_path,
        name=name,
        version="1.2.3",
    )
    store = _DiscoveryStore()
    registry = PluginRegistry(store)  # type: ignore[arg-type]

    try:
        assert registry.discover_directory(tmp_path, trusted_builtin=False) == 1
        assert marker.exists() is False
        assert name not in registry.loaded_plugins
        assert f"plugins.{name}.plugin" not in sys.modules

        await registry.reconcile_state()

        assert marker.exists() is False
        assert name not in registry.loaded_plugins

        store.state = _directory_state(
            candidate,
            name=name,
            version="1.2.3",
        )
        await registry.reconcile_state()

        assert marker.read_text(encoding="utf-8") == "executed"
        assert registry.loaded_plugins[name].meta.version == "1.2.3"
        assert registry.is_active(name) is True
    finally:
        sys.modules.pop(f"plugins.{name}.plugin", None)


@pytest.mark.asyncio
async def test_approved_external_directory_supports_relative_imports(
    tmp_path: Path,
) -> None:
    name = "external_relative_import"
    candidate, _marker = _write_external_candidate(
        tmp_path,
        name=name,
        version="1.2.3",
    )
    (candidate / "helper.py").write_text(
        "DESCRIPTION = 'loaded from sibling helper'\n",
        encoding="utf-8",
    )
    plugin_path = candidate / "plugin.py"
    plugin_path.write_text(
        plugin_path.read_text(encoding="utf-8").replace(
            "from app.plugin.base import Plugin, PluginContext, PluginMeta",
            "from app.plugin.base import Plugin, PluginContext, PluginMeta\n"
            "from .helper import DESCRIPTION",
        ).replace(
            "meta = PluginMeta(name='external_relative_import', version='1.2.3')",
            "meta = PluginMeta(name='external_relative_import', version='1.2.3', "
            "description=DESCRIPTION)",
        ),
        encoding="utf-8",
    )
    store = _DiscoveryStore(
        _directory_state(candidate, name=name, version="1.2.3")
    )
    registry = PluginRegistry(store)  # type: ignore[arg-type]

    try:
        registry.discover_directory(tmp_path, trusted_builtin=False)
        await registry.reconcile_state()

        assert registry.loaded_plugins[name].meta.description == (
            "loaded from sibling helper"
        )
        helper = sys.modules[f"plugins.{name}.helper"]
        assert Path(helper.__file__).resolve() == (candidate / "helper.py").resolve()
    finally:
        for module_name in tuple(sys.modules):
            if module_name == f"plugins.{name}" or module_name.startswith(
                f"plugins.{name}."
            ):
                sys.modules.pop(module_name, None)
        plugins_package = __import__("plugins")
        if hasattr(plugins_package, name):
            delattr(plugins_package, name)


def test_failed_file_load_restores_preexisting_package_modules(
    tmp_path: Path,
) -> None:
    name = "external_restore_modules"
    candidate = tmp_path / name
    candidate.mkdir()
    (candidate / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    (candidate / "plugin.py").write_text(
        "from .helper import VALUE\nraise RuntimeError(f'boom:{VALUE}')\n",
        encoding="utf-8",
    )
    namespace = f"plugins.{name}"
    old_package = ModuleType(namespace)
    old_package.__path__ = [str(candidate)]
    old_plugin = ModuleType(f"{namespace}.plugin")
    old_package.plugin = old_plugin
    plugins_package = __import__("plugins")
    sys.modules[namespace] = old_package
    sys.modules[f"{namespace}.plugin"] = old_plugin
    setattr(plugins_package, name, old_package)

    try:
        with pytest.raises(RuntimeError, match="boom:1"):
            PluginRegistry._load_from_file(candidate / "plugin.py", name)

        assert sys.modules[namespace] is old_package
        assert sys.modules[f"{namespace}.plugin"] is old_plugin
        assert f"{namespace}.helper" not in sys.modules
        assert getattr(plugins_package, name) is old_package
        assert old_package.plugin is old_plugin
    finally:
        for module_name in tuple(sys.modules):
            if module_name == namespace or module_name.startswith(f"{namespace}."):
                sys.modules.pop(module_name, None)
        if getattr(plugins_package, name, None) is old_package:
            delattr(plugins_package, name)


@pytest.mark.asyncio
async def test_external_registration_failure_rolls_back_package_cache(
    tmp_path: Path,
) -> None:
    name = "external_bad_identity"
    candidate, _marker = _write_external_candidate(
        tmp_path,
        name=name,
        version="1.2.3",
    )
    plugin_path = candidate / "plugin.py"
    plugin_path.write_text(
        plugin_path.read_text(encoding="utf-8").replace(
            f"PluginMeta(name={name!r}",
            "PluginMeta(name='different_identity'",
        ),
        encoding="utf-8",
    )
    store = _DiscoveryStore(
        _directory_state(candidate, name=name, version="1.2.3")
    )
    registry = PluginRegistry(store)  # type: ignore[arg-type]
    namespace = f"plugins.{name}"

    registry.discover_directory(tmp_path, trusted_builtin=False)
    await registry.reconcile_state()

    assert name not in registry.loaded_plugins
    assert all(
        module_name != namespace and not module_name.startswith(f"{namespace}.")
        for module_name in sys.modules
    )
    assert not hasattr(__import__("plugins"), name)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state_version", "state_source"),
    [("9.9.9", "local"), ("1.2.3", "builtin")],
)
async def test_external_directory_requires_exact_approved_generation(
    tmp_path: Path,
    state_version: str,
    state_source: str,
) -> None:
    name = f"external_generation_{state_version.replace('.', '_')}_{state_source}"
    candidate, marker = _write_external_candidate(
        tmp_path,
        name=name,
        version="1.2.3",
    )
    store = _DiscoveryStore(
        _directory_state(
            candidate,
            name=name,
            version="1.2.3",
            state_version=state_version,
            source=state_source,
        )
    )
    registry = PluginRegistry(store)  # type: ignore[arg-type]

    registry.discover_directory(tmp_path, trusted_builtin=False)
    await registry.reconcile_state()

    assert marker.exists() is False
    assert name not in registry.loaded_plugins
    assert f"plugins.{name}.plugin" not in sys.modules


@pytest.mark.asyncio
async def test_disabled_external_generation_is_acknowledged_without_import(
    tmp_path: Path,
) -> None:
    name = "disabled_external_generation"
    candidate, marker = _write_external_candidate(
        tmp_path,
        name=name,
        version="1.2.3",
    )
    store = _DiscoveryStore(
        _directory_state(
            candidate,
            name=name,
            version="1.2.3",
            enabled=False,
            restart_required=True,
        )
    )
    registry = PluginRegistry(store)  # type: ignore[arg-type]

    registry.discover_directory(tmp_path, trusted_builtin=False)
    await registry.reconcile_state()

    assert marker.exists() is False
    assert name not in registry.loaded_plugins
    assert store.state is not None
    assert store.state.restart_required is False


def test_external_directory_without_state_store_never_imports(tmp_path: Path) -> None:
    name = "external_without_state_store"
    _candidate, marker = _write_external_candidate(
        tmp_path,
        name=name,
        version="1.2.3",
    )
    registry = PluginRegistry()

    assert registry.discover_directory(tmp_path, trusted_builtin=False) == 1

    assert marker.exists() is False
    assert name not in registry.loaded_plugins


@pytest.mark.asyncio
async def test_tampered_external_directory_is_not_imported(tmp_path: Path) -> None:
    name = "tampered_external_generation"
    candidate, marker = _write_external_candidate(
        tmp_path,
        name=name,
        version="1.2.3",
    )
    store = _DiscoveryStore(
        _directory_state(candidate, name=name, version="1.2.3")
    )
    (candidate / "plugin.py").write_text(
        (candidate / "plugin.py").read_text(encoding="utf-8") + "\nTAMPERED = True\n",
        encoding="utf-8",
    )
    registry = PluginRegistry(store)  # type: ignore[arg-type]

    registry.discover_directory(tmp_path, trusted_builtin=False)
    await registry.reconcile_state()

    assert marker.exists() is False
    assert name not in registry.loaded_plugins


class _EntrypointPlugin(Plugin):
    meta = PluginMeta(name="approved_entrypoint", version="2.0.0")

    async def initialize(self, ctx: PluginContext) -> None:
        _ = ctx


class _Entrypoint:
    name = "approved_entrypoint"
    value = "distribution:plugin"
    dist = SimpleNamespace(version="2.0.0")

    def __init__(self) -> None:
        self.load_calls = 0

    def load(self) -> Plugin:
        self.load_calls += 1
        return _EntrypointPlugin()


class _Entrypoints(list[Any]):
    def select(self, *, group: str) -> _Entrypoints:
        assert group == "cs_system.plugins"
        return self


@pytest.mark.asyncio
async def test_entrypoint_load_is_deferred_until_exact_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entrypoint = _Entrypoint()
    monkeypatch.setattr(
        "importlib.metadata.entry_points",
        lambda: _Entrypoints([entrypoint]),
    )
    store = _DiscoveryStore()
    registry = PluginRegistry(store)  # type: ignore[arg-type]

    assert registry.discover_entrypoints() == 1
    assert entrypoint.load_calls == 0

    await registry.reconcile_state()
    assert entrypoint.load_calls == 0

    checksum = f"sha256:{'b' * 64}"
    store.state = PluginState(
        plugin_name="approved_entrypoint",
        version="2.0.0",
        source="entrypoint",
        installed=True,
        enabled=True,
        metadata={
            "manifest": {
                "name": "approved_entrypoint",
                "version": "2.0.0",
                "package": {"type": "wheel", "checksum": checksum},
            },
            "artifact": {"package_type": "wheel", "checksum": checksum},
        },
    )
    await registry.reconcile_state()

    assert entrypoint.load_calls == 1
    assert "approved_entrypoint" in registry.loaded_plugins


def test_entrypoint_without_state_store_never_loads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entrypoint = _Entrypoint()
    monkeypatch.setattr(
        "importlib.metadata.entry_points",
        lambda: _Entrypoints([entrypoint]),
    )
    registry = PluginRegistry()

    assert registry.discover_entrypoints() == 1
    assert entrypoint.load_calls == 0
    assert "approved_entrypoint" not in registry.loaded_plugins
