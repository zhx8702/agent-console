from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

import plugins.memory.store as memory_store_module
from plugins.memory.store import MemoryStore, _semantic_key


@pytest.fixture(autouse=True)
def _bind_unit_memory_transaction():
    """Keep storage unit tests inside an injected non-Postgres transaction."""

    token = memory_store_module._ACTIVE_MUTATION_CONNECTION.set(
        SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    )
    try:
        yield
    finally:
        memory_store_module._ACTIVE_MUTATION_CONNECTION.reset(token)


class _GraphSqlFake:
    def __init__(self) -> None:
        self.entities: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
        self.facts: dict[int, dict[str, Any]] = {}
        self.episodes: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.next_entity_id = 1

    async def exec(self, sql: str, params: dict | None = None) -> list[dict]:
        self.calls.append((sql, params))
        if "INSERT INTO plugin_memory_entity" in sql and params:
            key = (
                params["tid"],
                params["channel"],
                params["source_key"],
                params["uid"],
                params["entity_type"],
                params["normalized_name"],
            )
            if key not in self.entities:
                self.entities[key] = {"id": self.next_entity_id, **params}
                self.next_entity_id += 1
            else:
                self.entities[key].update(
                    {
                        "name": params["name"],
                        "confidence": max(
                            float(self.entities[key].get("confidence") or 0.0),
                            float(params.get("confidence") or 0.0),
                        ),
                    }
                )
                if self.entities[key].get("status") == "deleted":
                    self.entities[key]["status"] = params.get("status")
            return [{"id": self.entities[key]["id"]}]

        if "INSERT INTO plugin_memory_fact" in sql and params:
            existing = self.facts.get(params["memory_item_id"], {})
            self.facts[params["memory_item_id"]] = {
                **existing,
                **params,
                "confidence": max(
                    float(existing.get("confidence") or 0.0),
                    float(params.get("confidence") or 0.0),
                ),
            }
            return []

        if "INSERT INTO plugin_memory_episode" in sql and params:
            self.episodes[params["memory_item_ids"]] = dict(params)
            return []

        if "UPDATE plugin_memory_fact SET status" in sql and params:
            fact = self.facts.get(params["memory_item_id"])
            if fact is not None:
                fact["status"] = params["status"]
            return []

        if "UPDATE plugin_memory_episode SET status" in sql and params:
            episode = self.episodes.get(params["memory_item_ids_json"])
            if episode is not None:
                episode["status"] = params["status"]
            return []

        return []


def _item(
    *,
    id: int = 1,
    tenant_id: str = "demo",
    channel: str = "wechat",
    source_key: str = "wxbot",
    user_id: str = "wxid_a",
    session_id: str = "",
    memory_type: str = "preference",
    content: str = "用户喜欢 Adidas",
    normalized_key: str | None = None,
    status: str = "active",
    source_type: str = "auto",
    source_event_id: int | None = 10,
) -> dict[str, Any]:
    return {
        "id": id,
        "tenant_id": tenant_id,
        "channel": channel,
        "source_key": source_key,
        "user_id": user_id,
        "session_id": session_id,
        "scope_type": "identity" if not session_id else "session",
        "source_type": source_type,
        "memory_type": memory_type,
        "content": content,
        "value_json": "{}",
        "value": {},
        "normalized_key": normalized_key or _semantic_key("preference", "brand", "Adidas"),
        "confidence": 0.86,
        "status": status,
        "pinned": False,
        "priority": 0,
        "sensitivity": "normal",
        "source_event_id": source_event_id,
        "source_trace_id": "",
        "original_text": content,
        "occurrence_count": 1,
        "first_seen_at": None,
        "last_seen_at": None,
        "created_at": None,
        "updated_at": None,
        "deleted_at": None,
    }


@pytest.mark.asyncio
async def test_memory_graph_mapper_idempotent_preference_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _GraphSqlFake()
    monkeypatch.setattr(memory_store_module, "_exec", graph.exec)
    store = MemoryStore(SimpleNamespace())
    item = _item()

    await store._sync_memory_graph_for_item(item)
    await store._sync_memory_graph_for_item(item)

    assert len(graph.entities) == 2
    assert len(graph.facts) == 1
    fact = graph.facts[1]
    assert fact["predicate"] == "likes"
    assert fact["object_value"] == ""
    assert fact["status"] == "active"
    assert sum(1 for sql, _ in graph.calls if "INSERT INTO plugin_memory_fact" in sql) == 2
    fact_sql = next(sql for sql, _ in graph.calls if "INSERT INTO plugin_memory_fact" in sql)
    assert "CASE WHEN CAST(:status AS VARCHAR) IN" in fact_sql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "item,expected_predicate,expected_object_value",
    [
        (
            _item(
                content="用户喜欢 Adidas",
                normalized_key=_semantic_key("preference", "brand", "Adidas"),
            ),
            "likes",
            "",
        ),
        (
            _item(
                content="用户不喜欢 Adidas",
                normalized_key=_semantic_key("preference", "brand", "Adidas"),
            ),
            "dislikes",
            "",
        ),
        (
            _item(
                memory_type="constraint",
                content="默认中文简洁回答",
                normalized_key=_semantic_key("constraint", "response_defaults", "language_style"),
            ),
            "prefers_response_style",
            "默认中文简洁回答",
        ),
    ],
)
async def test_memory_graph_mapper_preference_dislike_and_constraint(
    monkeypatch: pytest.MonkeyPatch,
    item: dict[str, Any],
    expected_predicate: str,
    expected_object_value: str,
) -> None:
    facts: list[dict[str, Any]] = []
    next_entity_id = 1

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        nonlocal next_entity_id
        if "INSERT INTO plugin_memory_entity" in sql:
            entity_id = next_entity_id
            next_entity_id += 1
            return [{"id": entity_id}]
        if "INSERT INTO plugin_memory_fact" in sql and params:
            facts.append(dict(params))
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    await MemoryStore(SimpleNamespace())._sync_memory_graph_for_item(item)

    assert facts[-1]["predicate"] == expected_predicate
    assert facts[-1]["object_value"] == expected_object_value


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["invalidated", "deleted", "archived"])
async def test_memory_graph_status_syncs_from_memory_item(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    graph = _GraphSqlFake()
    monkeypatch.setattr(memory_store_module, "_exec", graph.exec)

    await MemoryStore(SimpleNamespace())._sync_memory_graph_for_item(_item(status=status))

    assert graph.facts[1]["status"] == status
    fact_sql = next(sql for sql, _ in graph.calls if "INSERT INTO plugin_memory_fact" in sql)
    assert "CASE WHEN CAST(:status AS VARCHAR) IN" in fact_sql


@pytest.mark.asyncio
async def test_memory_graph_listing_is_scoped_no_cross_user_leakage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_params: list[dict[str, Any]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        seen_params.append(params or {})
        assert "fact.tenant_id = :tid" in sql
        assert "fact.channel = :channel" in sql
        assert "fact.source_key = :source_key" in sql
        assert "fact.user_id = :uid" in sql
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    await MemoryStore(SimpleNamespace()).list_memory_graph_facts(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
    )

    assert seen_params == [
        {"tid": "demo", "lim": 100, "channel": "wechat", "source_key": "wxbot", "uid": "wxid_a"}
    ]


@pytest.mark.asyncio
async def test_memory_graph_listings_apply_supplied_empty_scope_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        seen.append((sql, params or {}))
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    store = MemoryStore(SimpleNamespace())

    await store.list_memory_graph_entities(
        tenant_id="demo", channel="", source_key="", user_id="", status=""
    )
    await store.list_memory_graph_episodes(
        tenant_id="demo",
        channel="",
        source_key="",
        user_id="",
        session_id="",
        status="",
    )

    assert "channel = :channel" in seen[0][0]
    assert "source_key = :source_key" in seen[0][0]
    assert "user_id = :uid" in seen[0][0]
    assert "status = :status" in seen[0][0]
    assert seen[0][1]["channel"] == ""
    assert seen[0][1]["source_key"] == ""
    assert seen[0][1]["uid"] == ""
    assert seen[0][1]["status"] == ""
    assert "session_id = :sid" in seen[1][0]
    assert seen[1][1]["sid"] == ""


@pytest.mark.asyncio
async def test_memory_graph_fact_join_is_scoped_to_fact_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_sql: list[str] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        seen_sql.append(sql)
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    await MemoryStore(SimpleNamespace()).list_memory_graph_facts(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
    )

    sql = seen_sql[0]
    assert "subject.tenant_id = fact.tenant_id" in sql
    assert "subject.channel = fact.channel" in sql
    assert "subject.source_key = fact.source_key" in sql
    assert "subject.user_id = fact.user_id" in sql
    assert "object_entity.tenant_id = fact.tenant_id" in sql
    assert "object_entity.channel = fact.channel" in sql
    assert "object_entity.source_key = fact.source_key" in sql
    assert "object_entity.user_id = fact.user_id" in sql


@pytest.mark.asyncio
async def test_group_relationship_graph_aggregates_safe_scope_and_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params))
        if "FROM plugin_memory_entity" in sql:
            return [
                {
                    "id": 1,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "entity_type": "person",
                    "name": "Alice",
                    "normalized_name": "alice",
                    "aliases_json": "[]",
                    "confidence": 0.93,
                    "status": "active",
                    "created_at": "2026-05-01T00:00:00",
                    "updated_at": "2026-05-10T00:00:00",
                },
                {
                    "id": 2,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_b",
                    "entity_type": "person",
                    "name": "Bob",
                    "normalized_name": "bob",
                    "aliases_json": "[]",
                    "confidence": 0.91,
                    "status": "active",
                    "created_at": "2026-05-01T00:00:00",
                    "updated_at": "2026-05-10T00:00:00",
                },
            ]
        if "FROM plugin_memory_fact fact" in sql:
            return [
                {
                    "id": 10,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "subject_entity_id": 1,
                    "subject_name": "Alice",
                    "predicate": "knows",
                    "object_entity_id": 2,
                    "object_name": "Bob",
                    "object_value": "",
                    "memory_item_id": 100,
                    "source_event_id": 500,
                    "confidence": 0.82,
                    "status": "active",
                    "valid_at": "2026-05-05T00:00:00",
                    "invalid_at": None,
                    "created_at": "2026-05-05T00:00:00",
                    "updated_at": "2026-05-12T00:00:00",
                },
                {
                    "id": 11,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "subject_entity_id": 1,
                    "subject_name": "Alice",
                    "predicate": "mentions",
                    "object_entity_id": None,
                    "object_name": None,
                    "object_value": "raw sentence must not leak",
                    "memory_item_id": 101,
                    "source_event_id": 501,
                    "confidence": 0.2,
                    "status": "active",
                    "valid_at": "2026-05-05T00:00:00",
                    "invalid_at": None,
                    "created_at": "2026-05-05T00:00:00",
                    "updated_at": "2026-05-12T00:00:00",
                },
            ]
        if "FROM plugin_memory_episode" in sql:
            return [
                {
                    "id": 20,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "session_id": "group-1@chatroom",
                    "title": "hidden episode title",
                    "summary": "hidden episode summary",
                    "event_ids_json": "[500, 501]",
                    "memory_item_ids_json": "[100, 101]",
                    "importance": 1,
                    "status": "active",
                    "created_at": "2026-05-05T00:00:00",
                    "updated_at": "2026-05-12T00:00:00",
                }
            ]
        if "FROM plugin_memory_item WHERE id = ANY" in sql:
            return [
                {
                    "id": 100,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "session_id": "group-1@chatroom",
                    "scope_type": "session",
                    "source_type": "auto",
                    "memory_type": "note",
                    "content": "private raw content",
                    "value_json": '{"acceptance":{"status":"needs_review"}}',
                    "normalized_key": "relation:knows",
                    "confidence": 0.82,
                    "status": "active",
                    "pinned": False,
                    "priority": 0,
                    "sensitivity": "normal",
                    "source_event_id": 500,
                    "source_trace_id": "",
                    "original_text": "private original text",
                    "occurrence_count": 1,
                    "first_seen_at": "2026-05-05T00:00:00",
                    "last_seen_at": "2026-05-12T00:00:00",
                    "created_at": "2026-05-05T00:00:00",
                    "updated_at": "2026-05-12T00:00:00",
                    "deleted_at": None,
                },
                {
                    "id": 101,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "session_id": "group-1@chatroom",
                    "scope_type": "session",
                    "source_type": "auto",
                    "memory_type": "note",
                    "content": "low confidence content",
                    "value_json": "{}",
                    "normalized_key": "relation:mentions",
                    "confidence": 0.2,
                    "status": "active",
                    "pinned": False,
                    "priority": 0,
                    "sensitivity": "normal",
                    "source_event_id": 501,
                    "source_trace_id": "",
                    "original_text": "low confidence original",
                    "occurrence_count": 1,
                    "first_seen_at": "2026-05-05T00:00:00",
                    "last_seen_at": "2026-05-12T00:00:00",
                    "created_at": "2026-05-05T00:00:00",
                    "updated_at": "2026-05-12T00:00:00",
                    "deleted_at": None,
                },
            ]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    graph = await store.get_group_relationship_graph(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="group-1@chatroom",
        node_type="person",
        edge_type="knows",
        acceptance_status="needs_review",
        min_confidence=0.45,
        limit=10,
    )

    assert graph["scope"] == {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "session_id": "group-1@chatroom",
    }
    assert graph["schema"]["version"] == "group-graph.v1"
    assert "person" in graph["schema"]["node_types"]
    assert "works_on" in graph["schema"]["edge_types"]
    assert graph["filters"]["node_type"] == "person"
    assert graph["filters"]["edge_type"] == "knows"
    assert graph["counts"] == {"nodes": 2, "edges": 1}
    assert graph["nodes"][0]["label"] == "Alice"
    assert graph["nodes"][0]["display_label"] == "Alice"
    assert graph["nodes"][0]["technical_label"] == "alice"
    assert graph["nodes"][0]["aliases"] == []
    assert graph["edges"][0]["acceptance_status"] == "needs_review"
    assert graph["edges"][0]["source_event_ids"] == [500, 501]
    assert graph["edges"][0]["memory_item_ids"] == [100, 101]
    event_sql = next(sql for sql, _ in calls if "FROM plugin_memory_event WHERE id = ANY" in sql)
    event_select = event_sql.split("FROM plugin_memory_event", 1)[0].lower()
    assert "user_text" not in event_select
    assert "assistant_text" not in event_select
    item_sql = next(sql for sql, _ in calls if "FROM plugin_memory_item WHERE id = ANY" in sql)
    item_select = item_sql.split("FROM plugin_memory_item", 1)[0].lower()
    assert "content" not in item_select
    assert "original_text" not in item_select
    serialized = str(graph)
    assert "content" not in serialized
    assert "original_text" not in serialized
    assert "private raw" not in serialized
    assert "hidden episode" not in serialized


