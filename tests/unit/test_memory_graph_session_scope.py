from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI

import plugins.memory.store as memory_store_module
from app.admin.authorization import (
    AdminRole,
    Principal,
    build_admin_authorization_dependency,
)
from app.common.config import Settings
from plugins.memory.router import build_memory_router
from plugins.memory.store import MemoryStore


@pytest.mark.asyncio
async def test_graph_entity_and_fact_queries_bind_backing_items_to_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, dict(params or {})))
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    store = MemoryStore(SimpleNamespace())

    await store.list_memory_graph_entities(
        tenant_id="tenant-a",
        channel="wechat",
        session_id="group-a@chatroom",
    )
    await store.list_memory_graph_facts(
        tenant_id="tenant-a",
        channel="wechat",
        session_id="group-a@chatroom",
    )

    entity_sql, entity_params = calls[0]
    fact_sql, fact_params = calls[1]
    assert "scope_item.id = scope_fact.memory_item_id" in entity_sql
    assert "scope_item.session_id = :sid" in entity_sql
    assert "scope_fact.tenant_id = entity.tenant_id" in entity_sql
    assert "scope_fact.channel = entity.channel" in entity_sql
    assert "scope_fact.source_key = entity.source_key" in entity_sql
    assert "scope_fact.user_id = entity.user_id" in entity_sql
    assert entity_params["sid"] == "group-a@chatroom"

    assert "scope_item.id = fact.memory_item_id" in fact_sql
    assert "scope_item.session_id = :sid" in fact_sql
    assert "scope_item.tenant_id = fact.tenant_id" in fact_sql
    assert "scope_item.channel = fact.channel" in fact_sql
    assert "scope_item.source_key = fact.source_key" in fact_sql
    assert "scope_item.user_id = fact.user_id" in fact_sql
    assert fact_params["sid"] == "group-a@chatroom"


class _PreviewStore:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(
            admin_bearer_token="admin-token",
            admin_principal_tokens_json="",
        )
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def list_memory_graph_entities(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("entities", kwargs))
        return []

    async def list_memory_graph_facts(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("facts", kwargs))
        return []

    async def list_memory_graph_episodes(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("episodes", kwargs))
        return []


@pytest.mark.asyncio
async def test_graph_preview_pushes_requested_session_to_every_graph_query() -> None:
    store = _PreviewStore()
    app = FastAPI()
    app.include_router(build_memory_router(store))  # type: ignore[arg-type]

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/graph/preview",
            params={
                "tenant_id": "demo",
                "channel": "wechat",
                "session_id": "group-a@chatroom",
            },
            headers={"Authorization": "Bearer admin-token"},
        )

    assert response.status_code == 200
    assert [name for name, _kwargs in store.calls] == [
        "entities",
        "facts",
        "episodes",
    ]
    assert all(kwargs["session_id"] == "group-a@chatroom" for _name, kwargs in store.calls)


@pytest.mark.asyncio
async def test_delegated_group_observer_preview_stays_on_authorized_session() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="admin-token",
        outbound_hmac_secret="test-secret",
        tenant_demo_secret="test-tenant-secret",
    )
    principal = Principal(
        subject="group-a-observer",
        roles=(AdminRole.OBSERVER.value,),
        tenant_ids=("demo",),
        group_ids=("group-a@chatroom",),
        auth_kind="test",
    )

    async def authenticate() -> Principal:
        return principal

    store = _PreviewStore()
    store.settings = settings
    guard = build_admin_authorization_dependency(
        settings,
        authentication_dependency=authenticate,
    )
    app = FastAPI()
    mounted = APIRouter(
        prefix="/plugins/memory",
        dependencies=[Depends(guard)],
    )
    mounted.include_router(build_memory_router(store))  # type: ignore[arg-type]
    app.include_router(mounted)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        allowed = await client.get(
            "/plugins/memory/graph/preview",
            params={
                "tenant_id": "demo",
                "channel": "wechat",
                "session_id": "group-a@chatroom",
            },
            headers={"Authorization": "Bearer admin-token"},
        )
        forbidden = await client.get(
            "/plugins/memory/graph/preview",
            params={
                "tenant_id": "demo",
                "channel": "wechat",
                "session_id": "group-b@chatroom",
            },
            headers={"Authorization": "Bearer admin-token"},
        )

    assert allowed.status_code == 200, allowed.text
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"] == "group_scope_forbidden"
    assert store.calls
    assert all(kwargs["session_id"] == "group-a@chatroom" for _name, kwargs in store.calls)
