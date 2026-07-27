from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import plugins.memory.store as memory_store_module
import plugins.memory.store_jobs as memory_jobs_module
import plugins.memory.store_retrieval as memory_retrieval_module
from plugins.memory.store import (
    MemoryStore,
    _memory_item_visible_for_audience,
)
from plugins.memory.store_jobs import MemoryExtractionJobStoreMixin
from plugins.memory.store_retrieval import MemoryRetrievalStoreMixin


def _item(
    item_id: int,
    *,
    user_id: str = "wxid-a",
    session_id: str = "",
    origin_session_kind: str = "group",
    audience_scope: str = "session",
    allowed_session_ids: list[str] | None = None,
    sensitivity_category: str = "normal",
    expires_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "id": item_id,
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": user_id,
        "session_id": session_id,
        "scope_type": "identity",
        "source_type": "auto",
        "memory_type": "note",
        "content": f"memory-{item_id}",
        "value_json": "{}",
        "normalized_key": f"note:{item_id}",
        "confidence": 0.95,
        "status": "active",
        "pinned": False,
        "priority": 0,
        "sensitivity": sensitivity_category,
        "sensitivity_category": sensitivity_category,
        "origin_session_kind": origin_session_kind,
        "audience_scope": audience_scope,
        "allowed_session_ids": allowed_session_ids or ["room-a@chatroom"],
        "source_kind": "conversation",
        "expires_at": expires_at,
        "occurrence_count": 1,
        "created_at": "2026-07-18T00:00:00",
        "updated_at": "2026-07-18T00:00:00",
        "deleted_at": None,
    }


def test_row_audience_filter_isolates_group_member_expiry_and_sensitivity() -> None:
    now = datetime(2026, 7, 18, 12, tzinfo=UTC)
    same_group = _item(1)

    assert _memory_item_visible_for_audience(
        same_group,
        session_id="room-a@chatroom",
        request_session_kind="group",
        user_id="wxid-a",
        now=now,
    )
    assert not _memory_item_visible_for_audience(
        same_group,
        session_id="room-b@chatroom",
        request_session_kind="group",
        user_id="wxid-a",
        now=now,
    )
    assert not _memory_item_visible_for_audience(
        same_group,
        session_id="room-a@chatroom",
        request_session_kind="group",
        user_id="wxid-b",
        now=now,
    )
    assert not _memory_item_visible_for_audience(
        _item(2, expires_at=now - timedelta(seconds=1)),
        session_id="room-a@chatroom",
        request_session_kind="group",
        user_id="wxid-a",
        now=now,
    )
    assert not _memory_item_visible_for_audience(
        _item(3, sensitivity_category="sensitive"),
        session_id="room-a@chatroom",
        request_session_kind="group",
        user_id="wxid-a",
        now=now,
    )


def test_private_reads_are_private_only_and_legacy_rows_fail_closed_in_groups() -> None:
    legacy = dict(_item(1))
    for key in ("origin_session_kind", "audience_scope", "allowed_session_ids"):
        legacy.pop(key)
    legacy["sensitivity"] = "normal"

    assert _memory_item_visible_for_audience(
        legacy,
        session_id="wxid-a",
        request_session_kind="private",
        user_id="wxid-a",
    )
    assert not _memory_item_visible_for_audience(
        legacy,
        session_id="room-a@chatroom",
        request_session_kind="group",
        user_id="wxid-a",
    )
    explicit_group = _item(
        2,
        origin_session_kind="private",
        audience_scope="explicit",
        allowed_session_ids=["room-a@chatroom"],
    )
    assert not _memory_item_visible_for_audience(
        explicit_group,
        session_id="wxid-a",
        request_session_kind="private",
        user_id="wxid-a",
    )
    assert _memory_item_visible_for_audience(
        explicit_group,
        session_id="room-a@chatroom",
        request_session_kind="group",
        user_id="wxid-a",
    )


