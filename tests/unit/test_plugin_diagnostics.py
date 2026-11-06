import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.plugin import diagnostics


@pytest.fixture
def runtime_fixture():
    state = SimpleNamespace(installed=True, enabled=True, status="active", restart_required=False, version="1.0.0", as_dict=lambda: {"enabled": True, "status": "active"})
    plugin = SimpleNamespace(meta=SimpleNamespace(version="1.0.0"), get_runtime_status=AsyncMock(return_value={"running": True}))
    registry = SimpleNamespace(global_execution_allowed=AsyncMock(return_value=True))
    store = SimpleNamespace(get=AsyncMock(return_value=state))
    return plugin, registry, store


@pytest.mark.parametrize("failure", ["timeout", "error", "invalid", "store_timeout", "store_error"])
async def test_diagnostics_failure_does_not_change_durable_enablement(runtime_fixture, monkeypatch, failure):
    plugin, registry, store = runtime_fixture
    monkeypatch.setattr(diagnostics, "RUNTIME_QUERY_TIMEOUT_SECONDS", 0.01)

    async def slow(*args):
        await asyncio.Event().wait()

    if failure == "timeout":
        plugin.get_runtime_status.side_effect = slow
    elif failure == "store_timeout":
        store.get.side_effect = slow
    elif failure == "store_error":
        store.get.side_effect = RuntimeError("private database credentials")
    elif failure == "error":
        plugin.get_runtime_status.side_effect = RuntimeError("private plugin credentials")
    else:
        plugin.get_runtime_status.return_value = []
    result = await diagnostics.query_runtime(plugin, registry, store, "sample")
    assert result["runtime_query"]["status"] == ("timeout" if "timeout" in failure else "error")
    assert result["runtime_status"] is None
    assert "private" not in str(result)
    if not failure.startswith("store"):
        assert result["state"] == {"enabled": True, "status": "active"}
    else:
        plugin.get_runtime_status.assert_not_awaited()


async def test_disabled_probe_is_never_called_and_disable_during_probe_is_revalidated(runtime_fixture):
    plugin, registry, store = runtime_fixture
    registry.global_execution_allowed.return_value = False
    result = await diagnostics.query_runtime(plugin, registry, store, "sample")
    assert result["runtime_query"]["status"] == "blocked"
    plugin.get_runtime_status.assert_not_awaited()
    registry.global_execution_allowed.side_effect = [True, False]
    result = await diagnostics.query_runtime(plugin, registry, store, "sample")
    assert result["runtime_status"] == {"running": False, "execution_allowed": False}
    assert result["runtime_query"]["status"] == "blocked"


async def test_caller_cancellation_is_not_downgraded_to_diagnostic_error(runtime_fixture):
    plugin, registry, store = runtime_fixture
    plugin.get_runtime_status.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await diagnostics.query_runtime(plugin, registry, store, "sample")


async def test_durable_disabled_state_denies_even_a_permissive_registry(runtime_fixture):
    plugin, registry, store = runtime_fixture
    store.get.return_value.enabled = False
    result = await diagnostics.query_runtime(plugin, registry, store, "sample")
    assert result["runtime_query"]["status"] == "blocked"
    plugin.get_runtime_status.assert_not_awaited()
    registry.global_execution_allowed.assert_not_awaited()
