from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from plugins.memory.store import MemoryStore


def _runtime_store() -> MemoryStore:
    store = MemoryStore(
        SimpleNamespace(
            wxbot_sdk_url="http://127.0.0.1:5080",
            wxbot_api_token="test-token",
            wxbot_default_tenant_id="tenant-a",
            memory_vector_index_enabled=False,
        )
    )
    store.runtime_scope_gates_required = True
    return store


@pytest.mark.asyncio
async def test_history_sdk_read_fails_closed_when_wxbot_owner_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _runtime_store()
    requests: list[str] = []

    async def memory_gate(_tenant_id: str, _session_id: str) -> bool:
        return True

    async def wxbot_gate(_tenant_id: str, _session_id: str) -> bool:
        return False

    async def fake_request(*args, **kwargs):
        _ = (args, kwargs)
        requests.append("sent")
        return httpx.Response(200, json={"ok": True, "rows": []})

    store.scope_execution_allowed = memory_gate
    store.history_scope_execution_allowed = wxbot_gate
    monkeypatch.setattr(
        "plugins.memory.store_backfill.safe_trusted_service_request",
        fake_request,
    )

    with pytest.raises(RuntimeError, match="wxbot plugin runtime disabled"):
        await store._sdk_query_read(
            tenant_id="tenant-a",
            session_id="room-a",
            connection_id="legacy-wechat-default",
            database="message",
            sql="SELECT 1",
        )

    assert requests == []


@pytest.mark.asyncio
async def test_history_sdk_response_is_discarded_when_scope_disables_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _runtime_store()
    wxbot_checks = 0

    async def memory_gate(_tenant_id: str, _session_id: str) -> bool:
        return True

    async def wxbot_gate(tenant_id: str, session_id: str) -> bool:
        nonlocal wxbot_checks
        assert (tenant_id, session_id) == ("tenant-a", "room-a")
        wxbot_checks += 1
        return wxbot_checks == 1

    async def fake_request(*args, **kwargs):
        _ = (args, kwargs)
        return httpx.Response(
            200,
            json={"ok": True, "rows": [{"private_message": "must-not-persist"}]},
        )

    store.scope_execution_allowed = memory_gate
    store.history_scope_execution_allowed = wxbot_gate
    monkeypatch.setattr(
        "plugins.memory.store_backfill.safe_trusted_service_request",
        fake_request,
    )

    with pytest.raises(RuntimeError, match="wxbot plugin runtime disabled"):
        await store._sdk_query_read(
            tenant_id="tenant-a",
            session_id="room-a",
            connection_id="legacy-wechat-default",
            database="message",
            sql="SELECT private_message",
        )

    assert wxbot_checks == 2


@pytest.mark.asyncio
async def test_profile_enrichment_rechecks_combined_owners_before_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _runtime_store()
    writes: list[dict[str, object]] = []

    async def deny_combined(_tenant_id: str, _session_id: str) -> bool:
        return False

    async def fake_insert(**kwargs):
        writes.append(kwargs)
        return kwargs

    store.combined_history_scope_execution_allowed = deny_combined
    monkeypatch.setattr(store, "_insert_or_touch_memory_item", fake_insert)

    with pytest.raises(RuntimeError, match="memory/wxbot plugin runtime disabled"):
        await store.create_profile_enrichment_candidate(
            tenant_id="tenant-a",
            session_id="room-a",
            user_id="member-a",
            report_payload={"profile": {"summary": "private report"}},
            require_history_owner=True,
        )

    assert writes == []