@pytest.mark.asyncio
async def test_runtime_profile_rebuilds_prompt_fields_from_authorized_rows_only() -> None:
    store = object.__new__(MemoryStore)
    identity = {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid-a",
        "long_term_memory": "private legacy cache",
        "manual_notes": "private legacy note",
        "long_term_items_json": "[]",
        "message_count": 1,
    }
    session = {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid-a",
        "session_id": "room-a@chatroom",
        "short_term_memory": "same-group recent turns",
        "manual_notes": "",
        "short_term_items_json": "[]",
        "message_count": 1,
    }
    rows = [
        {**_item(1), "content": "same group fact"},
        {
            **_item(2, audience_scope="private", origin_session_kind="private"),
            "allowed_session_ids": [],
            "content": "private fact",
        },
        {
            **_item(3, expires_at=datetime(2020, 1, 1, tzinfo=UTC)),
            "content": "expired fact",
        },
        {**_item(4, sensitivity_category="pii"), "content": "sensitive fact"},
        {**_item(5, user_id="wxid-b"), "content": "other member fact"},
    ]
    store.get_identity_profile = AsyncMock(return_value=identity)
    store.get_session_profile = AsyncMock(return_value=session)
    store.list_memory_items = AsyncMock(
        side_effect=lambda **kwargs: rows if kwargs["scope_type"] == "identity" else []
    )

    profile = await store.get_runtime_profile(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid-a",
        session_id="room-a@chatroom",
        request_session_kind="group",
    )

    assert "same group fact" in profile["long_term_memory"]
    assert "private legacy cache" not in profile["long_term_memory"]
    assert "private fact" not in profile["long_term_memory"]
    assert "expired fact" not in profile["long_term_memory"]
    assert "sensitive fact" not in profile["long_term_memory"]
    assert "other member fact" not in profile["long_term_memory"]
    assert profile["identity_manual_notes"] == ""


@pytest.mark.asyncio
async def test_vector_retrieval_applies_the_same_row_audience_filter() -> None:
    items = {
        1: _item(1),
        2: _item(2, allowed_session_ids=["room-b@chatroom"]),
        3: _item(3, user_id="wxid-b"),
        4: _item(4, sensitivity_category="sensitive"),
    }
    fake = SimpleNamespace(
        vector_index=SimpleNamespace(
            default_top_k=4,
            search_item_ids=AsyncMock(return_value=[(1, 0.9), (2, 0.89), (3, 0.88), (4, 0.87)]),
        ),
        get_memory_item=AsyncMock(side_effect=lambda item_id: dict(items[item_id])),
    )

    result = await MemoryRetrievalStoreMixin._retrieve_memory_items_vector(
        fake,
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid-a",
        session_id="room-a@chatroom",
        query="memory",
        request_session_kind="group",
        limit=10,
    )

    assert [item["id"] for item in result] == [1]


@pytest.mark.asyncio
async def test_graph_retrieval_filters_facts_and_mixed_audience_episodes(
    monkeypatch,
) -> None:
    facts = [
        {
            "id": item_id,
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": user_id,
            "subject_name": "memory",
            "subject_normalized_name": "memory",
            "predicate": "mentioned",
            "object_value": f"fact-{item_id}",
            "memory_item_id": item_id,
            "confidence": 0.9,
            "status": "active",
            "item_status": "active",
            "item_sensitivity": sensitivity,
            "item_sensitivity_category": sensitivity,
            "item_origin_session_kind": "group",
            "item_audience_scope": "session",
            "item_allowed_session_ids": allowed,
            "item_session_id": "",
        }
        for item_id, user_id, allowed, sensitivity in (
            (1, "wxid-a", ["room-a@chatroom"], "normal"),
            (2, "wxid-a", ["room-b@chatroom"], "normal"),
            (3, "wxid-b", ["room-a@chatroom"], "normal"),
            (4, "wxid-a", ["room-a@chatroom"], "sensitive"),
        )
    ]
    episodes = [
        {
            "id": 10,
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "wxid-a",
            "session_id": "room-a@chatroom",
            "title": "memory episode",
            "summary": "mixed audience",
            "event_ids_json": "[]",
            "memory_item_ids_json": "[1, 2]",
            "importance": 50,
            "status": "active",
        }
    ]

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        del params
        if "FROM plugin_memory_fact fact" in sql:
            return facts
        if "FROM plugin_memory_episode" in sql:
            return episodes
        if "id = ANY(:memory_item_ids)" in sql:
            return [
                {
                    "id": 1,
                    "user_id": "wxid-a",
                    "session_id": "",
                    "sensitivity": "normal",
                    "sensitivity_category": "normal",
                    "origin_session_kind": "group",
                    "audience_scope": "session",
                    "allowed_session_ids": ["room-a@chatroom"],
                },
                {
                    "id": 2,
                    "user_id": "wxid-a",
                    "session_id": "",
                    "sensitivity": "normal",
                    "sensitivity_category": "normal",
                    "origin_session_kind": "group",
                    "audience_scope": "session",
                    "allowed_session_ids": ["room-b@chatroom"],
                },
            ]
        return []

    monkeypatch.setattr(memory_retrieval_module, "_exec", fake_exec)
    store = object.__new__(MemoryStore)

    result = await store._retrieve_memory_graph_sql(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid-a",
        session_id="room-a@chatroom",
        query="memory",
        fact_top_k=10,
        episode_top_k=10,
        request_session_kind="group",
    )

    assert [fact["memory_item_id"] for fact in result["facts"]] == [1]
    assert result["episodes"] == []