@pytest.mark.asyncio
async def test_group_relationship_graph_defaults_to_accepted_active_backing_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "FROM plugin_memory_entity" in sql:
            return [
                {
                    "id": 1,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "entity_type": "person",
                    "name": "Alice",
                    "normalized_name": "alice",
                    "aliases_json": "[]",
                    "confidence": 0.93,
                    "status": "active",
                    "created_at": "2026-05-01T00:00:00",
                    "updated_at": "2026-05-10T00:00:00",
                },
                {
                    "id": 2,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_b",
                    "entity_type": "person",
                    "name": "Bob",
                    "normalized_name": "bob",
                    "aliases_json": "[]",
                    "confidence": 0.91,
                    "status": "active",
                    "created_at": "2026-05-01T00:00:00",
                    "updated_at": "2026-05-10T00:00:00",
                },
            ]
        if "FROM plugin_memory_fact fact" in sql:
            return [
                {
                    "id": 10,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "subject_entity_id": 1,
                    "subject_name": "Alice",
                    "predicate": "knows",
                    "object_entity_id": 2,
                    "object_name": "Bob",
                    "object_value": "",
                    "memory_item_id": 100,
                    "source_event_id": 500,
                    "confidence": 0.82,
                    "status": "active",
                    "valid_at": "2026-05-05T00:00:00",
                    "invalid_at": None,
                    "created_at": "2026-05-05T00:00:00",
                    "updated_at": "2026-05-12T00:00:00",
                }
            ]
        if "FROM plugin_memory_episode" in sql:
            return []
        if "FROM plugin_memory_item WHERE id = ANY" in sql:
            return [
                {
                    "id": 100,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "session_id": "group-1@chatroom",
                    "scope_type": "session",
                    "source_type": "auto",
                    "memory_type": "note",
                    "value_json": '{"acceptance":{"status":"needs_review"}}',
                    "normalized_key": "relation:knows",
                    "confidence": 0.82,
                    "status": "active",
                    "pinned": False,
                    "priority": 0,
                    "sensitivity": "normal",
                    "source_event_id": 500,
                    "source_trace_id": "",
                    "occurrence_count": 1,
                    "first_seen_at": "2026-05-05T00:00:00",
                    "last_seen_at": "2026-05-12T00:00:00",
                    "created_at": "2026-05-05T00:00:00",
                    "updated_at": "2026-05-12T00:00:00",
                    "deleted_at": None,
                }
            ]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    default_graph = await store.get_group_relationship_graph(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="group-1@chatroom",
        limit=10,
    )
    explicit_graph = await store.get_group_relationship_graph(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="group-1@chatroom",
        acceptance_status="needs_review",
        limit=10,
    )

    assert default_graph["counts"] == {"nodes": 0, "edges": 0}
    assert explicit_graph["counts"] == {"nodes": 2, "edges": 1}
    assert explicit_graph["edges"][0]["acceptance_status"] == "needs_review"


@pytest.mark.asyncio
async def test_group_relationship_graph_session_filter_uses_source_event_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    seen_sql: list[str] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        seen_sql.append(sql)
        if "FROM plugin_memory_entity" in sql:
            return [
                {
                    "id": 1,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "__group__",
                    "entity_type": "person",
                    "name": "Alice",
                    "normalized_name": "alice",
                    "aliases_json": "[]",
                    "confidence": 0.93,
                    "status": "active",
                    "created_at": "2026-05-01T00:00:00",
                    "updated_at": "2026-05-10T00:00:00",
                },
                {
                    "id": 2,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "__group__",
                    "entity_type": "person",
                    "name": "Bob",
                    "normalized_name": "bob",
                    "aliases_json": "[]",
                    "confidence": 0.91,
                    "status": "active",
                    "created_at": "2026-05-01T00:00:00",
                    "updated_at": "2026-05-10T00:00:00",
                },
            ]
        if "FROM plugin_memory_fact fact" in sql:
            return [
                {
                    "id": 10,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "__group__",
                    "subject_entity_id": 1,
                    "subject_name": "Alice",
                    "predicate": "knows",
                    "object_entity_id": 2,
                    "object_name": "Bob",
                    "object_value": "",
                    "memory_item_id": 100,
                    "source_event_id": 500,
                    "confidence": 0.82,
                    "status": "active",
                    "valid_at": "2026-05-05T00:00:00",
                    "invalid_at": None,
                    "created_at": "2026-05-05T00:00:00",
                    "updated_at": "2026-05-12T00:00:00",
                }
            ]
        if "FROM plugin_memory_episode" in sql:
            return []
        if "FROM plugin_memory_item WHERE id = ANY" in sql:
            return [
                {
                    "id": 100,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "__group__",
                    "session_id": "",
                    "scope_type": "identity",
                    "source_type": "auto",
                    "memory_type": "note",
                    "content": "private raw content",
                    "value_json": '{"acceptance":{"status":"accepted"}}',
                    "normalized_key": "relation:knows",
                    "confidence": 0.82,
                    "status": "active",
                    "pinned": False,
                    "priority": 0,
                    "sensitivity": "normal",
                    "source_event_id": 500,
                    "source_trace_id": "",
                    "original_text": "private original text",
                    "occurrence_count": 1,
                    "first_seen_at": "2026-05-05T00:00:00",
                    "last_seen_at": "2026-05-12T00:00:00",
                    "created_at": "2026-05-05T00:00:00",
                    "updated_at": "2026-05-12T00:00:00",
                    "deleted_at": None,
                }
            ]
        if "FROM plugin_memory_event WHERE id = ANY" in sql:
            return [
                {
                    "id": 500,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "__group__",
                    "session_id": "00000000000@chatroom",
                    "user_text": "private user text",
                    "assistant_text": "private assistant text",
                    "created_at": "2026-05-05T00:00:00",
                }
            ]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    graph = await store.get_group_relationship_graph(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="00000000000@chatroom",
        limit=10,
    )

    assert graph["counts"] == {"nodes": 2, "edges": 1}
    assert graph["edges"][0]["source_event_ids"] == [500]
    event_sql = next(sql for sql in seen_sql if "FROM plugin_memory_event WHERE id = ANY" in sql)
    event_select = event_sql.split("FROM plugin_memory_event", 1)[0].lower()
    assert "user_text" not in event_select
    assert "assistant_text" not in event_select
    serialized = str(graph)
    assert "private raw content" not in serialized
    assert "private original text" not in serialized
    assert "private user text" not in serialized
    assert "private assistant text" not in serialized


@pytest.mark.asyncio
async def test_group_relationship_graph_includes_deterministic_person_person_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "FROM plugin_memory_entity" in sql:
            return [
                {
                    "id": 1,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "__group__",
                    "entity_type": "person",
                    "name": "wxid_alice",
                    "normalized_name": "wxid_alice",
                    "aliases_json": "[]",
                    "confidence": 0.93,
                    "status": "active",
                    "created_at": "2026-05-15T08:00:00",
                    "updated_at": "2026-05-15T08:00:00",
                },
                {
                    "id": 2,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "__group__",
                    "entity_type": "person",
                    "name": "wxid_bob",
                    "normalized_name": "wxid_bob",
                    "aliases_json": "[]",
                    "confidence": 0.93,
                    "status": "active",
                    "created_at": "2026-05-15T08:01:00",
                    "updated_at": "2026-05-15T08:01:00",
                },
            ]
        if "FROM plugin_memory_fact fact" in sql:
            return [
                {
                    "id": 30,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "__group__",
                    "subject_entity_id": 2,
                    "subject_name": "wxid_bob",
                    "predicate": "addressed",
                    "object_entity_id": 1,
                    "object_name": "wxid_alice",
                    "object_value": "",
                    "memory_item_id": 1300,
                    "source_event_id": 702,
                    "confidence": 0.72,
                    "status": "active",
                    "valid_at": "2026-05-15T08:01:00",
                    "invalid_at": None,
                    "created_at": "2026-05-15T08:01:00",
                    "updated_at": "2026-05-15T08:01:00",
                }
            ]
        if "FROM plugin_memory_episode" in sql:
            return []
        if "FROM plugin_memory_item WHERE id = ANY" in sql:
            return [
                {
                    "id": 1300,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "__group__",
                    "session_id": "room-a@chatroom",
                    "scope_type": "session",
                    "source_type": "deterministic_group_window",
                    "memory_type": "note",
                    "value_json": '{"acceptance":{"status":"accepted"}}',
                    "normalized_key": "group-window-rel:addressed:test",
                    "confidence": 0.72,
                    "status": "active",
                    "pinned": False,
                    "priority": 0,
                    "sensitivity": "normal",
                    "source_event_id": 702,
                    "source_trace_id": "",
                    "occurrence_count": 1,
                    "first_seen_at": "2026-05-15T08:01:00",
                    "last_seen_at": "2026-05-15T08:01:00",
                    "created_at": "2026-05-15T08:01:00",
                    "updated_at": "2026-05-15T08:01:00",
                    "deleted_at": None,
                }
            ]
        if "FROM plugin_memory_event WHERE id = ANY" in sql:
            return [
                {
                    "id": 702,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "__group__",
                    "session_id": "room-a@chatroom",
                    "created_at": "2026-05-15T08:01:00",
                }
            ]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    graph = await store.get_group_relationship_graph(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="room-a@chatroom",
        limit=10,
    )

    assert graph["counts"] == {"nodes": 2, "edges": 1}
    assert graph["schema"]["edge_types"]
    assert "addressed" in graph["schema"]["edge_types"]
    assert graph["edges"][0]["type"] == "addressed"
    assert graph["edges"][0]["source"] == "entity:2"
    assert graph["edges"][0]["target"] == "entity:1"
    assert graph["edges"][0]["acceptance_status"] == "accepted"
    assert graph["edges"][0]["extraction_method"] == "deterministic_group_window"
    assert all(node["type"] == "person" for node in graph["nodes"])
    serialized = str(graph)
    assert "content" not in serialized
    assert "original_text" not in serialized


@pytest.mark.asyncio
async def test_group_relationship_graph_session_filter_rejects_mismatched_source_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "FROM plugin_memory_entity" in sql:
            return [
                {
                    "id": 1,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "__group__",
                    "entity_type": "person",
                    "name": "Alice",
                    "normalized_name": "alice",
                    "aliases_json": "[]",
                    "confidence": 0.93,
                    "status": "active",
                    "created_at": "2026-05-01T00:00:00",
                    "updated_at": "2026-05-10T00:00:00",
                },
                {
                    "id": 2,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "__group__",
                    "entity_type": "person",
                    "name": "Bob",
                    "normalized_name": "bob",
                    "aliases_json": "[]",
                    "confidence": 0.91,
                    "status": "active",
                    "created_at": "2026-05-01T00:00:00",
                    "updated_at": "2026-05-10T00:00:00",
                },
            ]
        if "FROM plugin_memory_fact fact" in sql:
            return [
                {
                    "id": 10,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "__group__",
                    "subject_entity_id": 1,
                    "subject_name": "Alice",
                    "predicate": "knows",
                    "object_entity_id": 2,
                    "object_name": "Bob",
                    "object_value": "",
                    "memory_item_id": 100,
                    "source_event_id": 500,
                    "confidence": 0.82,
                    "status": "active",
                    "valid_at": "2026-05-05T00:00:00",
                    "invalid_at": None,
                    "created_at": "2026-05-05T00:00:00",
                    "updated_at": "2026-05-12T00:00:00",
                }
            ]
        if "FROM plugin_memory_episode" in sql:
            return []
        if "FROM plugin_memory_item WHERE id = ANY" in sql:
            return [
                {
                    "id": 100,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "__group__",
                    "session_id": "",
                    "scope_type": "identity",
                    "source_type": "auto",
                    "memory_type": "note",
                    "value_json": '{"acceptance":{"status":"accepted"}}',
                    "normalized_key": "relation:knows",
                    "confidence": 0.82,
                    "status": "active",
                    "pinned": False,
                    "priority": 0,
                    "sensitivity": "normal",
                    "source_event_id": 500,
                    "source_trace_id": "",
                    "occurrence_count": 1,
                    "first_seen_at": "2026-05-05T00:00:00",
                    "last_seen_at": "2026-05-12T00:00:00",
                    "created_at": "2026-05-05T00:00:00",
                    "updated_at": "2026-05-12T00:00:00",
                    "deleted_at": None,
                }
            ]
        if "FROM plugin_memory_event WHERE id = ANY" in sql:
            return [
                {
                    "id": 500,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "__group__",
                    "session_id": "other-group@chatroom",
                    "created_at": "2026-05-05T00:00:00",
                }
            ]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    graph = await store.get_group_relationship_graph(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="00000000000@chatroom",
        limit=10,
    )

    assert graph["counts"] == {"nodes": 0, "edges": 0}


