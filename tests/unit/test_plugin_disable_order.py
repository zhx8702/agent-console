from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.plugin.base import Plugin, PluginContext, PluginMeta
from app.plugin.manager import PluginManager
from app.plugin.state import PluginState


class _Plugin(Plugin):
    meta = PluginMeta(name="demo", version="1.0.0")

    async def initialize(self, ctx: PluginContext) -> None:
        _ = ctx


class _StateStore:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.state = PluginState(plugin_name="demo", installed=True, enabled=True)

    async def get(self, plugin_name: str) -> PluginState | None:
        return self.state if plugin_name == "demo" else None

    async def list_states(self) -> list[PluginState]:
        return [self.state]

    async def set_enabled(
        self,
        plugin_name: str,
        enabled: bool,
        *,
        restart_required: bool = False,
    ) -> PluginState:
        assert plugin_name == "demo"
        assert enabled is False
        self.order.append("durable_gate_closed")
        self.state = PluginState(
            plugin_name="demo",
            installed=True,
            enabled=False,
            restart_required=restart_required,
        )
        return self.state

    async def append_event(self, *args, **kwargs) -> None:
        _ = (args, kwargs)


class _Registry:
    def __init__(self, state_store: _StateStore, order: list[str]) -> None:
        self.loaded_plugins = {"demo": _Plugin()}
        self._state_store = state_store
        self._order = order

    def descriptor(self, name: str):
        _ = name
        return None

    async def deactivate_plugin(self, name: str, container: object) -> dict[str, int]:
        _ = (name, container)
        assert self._state_store.state.enabled is False
        self._order.append("local_cleanup")
        return {"hooks": 0, "agent_tools": 0, "commands": 0, "cleanup_errors": 0}


@pytest.mark.asyncio
async def test_disable_closes_durable_gate_before_local_cleanup() -> None:
    order: list[str] = []
    state_store = _StateStore(order)
    registry = _Registry(state_store, order)
    settings = SimpleNamespace(
        project_root=Path(__file__).resolve().parents[2],
        plugin_marketplace_path="config/plugin-marketplace.yaml",
    )
    manager = PluginManager(  # type: ignore[arg-type]
        registry,
        state_store,
        PluginContext(container=object(), settings=settings),
    )

    result = await manager.disable("demo")

    assert order == ["durable_gate_closed", "local_cleanup"]
    assert result["plugin"]["enabled"] is False
