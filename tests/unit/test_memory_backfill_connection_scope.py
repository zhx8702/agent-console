from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.admin.mutation_ledger import MutationOutcome
from app.channel.identity import LEGACY_WXBOT_CONNECTION_ID
from plugins.memory.router import build_memory_router
from plugins.memory.store import MemoryStore


def _settings(**overrides: Any) -> SimpleNamespace:
    values = {
        "admin_bearer_token": "admin-token",
        "memory_vector_index_enabled": False,
        "wxbot_api_token": "sdk-token",
        "wxbot_default_tenant_id": "demo",
        "wxbot_sdk_url": "http://127.0.0.1:5080",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _BackfillRouterStore:
    def __init__(self) -> None:
        self.settings = _settings()
        self.params: dict[str, Any] | None = None

    async def backfill_from_sdk_idempotent(self, **kwargs: Any) -> MutationOutcome:
        self.params = dict(kwargs["params"])
        return MutationOutcome(
            response={"ok": True, "connection_id": self.params["connection_id"]},
            status_code=200,
            replayed=False,
            mutation_id="memory-backfill-scope-test",
        )


@pytest.mark.asyncio
async def test_backfill_router_requires_and_forwards_explicit_legacy_connection() -> None:
    store = _BackfillRouterStore()
    app = FastAPI()
    app.include_router(build_memory_router(store))  # type: ignore[arg-type]
    transport = httpx.ASGITransport(app=app)
    headers = {
        "Authorization": "Bearer admin-token",
        "Idempotency-Key": "backfill-scope",
    }
    base_payload = {
        "tenant_id": "demo",
        "session_ids": ["room@chatroom"],
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing = await client.post("/backfill", headers=headers, json=base_payload)
        empty = await client.post(
            "/backfill",
            headers=headers,
            json={**base_payload, "connection_id": ""},
        )
        managed = await client.post(
            "/backfill",
            headers=headers,
            json={**base_payload, "connection_id": "wechat-managed"},
        )
        wrong_tenant = await client.post(
            "/backfill",
            headers=headers,
            json={
                **base_payload,
                "tenant_id": "other",
                "connection_id": LEGACY_WXBOT_CONNECTION_ID,
            },
        )
        valid = await client.post(
            "/backfill",
            headers=headers,
            json={
                **base_payload,
                "connection_id": LEGACY_WXBOT_CONNECTION_ID,
            },
        )

    assert missing.status_code == 422
    assert empty.status_code == 400
    assert empty.json()["detail"] == "connection_id cannot be empty"
    assert managed.status_code == 400
    assert managed.json()["detail"] == "connection_scoped_history_unavailable"
    assert wrong_tenant.status_code == 400
    assert wrong_tenant.json()["detail"] == "legacy_wxbot_history_tenant_unavailable"
    assert valid.status_code == 200
    assert valid.json()["connection_id"] == LEGACY_WXBOT_CONNECTION_ID
    assert store.params is not None
    assert store.params["connection_id"] == LEGACY_WXBOT_CONNECTION_ID


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tenant_id", "connection_id", "expected_error"),
    [
        ("other", LEGACY_WXBOT_CONNECTION_ID, "legacy_wxbot_history_tenant_unavailable"),
        ("demo", "wechat-managed", "connection_scoped_history_unavailable"),
        ("demo", "", "connection_id cannot be empty"),
    ],
)
async def test_sdk_history_boundary_rejects_invalid_connection_scope_before_network(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: str,
    connection_id: str,
    expected_error: str,
) -> None:
    store = MemoryStore(_settings())
    network_calls: list[str] = []

    async def fail_if_called(*args: Any, **kwargs: Any) -> httpx.Response:
        _ = args, kwargs
        network_calls.append("called")
        raise AssertionError("network must not be called")

    monkeypatch.setattr(
        "plugins.memory.store_backfill.safe_trusted_service_request",
        fail_if_called,
    )

    with pytest.raises(RuntimeError, match=expected_error):
        await store._sdk_query_read(
            tenant_id=tenant_id,
            session_id="room@chatroom",
            connection_id=connection_id,
            database="message",
            sql="SELECT 1",
        )

    assert network_calls == []


@pytest.mark.asyncio
async def test_backfill_rejects_invalid_connection_before_entering_history_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(_settings())
    store.runtime_scope_gates_required = True
    adapter_calls: list[str] = []

    async def fail_if_called(**kwargs: Any) -> dict[str, Any]:
        _ = kwargs
        adapter_calls.append("called")
        raise AssertionError("history adapter must not be entered")

    monkeypatch.setattr(store, "_backfill_from_sdk_scoped", fail_if_called)

    with pytest.raises(RuntimeError, match="connection_scoped_history_unavailable"):
        await store.backfill_from_sdk(
            tenant_id="demo",
            connection_id="wechat-managed",
            channel="wechat",
            source_key="wxbot",
            user_id="wxid-member",
            session_ids=["room@chatroom"],
        )

    assert adapter_calls == []


@pytest.mark.asyncio
async def test_sdk_history_boundary_rechecks_combined_owner_gate_after_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(_settings())
    store.runtime_scope_gates_required = True
    gate_decisions = iter((True, False))
    gate_calls: list[tuple[str, str]] = []
    network_calls: list[str] = []
    sequence: list[str] = []

    async def combined_gate(tenant_id: str, session_id: str) -> bool:
        sequence.append("gate")
        gate_calls.append((tenant_id, session_id))
        return next(gate_decisions)

    async def fake_request(*args: Any, **kwargs: Any) -> httpx.Response:
        _ = args, kwargs
        sequence.append("network")
        network_calls.append("called")
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"ok": True, "rows": [{"private": "must-not-escape"}]},
            request=httpx.Request("POST", "http://127.0.0.1:5080/ext/query/read"),
        )

    store.combined_history_scope_execution_allowed = combined_gate
    monkeypatch.setattr(
        "plugins.memory.store_backfill.safe_trusted_service_request",
        fake_request,
    )

    with pytest.raises(RuntimeError, match="memory/wxbot plugin runtime disabled"):
        await store._sdk_query_read(
            tenant_id="demo",
            session_id="room@chatroom",
            connection_id=LEGACY_WXBOT_CONNECTION_ID,
            database="message",
            sql="SELECT private",
        )

    assert network_calls == ["called"]
    assert sequence == ["gate", "network", "gate"]
    assert gate_calls == [
        ("demo", "room@chatroom"),
        ("demo", "room@chatroom"),
    ]
