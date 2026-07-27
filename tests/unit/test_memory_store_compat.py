from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import plugins.memory.store as memory_store_module
from plugins.memory.store import (
    GROUP_HISTORY_USER_ID_SCOPE,
    MEMORY_DDL_CONSISTENCY_GUARD,
    MemoryStore,
    _build_group_relationship_edge_evidence_payload,
    _detect_sensitivity,
    _group_history_user_scope,
    _llm_extraction_job_enqueue_eligible,
    _redact_profile_enrichment_payload,
)


@pytest.mark.parametrize(
    (
        "job_enabled",
        "structured_enabled",
        "structured_llm_available",
        "graph_enabled",
        "graph_llm_available",
        "expected",
    ),
    [
        (True, True, True, False, False, True),
        (True, False, False, True, True, True),
        (True, True, True, True, True, True),
        (True, True, False, True, False, False),
        (True, False, False, False, False, False),
        (False, True, True, True, True, False),
    ],
)
def test_llm_extraction_job_enqueue_eligibility_is_extractor_or_job_gated(
    job_enabled: bool,
    structured_enabled: bool,
    structured_llm_available: bool,
    graph_enabled: bool,
    graph_llm_available: bool,
    expected: bool,
) -> None:
    assert (
        _llm_extraction_job_enqueue_eligible(
            job_enabled=job_enabled,
            structured_enabled=structured_enabled,
            structured_llm_available=structured_llm_available,
            graph_enabled=graph_enabled,
            graph_llm_available=graph_llm_available,
        )
        is expected
    )


@pytest.mark.parametrize(
    ("session_id", "user_id", "expected_user_id", "expected_auto"),
    [
        ("room-a@chatroom", None, GROUP_HISTORY_USER_ID_SCOPE, True),
        ("room-a@chatroom", "", GROUP_HISTORY_USER_ID_SCOPE, True),
        ("room-a@chatroom", "  __group__  ", GROUP_HISTORY_USER_ID_SCOPE, True),
        ("room-a@chatroom", "wxid_a", "wxid_a", False),
        ("wxid_private", None, "", False),
        ("wxid_private", GROUP_HISTORY_USER_ID_SCOPE, GROUP_HISTORY_USER_ID_SCOPE, False),
    ],
)
def test_group_history_user_scope_resolves_group_auto_scope_only_for_group_sessions(
    session_id: str,
    user_id: str | None,
    expected_user_id: str,
    expected_auto: bool,
) -> None:
    assert _group_history_user_scope(session_id, user_id) == (expected_user_id, expected_auto)


@pytest.mark.asyncio
async def test_sdk_query_read_uses_exact_private_origin_auth_and_bounded_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_safe_trusted_service_request(
        client,
        method,
        base_url,
        path,
        *,
        json,
        headers,
        timeout_seconds,
        max_response_bytes,
        allowed_response_content_types,
    ) -> httpx.Response:
        _ = client
        captured.update(
            method=method,
            base_url=base_url,
            path=path,
            json=json,
            headers=dict(headers),
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            content_types=allowed_response_content_types,
        )
        url = f"{base_url}{path}"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"ok": True, "rows": [{"value": 1}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(
        memory_store_module,
        "safe_trusted_service_request",
        fake_safe_trusted_service_request,
    )
    store = MemoryStore(
        SimpleNamespace(
            wxbot_sdk_url="http://127.0.0.1:5080",
            wxbot_api_token="sdk-secret",
            wxbot_default_tenant_id="default",
        )
    )

    result = await store._sdk_query_read(
        tenant_id="default",
        connection_id="legacy-wechat-default",
        database="message",
        sql="SELECT 1",
        params=["private-value"],
        limit=999,
    )

    assert result["rows"] == [{"value": 1}]
    assert captured["method"] == "POST"
    assert captured["base_url"] == "http://127.0.0.1:5080"
    assert captured["path"] == "/ext/query/read"
    assert captured["json"] == {
        "database": "message",
        "sql": "SELECT 1",
        "params": ["private-value"],
        "limit": 500,
    }
    assert captured["headers"] == {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": "Bearer sdk-secret",
    }
    assert captured["max_response_bytes"] == 10 * 1024 * 1024
    assert captured["timeout_seconds"] == 20.0


def test_group_relationship_edge_evidence_payload_keeps_only_safe_fields() -> None:
    payload = _build_group_relationship_edge_evidence_payload(
        fact={
            "id": 10,
            "subject_entity_id": 1,
            "predicate": "knows",
            "object_entity_id": 2,
            "confidence": 0.9,
            "status": "active",
            "object_value": "private object value",
            "valid_at": "2026-05-01T00:00:00",
            "updated_at": "2026-05-02T00:00:00",
        },
        backing_item={"status": "active", "acceptance_status": "accepted"},
        evidence_items=[
            {
                "id": 100,
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "user_id": GROUP_HISTORY_USER_ID_SCOPE,
                "session_id": "room-a@chatroom",
                "scope_type": "session",
                "source_type": "auto",
                "memory_type": "note",
                "normalized_key": "relation:knows",
                "confidence": 0.9,
                "status": "active",
                "sensitivity": "normal",
                "source_event_id": 500,
                "acceptance_status": "accepted",
                "content": "private memory content",
                "value_json": '{"private": true}',
                "original_text": "private original text",
            }
        ],
        events=[
            {
                "id": 500,
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "user_id": GROUP_HISTORY_USER_ID_SCOPE,
                "session_id": "room-a@chatroom",
                "trace_id": "trace-500",
                "event_key": "event-500",
                "created_at": "2026-05-01T00:00:00",
                "user_text": "private user text",
                "assistant_text": "private assistant text",
            }
        ],
        evidence_episodes=[
            {
                "id": 20,
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "user_id": GROUP_HISTORY_USER_ID_SCOPE,
                "session_id": "room-a@chatroom",
                "event_ids": [500],
                "memory_item_ids": [100],
                "importance": 1,
                "status": "active",
                "summary": "private episode summary",
            }
        ],
        memory_item_ids=[100, "100", "not-an-id"],
        event_ids=[500, "500", None],
    )

    assert payload["edge"]["id"] == "fact:10"
    assert payload["evidence_ids"] == {
        "backing_memory_item_id": None,
        "memory_item_ids": [100],
        "event_ids": [500],
        "episode_ids": [20],
    }
    assert payload["evidence_counts"] == {"memory_items": 1, "events": 1, "episodes": 1}
    assert set(payload["memory_items"][0]) == {
        "id",
        "tenant_id",
        "channel",
        "source_key",
        "user_id",
        "session_id",
        "scope_type",
        "source_type",
        "memory_type",
        "normalized_key",
        "confidence",
        "status",
        "sensitivity",
        "source_event_id",
        "acceptance_status",
        "created_at",
        "updated_at",
    }
    serialized = str(payload)
    assert "private memory content" not in serialized
    assert "private original text" not in serialized
    assert "private user text" not in serialized
    assert "private assistant text" not in serialized
    assert "private episode summary" not in serialized
    assert "private object value" not in serialized


def test_profile_enrichment_payload_redaction_covers_common_private_values() -> None:
    payload = _redact_profile_enrichment_payload(
        {
            "summary": (
                "email synthetic@example.com phone 13800138000 id 110101199001011234 "
                "token=abcdef1234567890abcdef1234567890 北京市海淀区测试路1号"
            ),
            "nested": ["secret=abcdef1234567890abcdef1234567890"],
        }
    )

    serialized = str(payload)
    assert "synthetic@example.com" not in serialized
    assert "13800138000" not in serialized
    assert "110101199001011234" not in serialized
    assert "abcdef1234567890abcdef1234567890" not in serialized
    assert "北京市海淀区测试路1号" not in serialized
    assert "[redacted-email]" in serialized
    assert "[redacted-phone]" in serialized
    assert "[redacted-id]" in serialized
    assert "[redacted-token]" in serialized
    assert "[redacted-address]" in serialized


@pytest.mark.asyncio
async def test_create_profile_enrichment_candidate_uses_memory_item_without_auto_accept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict | None]] = []
    inserted_value: dict[str, Any] = {}

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params))
        if sql.startswith("SELECT id FROM plugin_memory_item"):
            return []
        if sql.startswith("INSERT INTO plugin_memory_item"):
            assert params is not None
            inserted_value.update(params)
            return [
                {
                    "id": 99,
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
            ]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    store = MemoryStore(SimpleNamespace())

    item = await store.create_profile_enrichment_candidate(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="group-1@chatroom",
        user_id="wxid_member",
        report_payload={
            "profile": {
                "display_names": ["Synthetic Member"],
                "summary": "public candidate email synthetic@example.com",
            },
            "confidence": 0.88,
            "review": {"state": "accepted"},
            "external_candidates": [
                {"binding_status": "matched", "public_summary": "phone 13800138000"}
            ],
        },
        created_by="admin-test",
    )

    assert item is not None
    assert item["source_type"] == "profile_enrichment"
    assert item["memory_type"] == "profile_enrichment_candidate"
    assert item["status"] == "pending"
    assert item["acceptance_status"] == "needs_review"
    assert item["value"]["report"]["review"]["state"] == "needs_review"
    assert (
        item["value"]["report"]["external_candidates"][0]["binding_status"] == "needs_human_review"
    )
    assert inserted_value["scope_type"] == "session"
    assert inserted_value["normalized_key"].startswith("profile_enrichment:")
    serialized = str(item)
    assert "synthetic@example.com" not in serialized
    assert "13800138000" not in serialized
    assert "[redacted-email]" in serialized
    assert "[redacted-phone]" in serialized
    assert "accepted" not in item["acceptance_status"]


@pytest.mark.asyncio
async def test_get_identity_profile_falls_back_to_wildcard_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict | None]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params))
        return [
            {
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "*",
                "user_id": "wxid_private_a",
                "long_term_memory": "已知用户事实与偏好：\n- 偏好私聊联系",
                "manual_notes": "",
                "long_term_items_json": '["偏好私聊联系"]',
                "message_count": 5,
                "imported_message_count": 2,
                "last_session_id": "wxid_private_a",
                "last_seen_at": None,
                "updated_at": None,
            }
        ]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    store = MemoryStore(SimpleNamespace())

    profile = await store.get_identity_profile(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_private_a",
    )

    assert calls
    assert "source_key IN (:source_key, '*')" in calls[0][0]
    assert profile["source_key"] == "*"
    assert profile["user_id"] == "wxid_private_a"
    assert profile["long_term_items"] == ["偏好私聊联系"]


