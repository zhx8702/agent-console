from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.registry import AgentToolRegistry
from app.plugin.base import Plugin, PluginContext, PluginMeta
from app.plugin.registry import PluginRegistry
from app.plugin.resources import PluginResources


async def test_finalizers_are_lifo_idempotent_and_failed_finalizers_can_retry():
    calls = []
    scope = PluginResources("sample")
    async def first():
        calls.append("first")
    async def second():
        calls.append("second")
        if calls.count("second") == 1:
            raise RuntimeError("failed")
    scope.add_cleanup(first)
    scope.add_cleanup(second)
    with pytest.raises(ExceptionGroup):
        await scope.close()
    assert calls == ["second", "first"]
    await scope.close()
    await scope.close()
    assert calls == ["second", "first", "second"]
    with pytest.raises(RuntimeError, match="already closing"):
        scope.add_cleanup(first)


@pytest.mark.parametrize("fail_initialize", [True, False])
async def test_managed_resources_close_after_failed_activation_or_shutdown(fail_initialize):
    finalizer = AsyncMock()
    class ResourcePlugin(Plugin):
        meta = PluginMeta(name="resource_test", version="1.0.0")
        async def initialize(self, ctx):
            assert ctx.resources.owner == self.meta.name
            ctx.resources.add_cleanup(finalizer)
            if fail_initialize:
                raise RuntimeError("initialize failed")
        async def shutdown(self):
            raise RuntimeError("legacy cleanup failed")
    registry = PluginRegistry(allow_offline_execution=True)
    registry._register(ResourcePlugin())
    context = PluginContext(container=SimpleNamespace(agent_tool_registry=AgentToolRegistry()), settings=object())
    await registry.initialize_all(context)
    if fail_initialize:
        assert not registry.is_active("resource_test")
    else:
        await registry.shutdown_all()
        assert not registry.is_active("resource_test")
    finalizer.assert_awaited_once()
    await registry.shutdown_all()
    finalizer.assert_awaited_once()
    assert context.resources is None
