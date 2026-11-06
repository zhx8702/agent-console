"""Real PostgreSQL proof that captured contributions keep the durable gates."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.agent.registry import AgentToolRegistry
from app.common.types import CapabilityResult, Channel, PreprocessedMessage, RouteType, Session
from app.infra.db import get_engine
from app.plugin.base import Plugin, PluginContext, PluginMeta
from app.plugin.registry import PluginRegistry
from app.plugin.runtime import CapabilityOwnerExecutionDenied
from app.plugin.state import PluginScopeVersionConflictError, PluginStateStore
from tests.integration.conftest import requires_postgres

pytestmark = [pytest.mark.integration, requires_postgres]


async def test_two_replicas_recheck_dependency_scope_and_restart_fences(_reset_singletons):
    suffix = uuid4().hex[:12]
    dependency, owner = f"test_dep_{suffix}", f"test_owner_{suffix}"
    tenant = f"test_tenant_{suffix}"

    class SamplePlugin(Plugin):
        def __init__(self, name, dependencies=()):
            self.meta = PluginMeta(name=name, version="1.0.0", dependencies=list(dependencies))
            self.calls = 0
        async def initialize(self, ctx):
            pass
        def get_capability_engines(self):
            return {RouteType.FAQ: self} if self.meta.name == owner else {}
        async def answer(self, pre, session, hints=None):
            self.calls += 1
            return CapabilityResult(route=RouteType.FAQ, reply_text="allowed")

    store = PluginStateStore()
    replicas = []
    try:
        for _ in range(2):
            registry = PluginRegistry(state_store=store)
            registry._register(SamplePlugin(dependency))
            registry._register(SamplePlugin(owner, [dependency]))
            await registry.reconcile_state()
            await registry.initialize_all(PluginContext(
                container=SimpleNamespace(agent_tool_registry=AgentToolRegistry()), settings=object(), db_ok=True,
            ))
            replicas.append(registry)
        engine = replicas[1].all_capability_engines()[RouteType.FAQ]
        session = Session(tenant_id=tenant, session_id="room", user_id="user", channel=Channel.WECHAT)
        message = PreprocessedMessage(original_text="hello", cleaned_text="hello")
        assert (await engine.answer(message, session)).reply_text == "allowed"

        await store.set_enabled(dependency, False)
        with pytest.raises(CapabilityOwnerExecutionDenied):
            await engine.answer(message, session)
        assert replicas[1].is_active(dependency)  # No local unregister on replica two.
        await store.set_enabled(dependency, True)

        await store.set_scope_enabled(tenant_id=tenant, session_id="room", plugin_name=dependency,
                                      enabled=False, expected_version=0)
        with pytest.raises(CapabilityOwnerExecutionDenied):
            await engine.answer(message, session)
        with pytest.raises(PluginScopeVersionConflictError):
            await store.set_scope_enabled(tenant_id=tenant, session_id="room", plugin_name=dependency,
                                          enabled=True, expected_version=0)
        await store.set_scope_enabled(tenant_id=tenant, session_id="room", plugin_name=dependency,
                                      enabled=True, expected_version=1)
        assert (await engine.answer(message, session)).reply_text == "allowed"

        await store.set_enabled(owner, True, restart_required=True)
        with pytest.raises(CapabilityOwnerExecutionDenied):
            await engine.answer(message, session)
        await store.set_enabled(owner, False)
        assert await store.mark_initialized(owner, "1.0.0") is False

        # A fresh replica only sorts enabled plugins. The disabled owner's
        # enabled dependency must never substitute for the owner itself.
        fresh = PluginRegistry(state_store=store)
        fresh._register(SamplePlugin(dependency))
        fresh._register(SamplePlugin(owner, [dependency]))
        await fresh.reconcile_state()
        await fresh.initialize_all(PluginContext(
            container=SimpleNamespace(agent_tool_registry=AgentToolRegistry()), settings=object(), db_ok=True,
        ))
        replicas.append(fresh)
        assert fresh.is_initialized(dependency)
        assert not fresh.is_initialized(owner)
        assert await fresh.global_execution_allowed(owner) is False
        from app.plugin.diagnostics import query_runtime
        result = await query_runtime(fresh.loaded_plugins[owner], fresh, store, owner)
        assert result["runtime_query"]["status"] == "blocked"
    finally:
        for registry in replicas:
            await registry.shutdown_all()
        async with get_engine().begin() as connection:
            await connection.execute(text("DELETE FROM plugin_scope_state WHERE tenant_id=:tenant"), {"tenant": tenant})
            await connection.execute(text("DELETE FROM plugin_state WHERE plugin_name IN (:owner, :dependency)"),
                                     {"owner": owner, "dependency": dependency})