@pytest.mark.asyncio
async def test_list_profiles_prefers_exact_source_and_keeps_wildcard_only_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict | None]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params))
        return [
            {
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "user_id": "wxid_group_a",
                "long_term_memory": "",
                "manual_notes": "",
                "last_session_id": "group-1@chatroom",
                "message_count": 3,
                "imported_message_count": 1,
                "last_seen_at": None,
                "updated_at": None,
            },
            {
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "*",
                "user_id": "wxid_private_b",
                "long_term_memory": "",
                "manual_notes": "",
                "last_session_id": "wxid_private_b",
                "message_count": 2,
                "imported_message_count": 0,
                "last_seen_at": None,
                "updated_at": None,
            },
        ]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    store = MemoryStore(SimpleNamespace())

    rows = await store.list_profiles(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        limit=20,
    )

    assert calls
    sql, params = calls[0]
    assert "ROW_NUMBER() OVER" in sql
    assert "source_key IN (:source_key, '*')" in sql
    assert params == {"tid": "demo", "lim": 20, "channel": "wechat", "source_key": "wxbot"}
    assert [row["user_id"] for row in rows] == ["wxid_group_a", "wxid_private_b"]


@pytest.mark.asyncio
async def test_get_runtime_profile_merges_identity_and_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "plugin_memory_identity_profile" in sql:
            return [
                {
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "wxid_group_a",
                    "long_term_memory": "已知用户事实与偏好：\n- 偏好微信联系",
                    "manual_notes": "VIP",
                    "long_term_items_json": '["偏好微信联系"]',
                    "message_count": 6,
                    "imported_message_count": 4,
                    "last_session_id": "group-1@chatroom",
                    "last_seen_at": None,
                    "updated_at": None,
                }
            ]
        return [
            {
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "session_id": "group-1@chatroom",
                "user_id": "wxid_group_a",
                "short_term_memory": "用户最近说：群里问库存",
                "manual_notes": "这个群更关注库存",
                "short_term_items_json": "[]",
                "message_count": 2,
                "imported_message_count": 1,
                "last_seen_at": None,
                "updated_at": None,
            }
        ]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    store = MemoryStore(SimpleNamespace())

    async def fake_list_memory_items(**kwargs) -> list[dict]:
        base = {
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "wxid_group_a",
            "status": "active",
            "sensitivity": "normal",
            "sensitivity_category": "normal",
            "origin_session_kind": "group",
            "audience_scope": "session",
            "allowed_session_ids": ["group-1@chatroom"],
        }
        if kwargs["scope_type"] == "identity":
            return [
                {
                    **base,
                    "id": 1,
                    "session_id": "",
                    "scope_type": "identity",
                    "source_type": "auto",
                    "content": "偏好微信联系",
                    "confidence": 0.95,
                    "priority": 0,
                },
                {
                    **base,
                    "id": 2,
                    "session_id": "",
                    "scope_type": "identity",
                    "source_type": "manual",
                    "content": "VIP",
                    "confidence": 1.0,
                    "priority": 100,
                },
            ]
        return [
            {
                **base,
                "id": 3,
                "session_id": "group-1@chatroom",
                "scope_type": "session",
                "source_type": "manual",
                "content": "这个群更关注库存",
                "confidence": 1.0,
                "priority": 100,
            }
        ]

    monkeypatch.setattr(store, "list_memory_items", fake_list_memory_items)

    profile = await store.get_runtime_profile(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="group-1@chatroom",
        user_id="wxid_group_a",
    )

    assert "偏好微信联系" in profile["long_term_memory"]
    assert profile["short_term_memory"] == "用户最近说：群里问库存"
    assert profile["identity_manual_notes"] == "VIP"
    assert profile["session_manual_notes"] == "这个群更关注库存"
    assert profile["session_message_count"] == 2


@pytest.mark.asyncio
async def test_list_events_includes_exact_and_wildcard_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict | None]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params))
        return [
            {
                "id": 1,
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "user_id": "wxid_group_a",
                "session_id": "group-1@chatroom",
                "user_text": "群聊消息",
                "assistant_text": "群聊回复",
                "trace_id": "trace-1",
                "created_at": None,
            },
            {
                "id": 2,
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "*",
                "user_id": "wxid_private_a",
                "session_id": "wxid_private_a",
                "user_text": "私聊消息",
                "assistant_text": "私聊回复",
                "trace_id": "trace-2",
                "created_at": None,
            },
        ]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    store = MemoryStore(SimpleNamespace())

    rows = await store.list_events(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        limit=20,
    )

    assert calls
    sql, params = calls[0]
    assert "source_key IN (:source_key, '*')" in sql
    assert params == {"tid": "demo", "lim": 20, "channel": "wechat", "source_key": "wxbot"}
    assert [row["source_key"] for row in rows] == ["wxbot", "*"]