@pytest.mark.asyncio
async def test_group_relationship_graph_date_filters_compare_datetimes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "FROM plugin_memory_entity" in sql:
            return [
                {
                    "id": 1,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "entity_type": "person",
                    "name": "Alice",
                    "normalized_name": "alice",
                    "aliases_json": "[]",
                    "confidence": 0.93,
                    "status": "active",
                    "created_at": "2026-05-01T00:00:00Z",
                    "updated_at": "2026-05-10T00:00:00Z",
                },
                {
                    "id": 2,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_b",
                    "entity_type": "person",
                    "name": "Bob",
                    "normalized_name": "bob",
                    "aliases_json": "[]",
                    "confidence": 0.91,
                    "status": "active",
                    "created_at": "2026-05-01T00:00:00Z",
                    "updated_at": "2026-05-10T00:00:00Z",
                },
            ]
        if "FROM plugin_memory_fact fact" in sql:
            return [
                {
                    "id": 10,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "subject_entity_id": 1,
                    "subject_name": "Alice",
                    "predicate": "knows",
                    "object_entity_id": 2,
                    "object_name": "Bob",
                    "object_value": "",
                    "memory_item_id": 100,
                    "source_event_id": 500,
                    "confidence": 0.82,
                    "status": "active",
                    "valid_at": "2026-05-05T08:00:00+08:00",
                    "invalid_at": None,
                    "created_at": "2026-05-05T08:00:00+08:00",
                    "updated_at": "2026-05-12T00:00:00Z",
                }
            ]
        if "FROM plugin_memory_episode" in sql:
            return []
        if "FROM plugin_memory_item WHERE id = ANY" in sql:
            return [
                {
                    "id": 100,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "session_id": "group-1@chatroom",
                    "scope_type": "session",
                    "source_type": "auto",
                    "memory_type": "note",
                    "value_json": '{"acceptance":{"status":"accepted"}}',
                    "normalized_key": "relation:knows",
                    "confidence": 0.82,
                    "status": "active",
                    "pinned": False,
                    "priority": 0,
                    "sensitivity": "normal",
                    "source_event_id": 500,
                    "source_trace_id": "",
                    "occurrence_count": 1,
                    "first_seen_at": "2026-05-05T00:00:00Z",
                    "last_seen_at": "2026-05-12T00:00:00Z",
                    "created_at": "2026-05-05T00:00:00Z",
                    "updated_at": "2026-05-12T00:00:00Z",
                    "deleted_at": None,
                }
            ]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    included = await store.get_group_relationship_graph(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="group-1@chatroom",
        from_="2026-05-05T00:00:00Z",
        to="2026-05-05T00:30:00Z",
        limit=10,
    )
    excluded = await store.get_group_relationship_graph(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="group-1@chatroom",
        from_="2026-05-05T00:30:01Z",
        limit=10,
    )

    assert included["counts"] == {"nodes": 2, "edges": 1}
    assert excluded["counts"] == {"nodes": 0, "edges": 0}


@pytest.mark.asyncio
async def test_memory_graph_prompt_retrieval_sql_requires_accepted_backing_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    seen_sql: list[str] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        seen_sql.append(sql)
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    await store.retrieve_memory_graph(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="group-1@chatroom",
        query="关系",
        fact_top_k=2,
        episode_top_k=1,
    )

    joined_sql = "\n".join(seen_sql)
    assert "#>> '{acceptance,status}' = 'accepted'" in joined_sql
    assert "item.deleted_at IS NULL" in joined_sql
    assert "item.sensitivity = 'normal'" in joined_sql


