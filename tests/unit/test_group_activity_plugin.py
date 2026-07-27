from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.common.types import RouteType
from app.plugin.base import PluginContext
from plugins.group_activity.plugin import GroupActivityPlugin


class _FakeStore:
    ensured = 0

    def __init__(self, settings) -> None:
        self.settings = settings

    async def ensure_tables(self) -> None:
        type(self).ensured += 1


class _FakeService:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.calls = 0

    async def process_due_sessions(self):
        self.calls += 1


class _FakeRegistry:
    def __init__(self) -> None:
        self.calls = []
        self.loaded_plugins = {
            "wxbot": SimpleNamespace(meta=SimpleNamespace(version="0.2.0"))
        }

    async def owners_scope_execution_allowed(
        self,
        owners,
        *,
        tenant_id: str,
        session_id: str,
    ) -> bool:
        self.calls.append((tuple(owners), tenant_id, session_id))
        return True


@pytest.mark.asyncio
async def test_group_activity_plugin_initializes_router_hook_and_scheduler_only_in_scheduler_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plugins.group_activity.plugin as module

    _FakeStore.ensured = 0
    monkeypatch.setattr(module, "GroupActivityStore", _FakeStore)
    monkeypatch.setattr(module, "GroupActivityService", _FakeService)
    monkeypatch.setattr(module, "WxbotStore", lambda settings: SimpleNamespace(settings=settings))
    monkeypatch.setattr(
        module,
        "WxbotChannelOutbound",
        lambda store, **kwargs: SimpleNamespace(store=store, **kwargs),
    )

    registry = _FakeRegistry()
    plugin = GroupActivityPlugin()
    ctx = PluginContext(
        container=SimpleNamespace(
            capabilities={RouteType.AGENT: object()},
            plugin_registry=registry,
        ),
        settings=SimpleNamespace(app_process_role="scheduler"),
        db_ok=True,
        redis_ok=False,
    )

    await plugin.initialize(ctx)
    await asyncio.sleep(0)

    assert _FakeStore.ensured == 1
    assert plugin.get_api_router() is not None
    assert len(plugin.get_pipeline_hooks()) == 1
    assert (await plugin.get_runtime_status())["scheduler_enabled"] is True
    assert GroupActivityPlugin.meta.dependencies == ["wxbot>=0.2.0"]
    assert "network:wxbot" in plugin.get_permissions()
    combined_gate = plugin._service.kwargs["owners_scope_execution_allowed"]
    assert plugin._service.kwargs["execution_owner_versions"] == {
        "group_activity": "0.1.0",
        "wxbot": "0.2.0",
    }
    assert await combined_gate(
        ("group_activity", "wxbot"),
        tenant_id="demo",
        session_id="room@chatroom",
    )
    assert registry.calls == [
        (("group_activity", "wxbot"), "demo", "room@chatroom")
    ]

    await plugin.shutdown()
    assert (await plugin.get_runtime_status())["running"] is False

    api_plugin = GroupActivityPlugin()
    await api_plugin.initialize(
        PluginContext(
            container=SimpleNamespace(capabilities={RouteType.AGENT: object()}),
            settings=SimpleNamespace(app_process_role="api"),
            db_ok=True,
            redis_ok=False,
        )
    )
    await asyncio.sleep(0)
    assert (await api_plugin.get_runtime_status())["scheduler_enabled"] is False
    await api_plugin.shutdown()