@pytest.mark.asyncio
async def test_collect_session_history_pages_sdk_rows_and_bounds_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    calls: list[dict] = []

    async def fake_sdk_query_rows(**kwargs):
        calls.append(kwargs)
        sql = kwargs["sql"]
        if "sqlite_master" in sql:
            return [{"ok": 1}]
        assert kwargs["limit"] <= 500
        params = kwargs["params"]
        cursor_rowid = int(params[3]) if len(params) > 3 else 0
        page_limit = int(kwargs["limit"])
        end = min(cursor_rowid + page_limit, 150)
        rows = []
        for rowid in range(cursor_rowid + 1, end + 1):
            rows.append(
                {
                    "rowid": rowid,
                    "create_time": 1776746400,
                    "real_sender_id": 0,
                    "local_type": 1,
                    "message_content_hex": f"wxid_a:\nmessage-{rowid}".encode().hex(),
                    "compression_type": None,
                }
            )
        return rows

    monkeypatch.setattr(store, "_sdk_query_rows", fake_sdk_query_rows)

    messages = await store._collect_session_history(
        session_id="room-a@chatroom",
        user_id="wxid_a",
        cutoff_ts=1776740000,
        max_messages=75,
    )

    history_calls = [call for call in calls if "FROM [Msg_" in call["sql"]]
    assert len(history_calls) >= 2
    assert "ORDER BY create_time ASC, rowid ASC" in history_calls[0]["sql"]
    assert history_calls[1]["params"] == [1776740000, 1776746400, 1776746400, 75]
    assert len(messages) == 75
    assert messages[0]["user_text"] == "message-76"
    assert messages[-1]["user_text"] == "message-150"


@pytest.mark.asyncio
async def test_collect_session_history_supports_target_day_end_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    calls: list[dict] = []

    async def fake_sdk_query_rows(**kwargs):
        calls.append(kwargs)
        sql = kwargs["sql"]
        if "sqlite_master" in sql:
            return [{"ok": 1}]
        assert "create_time < ?" in sql
        assert kwargs["params"][:2] == [1776700800, 1776787200]
        return [
            {
                "rowid": 1,
                "create_time": 1776746400,
                "real_sender_id": 0,
                "local_type": 1,
                "message_content_hex": b"wxid_a:\nday message".hex(),
                "compression_type": None,
            }
        ]

    monkeypatch.setattr(store, "_sdk_query_rows", fake_sdk_query_rows)

    messages = await store._collect_session_history(
        session_id="room-a@chatroom",
        user_id="wxid_a",
        cutoff_ts=1776700800,
        end_ts=1776787200,
        max_messages=10000,
    )

    assert len(messages) == 1
    assert messages[0]["user_text"] == "day message"
    assert any("create_time < ?" in call["sql"] for call in calls if "FROM [Msg_" in call["sql"])


@pytest.mark.asyncio
async def test_collect_group_session_history_without_user_id_collects_all_senders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())

    async def fake_sdk_query_rows(**kwargs):
        sql = kwargs["sql"]
        if "sqlite_master" in sql:
            return [{"ok": 1}]
        return [
            {
                "rowid": 1,
                "create_time": 1776746400,
                "real_sender_id": 0,
                "local_type": 1,
                "message_content_hex": "wxid_a:\nA 的消息".encode().hex(),
                "compression_type": None,
            },
            {
                "rowid": 2,
                "create_time": 1776746460,
                "real_sender_id": 0,
                "local_type": 1,
                "message_content_hex": "wxid_b:\nB 的消息".encode().hex(),
                "compression_type": None,
            },
        ]

    monkeypatch.setattr(store, "_sdk_query_rows", fake_sdk_query_rows)

    messages = await store._collect_session_history(
        session_id="room-a@chatroom",
        user_id="",
        cutoff_ts=1776700800,
        max_messages=100,
    )

    assert [message["sender_wxid"] for message in messages] == ["wxid_a", "wxid_b"]
    assert [message["sender_id"] for message in messages] == ["wxid_a", "wxid_b"]
    assert [message["user_text"] for message in messages] == [
        "wxid_a: A 的消息",
        "wxid_b: B 的消息",
    ]


@pytest.mark.asyncio
async def test_backfill_from_sdk_merges_multiple_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryStore(SimpleNamespace())
    applied_sessions: list[tuple[str, list[dict]]] = []
    inserted_events: list[dict] = []

    async def fake_collect_session_history(
        *,
        session_id: str,
        user_id: str,
        cutoff_ts: int,
        max_messages: int,
        end_ts: int | None = None,
    ):
        assert user_id == "wxid_a"
        assert cutoff_ts > 0
        assert max_messages == 100
        assert end_ts is None
        return [
            {
                "session_id": session_id,
                "user_text": f"{session_id}-消息1",
                "assistant_text": "",
                "created_at": "2026-04-21 10:00:00",
                "ts": 1,
            },
            {
                "session_id": session_id,
                "user_text": f"{session_id}-消息2",
                "assistant_text": "",
                "created_at": "2026-04-21 11:00:00",
                "ts": 2,
            },
        ]

    async def fake_apply_backfill_session_messages(**kwargs):
        applied_sessions.append((kwargs["session_id"], kwargs["messages"]))
        return {"session_id": kwargs["session_id"]}

    async def fake_apply_backfill_identity_messages(**kwargs):
        assert kwargs["last_session_id"] == "room-b@chatroom"
        assert len(kwargs["messages"]) == 4
        return {"user_id": kwargs["user_id"], "imported_message_count": 4}

    async def fake_list_session_profiles(**kwargs):
        return [
            {"session_id": "room-a@chatroom", "user_id": kwargs["user_id"]},
            {"session_id": "room-b@chatroom", "user_id": kwargs["user_id"]},
        ]

    async def fake_insert_backfill_event(**kwargs):
        message = kwargs["message"]
        event = {
            "id": len(inserted_events) + 1,
            "session_id": message["session_id"],
            "trace_id": f"memory:backfill:{len(inserted_events) + 1}",
            "event_key": f"event-{len(inserted_events) + 1}",
            "user_text": message["user_text"],
        }
        inserted_events.append(event)
        return event, True

    monkeypatch.setattr(store, "_collect_session_history", fake_collect_session_history)
    monkeypatch.setattr(store, "_insert_backfill_event", fake_insert_backfill_event)
    monkeypatch.setattr(
        store, "_apply_backfill_session_messages", fake_apply_backfill_session_messages
    )
    monkeypatch.setattr(
        store, "_apply_backfill_identity_messages", fake_apply_backfill_identity_messages
    )
    monkeypatch.setattr(store, "list_session_profiles", fake_list_session_profiles)

    result = await store.backfill_from_sdk(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_ids=["room-a@chatroom", "room-b@chatroom"],
        days_limit=90,
        max_messages_per_session=100,
    )

    assert result["ok"] is True
    assert result["session_count"] == 2
    assert result["imported_count"] == 4
    assert result["events_inserted"] == 4
    assert result["events_duplicate"] == 0
    assert len(inserted_events) == 4
    assert [session["events_inserted"] for session in result["sessions"]] == [2, 2]
    assert [item[0] for item in applied_sessions] == ["room-a@chatroom", "room-b@chatroom"]