@pytest.mark.asyncio
async def test_memory_acceptance_review_appends_durable_audit_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    row = {
        "id": 41,
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
        "session_id": "group-1@chatroom",
        "scope_type": "session",
        "source_type": "auto",
        "memory_type": "preference",
        "content": "用户喜欢 Adidas",
        "value_json": json.dumps({"acceptance": {"status": "candidate", "history": []}}),
        "normalized_key": "preference:brand:adidas",
        "confidence": 0.88,
        "status": "pending",
        "pinned": False,
        "priority": 0,
        "sensitivity": "normal",
        "source_event_id": 7,
        "source_trace_id": "trace",
        "original_text": "",
        "occurrence_count": 1,
        "first_seen_at": None,
        "last_seen_at": None,
        "created_at": None,
        "updated_at": None,
        "deleted_at": None,
    }
    audit_rows: list[dict[str, Any]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        nonlocal row
        if "FROM plugin_memory_item WHERE id = :id" in sql:
            return [row]
        if sql.startswith("UPDATE plugin_memory_item SET"):
            assert params is not None
            row = {**row, "value_json": params["value_json"], "status": params["status"]}
            return []
        if "INSERT INTO plugin_memory_acceptance_audit" in sql:
            assert params is not None
            audit_rows.append(dict(params))
            return []
        return []

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", noop)
    monkeypatch.setattr(store, "_sync_memory_graph_for_item_safe", noop)
    monkeypatch.setattr(store, "_sync_memory_vector_for_item_safe", noop)

    result = await store.review_memory_item_acceptance(
        41,
        action="accept",
        review_reason="manual approve",
        reviewed_by="admin-test",
    )

    assert result is not None
    assert result["acceptance_status"] == "accepted"
    assert audit_rows == [
        {
            "item_id": 41,
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "wxid_a",
            "session_id": "group-1@chatroom",
            "scope_type": "session",
            "source_type": "auto",
            "action": "accept",
            "previous_status": "candidate",
            "new_status": "accepted",
            "previous_item_status": "pending",
            "new_item_status": "active",
            "reviewed_by": "admin-test",
            "actor": "admin-test",
            "reason": f"sha256:{hashlib.sha256(b'manual approve').hexdigest()}",
            "superseded_by_item_id": None,
            "supersedes_item_id": None,
            "reviewed_at": result["value"]["acceptance"]["reviewed_at"],
        }
    ]


@pytest.mark.asyncio
async def test_group_relationship_edge_evidence_safe_payload_uses_ids_not_raw_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    seen_sql: list[str] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        seen_sql.append(sql)
        if "FROM plugin_memory_fact fact" in sql:
            return [
                {
                    "id": 10,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "subject_entity_id": 1,
                    "subject_name": "Alice",
                    "predicate": "knows",
                    "object_entity_id": 2,
                    "object_name": "Bob",
                    "object_value": "raw object value",
                    "memory_item_id": 100,
                    "source_event_id": 500,
                    "confidence": 0.82,
                    "status": "active",
                    "valid_at": "2026-05-05T00:00:00",
                    "invalid_at": None,
                    "created_at": "2026-05-05T00:00:00",
                    "updated_at": "2026-05-12T00:00:00",
                }
            ]
        if "FROM plugin_memory_item WHERE id = ANY" in sql:
            return [
                {
                    "id": 100,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "session_id": "group-1@chatroom",
                    "scope_type": "session",
                    "source_type": "auto",
                    "memory_type": "note",
                    "content": "private raw content",
                    "value_json": '{"acceptance":{"status":"accepted"}}',
                    "normalized_key": "relation:knows",
                    "confidence": 0.82,
                    "status": "active",
                    "pinned": False,
                    "priority": 0,
                    "sensitivity": "normal",
                    "source_event_id": 500,
                    "source_trace_id": "",
                    "original_text": "private original text",
                    "occurrence_count": 1,
                    "first_seen_at": "2026-05-05T00:00:00",
                    "last_seen_at": "2026-05-12T00:00:00",
                    "created_at": "2026-05-05T00:00:00",
                    "updated_at": "2026-05-12T00:00:00",
                    "deleted_at": None,
                },
                {
                    "id": 101,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "session_id": "group-1@chatroom",
                    "scope_type": "session",
                    "source_type": "auto",
                    "memory_type": "note",
                    "content": "private episode memory",
                    "value_json": "{}",
                    "normalized_key": "relation:episode",
                    "confidence": 0.7,
                    "status": "active",
                    "pinned": False,
                    "priority": 0,
                    "sensitivity": "normal",
                    "source_event_id": 501,
                    "source_trace_id": "",
                    "original_text": "private episode original",
                    "occurrence_count": 1,
                    "first_seen_at": "2026-05-05T00:00:00",
                    "last_seen_at": "2026-05-12T00:00:00",
                    "created_at": "2026-05-05T00:00:00",
                    "updated_at": "2026-05-12T00:00:00",
                    "deleted_at": None,
                },
            ]
        if "FROM plugin_memory_episode" in sql:
            return [
                {
                    "id": 20,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "session_id": "group-1@chatroom",
                    "title": "private title",
                    "summary": "private episode summary",
                    "event_ids_json": "[500, 501]",
                    "memory_item_ids_json": "[100, 101]",
                    "importance": 1,
                    "status": "active",
                    "created_at": "2026-05-05T00:00:00",
                    "updated_at": "2026-05-12T00:00:00",
                }
            ]
        if "FROM plugin_memory_event WHERE id = ANY" in sql:
            return [
                {
                    "id": 500,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "session_id": "group-1@chatroom",
                    "user_text": "private user text",
                    "assistant_text": "private assistant text",
                    "trace_id": "trace-500",
                    "event_key": "event-500",
                    "created_at": "2026-05-05T00:00:00",
                }
            ]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    payload = await store.get_group_relationship_edge_evidence(
        edge_id="fact:10",
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="group-1@chatroom",
    )

    assert payload is not None
    assert payload["edge"]["id"] == "fact:10"
    assert payload["evidence_ids"] == {
        "backing_memory_item_id": 100,
        "memory_item_ids": [100, 101],
        "event_ids": [500, 501],
        "episode_ids": [20],
    }
    assert payload["evidence_counts"] == {"memory_items": 2, "events": 1, "episodes": 1}
    serialized = str(payload)
    assert "private raw content" not in serialized
    assert "private original text" not in serialized
    assert "private user text" not in serialized
    assert "private assistant text" not in serialized
    assert "private episode summary" not in serialized
    assert any(
        "SELECT id, tenant_id, channel, source_key, user_id, session_id, scope_type, source_type"
        in sql
        for sql in seen_sql
    )


@pytest.mark.asyncio
async def test_daily_group_relationship_extraction_stats_only_idempotent_and_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    items_by_key: dict[str, dict[str, Any]] = {}
    next_item_id = 700
    sync_calls: list[int] = []
    count_calls: list[dict[str, Any]] = []
    claim_calls: list[dict[str, Any]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        nonlocal next_item_id
        assert params is not None
        if "FROM plugin_memory_event" in sql:
            return [
                {
                    "id": 501,
                    "user_text": "wxid_a: RAW_FIELD_SENTINEL_A",
                    "trace_id": "trace-501",
                    "event_key": "event-501",
                    "created_at": "2026-05-15T08:00:00",
                },
                {
                    "id": 502,
                    "user_text": "wxid_b: RAW_FIELD_SENTINEL_B",
                    "trace_id": "trace-502",
                    "event_key": "event-502",
                    "created_at": "2026-05-15T09:00:00",
                },
            ]
        if "deleted_at IS NOT NULL" in sql:
            return []
        if (
            "FROM plugin_memory_item" in sql
            and "normalized_key = :normalized_key" in sql
            and "ORDER BY pinned DESC" in sql
        ):
            normalized_key = params["normalized_key"] if "normalized_key" in params else None
            if normalized_key is None:
                return list(items_by_key.values())
            item = items_by_key.get(str(normalized_key))
            return [item] if item else []
        if "SELECT id FROM plugin_memory_item" in sql:
            item = items_by_key.get(str(params["normalized_key"]))
            return [{"id": item["id"]}] if item else []
        if sql.startswith("INSERT INTO plugin_memory_item"):
            item = {
                "id": next_item_id,
                "tenant_id": params["tid"],
                "channel": params["channel"],
                "source_key": params["source_key"],
                "user_id": params["uid"],
                "session_id": params["sid"],
                "scope_type": params["scope_type"],
                "source_type": params["source_type"],
                "memory_type": params["memory_type"],
                "content": params["content"],
                "value_json": params["value_json"],
                "normalized_key": params["normalized_key"],
                "confidence": params["confidence"],
                "status": params["status"],
                "pinned": params["pinned"],
                "priority": params["priority"],
                "sensitivity": params["sensitivity"],
                "origin_session_kind": params["origin_session_kind"],
                "audience_scope": params["audience_scope"],
                "allowed_session_ids": json.loads(params["allowed_session_ids"]),
                "sensitivity_category": params["sensitivity_category"],
                "source_kind": params["source_kind"],
                "expires_at": params["expires_at"],
                "source_event_id": params["source_event_id"],
                "source_trace_id": params["source_trace_id"],
                "original_text": params["original_text"],
                "occurrence_count": 1,
                "first_seen_at": None,
                "last_seen_at": None,
                "created_at": None,
                "updated_at": None,
                "deleted_at": None,
            }
            items_by_key[str(params["normalized_key"])] = item
            next_item_id += 1
            return [item]
        if sql.startswith("UPDATE plugin_memory_item SET"):
            item = next(item for item in items_by_key.values() if item["id"] == params["id"])
            item.update(
                {
                    "content": params["content"],
                    "value_json": params["value_json"],
                    "memory_type": params["memory_type"],
                    "confidence": max(
                        float(item.get("confidence") or 0.0), float(params["confidence"])
                    ),
                    "status": params["status"],
                    "source_event_id": item.get("source_event_id") or params["source_event_id"],
                    "source_trace_id": item.get("source_trace_id") or params["source_trace_id"],
                    "original_text": item.get("original_text") or params["original_text"],
                    "occurrence_count": int(item.get("occurrence_count") or 0) + 1,
                }
            )
            return []
        if "FROM plugin_memory_item WHERE id = :id" in sql:
            item = next(
                (item for item in items_by_key.values() if item["id"] == params["id"]), None
            )
            return [item] if item else []
        return []

    async def fake_collect_session_history(**kwargs):
        return [
            {"user_text": "RAW_HISTORY_SENTINEL_A"},
            {"user_text": "RAW_HISTORY_SENTINEL_B"},
            {"user_text": "RAW_HISTORY_SENTINEL_C"},
        ]

    async def noop_refresh(*args, **kwargs):
        return None

    async def fake_sync_graph(item: dict[str, Any]) -> None:
        sync_calls.append(int(item["id"]))

    async def fake_job_counts(**kwargs):
        count_calls.append(kwargs)
        return {"pending": 1, "running": 0, "succeeded": 0, "failed": 0, "dead": 0}

    async def fake_claim_jobs(**kwargs):
        claim_calls.append(kwargs)
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_collect_session_history", fake_collect_session_history)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", noop_refresh)
    monkeypatch.setattr(store, "_sync_memory_graph_for_item_safe", fake_sync_graph)
    monkeypatch.setattr(store, "_sync_memory_vector_for_item_safe", noop_refresh)
    monkeypatch.setattr(store, "get_llm_extraction_job_status_counts_for_day", fake_job_counts)
    monkeypatch.setattr(store, "claim_llm_extraction_jobs_for_day", fake_claim_jobs)

    first = await store.run_daily_group_relationship_extraction(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="room-a@chatroom",
        user_id="",
        date="2026-05-15",
    )
    second = await store.run_daily_group_relationship_extraction(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="room-a@chatroom",
        user_id="",
        date="2026-05-15",
    )

    assert first["scope"]["user_id_scope"] == "__group__"
    assert first["status"] == "rule_only"
    assert first["skipped_reason"] == "no_llm"
    assert first["counts"]["raw_messages"] == 3
    assert first["counts"]["imported_messages"] == 2
    assert first["counts"]["senders"] == 2
    assert first["job_counts_before"]["pending"] == 1
    assert first["job_counts_after"]["pending"] == 1
    assert first["jobs"] == {"claimed": 0, "succeeded": 0, "failed": 0, "dead": 0, "batches": 0}
    assert first["controls"] == {
        "batch_limit": 50,
        "max_jobs": 50,
        "continuous": False,
        "time_budget_seconds": 60,
        "stop_reason": "llm_unavailable",
    }
    assert first["more_remain"] is True
    assert first["source_event_ids"] == [501, 502]
    assert first["sender_ids"] == ["wxid_a", "wxid_b"]
    assert first["created_count"] == 1
    assert second["run_key"] == first["run_key"]
    assert second["created_count"] == 0
    assert second["updated_count"] == 1
    assert second["memory_item_ids"] == first["memory_item_ids"]
    assert len(items_by_key) == 1
    assert len(sync_calls) == 2
    assert len(count_calls) == 4
    assert len(claim_calls) == 0
    serialized = str(first) + str(second)
    assert "RAW_FIELD_SENTINEL" not in serialized
    assert "RAW_HISTORY_SENTINEL" not in serialized


@pytest.mark.asyncio
async def test_daily_group_relationship_extraction_processes_single_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    store.graph_extractor.config = replace(store.graph_extractor.config, enabled=True)
    store.graph_extractor.llm_service = object()
    jobs = [{"id": 1}, {"id": 2}, {"id": 3}]
    claim_calls: list[dict[str, Any]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        params = params or {}
        if "FROM plugin_memory_event" in sql and "user_text" in sql:
            return [
                {
                    "id": 501,
                    "user_text": "wxid_a:\nhello",
                    "trace_id": "t1",
                    "event_key": "e1",
                    "created_at": None,
                },
            ]
        if "SELECT id FROM plugin_memory_item" in sql:
            return []
        if "INSERT INTO plugin_memory_item" in sql:
            return [{"id": 900, **params}]
        return []

    async def fake_collect_session_history(**kwargs):
        return []

    async def fake_counts(**kwargs):
        return {"pending": 3, "running": 0, "succeeded": 0, "failed": 0, "dead": 0}

    async def fake_claim_jobs(**kwargs):
        claim_calls.append(kwargs)
        return jobs

    async def fake_process(job: dict[str, Any]) -> str:
        return "succeeded"

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_collect_session_history", fake_collect_session_history)
    monkeypatch.setattr(store, "get_llm_extraction_job_status_counts_for_day", fake_counts)
    monkeypatch.setattr(store, "claim_llm_extraction_jobs_for_day", fake_claim_jobs)
    monkeypatch.setattr(store, "process_llm_extraction_job", fake_process)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", noop)
    monkeypatch.setattr(store, "_sync_memory_graph_for_item_safe", noop)
    monkeypatch.setattr(store, "_sync_memory_vector_for_item_safe", noop)

    result = await store.run_daily_group_relationship_extraction(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="room-a@chatroom",
        date="2026-05-15",
        batch_limit=50,
    )

    assert result["jobs"] == {"claimed": 3, "succeeded": 3, "failed": 0, "dead": 0, "batches": 1}
    assert result["controls"]["stop_reason"] == "single_batch_complete"
    assert claim_calls[0]["limit"] == 50


@pytest.mark.asyncio
async def test_daily_group_relationship_extraction_continuous_stops_at_max_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    store.graph_extractor.config = replace(store.graph_extractor.config, enabled=True)
    store.graph_extractor.llm_service = object()
    claimed_batches = [[{"id": 1}, {"id": 2}], [{"id": 3}]]
    claim_calls: list[dict[str, Any]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        if "FROM plugin_memory_event" in sql and "user_text" in sql:
            return [
                {
                    "id": 501,
                    "user_text": "wxid_a:\nhello",
                    "trace_id": "t1",
                    "event_key": "e1",
                    "created_at": None,
                }
            ]
        return []

    async def fake_counts(**kwargs):
        return {"pending": 3, "running": 0, "succeeded": 0, "failed": 0, "dead": 0}

    async def fake_claim_jobs(**kwargs):
        claim_calls.append(kwargs)
        return claimed_batches.pop(0)

    async def fake_process(job: dict[str, Any]) -> str:
        return "succeeded"

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_collect_session_history", lambda **kwargs: [])
    monkeypatch.setattr(store, "get_llm_extraction_job_status_counts_for_day", fake_counts)
    monkeypatch.setattr(store, "claim_llm_extraction_jobs_for_day", fake_claim_jobs)
    monkeypatch.setattr(store, "process_llm_extraction_job", fake_process)

    result = await store.run_daily_group_relationship_extraction(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="room-a@chatroom",
        date="2026-05-15",
        batch_limit=2,
        max_jobs=3,
        continuous=True,
    )

    assert result["jobs"]["claimed"] == 3
    assert result["jobs"]["batches"] == 2
    assert result["controls"]["stop_reason"] == "max_jobs_reached"
    assert [call["limit"] for call in claim_calls] == [2, 1]


@pytest.mark.asyncio
async def test_daily_group_relationship_extraction_no_ready_jobs_stop_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    store.graph_extractor.config = replace(store.graph_extractor.config, enabled=True)
    store.graph_extractor.llm_service = object()

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        if "FROM plugin_memory_event" in sql and "user_text" in sql:
            return [
                {
                    "id": 501,
                    "user_text": "wxid_a:\nhello",
                    "trace_id": "t1",
                    "event_key": "e1",
                    "created_at": None,
                }
            ]
        return []

    async def fake_counts(**kwargs):
        return {"pending": 0, "running": 0, "succeeded": 1, "failed": 0, "dead": 0}

    async def fake_claim_jobs(**kwargs):
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_collect_session_history", lambda **kwargs: [])
    monkeypatch.setattr(store, "get_llm_extraction_job_status_counts_for_day", fake_counts)
    monkeypatch.setattr(store, "claim_llm_extraction_jobs_for_day", fake_claim_jobs)

    result = await store.run_daily_group_relationship_extraction(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="room-a@chatroom",
        date="2026-05-15",
    )

    assert result["jobs"]["claimed"] == 0
    assert result["jobs"]["batches"] == 0
    assert result["controls"]["stop_reason"] == "no_ready_jobs"


@pytest.mark.asyncio
async def test_group_relationship_window_extraction_dry_run_builds_safe_windows_without_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        calls.append((sql, params))
        if "FROM plugin_memory_event" in sql and "id > :cursor_event_id" in sql:
            return [
                {
                    "id": 501,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "__group__",
                    "session_id": "room-a@chatroom",
                    "user_text": "wxid_a: RAW_FIELD_SENTINEL_A",
                    "assistant_text": "",
                    "trace_id": "trace-501",
                    "event_key": "event-501",
                    "created_at": "2026-05-15T08:00:00",
                },
                {
                    "id": 502,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "__group__",
                    "session_id": "room-a@chatroom",
                    "user_text": "wxid_b: RAW_FIELD_SENTINEL_B",
                    "assistant_text": "",
                    "trace_id": "trace-502",
                    "event_key": "event-502",
                    "created_at": "2026-05-15T08:01:00",
                },
            ]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    result = await store.run_group_relationship_window_extraction(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="room-a@chatroom",
        date="2026-05-15",
        window_size=10,
        max_windows=1,
        dry_run=True,
    )

    assert result["status"] == "dry_run"
    assert result["windows"] == [
        {
            "index": 1,
            "event_count": 2,
            "first_event_id": 501,
            "last_event_id": 502,
            "sender_count": 2,
            "candidate_count": 0,
            "applied_count": 0,
            "skipped_count": 0,
        }
    ]
    assert result["totals"] == {
        "events": 2,
        "windows": 1,
        "candidates": 0,
        "applied": 0,
        "skipped": 0,
    }
    assert result["next_cursor_event_id"] == 502
    assert all("INSERT INTO plugin_memory_item" not in sql for sql, _ in calls)
    serialized = str(result)
    assert "RAW_FIELD_SENTINEL" not in serialized
    assert "user_text" not in serialized


@pytest.mark.asyncio
async def test_group_relationship_window_extraction_fake_llm_applies_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(
        SimpleNamespace(memory_graph_llm_extraction_enabled=True), llm_service=object()
    )
    store.graph_extractor.config = replace(store.graph_extractor.config, enabled=True)
    store.graph_extractor.llm_service = object()
    items_by_key: dict[str, dict[str, Any]] = {}
    next_item_id = 900
    refresh_calls: list[int] = []
    graph_sync_calls: list[int] = []
    vector_sync_calls: list[int] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        nonlocal next_item_id
        params = params or {}
        if "SELECT id, source_member_id, source_message_id" in sql:
            return [
                {
                    "id": int(event_id),
                    "source_member_id": "",
                    "source_message_id": f"event-{event_id}",
                }
                for event_id in params.get("event_ids", [])
            ]
        if "FROM plugin_memory_event" in sql and "id > :cursor_event_id" in sql:
            return [
                {
                    "id": 501,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "__group__",
                    "session_id": "room-a@chatroom",
                    "user_text": "wxid_a: asks private raw question",
                    "assistant_text": "",
                    "trace_id": "trace-501",
                    "event_key": "event-501",
                    "created_at": "2026-05-15T08:00:00",
                },
                {
                    "id": 502,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "__group__",
                    "session_id": "room-a@chatroom",
                    "user_text": "wxid_b: answers private raw answer",
                    "assistant_text": "",
                    "trace_id": "trace-502",
                    "event_key": "event-502",
                    "created_at": "2026-05-15T08:01:00",
                },
            ]
        if "deleted_at IS NOT NULL" in sql:
            return []
        if (
            "FROM plugin_memory_item" in sql
            and "normalized_key = :normalized_key" in sql
            and "ORDER BY pinned DESC" in sql
        ):
            item = items_by_key.get(str(params["normalized_key"]))
            return [dict(item)] if item else []
        if "SELECT id FROM plugin_memory_item" in sql:
            item = items_by_key.get(str(params["normalized_key"]))
            return [{"id": item["id"]}] if item else []
        if sql.startswith("INSERT INTO plugin_memory_item"):
            item = {
                "id": next_item_id,
                "tenant_id": params["tid"],
                "channel": params["channel"],
                "source_key": params["source_key"],
                "user_id": params["uid"],
                "session_id": params["sid"],
                "scope_type": params["scope_type"],
                "source_type": params["source_type"],
                "memory_type": params["memory_type"],
                "content": params["content"],
                "value_json": params["value_json"],
                "normalized_key": params["normalized_key"],
                "confidence": params["confidence"],
                "status": params["status"],
                "pinned": params["pinned"],
                "priority": params["priority"],
                "sensitivity": params["sensitivity"],
                "origin_session_kind": params["origin_session_kind"],
                "audience_scope": params["audience_scope"],
                "allowed_session_ids": json.loads(params["allowed_session_ids"]),
                "sensitivity_category": params["sensitivity_category"],
                "source_kind": params["source_kind"],
                "expires_at": params["expires_at"],
                "source_event_id": params["source_event_id"],
                "source_trace_id": params["source_trace_id"],
                "original_text": params["original_text"],
                "occurrence_count": 1,
                "first_seen_at": None,
                "last_seen_at": None,
                "created_at": None,
                "updated_at": None,
                "deleted_at": None,
            }
            next_item_id += 1
            items_by_key[str(params["normalized_key"])] = item
            return [dict(item)]
        if sql.startswith("UPDATE plugin_memory_item SET"):
            item = next(item for item in items_by_key.values() if item["id"] == params["id"])
            item["occurrence_count"] = int(item["occurrence_count"]) + 1
            item["value_json"] = params["value_json"]
            item["confidence"] = max(float(item["confidence"]), float(params["confidence"]))
            return []
        if "FROM plugin_memory_item WHERE id = :id" in sql:
            item = next(
                (item for item in items_by_key.values() if item["id"] == params["id"]), None
            )
            return [dict(item)] if item else []
        return []

    async def fake_extract(**kwargs: Any) -> dict[str, Any]:
        assert set(kwargs["event_ids"]) == {501, 502}
        assert "private raw question" in kwargs["transcript"]
        return {
            "relations": [
                {
                    "subject": "wxid_a",
                    "subject_type": "person",
                    "predicate": "asked",
                    "object": "wxid_b",
                    "object_type": "person",
                    "confidence": 1.7,
                    "evidence_event_ids": [501, 502, 999],
                    "reason": "safe reason",
                },
                {
                    "subject": "wxid_a",
                    "predicate": "unsupported",
                    "object": "wxid_b",
                    "evidence_event_ids": [501],
                },
            ]
        }

    async def fake_refresh(item: dict[str, Any]) -> None:
        refresh_calls.append(int(item["id"]))

    async def fake_graph_sync(item: dict[str, Any]) -> None:
        graph_sync_calls.append(int(item["id"]))

    async def fake_vector_sync(item: dict[str, Any]) -> None:
        vector_sync_calls.append(int(item["id"]))

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_extract_group_relationship_window_candidates", fake_extract)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", fake_refresh)
    monkeypatch.setattr(store, "_sync_memory_graph_for_item_safe", fake_graph_sync)
    monkeypatch.setattr(store, "_sync_memory_vector_for_item_safe", fake_vector_sync)

    first = await store.run_group_relationship_window_extraction(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="room-a@chatroom",
        date="2026-05-15",
        window_size=10,
        max_windows=1,
    )
    second = await store.run_group_relationship_window_extraction(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="room-a@chatroom",
        date="2026-05-15",
        window_size=10,
        max_windows=1,
    )

    assert first["status"] == "partial"
    assert first["totals"] == {
        "events": 2,
        "windows": 1,
        "candidates": 1,
        "applied": 1,
        "skipped": 1,
    }
    assert second["totals"]["applied"] == 1
    assert len(items_by_key) == 1
    asked_item = next(
        item
        for item in items_by_key.values()
        if json.loads(item["value_json"])["relation"]["predicate"] == "asked"
    )
    assert asked_item["source_type"] == "llm_group_window"
    assert asked_item["memory_type"] == "note"
    assert asked_item["status"] == "pending"
    assert asked_item["original_text"] == ""
    assert asked_item["occurrence_count"] == 2
    asked_value = json.loads(asked_item["value_json"])
    assert asked_value["relation"]["confidence"] == 1.0
    assert asked_value["relation"]["evidence_event_ids"] == [501, 502]
    assert asked_value["acceptance"]["status"] == "needs_review"
    assert refresh_calls == [900, 900]
    assert graph_sync_calls == [900, 900]
    assert vector_sync_calls == [900, 900]
    serialized = str(first)
    assert "private raw" not in serialized
    assert "user_text" not in serialized


@pytest.mark.asyncio
async def test_group_relationship_window_extraction_merges_same_relation_across_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(
        SimpleNamespace(memory_graph_llm_extraction_enabled=True), llm_service=object()
    )
    store.graph_extractor.config = replace(store.graph_extractor.config, enabled=True)
    store.graph_extractor.llm_service = object()
    items_by_key: dict[str, dict[str, Any]] = {}
    next_item_id = 910

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        nonlocal next_item_id
        params = params or {}
        if "SELECT id, source_member_id, source_message_id" in sql:
            return [
                {
                    "id": int(event_id),
                    "source_member_id": "",
                    "source_message_id": f"event-{event_id}",
                }
                for event_id in params.get("event_ids", [])
            ]
        if "FROM plugin_memory_event" in sql and "id > :cursor_event_id" in sql:
            cursor = int(params["cursor_event_id"])
            if cursor < 501:
                return [
                    {
                        "id": 501,
                        "tenant_id": "demo",
                        "channel": "wechat",
                        "source_key": "wxbot",
                        "user_id": "__group__",
                        "session_id": "room-a@chatroom",
                        "user_text": "wxid_a: raw one",
                        "assistant_text": "",
                        "trace_id": "trace-501",
                        "event_key": "event-501",
                        "created_at": "2026-05-15T08:00:00",
                    }
                ]
            return [
                {
                    "id": 502,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "__group__",
                    "session_id": "room-a@chatroom",
                    "user_text": "wxid_a: raw two",
                    "assistant_text": "",
                    "trace_id": "trace-502",
                    "event_key": "event-502",
                    "created_at": "2026-05-15T08:01:00",
                }
            ]
        if "deleted_at IS NOT NULL" in sql:
            return []
        if "normalized_key = :normalized_key" in sql and "FROM plugin_memory_item" in sql:
            item = items_by_key.get(str(params["normalized_key"]))
            return [dict(item)] if item else []
        if sql.startswith("INSERT INTO plugin_memory_item"):
            item = {
                "id": next_item_id,
                "tenant_id": params["tid"],
                "channel": params["channel"],
                "source_key": params["source_key"],
                "user_id": params["uid"],
                "session_id": params["sid"],
                "scope_type": params["scope_type"],
                "source_type": params["source_type"],
                "memory_type": params["memory_type"],
                "content": params["content"],
                "value_json": params["value_json"],
                "normalized_key": params["normalized_key"],
                "confidence": params["confidence"],
                "status": params["status"],
                "pinned": params["pinned"],
                "priority": params["priority"],
                "sensitivity": params["sensitivity"],
                "origin_session_kind": params["origin_session_kind"],
                "audience_scope": params["audience_scope"],
                "allowed_session_ids": json.loads(params["allowed_session_ids"]),
                "sensitivity_category": params["sensitivity_category"],
                "source_kind": params["source_kind"],
                "expires_at": params["expires_at"],
                "source_event_id": params["source_event_id"],
                "source_trace_id": params["source_trace_id"],
                "original_text": params["original_text"],
                "occurrence_count": 1,
                "first_seen_at": None,
                "last_seen_at": None,
                "created_at": None,
                "updated_at": None,
                "deleted_at": None,
            }
            next_item_id += 1
            items_by_key[str(params["normalized_key"])] = item
            return [dict(item)]
        if sql.startswith("UPDATE plugin_memory_item SET"):
            item = next(item for item in items_by_key.values() if item["id"] == params["id"])
            item["occurrence_count"] = int(item["occurrence_count"]) + 1
            item["value_json"] = params["value_json"]
            item["confidence"] = max(float(item["confidence"]), float(params["confidence"]))
            return []
        if "FROM plugin_memory_item WHERE id = :id" in sql:
            item = next(
                (item for item in items_by_key.values() if item["id"] == params["id"]), None
            )
            return [dict(item)] if item else []
        return []

    async def fake_extract(**kwargs: Any) -> dict[str, Any]:
        return {
            "relations": [
                {
                    "subject": "wxid_a",
                    "subject_type": "person",
                    "predicate": "asked",
                    "object": "wxid_b",
                    "object_type": "person",
                    "confidence": 0.8,
                    "evidence_event_ids": kwargs["event_ids"],
                }
            ]
        }

    async def fake_noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_extract_group_relationship_window_candidates", fake_extract)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", fake_noop)
    monkeypatch.setattr(store, "_sync_memory_graph_for_item_safe", fake_noop)
    monkeypatch.setattr(store, "_sync_memory_vector_for_item_safe", fake_noop)

    first = await store.run_group_relationship_window_extraction(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="room-a@chatroom",
        date="2026-05-15",
        window_size=10,
        cursor_event_id=0,
    )
    second = await store.run_group_relationship_window_extraction(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="room-a@chatroom",
        date="2026-05-15",
        window_size=10,
        cursor_event_id=501,
    )

    assert first["totals"]["applied"] == 1
    assert second["totals"]["applied"] == 1
    assert len(items_by_key) == 1
    item = next(iter(items_by_key.values()))
    assert item["occurrence_count"] == 2
    assert item["original_text"] == ""
    assert item["normalized_key"].startswith("group-window-rel:asked:")
    value = json.loads(item["value_json"])
    assert value["relation"]["evidence_event_ids"] == [501, 502]
    assert value["source_event_ids"] == [501, 502]


@pytest.mark.asyncio
async def test_group_relationship_window_catchup_stops_at_max_and_no_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    calls: list[int] = []

    async def fake_run(**kwargs: Any) -> dict[str, Any]:
        calls.append(int(kwargs["cursor_event_id"]))
        if len(calls) == 1:
            return {
                "status": "completed",
                "totals": {"events": 10, "windows": 1, "candidates": 1, "applied": 1, "skipped": 0},
                "next_cursor_event_id": 100,
                "more_remain": True,
            }
        return {
            "status": "completed",
            "totals": {"events": 0, "windows": 0, "candidates": 0, "applied": 0, "skipped": 0},
            "next_cursor_event_id": 100,
            "more_remain": False,
        }

    monkeypatch.setattr(store, "run_group_relationship_window_extraction", fake_run)
    maxed = await store.run_group_relationship_window_catchup(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="room-a@chatroom",
        date="2026-05-15",
        max_windows_per_run=1,
    )
    assert maxed["stop_reason"] == "max_windows_reached"
    assert maxed["windows_processed"] == 1

    calls.clear()
    done = await store.run_group_relationship_window_catchup(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="room-a@chatroom",
        date="2026-05-15",
        max_windows_per_run=3,
    )
    assert done["stop_reason"] == "no_more_events"
    assert done["windows_processed"] == 1
    assert calls == [0, 100]


@pytest.mark.asyncio
async def test_group_relationship_window_catchup_stops_at_time_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    ticks = iter([0.0, 2.0])
    monkeypatch.setattr(memory_store_module, "monotonic", lambda: next(ticks))

    async def fake_run(**kwargs: Any) -> dict[str, Any]:
        return {
            "status": "completed",
            "totals": {"events": 10, "windows": 1, "candidates": 1, "applied": 1, "skipped": 0},
            "next_cursor_event_id": 100,
            "more_remain": True,
        }

    monkeypatch.setattr(store, "run_group_relationship_window_extraction", fake_run)
    result = await store.run_group_relationship_window_catchup(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="room-a@chatroom",
        date="2026-05-15",
        max_windows_per_run=3,
        time_budget_seconds=1,
    )
    assert result["stop_reason"] == "time_budget_reached"
    assert result["windows_processed"] == 0
    assert result["more_remain"] is True


@pytest.mark.asyncio
async def test_group_relationship_window_stats_safe_aggregation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())

    async def fake_list(**kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "id": 1,
                "status": "pending",
                "acceptance_status": "needs_review",
                "value_json": json.dumps(
                    {
                        "kind": "group_window_relation",
                        "date": "2026-05-15",
                        "window": {"first_event_id": 501, "last_event_id": 502},
                        "relation": {"predicate": "asked", "evidence_event_ids": [501, 502]},
                        "acceptance": {"status": "needs_review"},
                    }
                ),
                "content": "RAW_FIELD_SENTINEL",
            },
            {
                "id": 2,
                "status": "active",
                "acceptance_status": "accepted",
                "value_json": json.dumps(
                    {
                        "kind": "group_window_relation",
                        "date": "2026-05-15",
                        "window": {"first_event_id": 503, "last_event_id": 503},
                        "relation": {"predicate": "answered", "evidence_event_ids": [503]},
                        "acceptance": {"status": "accepted"},
                    }
                ),
            },
            {
                "id": 3,
                "status": "pending",
                "value_json": json.dumps({"date": "2026-05-16"}),
            },
        ]

    monkeypatch.setattr(store, "_list_memory_acceptance_audit_items", fake_list)
    result = await store.get_group_relationship_window_stats(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="room-a@chatroom",
        date="2026-05-15",
    )
    assert result["totals"]["items"] == 2
    assert result["totals"]["events"] == 3
    assert result["totals"]["windows"] == 2
    assert result["acceptance_counts"] == {"needs_review": 1, "accepted": 1}
    assert result["predicate_counts"] == {"asked": 1, "answered": 1}
    assert "RAW_FIELD_SENTINEL" not in str(result)