@pytest.mark.asyncio
async def test_writer_persists_group_audience_as_a_distinct_dedupe_sibling(
    monkeypatch,
) -> None:
    calls: list[tuple[str, dict]] = []
    private_sibling = {
        "id": 9,
        "origin_session_kind": "private",
        "audience_scope": "private",
        "allowed_session_ids": [],
        "sensitivity": "normal",
        "sensitivity_category": "normal",
        "expires_at": None,
    }

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        payload = dict(params or {})
        calls.append((sql, payload))
        if sql.startswith("SELECT id, audience_scope"):
            return [private_sibling]
        if sql.startswith("INSERT INTO plugin_memory_item"):
            return [
                {
                    **_item(10),
                    "content": payload["content"],
                    "normalized_key": payload["normalized_key"],
                    "allowed_session_ids": json.loads(payload["allowed_session_ids"]),
                    "expires_at": payload["expires_at"],
                }
            ]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    store = object.__new__(MemoryStore)
    expiry = datetime(2026, 8, 1, tzinfo=UTC)

    item = await store._insert_or_touch_memory_item(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid-a",
        scope_type="identity",
        source_type="auto",
        memory_type="note",
        content="remember this",
        normalized_key="note:shared",
        confidence=0.95,
        origin_session_kind="group",
        audience_scope="session",
        allowed_session_ids=["room-a@chatroom"],
        sensitivity_category="normal",
        expires_at=expiry,
    )

    assert item is not None
    insert_sql, insert_params = next(
        (sql, params) for sql, params in calls if sql.startswith("INSERT INTO plugin_memory_item")
    )
    assert "ON CONFLICT DO NOTHING" in insert_sql
    assert insert_params["origin_session_kind"] == "group"
    assert insert_params["audience_scope"] == "session"
    assert json.loads(insert_params["allowed_session_ids"]) == ["room-a@chatroom"]
    assert insert_params["sensitivity_category"] == "normal"
    assert insert_params["expires_at"] == expiry.replace(tzinfo=None)
    assert not any(sql.startswith("UPDATE plugin_memory_item") for sql, _ in calls)


@pytest.mark.asyncio
async def test_dedupe_hash_conflict_refuses_cross_audience_merge(monkeypatch) -> None:
    private_sibling = {
        "id": 9,
        "origin_session_kind": "private",
        "audience_scope": "private",
        "allowed_session_ids": [],
        "sensitivity": "normal",
        "sensitivity_category": "normal",
        "expires_at": None,
    }
    writes: list[str] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        del params
        if sql.startswith("SELECT id, audience_scope"):
            return [private_sibling]
        if sql.startswith("INSERT INTO plugin_memory_item"):
            writes.append(sql)
            return []
        if sql.startswith("UPDATE plugin_memory_item"):
            writes.append(sql)
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    store = object.__new__(MemoryStore)

    item = await store._insert_or_touch_memory_item(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid-a",
        scope_type="identity",
        source_type="auto",
        memory_type="note",
        content="remember this",
        normalized_key="note:shared",
        confidence=0.95,
        origin_session_kind="group",
        audience_scope="session",
        allowed_session_ids=["room-a@chatroom"],
    )

    assert item is None
    assert len(writes) == 1
    assert writes[0].startswith("INSERT INTO plugin_memory_item")


@pytest.mark.asyncio
async def test_llm_job_captures_immutable_audience_metadata(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        assert sql.startswith("INSERT INTO plugin_memory_extraction_job")
        captured.update(params or {})
        return []

    monkeypatch.setattr(memory_jobs_module, "_exec", fake_exec)
    fake = SimpleNamespace(
        settings=SimpleNamespace(
            memory_llm_extraction_job_enabled=True,
            memory_llm_extraction_job_max_attempts=3,
        )
    )

    await MemoryExtractionJobStoreMixin.enqueue_llm_extraction_job(
        fake,
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid-a",
        session_id="room-a@chatroom",
        trace_id="trace-a",
        source_event_id=1,
        origin_session_kind="group",
        audience_scope="session",
        allowed_session_ids=["room-a@chatroom"],
        sensitivity_category="normal",
        expires_at="2026-08-01T00:00:00+00:00",
    )

    audience = json.loads(str(captured["result_json"]))["audience"]
    assert audience == {
        "origin_session_kind": "group",
        "audience_scope": "session",
        "allowed_session_ids": ["room-a@chatroom"],
        "expires_at": "2026-08-01T00:00:00",
        "sensitivity_category": "normal",
        "source_kind": "conversation",
    }