@pytest.mark.asyncio
async def test_history_sync_adapter_routes_wechat_to_existing_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    collect_calls: list[dict[str, Any]] = []

    async def fake_collect_session_history(**kwargs: Any) -> list[dict[str, Any]]:
        collect_calls.append(kwargs)
        return [
            {
                "session_id": kwargs["session_id"],
                "user_text": "sanitized adapter message",
                "assistant_text": "",
                "created_at": "2026-05-15 10:00:00",
                "ts": 1778791200,
            }
        ]

    async def fake_insert_backfill_event(**kwargs: Any) -> tuple[dict[str, Any], bool]:
        return {
            "id": 1,
            "session_id": kwargs["message"]["session_id"],
            "trace_id": "memory:backfill:adapter",
        }, True

    async def fake_apply_backfill_session_messages(**kwargs: Any) -> dict[str, Any]:
        return {"session_id": kwargs["session_id"], "user_id": kwargs["user_id"]}

    async def fake_apply_backfill_identity_messages(**kwargs: Any) -> dict[str, Any]:
        return {"user_id": kwargs["user_id"]}

    async def fake_list_session_profiles(**kwargs: Any) -> list[dict[str, Any]]:
        return [{"session_id": "room-a@chatroom", "user_id": kwargs["user_id"]}]

    monkeypatch.setattr(store, "_collect_session_history", fake_collect_session_history)
    monkeypatch.setattr(store, "_insert_backfill_event", fake_insert_backfill_event)
    monkeypatch.setattr(
        store, "_apply_backfill_session_messages", fake_apply_backfill_session_messages
    )
    monkeypatch.setattr(
        store, "_apply_backfill_identity_messages", fake_apply_backfill_identity_messages
    )
    monkeypatch.setattr(store, "list_session_profiles", fake_list_session_profiles)

    result = await store.backfill_from_sdk(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_ids=["room-a@chatroom"],
        days_limit=7,
        max_messages_per_session=20,
    )

    assert result["ok"] is True
    assert result["imported_count"] == 1
    assert len(collect_calls) == 1
    assert collect_calls[0]["session_id"] == "room-a@chatroom"
    assert collect_calls[0]["user_id"] == "wxid_a"
    assert collect_calls[0]["max_messages"] == 20
    assert collect_calls[0]["end_ts"] is None


@pytest.mark.asyncio
async def test_history_sync_adapter_rejects_unsupported_provider() -> None:
    store = MemoryStore(SimpleNamespace())

    with pytest.raises(RuntimeError, match="does not support provider/channel: slack"):
        await store.backfill_from_sdk(
            tenant_id="demo",
            channel="slack",
            source_key="slackbot",
            user_id="u1",
            session_ids=["c1"],
        )