@pytest.mark.asyncio
async def test_group_relationship_window_extraction_no_llm_skips_with_safe_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        if "FROM plugin_memory_event" in sql and "id > :cursor_event_id" in sql:
            return [
                {
                    "id": 501,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "__group__",
                    "session_id": "room-a@chatroom",
                    "user_text": "wxid_a: RAW_FIELD_SENTINEL_A",
                    "assistant_text": "",
                    "trace_id": "trace-501",
                    "event_key": "event-501",
                    "created_at": "2026-05-15T08:00:00",
                }
            ]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    result = await store.run_group_relationship_window_extraction(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="room-a@chatroom",
        date="2026-05-15",
    )

    assert result["status"] == "skipped"
    assert result["skipped_reason"] == "no_deterministic_candidates"
    assert result["windows"][0]["event_count"] == 1
    assert result["totals"]["skipped"] == 1
    assert result["generated_from"] == ["plugin_memory_event", "deterministic_window_participants"]
    serialized = str(result)
    assert "RAW_FIELD_SENTINEL" not in serialized
    assert "user_text" not in serialized


@pytest.mark.asyncio
async def test_memory_graph_mapper_group_window_relation_creates_person_person_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _GraphSqlFake()
    monkeypatch.setattr(memory_store_module, "_exec", graph.exec)
    item = _item(
        id=901,
        user_id="__group__",
        session_id="room-a@chatroom",
        source_type="llm_group_window",
        memory_type="note",
        content="Group window relation: wxid_a co_participated wxid_b",
        normalized_key="group-window-rel:co_participated:test",
        status="pending",
        source_event_id=501,
    )
    item["value_json"] = json.dumps(
        {
            "kind": "group_window_relation",
            "relation": {
                "subject": "wxid_a",
                "subject_type": "person",
                "predicate": "co_participated",
                "object": "wxid_b",
                "object_type": "person",
                "confidence": 0.55,
                "evidence_event_ids": [501, 502],
                "reason": "deterministic_shared_window",
            },
            "source_event_ids": [501, 502],
            "acceptance": {"status": "needs_review"},
        }
    )
    item["value"] = json.loads(item["value_json"])

    await MemoryStore(SimpleNamespace())._sync_memory_graph_for_item(item)

    assert len(graph.entities) == 2
    entity_names = {entity["name"] for entity in graph.entities.values()}
    assert entity_names == {"wxid_a", "wxid_b"}
    assert len(graph.facts) == 1
    fact = graph.facts[901]
    assert fact["predicate"] == "co_participated"
    assert fact["object_value"] == ""
    assert fact["source_event_id"] == 501
    assert fact["status"] == "pending"


