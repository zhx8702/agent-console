from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent.registry import AgentToolRegistry
from app.common.types import RouteType
from app.plugin.base import Plugin, PluginContext, PluginMeta
from app.plugin.registry import PluginRegistry


class _CapabilityEngine:
    name = "mutable-capability"

    async def answer(self, *_args, **_kwargs):
        raise AssertionError("descriptor collector must not execute the engine")


class _MutableCapabilityPlugin(Plugin):
    meta = PluginMeta(name="mutable_capability", version="1.0.0")

    def __init__(self) -> None:
        self.route_type = RouteType.FAQ
        self.engine = _CapabilityEngine()

    async def initialize(self, ctx: PluginContext) -> None:
        _ = ctx

    def get_capability_engines(self):
        return {self.route_type: self.engine}


class _MediaProvider:
    def __init__(self) -> None:
        self.name = "stable-media"

    async def list_recent_media_events(self, **_kwargs):
        return []


class _MutableMediaPlugin(Plugin):
    meta = PluginMeta(name="mutable_media", version="1.0.0")

    def __init__(self) -> None:
        self.provider = _MediaProvider()

    async def initialize(self, ctx: PluginContext) -> None:
        _ = ctx

    def get_admin_media_event_provider(self):
        return self.provider


def _context() -> PluginContext:
    return PluginContext(
        container=SimpleNamespace(agent_tool_registry=AgentToolRegistry()),
        settings=object(),
    )


@pytest.mark.asyncio
async def test_capability_engine_collector_rejects_post_init_descriptor_drift() -> None:
    registry = PluginRegistry(allow_offline_execution=True)
    plugin = _MutableCapabilityPlugin()
    registry._register(plugin)
    await registry.initialize_all(_context())

    assert set(registry.all_capability_engines()) == {RouteType.FAQ}

    plugin.route_type = RouteType.RAG
    with pytest.raises(
        RuntimeError,
        match=r"plugin descriptor drift: mutable_capability\.capability_engines",
    ):
        registry.all_capability_engines()


@pytest.mark.asyncio
async def test_admin_media_collector_rejects_post_init_descriptor_drift() -> None:
    registry = PluginRegistry(allow_offline_execution=True)
    plugin = _MutableMediaPlugin()
    registry._register(plugin)
    await registry.initialize_all(_context())

    assert [provider.name for provider in registry.all_admin_media_event_providers()] == [
        "stable-media"
    ]

    plugin.provider.name = "changed-media"
    with pytest.raises(
        RuntimeError,
        match=r"plugin descriptor drift: mutable_media\.admin_media_providers",
    ):
        registry.all_admin_media_event_providers()