@pytest.mark.asyncio
async def test_backfill_writes_events_items_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(
        SimpleNamespace(memory_llm_extraction_enabled=True, memory_llm_extraction_job_enabled=True),
        llm_service=object(),
    )
    events_by_key: dict[str, dict] = {}
    items_by_key: dict[str, dict] = {}
    jobs_by_key: dict[str, dict] = {}
    calls: list[tuple[str, dict | None]] = []
    next_event_id = 1
    next_item_id = 1
    next_job_id = 1

    history = [
        {
            "session_id": "room-a@chatroom",
            "user_text": "我喜欢 Adidas",
            "assistant_text": "",
            "created_at": "2026-04-21 10:00:00",
            "ts": 1776746400,
        },
        {
            "session_id": "room-a@chatroom",
            "user_text": "记住我手机号是 13800138000",
            "assistant_text": "",
            "created_at": "2026-04-21 10:05:00",
            "ts": 1776746700,
        },
    ]

    async def fake_collect_session_history(**kwargs):
        assert kwargs["max_messages"] == 100
        return list(history)

    async def fake_get_session_profile(**kwargs):
        return {
            **kwargs,
            "short_term_memory": "",
            "manual_notes": "",
            "short_term_items": [],
            "message_count": 0,
            "imported_message_count": 0,
        }

    async def fake_get_identity_profile(**kwargs):
        return {
            **kwargs,
            "long_term_memory": "",
            "manual_notes": "",
            "long_term_items": [],
            "message_count": 0,
            "imported_message_count": 0,
        }

    async def fake_list_memory_items(**kwargs):
        return []

    async def fake_import_legacy_identity_items(profile):
        return None

    async def fake_refresh(item):
        return None

    async def fake_get_memory_item(item_id: int):
        for item in items_by_key.values():
            if item["id"] == item_id:
                return dict(item)
        return None

    async def fake_list_session_profiles(**kwargs):
        return []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        nonlocal next_event_id, next_item_id, next_job_id
        calls.append((sql, params))
        params = params or {}
        if "INSERT INTO plugin_memory_event" in sql:
            key = params["event_key"]
            if key in events_by_key:
                return []
            row = {
                "id": next_event_id,
                "tenant_id": params["tid"],
                "channel": params["channel"],
                "source_key": params["source_key"],
                "user_id": params["uid"],
                "session_id": params["sid"],
                "user_text": params["user_text"],
                "assistant_text": params["assistant_text"],
                "trace_id": params["trace"],
                "event_key": key,
                "created_at": params["created_at"],
            }
            next_event_id += 1
            events_by_key[key] = row
            return [dict(row)]
        if "FROM plugin_memory_event WHERE event_key" in sql:
            event = events_by_key.get(params["event_key"])
            return [dict(event)] if event else []
        if "SELECT id FROM plugin_memory_item" in sql:
            key = params["normalized_key"]
            item = items_by_key.get(key)
            return [{"id": item["id"]}] if item else []
        if "FROM plugin_memory_item" in sql and "normalized_key = :normalized_key" in sql:
            item = items_by_key.get(params["normalized_key"])
            return [dict(item)] if item else []
        if "INSERT INTO plugin_memory_item" in sql:
            row = {
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
            items_by_key[row["normalized_key"]] = row
            return [dict(row)]
        if "UPDATE plugin_memory_item SET" in sql:
            item = next(item for item in items_by_key.values() if item["id"] == params["id"])
            item["occurrence_count"] += 1
            item.update(
                {
                    "content": params["content"],
                    "value_json": params["value_json"],
                    "memory_type": params["memory_type"],
                    "confidence": max(float(item["confidence"]), float(params["confidence"])),
                    "status": params.get("status", item["status"]),
                    "sensitivity": params["sensitivity"],
                    "source_event_id": item["source_event_id"] or params["source_event_id"],
                    "source_trace_id": item["source_trace_id"] or params["source_trace_id"],
                    "original_text": item["original_text"] or params["original_text"],
                }
            )
            return []
        if "INSERT INTO plugin_memory_extraction_job" in sql:
            key = params["idempotency_key"]
            if key not in jobs_by_key:
                jobs_by_key[key] = {"id": next_job_id, "status": "pending", "attempts": 0, **params}
                next_job_id += 1
            return [dict(jobs_by_key[key])]
        return []

    monkeypatch.setattr(store, "_collect_session_history", fake_collect_session_history)
    monkeypatch.setattr(store, "get_session_profile", fake_get_session_profile)
    monkeypatch.setattr(store, "get_identity_profile", fake_get_identity_profile)
    monkeypatch.setattr(store, "list_memory_items", fake_list_memory_items)
    monkeypatch.setattr(store, "_import_legacy_identity_items", fake_import_legacy_identity_items)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", fake_refresh)
    monkeypatch.setattr(store, "get_memory_item", fake_get_memory_item)
    monkeypatch.setattr(store, "list_session_profiles", fake_list_session_profiles)
    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    first = await store.backfill_from_sdk(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_ids=["room-a@chatroom"],
        days_limit=90,
        max_messages_per_session=100,
        enqueue_llm_jobs=True,
    )
    second = await store.backfill_from_sdk(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_ids=["room-a@chatroom"],
        days_limit=90,
        max_messages_per_session=100,
        enqueue_llm_jobs=True,
    )

    assert first["processed_count"] == 2
    assert first["imported_count"] == 2
    assert first["events_inserted"] == 2
    assert first["events_duplicate"] == 0
    assert first["items_created"] == 2
    assert first["items_updated"] == 0
    assert first["items_pending"] == 1
    assert first["jobs_enqueued"] == 2
    assert second["processed_count"] == 2
    assert second["imported_count"] == 0
    assert second["events_inserted"] == 0
    assert second["events_duplicate"] == 2
    assert second["items_created"] == 0
    assert second["items_updated"] == 0
    assert second["jobs_enqueued"] == 0
    assert len(events_by_key) == 2
    assert len(items_by_key) == 2
    assert len(jobs_by_key) == 2
    assert {item["source_type"] for item in items_by_key.values()} == {"backfill"}
    pii_items = [item for item in items_by_key.values() if item["sensitivity"] == "pii"]
    assert len(pii_items) == 1
    assert pii_items[0]["status"] == "pending"
    assert _detect_sensitivity(pii_items[0]["content"]) == "pii"


@pytest.mark.asyncio
async def test_backfill_sanitizes_nul_text_before_event_item_and_job_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(
        SimpleNamespace(memory_llm_extraction_enabled=True, memory_llm_extraction_job_enabled=True),
        llm_service=object(),
    )
    events_by_key: dict[str, dict[str, Any]] = {}
    items_by_key: dict[str, dict[str, Any]] = {}
    jobs_by_key: dict[str, dict[str, Any]] = {}
    next_event_id = 1
    next_item_id = 1
    next_job_id = 1

    async def fake_collect_session_history(**kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "session_id": "room-a@chatroom",
                "user_text": "我喜欢 Ni\x00ke",
                "assistant_text": "ok\x00",
                "created_at": "2026-04-21 10:00:00",
                "ts": 1776746400,
            }
        ]

    async def fake_get_session_profile(**kwargs: Any) -> dict[str, Any]:
        return {
            **kwargs,
            "short_term_memory": "",
            "manual_notes": "",
            "short_term_items": [],
            "message_count": 0,
            "imported_message_count": 0,
        }

    async def fake_get_identity_profile(**kwargs: Any) -> dict[str, Any]:
        return {
            **kwargs,
            "long_term_memory": "",
            "manual_notes": "",
            "long_term_items": [],
            "message_count": 0,
            "imported_message_count": 0,
        }

    async def fake_list_memory_items(**kwargs: Any) -> list[dict[str, Any]]:
        return []

    async def fake_import_legacy_identity_items(profile: dict[str, Any]) -> None:
        return None

    async def fake_refresh(item: dict[str, Any]) -> None:
        return None

    async def fake_get_memory_item(item_id: int) -> dict[str, Any] | None:
        for item in items_by_key.values():
            if int(item["id"]) == item_id:
                return dict(item)
        return None

    async def fake_list_session_profiles(**kwargs: Any) -> list[dict[str, Any]]:
        return []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        nonlocal next_event_id, next_item_id, next_job_id
        params = params or {}
        for value in params.values():
            if isinstance(value, str):
                assert "\x00" not in value
        if "INSERT INTO plugin_memory_event" in sql:
            key = str(params["event_key"])
            row = {
                "id": next_event_id,
                "tenant_id": params["tid"],
                "channel": params["channel"],
                "source_key": params["source_key"],
                "user_id": params["uid"],
                "session_id": params["sid"],
                "user_text": params["user_text"],
                "assistant_text": params["assistant_text"],
                "trace_id": params["trace"],
                "event_key": key,
                "created_at": params["created_at"],
            }
            next_event_id += 1
            events_by_key[key] = row
            return [dict(row)]
        if "FROM plugin_memory_event WHERE event_key" in sql:
            event = events_by_key.get(str(params["event_key"]))
            return [dict(event)] if event else []
        if "SELECT id FROM plugin_memory_item" in sql:
            item = items_by_key.get(str(params["normalized_key"]))
            return [{"id": item["id"]}] if item else []
        if "FROM plugin_memory_item" in sql and "normalized_key = :normalized_key" in sql:
            item = items_by_key.get(str(params["normalized_key"]))
            return [dict(item)] if item else []
        if "INSERT INTO plugin_memory_item" in sql:
            row = {
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
            items_by_key[row["normalized_key"]] = row
            return [dict(row)]
        if "INSERT INTO plugin_memory_extraction_job" in sql:
            key = str(params["idempotency_key"])
            jobs_by_key[key] = {"id": next_job_id, "status": "pending", "attempts": 0, **params}
            next_job_id += 1
            return [dict(jobs_by_key[key])]
        return []

    monkeypatch.setattr(store, "_collect_session_history", fake_collect_session_history)
    monkeypatch.setattr(store, "get_session_profile", fake_get_session_profile)
    monkeypatch.setattr(store, "get_identity_profile", fake_get_identity_profile)
    monkeypatch.setattr(store, "list_memory_items", fake_list_memory_items)
    monkeypatch.setattr(store, "_import_legacy_identity_items", fake_import_legacy_identity_items)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", fake_refresh)
    monkeypatch.setattr(store, "get_memory_item", fake_get_memory_item)
    monkeypatch.setattr(store, "list_session_profiles", fake_list_session_profiles)
    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    result = await store.backfill_from_sdk(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_ids=["room-a@chatroom"],
        days_limit=90,
        max_messages_per_session=100,
        enqueue_llm_jobs=True,
    )

    assert result["events_inserted"] == 1
    assert result["items_created"] == 1
    assert result["jobs_enqueued"] == 1
    event = next(iter(events_by_key.values()))
    assert event["user_text"] == "我喜欢 Nike"
    assert event["assistant_text"] == "ok"
    item = next(iter(items_by_key.values()))
    assert item["content"] == "用户喜欢 Nike"
    assert item["original_text"] == "我喜欢 Nike"
    assert "\x00" not in item["value_json"]


@pytest.mark.asyncio
async def test_backfill_llm_jobs_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryStore(
        SimpleNamespace(memory_llm_extraction_job_enabled=True), llm_service=object()
    )
    enqueued = []

    async def fake_collect_session_history(**kwargs):
        return [
            {
                "session_id": "room-a@chatroom",
                "user_text": "我喜欢 Nike",
                "assistant_text": "",
                "created_at": "2026-04-21 10:00:00",
                "ts": 1776746400,
            }
        ]

    async def fake_insert_backfill_event(**kwargs):
        return (
            {
                "id": 1,
                "session_id": "room-a@chatroom",
                "trace_id": "memory:backfill:test",
                "user_text": "我喜欢 Nike",
            },
            True,
        )

    async def fake_apply_session(**kwargs):
        return {"session_id": kwargs["session_id"]}

    async def fake_apply_identity(**kwargs):
        return {"user_id": kwargs["user_id"]}

    async def fake_apply_action(**kwargs):
        return {
            "id": 1,
            "occurrence_count": 1,
            "status": "active",
            "scope_type": "identity",
            "tenant_id": kwargs["tenant_id"],
            "channel": kwargs["channel"],
            "source_key": kwargs["source_key"],
            "user_id": kwargs["user_id"],
            "session_id": "",
        }

    async def fake_enqueue(**kwargs):
        enqueued.append(kwargs)
        return {"id": 1, **kwargs}

    monkeypatch.setattr(store, "_collect_session_history", fake_collect_session_history)
    monkeypatch.setattr(store, "_insert_backfill_event", fake_insert_backfill_event)
    monkeypatch.setattr(store, "_apply_backfill_session_messages", fake_apply_session)
    monkeypatch.setattr(store, "_apply_backfill_identity_messages", fake_apply_identity)
    monkeypatch.setattr(store, "_apply_structured_memory_action", fake_apply_action)
    monkeypatch.setattr(store, "enqueue_llm_extraction_job", fake_enqueue)

    async def fake_list_session_profiles(**kwargs):
        return []

    monkeypatch.setattr(store, "list_session_profiles", fake_list_session_profiles)

    result = await store.backfill_from_sdk(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_ids=["room-a@chatroom"],
    )

    assert result["events_inserted"] == 1
    assert result["jobs_enqueued"] == 0
    assert result["llm_jobs_enabled"] is False
    assert enqueued == []


@pytest.mark.parametrize(
    ("settings_kwargs", "llm_service", "expected_enabled"),
    [
        pytest.param(
            {
                "memory_llm_extraction_enabled": True,
                "memory_graph_llm_extraction_enabled": False,
                "memory_llm_extraction_job_enabled": True,
            },
            object(),
            True,
            id="structured-only",
        ),
        pytest.param(
            {
                "memory_llm_extraction_enabled": False,
                "memory_graph_llm_extraction_enabled": True,
                "memory_llm_extraction_job_enabled": True,
            },
            object(),
            True,
            id="graph-only",
        ),
        pytest.param(
            {
                "memory_llm_extraction_enabled": False,
                "memory_graph_llm_extraction_enabled": False,
                "memory_llm_extraction_job_enabled": True,
            },
            object(),
            False,
            id="extractors-disabled",
        ),
        pytest.param(
            {
                "memory_llm_extraction_enabled": True,
                "memory_graph_llm_extraction_enabled": True,
                "memory_llm_extraction_job_enabled": True,
            },
            None,
            False,
            id="no-llm-service",
        ),
        pytest.param(
            {
                "memory_llm_extraction_enabled": True,
                "memory_graph_llm_extraction_enabled": True,
                "memory_llm_extraction_job_enabled": False,
            },
            object(),
            False,
            id="job-setting-disabled",
        ),
    ],
)
@pytest.mark.asyncio
async def test_backfill_llm_job_enqueue_eligibility_matches_available_extractors(
    monkeypatch: pytest.MonkeyPatch,
    settings_kwargs: dict,
    llm_service: object | None,
    expected_enabled: bool,
) -> None:
    store = MemoryStore(SimpleNamespace(**settings_kwargs), llm_service=llm_service)
    enqueued: list[dict] = []

    async def fake_collect_session_history(**kwargs):
        return [
            {
                "session_id": "room-a@chatroom",
                "user_text": "hello there",
                "assistant_text": "",
                "created_at": "2026-04-21 10:00:00",
                "ts": 1776746400,
            }
        ]

    async def fake_insert_backfill_event(**kwargs):
        return (
            {
                "id": 1,
                "session_id": "room-a@chatroom",
                "trace_id": "memory:backfill:test",
                "user_text": "hello there",
            },
            True,
        )

    async def fake_apply_session(**kwargs):
        return {"session_id": kwargs["session_id"]}

    async def fake_apply_identity(**kwargs):
        return {"user_id": kwargs["user_id"]}

    async def fake_list_session_profiles(**kwargs):
        return []

    async def fake_enqueue(**kwargs):
        enqueued.append(kwargs)
        return {"id": 1, **kwargs}

    monkeypatch.setattr(store, "_collect_session_history", fake_collect_session_history)
    monkeypatch.setattr(store, "_insert_backfill_event", fake_insert_backfill_event)
    monkeypatch.setattr(store, "_apply_backfill_session_messages", fake_apply_session)
    monkeypatch.setattr(store, "_apply_backfill_identity_messages", fake_apply_identity)
    monkeypatch.setattr(store, "list_session_profiles", fake_list_session_profiles)
    monkeypatch.setattr(store, "enqueue_llm_extraction_job", fake_enqueue)

    result = await store.backfill_from_sdk(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_ids=["room-a@chatroom"],
        enqueue_llm_jobs=True,
    )

    assert result["events_inserted"] == 1
    assert result["llm_jobs_enabled"] is expected_enabled
    assert result["jobs_enqueued"] == (1 if expected_enabled else 0)
    assert len(enqueued) == (1 if expected_enabled else 0)


@pytest.mark.asyncio
async def test_target_date_backfill_uses_single_day_window(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryStore(SimpleNamespace())
    collect_calls: list[dict] = []

    async def fake_collect_session_history(**kwargs):
        collect_calls.append(kwargs)
        return [
            {
                "session_id": kwargs["session_id"],
                "user_text": "单日消息",
                "assistant_text": "",
                "created_at": "2026-05-15 12:00:00",
                "ts": kwargs["cutoff_ts"] + 3600,
            }
        ]

    async def fake_insert_backfill_event(**kwargs):
        message = kwargs["message"]
        return (
            {
                "id": 1,
                "session_id": message["session_id"],
                "trace_id": "memory:backfill:test",
                "user_text": message["user_text"],
            },
            True,
        )

    async def fake_apply_session(**kwargs):
        return {"session_id": kwargs["session_id"]}

    async def fake_apply_identity(**kwargs):
        return {"user_id": kwargs["user_id"]}

    async def fake_list_session_profiles(**kwargs):
        return []

    monkeypatch.setattr(store, "_collect_session_history", fake_collect_session_history)
    monkeypatch.setattr(store, "_insert_backfill_event", fake_insert_backfill_event)
    monkeypatch.setattr(store, "_apply_backfill_session_messages", fake_apply_session)
    monkeypatch.setattr(store, "_apply_backfill_identity_messages", fake_apply_identity)
    monkeypatch.setattr(store, "list_session_profiles", fake_list_session_profiles)

    result = await store.backfill_from_sdk(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_ids=["room-a@chatroom"],
        days_limit=90,
        max_messages_per_session=10,
        target_date="2026-05-15",
    )

    assert result["target_date"] == "2026-05-15"
    assert result["max_messages_per_session"] == 10000
    assert result["imported_count"] == 1
    assert collect_calls[0]["max_messages"] == 10000
    assert collect_calls[0]["end_ts"] - collect_calls[0]["cutoff_ts"] == 86400


@pytest.mark.asyncio
async def test_group_backfill_without_user_id_uses_group_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    collect_calls: list[dict] = []
    event_user_ids: list[str] = []

    async def fake_collect_session_history(**kwargs):
        collect_calls.append(kwargs)
        return [
            {
                "session_id": kwargs["session_id"],
                "user_text": "wxid_a: 群聊消息",
                "assistant_text": "",
                "created_at": "2026-05-15 12:00:00",
                "ts": kwargs["cutoff_ts"] + 3600,
                "sender_wxid": "wxid_a",
            }
        ]

    async def fake_insert_backfill_event(**kwargs):
        event_user_ids.append(kwargs["user_id"])
        message = kwargs["message"]
        return (
            {
                "id": 1,
                "session_id": message["session_id"],
                "trace_id": "memory:backfill:test",
                "user_text": message["user_text"],
            },
            True,
        )

    async def fake_apply_session(**kwargs):
        assert kwargs["user_id"] == "__group__"
        return {"session_id": kwargs["session_id"], "user_id": kwargs["user_id"]}

    async def fake_apply_identity(**kwargs):
        assert kwargs["user_id"] == "__group__"
        return {"user_id": kwargs["user_id"]}

    async def fake_list_session_profiles(**kwargs):
        assert kwargs["user_id"] == "__group__"
        return [{"session_id": "room-a@chatroom", "user_id": kwargs["user_id"]}]

    monkeypatch.setattr(store, "_collect_session_history", fake_collect_session_history)
    monkeypatch.setattr(store, "_insert_backfill_event", fake_insert_backfill_event)
    monkeypatch.setattr(store, "_apply_backfill_session_messages", fake_apply_session)
    monkeypatch.setattr(store, "_apply_backfill_identity_messages", fake_apply_identity)
    monkeypatch.setattr(store, "list_session_profiles", fake_list_session_profiles)

    result = await store.backfill_from_sdk(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="",
        session_ids=["room-a@chatroom"],
        target_date="2026-05-15",
    )

    assert result["user_id"] == "__group__"
    assert result["user_id_scope"] == "__group__"
    assert result["user_id_auto"] is True
    assert result["imported_count"] == 1
    assert collect_calls[0]["user_id"] == "__group__"
    assert event_user_ids == ["__group__"]


@pytest.mark.asyncio
async def test_group_backfill_closed_loop_persists_group_scoped_events_items_jobs_and_dedupes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(
        SimpleNamespace(memory_llm_extraction_enabled=True, memory_llm_extraction_job_enabled=True),
        llm_service=object(),
    )
    events_by_key: dict[str, dict[str, Any]] = {}
    items_by_id: dict[int, dict[str, Any]] = {}
    items_by_scope_key: dict[tuple[str, str, str, str, str, str, str], int] = {}
    jobs_by_key: dict[str, dict[str, Any]] = {}
    next_event_id = 1
    next_item_id = 1
    next_job_id = 1

    async def fake_collect_session_history(**kwargs: Any) -> list[dict[str, Any]]:
        assert kwargs["session_id"] == "room-a@chatroom"
        assert kwargs["user_id"] == "__group__"
        assert kwargs["max_messages"] == 10000
        return [
            {
                "session_id": kwargs["session_id"],
                "user_text": "成员甲: 我喜欢 ExampleBrand",
                "assistant_text": "",
                "created_at": "2026-05-15 09:30:00",
                "ts": 1778779800,
                "sender_wxid": "wxid_member_a",
            },
            {
                "session_id": kwargs["session_id"],
                "user_text": "成员乙: 我喜欢 ExampleBrand",
                "assistant_text": "",
                "created_at": "2026-05-15 09:35:00",
                "ts": 1778780100,
                "sender_wxid": "wxid_member_b",
            },
        ]

    async def fake_apply_session(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["user_id"] == "__group__"
        return {"session_id": kwargs["session_id"], "user_id": kwargs["user_id"]}

    async def fake_apply_identity(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["user_id"] == "__group__"
        return {"user_id": kwargs["user_id"]}

    async def fake_list_session_profiles(**kwargs: Any) -> list[dict[str, Any]]:
        assert kwargs["user_id"] == "__group__"
        return [{"session_id": "room-a@chatroom", "user_id": "__group__"}]

    async def fake_sync_graph(item: dict[str, Any] | None) -> None:
        return None

    async def fake_sync_vector(item: dict[str, Any] | None) -> None:
        return None

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        nonlocal next_event_id, next_item_id, next_job_id
        params = params or {}
        if "INSERT INTO plugin_memory_event" in sql:
            key = str(params["event_key"])
            if key in events_by_key:
                return []
            row = {
                "id": next_event_id,
                "tenant_id": params["tid"],
                "channel": params["channel"],
                "source_key": params["source_key"],
                "user_id": params["uid"],
                "session_id": params["sid"],
                "user_text": params["user_text"],
                "assistant_text": params["assistant_text"],
                "trace_id": params["trace"],
                "event_key": key,
                "created_at": params["created_at"],
            }
            next_event_id += 1
            events_by_key[key] = row
            return [dict(row)]
        if "FROM plugin_memory_event WHERE event_key" in sql:
            event = events_by_key.get(str(params["event_key"]))
            return [dict(event)] if event else []
        if (
            "SELECT id, tenant_id, channel, source_key, user_id, session_id, scope_type, source_type"
            in sql
        ):
            if "FROM plugin_memory_item WHERE id = :id" in sql:
                item = items_by_id.get(int(params["id"]))
                return [dict(item)] if item else []
            normalized_key = str(params.get("normalized_key") or "")
            rows = [
                item
                for item in items_by_id.values()
                if item["tenant_id"] == params.get("tid")
                and item["channel"] == params.get("channel")
                and item["source_key"] == params.get("source_key")
                and item["user_id"] == params.get("uid")
                and item["scope_type"] == params.get("scope_type")
                and item["session_id"] == params.get("sid")
                and item["normalized_key"] == normalized_key
                and item.get("deleted_at") is None
            ]
            status_filter = {
                value for key, value in params.items() if str(key).startswith("status_")
            }
            if status_filter:
                rows = [item for item in rows if item["status"] in status_filter]
            return [dict(item) for item in rows[: int(params.get("lim") or len(rows) or 1)]]
        if sql.startswith("SELECT id, audience_scope"):
            return [
                {
                    "id": item["id"],
                    "audience_scope": item["audience_scope"],
                    "origin_session_kind": item["origin_session_kind"],
                    "allowed_session_ids": item["allowed_session_ids"],
                    "sensitivity": item["sensitivity"],
                    "sensitivity_category": item["sensitivity_category"],
                    "expires_at": item["expires_at"],
                }
                for item in items_by_id.values()
                if item["tenant_id"] == params.get("tid")
                and item["channel"] == params.get("channel")
                and item["source_key"] == params.get("source_key")
                and item["user_id"] == params.get("uid")
                and item["scope_type"] == params.get("scope_type")
                and item["session_id"] == params.get("sid")
                and item["source_type"] == params.get("source_type")
                and item["normalized_key"] == params.get("normalized_key")
                and item.get("deleted_at") is None
            ]
        if "SELECT id FROM plugin_memory_item" in sql:
            scope_key = (
                str(params["tid"]),
                str(params["channel"]),
                str(params["source_key"]),
                str(params["uid"]),
                str(params["scope_type"]),
                str(params["sid"]),
                str(params["normalized_key"]),
            )
            item_id = items_by_scope_key.get(scope_key)
            return [{"id": item_id}] if item_id is not None else []
        if "INSERT INTO plugin_memory_item" in sql:
            row = {
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
                "audience_scope": params["audience_scope"],
                "origin_session_kind": params["origin_session_kind"],
                "allowed_session_ids": json.loads(params["allowed_session_ids"]),
                "source_kind": params["source_kind"],
                "sensitivity_category": params["sensitivity_category"],
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
            items_by_id[next_item_id] = row
            items_by_scope_key[
                (
                    row["tenant_id"],
                    row["channel"],
                    row["source_key"],
                    row["user_id"],
                    row["scope_type"],
                    row["session_id"],
                    row["normalized_key"],
                )
            ] = next_item_id
            next_item_id += 1
            return [dict(row)]
        if "UPDATE plugin_memory_item SET" in sql:
            item = items_by_id.get(int(params["id"]))
            if item is None:
                return []
            item.update(
                {
                    "content": params.get("content", item["content"]),
                    "value_json": params.get("value_json", item["value_json"]),
                    "memory_type": params.get("memory_type", item["memory_type"]),
                    "confidence": max(
                        float(item["confidence"]), float(params.get("confidence") or 0.0)
                    ),
                    "status": params.get("status", item["status"]),
                    "sensitivity": params.get("sensitivity", item["sensitivity"]),
                    "source_event_id": item["source_event_id"] or params.get("source_event_id"),
                    "source_trace_id": item["source_trace_id"]
                    or params.get("source_trace_id")
                    or "",
                    "original_text": item["original_text"] or params.get("original_text") or "",
                    "occurrence_count": int(item.get("occurrence_count") or 1) + 1,
                }
            )
            return []
        if "INSERT INTO plugin_memory_extraction_job" in sql:
            key = str(params["idempotency_key"])
            if key not in jobs_by_key:
                jobs_by_key[key] = {
                    "id": next_job_id,
                    "tenant_id": params["tid"],
                    "channel": params["channel"],
                    "source_key": params["source_key"],
                    "user_id": params["uid"],
                    "session_id": params["sid"],
                    "source_event_id": params["source_event_id"],
                    "source_trace_id": params["trace"],
                    "status": "pending",
                    "attempts": 0,
                    "max_attempts": params["max_attempts"],
                    "next_run_at": None,
                    "locked_until": None,
                    "locked_by": "",
                    "last_error": "",
                    "result_json": None,
                    "idempotency_key": key,
                    "created_at": None,
                    "updated_at": None,
                }
                next_job_id += 1
            return [dict(jobs_by_key[key])]
        return []

    monkeypatch.setattr(store, "_collect_session_history", fake_collect_session_history)
    monkeypatch.setattr(store, "_apply_backfill_session_messages", fake_apply_session)
    monkeypatch.setattr(store, "_apply_backfill_identity_messages", fake_apply_identity)
    monkeypatch.setattr(store, "list_session_profiles", fake_list_session_profiles)
    monkeypatch.setattr(store, "_sync_memory_graph_for_item_safe", fake_sync_graph)
    monkeypatch.setattr(store, "_sync_memory_vector_for_item_safe", fake_sync_vector)
    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    first = await store.backfill_from_sdk(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="",
        session_ids=["room-a@chatroom"],
        target_date="2026-05-15",
        enqueue_llm_jobs=True,
    )
    second = await store.backfill_from_sdk(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="",
        session_ids=["room-a@chatroom"],
        target_date="2026-05-15",
        enqueue_llm_jobs=True,
    )

    assert first["user_id_scope"] == "__group__"
    assert first["user_id_auto"] is True
    assert first["events_inserted"] == 2
    assert first["items_created"] == 1
    assert first["jobs_enqueued"] == 2
    assert second["events_inserted"] == 0
    assert second["events_duplicate"] == 2
    assert second["items_created"] == 0
    assert second["jobs_enqueued"] == 0
    assert len(events_by_key) == 2
    assert len(items_by_id) == 1
    assert len(jobs_by_key) == 2
    assert {event["user_id"] for event in events_by_key.values()} == {"__group__"}
    assert {item["user_id"] for item in items_by_id.values()} == {"__group__"}
    assert {item["source_type"] for item in items_by_id.values()} == {"backfill"}
    assert {job["user_id"] for job in jobs_by_key.values()} == {"__group__"}
    assert {job["session_id"] for job in jobs_by_key.values()} == {"room-a@chatroom"}


@pytest.mark.asyncio
async def test_private_backfill_without_user_id_still_requires_user_id() -> None:
    store = MemoryStore(SimpleNamespace())

    with pytest.raises(RuntimeError, match="user_id required"):
        await store.backfill_from_sdk(
            tenant_id="demo",
            channel="wechat",
            source_key="wxbot",
            user_id="",
            session_ids=["wxid_private"],
        )


@pytest.mark.asyncio
async def test_group_graph_history_dates_returns_counts_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    collect_calls: list[dict] = []

    async def fake_collect_session_history(**kwargs):
        collect_calls.append(kwargs)
        if len(collect_calls) == 1:
            return [
                {"user_text": "sanitized-message-a"},
                {"user_text": "sanitized-message-b"},
            ]
        if len(collect_calls) == 2:
            return [{"user_text": "sanitized-message-c"}]
        return []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "FROM plugin_memory_extraction_job" in sql:
            if len(collect_calls) == 1:
                return [{"status": "pending", "count": 1}, {"status": "succeeded", "count": 2}]
            if len(collect_calls) == 2:
                return [{"status": "failed", "count": 1}]
            return []
        if len(collect_calls) == 1:
            return [{"count": 1}]
        if len(collect_calls) == 2:
            return [{"count": 1}]
        return [{"count": 0}]

    monkeypatch.setattr(store, "_collect_session_history", fake_collect_session_history)
    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    result = await store.get_group_graph_history_dates(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="room-a@chatroom",
        user_id="",
        recent_days=3,
    )

    assert result["user_id"] == "__group__"
    assert result["user_id_scope"] == "__group__"
    assert result["user_id_auto"] is True
    assert result["items"][0]["raw_message_count"] == 2
    assert result["items"][0]["imported_count"] == 1
    assert result["items"][0]["job_counts"] == {
        "pending": 1,
        "running": 0,
        "succeeded": 2,
        "failed": 0,
        "dead": 0,
    }
    assert result["items"][0]["status"] == "partial"
    assert result["items"][1]["raw_message_count"] == 1
    assert result["items"][1]["imported_count"] == 1
    assert result["items"][1]["job_counts"]["failed"] == 1
    assert result["items"][1]["status"] == "extracted"
    assert result["items"][2]["raw_message_count"] == 0
    assert result["items"][2]["imported_count"] == 0
    assert result["items"][2]["status"] == "not_extracted"
    assert "sanitized-message" not in str(result)
    assert all(call["user_id"] == "__group__" for call in collect_calls)
    assert all(call["end_ts"] - call["cutoff_ts"] == 86400 for call in collect_calls)


@pytest.mark.asyncio
async def test_selected_day_extraction_claim_scopes_jobs_by_event_date_and_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(
        SimpleNamespace(
            memory_llm_extraction_job_enabled=True,
            memory_graph_llm_extraction_enabled=True,
        ),
        llm_service=object(),
    )
    start_at = datetime(2026, 5, 15)
    end_at = datetime(2026, 5, 16)
    seen: dict[str, Any] = {}

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        seen["sql"] = sql
        seen["params"] = params or {}
        return [
            {
                "id": 42,
                "tenant_id": params["tid"],
                "channel": params["channel"],
                "source_key": params["source_key"],
                "user_id": params["uid"],
                "session_id": params["sid"],
                "source_event_id": 501,
                "source_trace_id": "trace-501",
                "status": "running",
                "attempts": 0,
                "max_attempts": 3,
                "next_run_at": None,
                "locked_until": None,
                "locked_by": params["locked_by"],
                "last_error": "",
                "result_json": {},
                "idempotency_key": "job-501",
                "created_at": None,
                "updated_at": None,
            }
        ]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    jobs = await store.claim_llm_extraction_jobs_for_day(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="__group__",
        session_id="room-a@chatroom",
        start_at=start_at,
        end_at=end_at,
        limit=99,
        worker_id="selected-day-worker",
    )

    assert jobs[0]["id"] == 42
    assert seen["params"]["tid"] == "demo"
    assert seen["params"]["channel"] == "wechat"
    assert seen["params"]["source_key"] == "wxbot"
    assert seen["params"]["uid"] == "__group__"
    assert seen["params"]["sid"] == "room-a@chatroom"
    assert seen["params"]["start_at"] == start_at
    assert seen["params"]["end_at"] == end_at
    assert seen["params"]["limit"] == 99
    sql = seen["sql"]
    assert "job.tenant_id = :tid" in sql
    assert "job.channel = :channel" in sql
    assert "job.source_key = :source_key" in sql
    assert "job.user_id = :uid AND job.session_id = :sid" in sql
    assert "created_at >= :start_at AND created_at < :end_at" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql


def test_memory_schema_consistency_guard_covers_alembic_migrations() -> None:
    store_path = Path(memory_store_module.__file__)
    migrations_dir = store_path.resolve().parents[2] / "migrations" / "versions"

    for table, spec in MEMORY_DDL_CONSISTENCY_GUARD.items():
        migration_sources: list[str] = []
        for migration_name in spec["migrations"]:
            migration_source = (migrations_dir / migration_name).read_text(encoding="utf-8")
            migration_sources.append(migration_source)
            assert table in migration_source, f"{table} missing from {migration_name}"
            assert any(column in migration_source for column in spec["columns"]), (
                f"{migration_name} has no guarded columns for {table}"
            )
        combined_source = "\n".join(migration_sources)
        for column in spec["columns"]:
            assert column in combined_source, f"{table}.{column} missing from Alembic schema"
        for index in spec["indexes"]:
            assert index in combined_source, f"{index} missing from Alembic schema"