@pytest.mark.asyncio
async def test_group_relationship_window_extraction_no_llm_builds_person_person_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    items_by_key: dict[str, dict[str, Any]] = {}
    graph_sync_items: list[dict[str, Any]] = []
    next_item_id = 950

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        nonlocal next_item_id
        params = params or {}
        if "SELECT id, source_member_id, source_message_id" in sql:
            return [
                {
                    "id": int(event_id),
                    "source_member_id": "",
                    "source_message_id": f"event-{event_id}",
                }
                for event_id in params.get("event_ids", [])
            ]
        if "FROM plugin_memory_event" in sql and "id > :cursor_event_id" in sql:
            return [
                {
                    "id": 501,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "__group__",
                    "session_id": "room-a@chatroom",
                    "user_text": "wxid_a: RAW_FIELD_SENTINEL_A",
                    "assistant_text": "",
                    "trace_id": "trace-501",
                    "event_key": "event-501",
                    "created_at": "2026-05-15T08:00:00",
                },
                {
                    "id": 502,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "__group__",
                    "session_id": "room-a@chatroom",
                    "user_text": "wxid_b: 回复: RAW_FIELD_SENTINEL_B",
                    "assistant_text": "",
                    "trace_id": "trace-502",
                    "event_key": "event-502",
                    "created_at": "2026-05-15T08:01:00",
                },
            ]
        if "normalized_key = :normalized_key" in sql and "FROM plugin_memory_item" in sql:
            item = items_by_key.get(str(params["normalized_key"]))
            return [dict(item)] if item else []
        if sql.startswith("INSERT INTO plugin_memory_item"):
            item = {
                "id": next_item_id,
                "tenant_id": params["tid"],
                "channel": params["channel"],
                "source_key": params["source_key"],
                "user_id": params["uid"],
                "session_id": params["sid"],
                "scope_type": params["scope_type"],
                "source_type": params["source_type"],
                "memory_type": params["memory_type"],
                "content": params["content"],
                "value_json": params["value_json"],
                "normalized_key": params["normalized_key"],
                "confidence": params["confidence"],
                "status": params["status"],
                "pinned": params["pinned"],
                "priority": params["priority"],
                "sensitivity": params["sensitivity"],
                "source_event_id": params["source_event_id"],
                "source_trace_id": params["source_trace_id"],
                "original_text": params["original_text"],
                "occurrence_count": 1,
                "first_seen_at": None,
                "last_seen_at": None,
                "created_at": None,
                "updated_at": None,
                "deleted_at": None,
            }
            next_item_id += 1
            items_by_key[str(params["normalized_key"])] = item
            return [dict(item)]
        return []

    async def fake_noop(*args: Any, **kwargs: Any) -> None:
        return None

    async def fake_graph_sync(item: dict[str, Any]) -> None:
        graph_sync_items.append(item)

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", fake_noop)
    monkeypatch.setattr(store, "_sync_memory_vector_for_item_safe", fake_noop)
    monkeypatch.setattr(store, "_sync_memory_graph_for_item_safe", fake_graph_sync)

    result = await store.run_group_relationship_window_extraction(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="room-a@chatroom",
        date="2026-05-15",
        window_size=10,
        max_windows=1,
    )

    assert result["status"] == "completed"
    assert result["totals"] == {
        "events": 2,
        "windows": 1,
        "candidates": 1,
        "applied": 1,
        "skipped": 0,
    }
    assert result["generated_from"] == ["plugin_memory_event", "deterministic_window_participants"]
    assert len(items_by_key) == 1
    replied_to_item = next(
        item
        for item in items_by_key.values()
        if json.loads(item["value_json"])["relation"]["predicate"] == "replied_to"
    )
    assert replied_to_item["source_type"] == "deterministic_group_window"
    assert replied_to_item["original_text"] == ""
    assert replied_to_item["content"] == "Group window relation: wxid_b replied_to wxid_a"
    value = json.loads(replied_to_item["value_json"])
    assert value["relation"] == {
        "subject": "wxid_b",
        "subject_type": "person",
        "predicate": "replied_to",
        "object": "wxid_a",
        "object_type": "person",
        "confidence": 0.62,
        "evidence_event_ids": [501, 502],
        "reason": "deterministic_adjacent_reply_window",
        "extraction_method": "deterministic_group_window",
    }
    assert value["source_event_ids"] == [501, 502]
    assert value["acceptance"]["status"] == "needs_review"
    assert replied_to_item["status"] == "pending"
    assert [item["id"] for item in graph_sync_items] == [replied_to_item["id"]]
    serialized = str(result) + str(value)
    assert "RAW_FIELD_SENTINEL" not in serialized
    assert "user_text" not in serialized


@pytest.mark.asyncio
async def test_group_relationship_window_extraction_no_llm_creates_person_person_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    items_by_key: dict[str, dict[str, Any]] = {}
    graph_items: list[dict[str, Any]] = []
    next_item_id = 1200

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        nonlocal next_item_id
        params = params or {}
        if "SELECT id, source_member_id, source_message_id" in sql:
            return [
                {
                    "id": int(event_id),
                    "source_member_id": "",
                    "source_message_id": f"event-{event_id}",
                }
                for event_id in params.get("event_ids", [])
            ]
        if "FROM plugin_memory_event" in sql and "id > :cursor_event_id" in sql:
            return [
                {
                    "id": 701,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "__group__",
                    "session_id": "room-a@chatroom",
                    "user_text": "wxid_alice: RAW_FIELD_SENTINEL_A",
                    "assistant_text": "",
                    "trace_id": "trace-701",
                    "event_key": "event-701",
                    "created_at": "2026-05-15T08:00:00",
                },
                {
                    "id": 702,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "__group__",
                    "session_id": "room-a@chatroom",
                    "user_text": "wxid_bob: @wxid_alice RAW_FIELD_SENTINEL_B",
                    "assistant_text": "",
                    "trace_id": "trace-702",
                    "event_key": "event-702",
                    "created_at": "2026-05-15T08:01:00",
                },
            ]
        if "normalized_key = :normalized_key" in sql and "FROM plugin_memory_item" in sql:
            item = items_by_key.get(str(params["normalized_key"]))
            return [dict(item)] if item else []
        if "SELECT id FROM plugin_memory_item" in sql:
            item = items_by_key.get(str(params["normalized_key"]))
            return [{"id": item["id"]}] if item else []
        if sql.startswith("INSERT INTO plugin_memory_item"):
            item = {
                "id": next_item_id,
                "tenant_id": params["tid"],
                "channel": params["channel"],
                "source_key": params["source_key"],
                "user_id": params["uid"],
                "session_id": params["sid"],
                "scope_type": params["scope_type"],
                "source_type": params["source_type"],
                "memory_type": params["memory_type"],
                "content": params["content"],
                "value_json": params["value_json"],
                "value": json.loads(params["value_json"]),
                "normalized_key": params["normalized_key"],
                "confidence": params["confidence"],
                "status": params["status"],
                "pinned": params["pinned"],
                "priority": params["priority"],
                "sensitivity": params["sensitivity"],
                "source_event_id": params["source_event_id"],
                "source_trace_id": params["source_trace_id"],
                "original_text": params["original_text"],
                "occurrence_count": 1,
                "first_seen_at": None,
                "last_seen_at": None,
                "created_at": None,
                "updated_at": None,
                "deleted_at": None,
            }
            items_by_key[str(params["normalized_key"])] = item
            next_item_id += 1
            return [dict(item)]
        if "FROM plugin_memory_item WHERE id = :id" in sql:
            item = next(
                (item for item in items_by_key.values() if item["id"] == params["id"]), None
            )
            return [dict(item)] if item else []
        return []

    async def fake_noop(*args: Any, **kwargs: Any) -> None:
        return None

    async def fake_graph_sync(item: dict[str, Any]) -> None:
        graph_items.append(item)

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", fake_noop)
    monkeypatch.setattr(store, "_sync_memory_graph_for_item_safe", fake_graph_sync)
    monkeypatch.setattr(store, "_sync_memory_vector_for_item_safe", fake_noop)

    result = await store.run_group_relationship_window_extraction(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="room-a@chatroom",
        date="2026-05-15",
        window_size=10,
        max_windows=1,
    )

    assert result["status"] == "completed"
    assert result["generated_from"] == ["plugin_memory_event", "deterministic_window_participants"]
    assert result["totals"]["applied"] >= 1
    assert graph_items
    relations = [json.loads(item["value_json"])["relation"] for item in graph_items]
    assert any(
        relation["subject"] == "wxid_bob"
        and relation["predicate"] in {"addressed", "replied_to"}
        and relation["object"] == "wxid_alice"
        for relation in relations
    )
    assert all(item["source_type"] == "deterministic_group_window" for item in graph_items)
    assert all(item["status"] == "pending" for item in graph_items)
    assert all(item["value"]["acceptance"]["status"] == "needs_review" for item in graph_items)
    assert all(item["original_text"] == "" for item in graph_items)
    serialized = str(result) + str(graph_items)
    assert "RAW_FIELD_SENTINEL" not in serialized
    assert "user_text" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected_acceptance"),
    [("accept", "accepted"), ("reject", "rejected")],
)
async def test_group_relationship_edge_review_maps_to_backing_memory_item(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    expected_acceptance: str,
) -> None:
    store = MemoryStore(SimpleNamespace())
    row = {
        "id": 100,
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
        "session_id": "group-1@chatroom",
        "scope_type": "session",
        "source_type": "auto",
        "memory_type": "note",
        "content": "RAW_FIELD_SENTINEL_ITEM",
        "value_json": '{"acceptance":{"status":"needs_review"}}',
        "normalized_key": "relation:knows",
        "confidence": 0.82,
        "status": "pending",
        "pinned": False,
        "priority": 0,
        "sensitivity": "normal",
        "source_event_id": 500,
        "source_trace_id": "",
        "original_text": "RAW_FIELD_SENTINEL_ORIGINAL",
        "occurrence_count": 1,
        "first_seen_at": None,
        "last_seen_at": None,
        "created_at": None,
        "updated_at": None,
        "deleted_at": None,
    }
    audit_rows: list[dict[str, Any]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        nonlocal row
        if "FROM plugin_memory_fact fact" in sql:
            return [
                {
                    "id": 10,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "subject_entity_id": 1,
                    "subject_name": "Alice",
                    "predicate": "knows",
                    "object_entity_id": 2,
                    "object_name": "Bob",
                    "object_value": "RAW_FIELD_SENTINEL_OBJECT",
                    "memory_item_id": 100,
                    "source_event_id": 500,
                    "confidence": 0.82,
                    "status": "active",
                    "valid_at": "2026-05-05T00:00:00",
                    "invalid_at": None,
                    "created_at": "2026-05-05T00:00:00",
                    "updated_at": "2026-05-12T00:00:00",
                }
            ]
        if "FROM plugin_memory_item WHERE id = ANY" in sql:
            return [row]
        if "FROM plugin_memory_episode" in sql:
            return []
        if "FROM plugin_memory_event WHERE id = ANY" in sql:
            return [{"id": 500, "tenant_id": "demo", "created_at": "2026-05-05T00:00:00"}]
        if "FROM plugin_memory_item WHERE id = :id" in sql:
            return [row]
        if sql.startswith("UPDATE plugin_memory_item SET"):
            assert params is not None
            row = {**row, "value_json": params["value_json"], "status": params["status"]}
            return []
        if "INSERT INTO plugin_memory_acceptance_audit" in sql:
            assert params is not None
            audit_rows.append(dict(params))
            return []
        return []

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", noop)
    monkeypatch.setattr(store, "_sync_memory_graph_for_item_safe", noop)
    monkeypatch.setattr(store, "_sync_memory_vector_for_item_safe", noop)

    result = await store.review_group_relationship_edge(
        edge_id="fact:10",
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="group-1@chatroom",
        action=action,
        review_reason="edge review",
        reviewed_by="admin-test",
    )

    assert result is not None
    assert result["ok"] is True
    assert result["edge_id"] == "fact:10"
    assert result["result"]["memory_item_ids"] == [100]
    assert result["result"]["item_statuses"][0]["acceptance_status"] == expected_acceptance
    assert audit_rows[0]["item_id"] == 100
    assert audit_rows[0]["action"] == action
    assert audit_rows[0]["new_status"] == expected_acceptance
    serialized = str(result)
    assert "RAW_FIELD_SENTINEL_ITEM" not in serialized
    assert "RAW_FIELD_SENTINEL_ORIGINAL" not in serialized
    assert "RAW_FIELD_SENTINEL_OBJECT" not in serialized


@pytest.mark.asyncio
async def test_group_relationship_graph_filters_value_nodes_and_hashes_fallback_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    secret_value = "raw object value must not leak"

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "FROM plugin_memory_entity" in sql:
            return [
                {
                    "id": 1,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "entity_type": "person",
                    "name": "Alice",
                    "normalized_name": "alice",
                    "aliases_json": "[]",
                    "confidence": 0.93,
                    "status": "active",
                    "created_at": "2026-05-01T00:00:00",
                    "updated_at": "2026-05-10T00:00:00",
                }
            ]
        if "FROM plugin_memory_fact fact" in sql:
            return [
                {
                    "id": None,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "subject_entity_id": 1,
                    "subject_name": "Alice",
                    "predicate": "note",
                    "object_entity_id": None,
                    "object_name": None,
                    "object_value": secret_value,
                    "memory_item_id": 100,
                    "source_event_id": 500,
                    "confidence": 0.82,
                    "status": "active",
                    "valid_at": "2026-05-05T00:00:00",
                    "invalid_at": None,
                    "created_at": "2026-05-05T00:00:00",
                    "updated_at": "2026-05-12T00:00:00",
                }
            ]
        if "FROM plugin_memory_episode" in sql:
            return []
        if "FROM plugin_memory_item WHERE id = ANY" in sql:
            return [
                {
                    "id": 100,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "session_id": "group-1@chatroom",
                    "scope_type": "session",
                    "source_type": "auto",
                    "memory_type": "note",
                    "value_json": '{"acceptance":{"status":"accepted"}}',
                    "normalized_key": "relation:note",
                    "confidence": 0.82,
                    "status": "active",
                    "pinned": False,
                    "priority": 0,
                    "sensitivity": "normal",
                    "source_event_id": 500,
                    "source_trace_id": "",
                    "occurrence_count": 1,
                    "first_seen_at": "2026-05-05T00:00:00",
                    "last_seen_at": "2026-05-12T00:00:00",
                    "created_at": "2026-05-05T00:00:00",
                    "updated_at": "2026-05-12T00:00:00",
                    "deleted_at": None,
                }
            ]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    filtered_graph = await store.get_group_relationship_graph(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="group-1@chatroom",
        node_type="person",
        limit=10,
    )
    value_graph = await store.get_group_relationship_graph(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="group-1@chatroom",
        limit=10,
    )

    assert filtered_graph["counts"] == {"nodes": 0, "edges": 0}
    assert value_graph["counts"] == {"nodes": 2, "edges": 1}
    assert secret_value not in str(value_graph)
    assert value_graph["nodes"][1]["display_label"] == "note"
    assert value_graph["nodes"][1]["technical_label"].startswith("value:")
    assert value_graph["edges"][0]["id"].startswith("fact:1:note:value:")


@pytest.mark.asyncio
async def test_group_relationship_graph_display_label_prefers_alias_over_technical_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "FROM plugin_memory_entity" in sql:
            return [
                {
                    "id": 1,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "entity_type": "person",
                    "name": "wxid_e9uzabcdefghijklmnopq22",
                    "normalized_name": "wxid_e9uzabcdefghijklmnopq22",
                    "aliases_json": '["产品经理"]',
                    "confidence": 0.93,
                    "status": "active",
                    "created_at": "2026-05-01T00:00:00",
                    "updated_at": "2026-05-10T00:00:00",
                },
                {
                    "id": 2,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_b",
                    "entity_type": "person",
                    "name": "wxid_plaintechnicalabcdefghijklmn",
                    "normalized_name": "wxid_plaintechnicalabcdefghijklmn",
                    "aliases_json": "[]",
                    "confidence": 0.91,
                    "status": "active",
                    "created_at": "2026-05-01T00:00:00",
                    "updated_at": "2026-05-10T00:00:00",
                },
            ]
        if "FROM plugin_memory_fact fact" in sql:
            return [
                {
                    "id": 10,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "subject_entity_id": 1,
                    "subject_name": "wxid_e9uzabcdefghijklmnopq22",
                    "predicate": "asked",
                    "object_entity_id": 2,
                    "object_name": "wxid_plaintechnicalabcdefghijklmn",
                    "object_value": "",
                    "memory_item_id": 100,
                    "source_event_id": 500,
                    "confidence": 0.82,
                    "status": "active",
                    "valid_at": "2026-05-05T00:00:00",
                    "invalid_at": None,
                    "created_at": "2026-05-05T00:00:00",
                    "updated_at": "2026-05-12T00:00:00",
                }
            ]
        if "FROM plugin_memory_episode" in sql:
            return []
        if "FROM plugin_memory_item WHERE id = ANY" in sql:
            return [
                {
                    "id": 100,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "session_id": "group-1@chatroom",
                    "scope_type": "session",
                    "source_type": "auto",
                    "memory_type": "note",
                    "value_json": '{"acceptance":{"status":"accepted"}}',
                    "normalized_key": "relation:asked",
                    "confidence": 0.82,
                    "status": "active",
                    "pinned": False,
                    "priority": 0,
                    "sensitivity": "normal",
                    "source_event_id": 500,
                    "source_trace_id": "",
                    "occurrence_count": 1,
                    "first_seen_at": "2026-05-05T00:00:00",
                    "last_seen_at": "2026-05-12T00:00:00",
                    "created_at": "2026-05-05T00:00:00",
                    "updated_at": "2026-05-12T00:00:00",
                    "deleted_at": None,
                }
            ]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    graph = await store.get_group_relationship_graph(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="group-1@chatroom",
        limit=10,
    )

    assert graph["counts"] == {"nodes": 2, "edges": 1}
    assert graph["nodes"][0]["display_label"] == "产品经理"
    assert graph["nodes"][0]["technical_label"] == "wxid_e9uzabcdefghijklmnopq22"
    assert graph["nodes"][0]["aliases"] == ["产品经理"]
    assert graph["nodes"][1]["display_label"] == "wxid_plaintechnicalabcdefghijklmn"


@pytest.mark.asyncio
async def test_group_relationship_graph_maps_wxbot_contact_display_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "FROM plugin_memory_entity" in sql:
            return [
                {
                    "id": 1,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "entity_type": "person",
                    "name": "wxid_contactabcdefghijklmnopq",
                    "normalized_name": "wxid_contactabcdefghijklmnopq",
                    "aliases_json": "[]",
                    "confidence": 0.93,
                    "status": "active",
                    "created_at": "2026-05-01T00:00:00",
                    "updated_at": "2026-05-10T00:00:00",
                },
                {
                    "id": 2,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_b",
                    "entity_type": "person",
                    "name": "wxid_nickabcdefghijklmnopqrs",
                    "normalized_name": "wxid_nickabcdefghijklmnopqrs",
                    "aliases_json": '["业务同事"]',
                    "confidence": 0.91,
                    "status": "active",
                    "created_at": "2026-05-01T00:00:00",
                    "updated_at": "2026-05-10T00:00:00",
                },
            ]
        if "FROM plugin_memory_fact fact" in sql:
            return [
                {
                    "id": 10,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "subject_entity_id": 1,
                    "subject_name": "wxid_contactabcdefghijklmnopq",
                    "predicate": "knows",
                    "object_entity_id": 2,
                    "object_name": "wxid_nickabcdefghijklmnopqrs",
                    "object_value": "",
                    "memory_item_id": 100,
                    "source_event_id": 500,
                    "confidence": 0.82,
                    "status": "active",
                    "valid_at": "2026-05-05T00:00:00",
                    "invalid_at": None,
                    "created_at": "2026-05-05T00:00:00",
                    "updated_at": "2026-05-12T00:00:00",
                }
            ]
        if "FROM plugin_memory_episode" in sql:
            return []
        if "FROM plugin_memory_item WHERE id = ANY" in sql:
            return [
                {
                    "id": 100,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "session_id": "group-1@chatroom",
                    "scope_type": "session",
                    "source_type": "auto",
                    "memory_type": "note",
                    "value_json": '{"acceptance":{"status":"accepted"}}',
                    "normalized_key": "relation:knows",
                    "confidence": 0.82,
                    "status": "active",
                    "pinned": False,
                    "priority": 0,
                    "sensitivity": "normal",
                    "source_event_id": 500,
                    "source_trace_id": "",
                    "occurrence_count": 1,
                    "first_seen_at": "2026-05-05T00:00:00",
                    "last_seen_at": "2026-05-12T00:00:00",
                    "created_at": "2026-05-05T00:00:00",
                    "updated_at": "2026-05-12T00:00:00",
                    "deleted_at": None,
                }
            ]
        return []

    async def fake_display_map(*, session_id: str, usernames: Any) -> dict[str, dict[str, str]]:
        assert session_id == "group-1@chatroom"
        assert set(usernames) == {
            "wxid_contactabcdefghijklmnopq",
            "wxid_nickabcdefghijklmnopqrs",
        }
        return {
            "wxid_contactabcdefghijklmnopq": {
                "remark": "客户 张三",
                "nick_name": "张三昵称",
                "alias": "zhangsan",
            },
            "wxid_nickabcdefghijklmnopqrs": {
                "remark": "",
                "nick_name": "李四昵称",
                "alias": "lisi",
            },
        }

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_load_wechat_group_contact_display_map", fake_display_map)

    graph = await store.get_group_relationship_graph(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="group-1@chatroom",
        limit=10,
    )

    labels_by_technical = {node["technical_label"]: node for node in graph["nodes"]}
    contact_node = labels_by_technical["wxid_contactabcdefghijklmnopq"]
    nick_node = labels_by_technical["wxid_nickabcdefghijklmnopqrs"]
    assert contact_node["display_label"] == "客户 张三"
    assert contact_node["aliases"] == ["客户 张三", "张三昵称", "zhangsan"]
    assert nick_node["display_label"] == "李四昵称"
    assert nick_node["aliases"] == ["业务同事", "李四昵称", "lisi"]


@pytest.mark.asyncio
async def test_load_wechat_group_contact_display_map_falls_back_on_sdk_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())

    async def fake_sdk_query_rows(**kwargs: Any) -> list[dict]:
        raise RuntimeError("sdk unavailable")

    monkeypatch.setattr(store, "_sdk_query_rows", fake_sdk_query_rows)

    result = await store._load_wechat_group_contact_display_map(
        session_id="group-1@chatroom",
        usernames=["wxid_contactabcdefghijklmnopq"],
    )

    assert result == {}


@pytest.mark.asyncio
async def test_memory_graph_episode_mapping_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _GraphSqlFake()
    monkeypatch.setattr(memory_store_module, "_exec", graph.exec)
    item = _item(
        id=7,
        session_id="session-a",
        memory_type="episodic",
        content="用户询问过物流进度",
        normalized_key="episodic:text:logistics",
        source_event_id=55,
    )

    await MemoryStore(SimpleNamespace())._sync_memory_graph_for_item(item)
    await MemoryStore(SimpleNamespace())._sync_memory_graph_for_item(item)

    assert len(graph.episodes) == 1
    episode = graph.episodes["[7]"]
    assert episode["sid"] == "session-a"
    assert episode["event_ids"] == "[55]"
    assert episode["status"] == "active"


@pytest.mark.asyncio
async def test_memory_graph_invalidates_stale_projection_when_mapping_type_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _GraphSqlFake()
    monkeypatch.setattr(memory_store_module, "_exec", graph.exec)
    store = MemoryStore(SimpleNamespace())

    await store._sync_memory_graph_for_item(_item(id=3))
    await store._sync_memory_graph_for_item(
        _item(
            id=3,
            session_id="session-a",
            memory_type="episodic",
            content="用户询问过物流进度",
            normalized_key="episodic:text:logistics",
        )
    )

    assert graph.facts[3]["status"] == "invalidated"
    assert graph.episodes["[3]"]["status"] == "active"
    update_sql = next(
        sql
        for sql, params in graph.calls
        if "UPDATE plugin_memory_fact SET status" in sql
        and params
        and params.get("status") == "invalidated"
    )
    assert "CASE WHEN CAST(:status AS VARCHAR) IN" in update_sql


@pytest.mark.asyncio
async def test_memory_graph_mapper_maps_plain_notes_conservatively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _GraphSqlFake()
    monkeypatch.setattr(memory_store_module, "_exec", graph.exec)
    store = MemoryStore(SimpleNamespace())

    await store._sync_memory_graph_for_item(
        _item(
            id=4,
            memory_type="note",
            content="重点客户",
            normalized_key=_semantic_key("note", "manual", "重点客户"),
        )
    )

    assert graph.facts[4]["predicate"] == "note"
    assert graph.facts[4]["object_value"] == "重点客户"
    assert graph.facts[4]["object_entity_id"] is None
    assert len(graph.episodes) == 0


@pytest.mark.asyncio
async def test_memory_graph_mapper_ignores_unknown_types_and_invalidates_old_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _GraphSqlFake()
    monkeypatch.setattr(memory_store_module, "_exec", graph.exec)
    store = MemoryStore(SimpleNamespace())

    await store._sync_memory_graph_for_item(_item(id=5))
    await store._sync_memory_graph_for_item(
        _item(
            id=5,
            memory_type="unknown",
            content="unsupported fact",
            normalized_key=_semantic_key("unknown", "text", "unsupported fact"),
        )
    )

    assert graph.facts[5]["status"] == "invalidated"
    assert len(graph.episodes) == 0


@pytest.mark.asyncio
async def test_memory_graph_retrieve_filters_scope_status_and_ranks_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fact_rows = [
        {
            "id": 1,
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "wxid_a",
            "subject_name": "用户",
            "predicate": "likes",
            "object_name": "Adidas",
            "object_value": "",
            "memory_item_id": 11,
            "confidence": 0.82,
            "status": "active",
            "invalid_at": None,
            "item_source_type": "auto",
            "item_pinned": False,
            "item_priority": 0,
            "item_confidence": 0.82,
            "item_sensitivity": "normal",
            "item_sensitivity_category": "normal",
            "item_origin_session_kind": "unknown",
            "item_audience_scope": "private",
            "item_allowed_session_ids": [],
            "item_session_id": "",
        },
        {
            "id": 2,
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "wxid_a",
            "subject_name": "用户",
            "predicate": "likes",
            "object_name": "Nike",
            "object_value": "",
            "memory_item_id": 12,
            "confidence": 0.7,
            "status": "invalidated",
            "invalid_at": None,
        },
    ]
    for row in fact_rows:
        row.update(
            item_sensitivity="normal",
            item_sensitivity_category="normal",
            item_origin_session_kind="unknown",
            item_audience_scope="private",
            item_allowed_session_ids=[],
            item_session_id="",
        )
    episode_rows = [
        {
            "id": 20,
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "wxid_a",
            "session_id": "s1",
            "title": "用户询问 Adidas 鞋码",
            "summary": "偏好 Adidas",
            "event_ids_json": "[9]",
            "memory_item_ids_json": "[21]",
            "importance": 6,
            "status": "active",
        },
        {
            "id": 21,
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "other",
            "user_id": "wxid_a",
            "session_id": "s1",
            "title": "跨来源事件",
            "summary": "Adidas",
            "event_ids_json": "[]",
            "memory_item_ids_json": "[22]",
            "importance": 100,
            "status": "active",
        },
    ]

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        assert params is not None
        if "FROM plugin_memory_fact fact" in sql:
            assert "fact.tenant_id = :tid" in sql
            assert "fact.source_key IN (:source_key, '*')" in sql
            return [
                row
                for row in fact_rows
                if row["tenant_id"] == params["tid"]
                and row["channel"] == params["channel"]
                and row["user_id"] == params["uid"]
                and row["source_key"] in {params["source_key"], "*"}
                and row["status"] == "active"
            ]
        if "FROM plugin_memory_episode" in sql:
            return [
                row
                for row in episode_rows
                if row["tenant_id"] == params["tid"]
                and row["channel"] == params["channel"]
                and row["user_id"] == params["uid"]
                and row["source_key"] in {params["source_key"], "*"}
                and row["status"] == "active"
            ]
        if "FROM plugin_memory_item" in sql:
            return [
                {
                    "id": 21,
                    "user_id": "wxid_a",
                    "session_id": "s1",
                    "sensitivity": "normal",
                    "sensitivity_category": "normal",
                    "origin_session_kind": "unknown",
                    "audience_scope": "private",
                    "allowed_session_ids": [],
                }
            ]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    result = await MemoryStore(SimpleNamespace()).retrieve_memory_graph(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="s1",
        query="Adidas 鞋码",
        fact_top_k=3,
        episode_top_k=3,
        budget_chars=500,
    )

    assert [fact["memory_item_id"] for fact in result["facts"]] == [11]
    assert result["facts"][0]["score"] > 100
    assert result["facts"][0]["reason"].startswith("query_match")
    assert [episode["id"] for episode in result["episodes"]] == [20]
    assert result["episodes"][0]["memory_item_ids"] == [21]


@pytest.mark.asyncio
async def test_memory_graph_retrieve_excludes_duplicate_fact_and_sensitive_episode_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "FROM plugin_memory_fact fact" in sql:
            return [
                {
                    "id": 1,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "subject_name": "用户",
                    "predicate": "likes",
                    "object_name": "Adidas",
                    "object_value": "",
                    "memory_item_id": 11,
                    "confidence": 0.95,
                    "status": "active",
                    "invalid_at": None,
                    "item_source_type": "manual",
                    "item_pinned": True,
                    "item_priority": 100,
                    "item_confidence": 1.0,
                }
            ]
        if "FROM plugin_memory_episode" in sql:
            return [
                {
                    "id": 20,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "session_id": "s1",
                    "title": "敏感来源事件",
                    "summary": "Adidas",
                    "event_ids_json": "[]",
                    "memory_item_ids_json": "[99]",
                    "importance": 10,
                    "status": "active",
                }
            ]
        if "FROM plugin_memory_item" in sql:
            return []
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    result = await MemoryStore(SimpleNamespace()).retrieve_memory_graph(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="s1",
        query="Adidas",
        fact_top_k=2,
        episode_top_k=2,
        budget_chars=500,
        exclude_memory_item_ids=[11],
    )

    assert result["facts"] == []
    assert result["episodes"] == []


@pytest.mark.asyncio
async def test_memory_hybrid_graph_dedupe_with_selected_memory_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "FROM plugin_memory_item" in sql and "WHERE id = :id" not in sql:
            return [
                {
                    "id": 11,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "session_id": "",
                    "scope_type": "identity",
                    "source_type": "manual",
                    "memory_type": "preference",
                    "content": "用户喜欢 Adidas",
                    "value_json": "{}",
                    "normalized_key": "preference:brand:adidas",
                    "confidence": 1.0,
                    "status": "active",
                    "pinned": True,
                    "priority": 100,
                    "sensitivity": "normal",
                    "source_event_id": None,
                    "source_trace_id": "",
                    "original_text": "",
                    "occurrence_count": 1,
                    "match_count": 1,
                    "deleted_at": None,
                }
            ]
        if "FROM plugin_memory_fact fact" in sql:
            return [
                {
                    "id": 1,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "subject_name": "用户",
                    "predicate": "likes",
                    "object_name": "Adidas",
                    "object_value": "",
                    "memory_item_id": 11,
                    "confidence": 0.95,
                    "status": "active",
                    "invalid_at": None,
                    "item_source_type": "manual",
                    "item_pinned": True,
                    "item_priority": 100,
                    "item_confidence": 1.0,
                }
            ]
        if "FROM plugin_memory_episode" in sql:
            return []
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    result = await MemoryStore(
        SimpleNamespace(memory_hybrid_retrieval_enabled=True)
    ).retrieve_memory_hybrid(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="",
        query="Adidas",
        limit=1,
        fact_top_k=2,
        episode_top_k=1,
        include_graph=True,
    )

    assert [item["id"] for item in result["items"]] == [11]
    assert result["facts"] == []


@pytest.mark.asyncio
async def test_memory_graph_retrieve_matches_normalized_entity_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "FROM plugin_memory_fact fact" in sql:
            return [
                {
                    "id": 1,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_a",
                    "subject_name": "用户",
                    "subject_normalized_name": "user",
                    "predicate": "likes",
                    "object_name": "",
                    "object_normalized_name": "acme premium",
                    "object_value": "",
                    "memory_item_id": 11,
                    "confidence": 0.8,
                    "status": "active",
                    "invalid_at": None,
                    "created_at": "2026-01-01T00:00:00",
                    "updated_at": "2026-01-02T00:00:00",
                    "item_source_type": "auto",
                    "item_pinned": False,
                    "item_priority": 0,
                    "item_confidence": 0.8,
                    "item_sensitivity": "normal",
                    "item_sensitivity_category": "normal",
                    "item_origin_session_kind": "unknown",
                    "item_audience_scope": "private",
                    "item_allowed_session_ids": [],
                    "item_session_id": "",
                }
            ]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    result = await MemoryStore(SimpleNamespace()).retrieve_memory_graph(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="s1",
        query="acme",
        fact_top_k=1,
        episode_top_k=0,
        budget_chars=500,
    )

    assert result["facts"][0]["memory_item_id"] == 11
    assert result["facts"][0]["match_count"] == 1
    assert result["facts"][0]["object_normalized_name"] == "acme premium"
    assert result["facts"][0]["created_at"] == "2026-01-01T00:00:00"
    assert result["facts"][0]["updated_at"] == "2026-01-02T00:00:00"


@pytest.mark.asyncio
async def test_memory_graph_retrieve_uses_candidate_order_as_recency_tie_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fact_rows = [
        {
            "id": 1,
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "wxid_a",
            "subject_name": "用户",
            "subject_normalized_name": "user",
            "predicate": "likes",
            "object_name": "Adidas",
            "object_normalized_name": "adidas",
            "object_value": "",
            "memory_item_id": 11,
            "confidence": 0.8,
            "status": "active",
            "invalid_at": None,
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-03T00:00:00",
            "item_source_type": "auto",
            "item_pinned": False,
            "item_priority": 0,
            "item_confidence": 0.8,
        },
        {
            "id": 2,
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "wxid_a",
            "subject_name": "用户",
            "subject_normalized_name": "user",
            "predicate": "likes",
            "object_name": "Nike",
            "object_normalized_name": "nike",
            "object_value": "",
            "memory_item_id": 12,
            "confidence": 0.8,
            "status": "active",
            "invalid_at": None,
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-02T00:00:00",
            "item_source_type": "auto",
            "item_pinned": False,
            "item_priority": 0,
            "item_confidence": 0.8,
        },
    ]
    for row in fact_rows:
        row.update(
            item_sensitivity="normal",
            item_sensitivity_category="normal",
            item_origin_session_kind="unknown",
            item_audience_scope="private",
            item_allowed_session_ids=[],
            item_session_id="",
        )
    episode_rows = [
        {
            "id": 20,
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "wxid_a",
            "session_id": "s1",
            "title": "最近事件",
            "summary": "",
            "event_ids_json": "[]",
            "memory_item_ids_json": "[21]",
            "importance": 0,
            "status": "active",
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-03T00:00:00",
        },
        {
            "id": 21,
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "wxid_a",
            "session_id": "s1",
            "title": "较早事件",
            "summary": "",
            "event_ids_json": "[]",
            "memory_item_ids_json": "[22]",
            "importance": 0,
            "status": "active",
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-02T00:00:00",
        },
    ]

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "FROM plugin_memory_fact fact" in sql:
            return fact_rows
        if "FROM plugin_memory_episode" in sql:
            return episode_rows
        if "FROM plugin_memory_item" in sql:
            return [
                {
                    "id": item_id,
                    "user_id": "wxid_a",
                    "session_id": "s1",
                    "sensitivity": "normal",
                    "sensitivity_category": "normal",
                    "origin_session_kind": "unknown",
                    "audience_scope": "private",
                    "allowed_session_ids": [],
                }
                for item_id in (21, 22)
            ]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    result = await MemoryStore(SimpleNamespace()).retrieve_memory_graph(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="s1",
        query="",
        fact_top_k=2,
        episode_top_k=2,
        budget_chars=500,
    )

    assert [fact["id"] for fact in result["facts"]] == [1, 2]
    assert result["facts"][0]["recency_boost"] > result["facts"][1]["recency_boost"]
    assert result["facts"][0]["updated_at"] == "2026-01-03T00:00:00"
    assert [episode["id"] for episode in result["episodes"]] == [20, 21]
    assert result["episodes"][0]["recency_boost"] > result["episodes"][1]["recency_boost"]
    assert result["episodes"][0]["updated_at"] == "2026-01-03T00:00:00"


@pytest.mark.asyncio
async def test_memory_graph_retrieve_empty_candidates_yield_no_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params))
        if "FROM plugin_memory_fact fact" in sql:
            assert "1 = 0" in sql
            assert "fact_candidate_ids" not in (params or {})
        if "FROM plugin_memory_episode" in sql:
            assert "1 = 0" in sql
            assert "episode_candidate_ids" not in (params or {})
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    result = await MemoryStore(SimpleNamespace())._retrieve_memory_graph_sql(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="s1",
        query="Adidas",
        fact_top_k=1,
        episode_top_k=1,
        candidate_fact_ids=[],
        candidate_episode_ids=[],
    )

    assert result["facts"] == []
    assert result["episodes"] == []
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_memory_graph_retrieve_candidate_params_are_normalized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        assert params is not None
        if "FROM plugin_memory_fact fact" in sql:
            assert params["fact_candidate_ids"] == [1, 2]
        if "FROM plugin_memory_episode" in sql:
            assert params["episode_candidate_ids"] == [3, 4]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    await MemoryStore(SimpleNamespace())._retrieve_memory_graph_sql(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="s1",
        query="Adidas",
        fact_top_k=1,
        episode_top_k=1,
        candidate_fact_ids=["2", 1, "bad", 2],
        candidate_episode_ids=["4", 3, None, 4],
    )
