from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import plugins.memory.store as memory_store_module
from app.common.types import ChatResponse
from plugins.memory.store import MemoryStore, _semantic_key
from plugins.memory.structured_extractor import MemoryStructuredExtractor


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


class _LLM:
    def __init__(
        self, content: str | None = None, *, exc: Exception | None = None, delay: float = 0.0
    ) -> None:
        self.content = content or "{}"
        self.exc = exc
        self.delay = delay
        self.requests = []

    async def chat(self, request):
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        return ChatResponse(content=self.content)


def _settings(**kwargs):
    data = {
        "memory_llm_extraction_enabled": True,
        "memory_llm_extraction_timeout_seconds": 0.2,
        "memory_llm_extraction_max_actions": 4,
        "memory_llm_extraction_min_confidence": 0.75,
    }
    data.update(kwargs)
    return SimpleNamespace(**data)


async def _extract(content: str, **settings_kwargs):
    extractor = MemoryStructuredExtractor(
        settings=_settings(**settings_kwargs),
        llm_service=_LLM(content),
        deterministic_extractor=lambda text: [
            {
                "op": "add",
                "content": "deterministic",
                "memory_type": "note",
                "normalized_key": "note:text:deterministic",
                "confidence": 0.9,
                "sensitivity": "normal",
                "status": "active",
                "reason": "fallback",
            }
        ],
    )
    return await extractor.extract_actions(
        tenant_id="demo",
        trace_id="trace",
        user_text="记住我喜欢 Adidas",
        assistant_text="好的",
        existing_items_summary="none",
    )


@pytest.mark.asyncio
async def test_llm_structured_extractor_is_disabled_by_default() -> None:
    llm = _LLM(
        json.dumps(
            {
                "actions": [
                    {
                        "op": "add",
                        "memory_type": "preference",
                        "content": "llm",
                        "normalized_key": "preference:text:llm",
                        "confidence": 0.99,
                        "sensitivity": "normal",
                        "reason": "would be ignored",
                    }
                ]
            }
        )
    )
    extractor = MemoryStructuredExtractor(
        settings=SimpleNamespace(),
        llm_service=llm,
        deterministic_extractor=lambda text: [
            {
                "op": "add",
                "content": "deterministic",
                "memory_type": "note",
                "normalized_key": "note:text:deterministic",
                "confidence": 0.9,
                "sensitivity": "normal",
                "status": "active",
                "reason": "fallback",
            }
        ],
    )

    actions = await extractor.extract_actions(
        tenant_id="demo",
        trace_id="trace",
        user_text="记住我喜欢 Adidas",
        assistant_text="好的",
        existing_items_summary="none",
    )

    assert actions[0]["content"] == "deterministic"
    assert llm.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["add", "update", "invalidate", "ignore"])
async def test_llm_structured_extractor_accepts_core_ops(op: str) -> None:
    key = _semantic_key("preference", "brand", "adidas")
    payload = {
        "actions": [
            {
                "op": op,
                "memory_type": "preference",
                "content": "用户喜欢 Adidas",
                "normalized_key": key,
                "confidence": 0.92,
                "sensitivity": "normal",
                "reason": "explicit preference",
                "invalidates_normalized_key": key if op == "invalidate" else None,
                "target_item_id": 3 if op == "invalidate" else None,
            }
        ]
    }

    actions = await _extract(json.dumps(payload, ensure_ascii=False))

    assert len(actions) == 1
    assert actions[0]["op"] == op
    assert actions[0]["normalized_key"] == key
    assert actions[0]["status"] == "active"
    if op == "invalidate":
        assert actions[0]["target_item_id"] == 3


@pytest.mark.asyncio
async def test_llm_structured_extractor_malformed_json_falls_back() -> None:
    actions = await _extract("not json")

    assert actions[0]["content"] == "deterministic"


@pytest.mark.asyncio
async def test_llm_structured_extractor_parses_fenced_or_embedded_json() -> None:
    payload = {
        "actions": [
            {
                "op": "add",
                "memory_type": "preference",
                "content": "用户喜欢 Adidas",
                "normalized_key": "preference:品牌:阿迪达斯",
                "confidence": 0.92,
                "sensitivity": "normal",
                "reason": "explicit preference",
                "invalidates_normalized_key": "旧偏好:品牌:耐克",
            }
        ]
    }

    fenced_actions = await _extract("```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```")
    embedded_actions = await _extract("memory result:\n" + json.dumps(payload, ensure_ascii=False))

    assert fenced_actions[0]["normalized_key"] == "preference:品牌:阿迪达斯"
    assert fenced_actions[0]["invalidates_normalized_key"] == "旧偏好:品牌:耐克"
    assert embedded_actions[0]["normalized_key"] == "preference:品牌:阿迪达斯"


@pytest.mark.asyncio
async def test_llm_structured_extractor_stably_shortens_long_keys() -> None:
    short_key = "  preference:品牌:阿迪达斯，常买  "
    long_key = "preference:品牌:阿迪达斯，喜欢黑色包装；" + "限量款" * 20
    invalidates_key = "旧偏好:品牌:耐克，喜欢白色包装；" + "跑步鞋" * 20
    payload = {
        "actions": [
            {
                "op": "add",
                "memory_type": "preference",
                "content": "用户喜欢 Adidas 黑色包装限量款",
                "normalized_key": short_key,
                "confidence": 0.92,
                "sensitivity": "normal",
                "reason": "explicit preference",
            },
            {
                "op": "update",
                "memory_type": "preference",
                "content": "用户喜欢 Adidas 黑色包装限量款",
                "normalized_key": long_key,
                "confidence": 0.92,
                "sensitivity": "normal",
                "reason": "explicit preference",
                "invalidates_normalized_key": invalidates_key,
            },
        ]
    }

    first_actions = await _extract(json.dumps(payload, ensure_ascii=False))
    second_actions = await _extract(json.dumps(payload, ensure_ascii=False))

    assert first_actions[0]["normalized_key"] == short_key.strip()
    assert first_actions[1]["normalized_key"] == second_actions[1]["normalized_key"]
    assert (
        first_actions[1]["invalidates_normalized_key"]
        == second_actions[1]["invalidates_normalized_key"]
    )
    assert len(first_actions[1]["normalized_key"]) == 64
    assert len(first_actions[1]["invalidates_normalized_key"]) == 64
    assert first_actions[1]["normalized_key"].startswith(long_key[:51])
    assert first_actions[1]["invalidates_normalized_key"].startswith(invalidates_key[:51])
    assert first_actions[1]["normalized_key"][51] == ":"
    assert first_actions[1]["invalidates_normalized_key"][51] == ":"


@pytest.mark.asyncio
async def test_llm_structured_extractor_timeout_and_error_fall_back() -> None:
    fallback = [{"op": "ignore", "content": "", "normalized_key": "ignore:text:x"}]
    timeout_extractor = MemoryStructuredExtractor(
        settings=_settings(memory_llm_extraction_timeout_seconds=0.01),
        llm_service=_LLM('{"actions":[]}', delay=0.05),
        deterministic_extractor=lambda text: fallback,
    )
    error_extractor = MemoryStructuredExtractor(
        settings=_settings(),
        llm_service=_LLM(exc=RuntimeError("boom")),
        deterministic_extractor=lambda text: fallback,
    )

    timeout_actions = await timeout_extractor.extract_actions(
        tenant_id="demo",
        trace_id="trace",
        user_text="hello",
        assistant_text="hi",
        existing_items_summary="none",
    )
    error_actions = await error_extractor.extract_actions(
        tenant_id="demo",
        trace_id="trace",
        user_text="hello",
        assistant_text="hi",
        existing_items_summary="none",
    )

    assert timeout_actions == fallback
    assert error_actions == fallback


@pytest.mark.asyncio
async def test_llm_structured_extractor_caps_actions_and_validates_schema() -> None:
    payload = {
        "actions": [
            {
                "op": "add",
                "memory_type": "unknown",
                "content": "bad",
                "normalized_key": "bad",
                "confidence": 0.9,
            },
            {
                "op": "add",
                "memory_type": "note",
                "content": "a",
                "normalized_key": "note:text:a",
                "confidence": 0.9,
                "sensitivity": "normal",
                "reason": "ok",
            },
            {
                "op": "add",
                "memory_type": "note",
                "content": "b",
                "normalized_key": "note:text:b",
                "confidence": 0.9,
                "sensitivity": "normal",
                "reason": "ok",
            },
        ]
    }

    actions = await _extract(json.dumps(payload), memory_llm_extraction_max_actions=1)

    assert [action["normalized_key"] for action in actions] == ["note:text:a"]


@pytest.mark.asyncio
async def test_llm_low_confidence_and_sensitive_actions_are_pending() -> None:
    payload = {
        "actions": [
            {
                "op": "add",
                "memory_type": "note",
                "content": "用户喜欢黑色包装",
                "normalized_key": "note:text:black_package",
                "confidence": 0.5,
                "sensitivity": "normal",
                "reason": "weak signal",
            },
            {
                "op": "add",
                "memory_type": "profile_fact",
                "content": "手机号 13800138000",
                "normalized_key": "profile_fact:phone:main",
                "confidence": 0.95,
                "sensitivity": "normal",
                "reason": "pii must pend",
            },
        ]
    }

    actions = await _extract(json.dumps(payload, ensure_ascii=False))

    assert [action["status"] for action in actions] == ["pending", "pending"]
    assert actions[1]["sensitivity"] == "pii"
    assert actions[0]["extraction_confidence"] == 0.5


@pytest.mark.asyncio
async def test_acceptance_metadata_keeps_joke_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryStore(_settings(), llm_service=_LLM())
    inserted: dict = {}

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        nonlocal inserted
        if "FROM plugin_memory_event WHERE id = :source_event_id" in sql:
            event_id = int((params or {})["source_event_id"])
            return [
                {
                    "id": event_id,
                    "source_member_id": "",
                    "source_message_id": f"event-{event_id}",
                }
            ]
        if "FROM plugin_memory_event WHERE id = ANY(:event_ids)" in sql:
            return [
                {
                    "id": int(event_id),
                    "source_member_id": "",
                    "source_message_id": f"event-{event_id}",
                }
                for event_id in (params or {}).get("event_ids", [])
            ]
        if "SELECT id FROM plugin_memory_item" in sql:
            return []
        if (
            "SELECT id, tenant_id, channel, source_key, user_id, session_id" in sql
            and "FROM plugin_memory_item" in sql
            and "normalized_key = :normalized_key" in sql
        ):
            return []
        if "INSERT INTO plugin_memory_item" in sql:
            assert params is not None
            inserted = {
                "id": 12,
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
                "source_event_id": params.get("source_event_id"),
                "source_trace_id": params.get("source_trace_id") or "",
                "original_text": params.get("original_text") or "",
                "occurrence_count": 1,
                "first_seen_at": None,
                "last_seen_at": None,
                "created_at": None,
                "updated_at": None,
                "deleted_at": None,
            }
            return [inserted]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    item = await store._apply_structured_memory_action(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        action={
            "op": "add",
            "memory_type": "preference",
            "content": "用户喜欢 Neon",
            "normalized_key": "preference:brand:neon",
            "confidence": 0.96,
            "sensitivity": "normal",
            "reason": "user said it was a joke",
            "scores": {"joke_score": 1.0},
        },
        source_event_id=7,
        source_trace_id="trace",
        original_text="记住我喜欢 Neon，开玩笑的",
    )

    assert item is not None
    assert item["status"] == "pending"
    assert item["acceptance_status"] == "rejected"
    assert item["acceptance_score"] is not None
    assert item["acceptance_signals"]["joke_score"] == 1.0


@pytest.mark.asyncio
async def test_acceptance_metadata_auto_accepts_clear_preference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(_settings(), llm_service=_LLM())

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "FROM plugin_memory_event WHERE id = :source_event_id" in sql:
            event_id = int((params or {})["source_event_id"])
            return [
                {
                    "id": event_id,
                    "source_member_id": "",
                    "source_message_id": f"event-{event_id}",
                }
            ]
        if "FROM plugin_memory_event WHERE id = ANY(:event_ids)" in sql:
            return [
                {
                    "id": int(event_id),
                    "source_member_id": "",
                    "source_message_id": f"event-{event_id}",
                }
                for event_id in (params or {}).get("event_ids", [])
            ]
        if "SELECT id FROM plugin_memory_item" in sql:
            return []
        if (
            "SELECT id, tenant_id, channel, source_key, user_id, session_id" in sql
            and "FROM plugin_memory_item" in sql
            and "normalized_key = :normalized_key" in sql
        ):
            return []
        if "INSERT INTO plugin_memory_item" in sql:
            assert params is not None
            return [
                {
                    "id": 13,
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
                    "source_event_id": params.get("source_event_id"),
                    "source_trace_id": params.get("source_trace_id") or "",
                    "original_text": params.get("original_text") or "",
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

    item = await store._apply_structured_memory_action(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        action={
            "op": "add",
            "source_type": "explicit_user",
            "memory_type": "preference",
            "content": "用户喜欢 Adidas",
            "normalized_key": _semantic_key("preference", "brand", "adidas"),
            "confidence": 0.95,
            "sensitivity": "normal",
            "reason": "explicit preference",
        },
        source_event_id=7,
        source_trace_id="trace",
        original_text="记住我喜欢 Adidas",
    )

    assert item is not None
    assert item["status"] == "active"
    assert item["acceptance_status"] == "accepted"
    assert item["acceptance_score"] is not None and item["acceptance_score"] >= 0.78


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected_acceptance", "expected_status", "expected_reason"),
    [
        ("accept", "accepted", "active", "manual approve"),
        ("reject", "rejected", "pending", "bad memory"),
        ("needs_review", "needs_review", "pending", "uncertain"),
        ("mark_joke", "rejected", "pending", "joking_or_hyperbole"),
        ("expire", "expired", "archived", "outdated"),
    ],
)
async def test_review_memory_item_acceptance_updates_metadata_and_status(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    expected_acceptance: str,
    expected_status: str,
    expected_reason: str,
) -> None:
    store = MemoryStore(_settings(), llm_service=_LLM())
    row = {
        "id": 41,
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
        "session_id": "",
        "scope_type": "identity",
        "source_type": "auto",
        "memory_type": "preference",
        "content": "用户喜欢 Adidas",
        "value_json": json.dumps(
            {
                "acceptance": {
                    "status": "candidate",
                    "score": 0.64,
                    "reason": "auto_candidate",
                    "signals": {"explicitness": 0.7},
                    "extraction_confidence": 0.88,
                    "recommendation": "needs_review",
                    "history": [
                        {
                            "action": "needs_review",
                            "status": "candidate",
                            "reason": "initial import",
                            "reviewed_at": "2026-05-01T00:00:00Z",
                            "reviewed_by": "system",
                        }
                    ],
                }
            },
            ensure_ascii=False,
        ),
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

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        nonlocal row
        if (
            "SELECT id, tenant_id, channel, source_key, user_id, session_id" in sql
            and "FROM plugin_memory_item" in sql
        ):
            return [row]
        if sql.startswith("UPDATE plugin_memory_item SET"):
            assert params is not None
            row = {**row, "value_json": params["value_json"], "status": params["status"]}
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
        action=action,
        review_reason="" if action == "mark_joke" else expected_reason,
        reviewed_by="admin-test",
    )

    assert result is not None
    assert result["status"] == expected_status
    assert result["acceptance_status"] == expected_acceptance
    acceptance = result["value"]["acceptance"]
    assert acceptance["score"] == 0.64
    assert acceptance["signals"]["explicitness"] == 0.7
    assert acceptance["extraction_confidence"] == 0.88
    assert acceptance["recommendation"] == "needs_review"
    assert acceptance["previous_status"] == "candidate"
    assert acceptance["reviewed_by"] == "admin-test"
    assert acceptance["review_reason"] == expected_reason
    assert acceptance["reviewed_at"].endswith("Z")
    assert len(acceptance["history"]) == 2
    assert acceptance["history"][0]["reason"] == "initial import"
    assert acceptance["history"][-1]["action"] == action
    assert acceptance["history"][-1]["status"] == expected_acceptance
    assert acceptance["history"][-1]["reason"] == expected_reason
    assert acceptance["history"][-1]["reviewed_by"] == "admin-test"
    assert acceptance["history"][-1]["previous_status"] == "candidate"
    assert acceptance["history"][-1]["previous_acceptance_status"] == "candidate"
    assert acceptance["history"][-1]["previous_item_status"] == "pending"
    assert acceptance["history"][-1]["current_item_status"] == expected_status
    assert result["acceptance_history"] == acceptance["history"]
    if action == "mark_joke":
        assert acceptance["reason"] == "joking_or_hyperbole"
        assert acceptance["signals"]["joke_score"] == 1.0


@pytest.mark.asyncio
async def test_review_memory_item_acceptance_supersedes_current_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(_settings(), llm_service=_LLM())
    row = {
        "id": 41,
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
        "session_id": "",
        "scope_type": "identity",
        "source_type": "auto",
        "memory_type": "preference",
        "content": "用户喜欢 Adidas",
        "value_json": json.dumps(
            {"acceptance": {"status": "accepted", "score": 0.91}}, ensure_ascii=False
        ),
        "normalized_key": "preference:brand:adidas",
        "confidence": 0.91,
        "status": "active",
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

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        nonlocal row
        if (
            "SELECT id, tenant_id, channel, source_key, user_id, session_id" in sql
            and "FROM plugin_memory_item" in sql
        ):
            return [row]
        if sql.startswith("UPDATE plugin_memory_item SET"):
            assert params is not None
            row = {**row, "value_json": params["value_json"], "status": params["status"]}
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
        action="supersede",
        review_reason="newer preference",
        reviewed_by="admin-test",
        superseded_by_item_id=42,
    )

    assert result is not None
    assert result["status"] == "invalidated"
    assert result["acceptance_status"] == "superseded"
    acceptance = result["value"]["acceptance"]
    assert acceptance["superseded_by_item_id"] == 42
    assert result["superseded_by_item_id"] == 42
    assert acceptance["history"][-1]["action"] == "supersede"
    assert acceptance["history"][-1]["status"] == "superseded"
    assert acceptance["history"][-1]["superseded_by_item_id"] == 42


@pytest.mark.asyncio
async def test_acceptance_stats_and_legacy_audit_count_missing_without_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(_settings(), llm_service=_LLM())
    rows = [
        {
            "id": 1,
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "wxid_a",
            "session_id": "",
            "scope_type": "identity",
            "source_type": "manual",
            "memory_type": "note",
            "value_json": json.dumps({}),
            "normalized_key": "k1",
            "confidence": 1.0,
            "status": "active",
            "pinned": True,
            "priority": 100,
            "sensitivity": "normal",
            "source_event_id": None,
            "source_trace_id": "",
            "occurrence_count": 1,
            "first_seen_at": None,
            "last_seen_at": None,
            "created_at": None,
            "updated_at": None,
            "deleted_at": None,
        },
        {
            "id": 2,
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "wxid_a",
            "session_id": "",
            "scope_type": "identity",
            "source_type": "auto",
            "memory_type": "preference",
            "value_json": json.dumps({"acceptance": {"status": "accepted"}}),
            "normalized_key": "k2",
            "confidence": 0.9,
            "status": "active",
            "pinned": False,
            "priority": 0,
            "sensitivity": "normal",
            "source_event_id": None,
            "source_trace_id": "",
            "occurrence_count": 1,
            "first_seen_at": None,
            "last_seen_at": None,
            "created_at": None,
            "updated_at": None,
            "deleted_at": None,
        },
        {
            "id": 3,
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "wxid_a",
            "session_id": "s1",
            "scope_type": "session",
            "source_type": "backfill",
            "memory_type": "episodic",
            "value_json": json.dumps({}),
            "normalized_key": "k3",
            "confidence": 0.5,
            "status": "pending",
            "pinned": False,
            "priority": 0,
            "sensitivity": "pii",
            "source_event_id": None,
            "source_trace_id": "",
            "occurrence_count": 1,
            "first_seen_at": None,
            "last_seen_at": None,
            "created_at": None,
            "updated_at": None,
            "deleted_at": None,
        },
        {
            "id": 4,
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "wxid_a",
            "session_id": "",
            "scope_type": "identity",
            "source_type": "auto",
            "memory_type": "note",
            "value_json": json.dumps({"acceptance": {"status": "rejected"}}),
            "normalized_key": "k4",
            "confidence": 0.2,
            "status": "pending",
            "pinned": False,
            "priority": 0,
            "sensitivity": "sensitive",
            "source_event_id": None,
            "source_trace_id": "",
            "occurrence_count": 1,
            "first_seen_at": None,
            "last_seen_at": None,
            "created_at": None,
            "updated_at": None,
            "deleted_at": None,
        },
    ]

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        assert "content" not in sql
        assert "original_text" not in sql
        return rows

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    stats = await store.get_memory_acceptance_stats(tenant_id="demo", channel="wechat")
    audit = await store.audit_legacy_acceptance(tenant_id="demo", channel="wechat")

    assert stats["total"] == 4
    assert stats["counts"]["missing_acceptance"] == 2
    assert stats["counts"]["accepted"] == 1
    assert stats["counts"]["rejected"] == 1
    assert stats["sensitivity_counts"] == {"normal": 2, "private": 1, "sensitive": 1}
    assert stats["ids_preview"] == [1, 2, 3, 4]
    assert audit["missing_acceptance"] == 2
    assert audit["ids_preview"] == [1, 3]
    assert audit["groups"][0]["suggested_action"] == "needs_review"
    assert all("content" not in group and "original_text" not in group for group in audit["groups"])


@pytest.mark.asyncio
async def test_acceptance_legacy_backfill_dry_run_does_not_mutate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(_settings(), llm_service=_LLM())
    rows = [
        {
            "id": 1,
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "wxid_a",
            "session_id": "",
            "scope_type": "identity",
            "source_type": "manual",
            "memory_type": "note",
            "value_json": json.dumps({}),
            "normalized_key": "k1",
            "confidence": 1.0,
            "status": "active",
            "pinned": True,
            "priority": 100,
            "sensitivity": "normal",
            "source_event_id": None,
            "source_trace_id": "",
            "occurrence_count": 1,
            "first_seen_at": None,
            "last_seen_at": None,
            "created_at": None,
            "updated_at": None,
            "deleted_at": None,
        }
    ]
    update_calls: list[tuple[int, dict]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        return rows

    async def fake_update(item_id: int, **updates: dict):
        update_calls.append((item_id, updates))
        return {"id": item_id}

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "update_memory_item", fake_update)

    result = await store.backfill_legacy_acceptance(
        tenant_id="demo",
        dry_run=True,
        max_items=10,
        mark_missing_as="needs_review",
    )

    assert result["dry_run"] is True
    assert result["would_affect"] == 1
    assert result["affected"] == 0
    assert result["ids_preview"] == [1]
    assert update_calls == []


@pytest.mark.asyncio
async def test_acceptance_legacy_backfill_non_dry_run_limit_history_and_no_overwrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(_settings(), llm_service=_LLM())
    rows = {
        1: {
            "id": 1,
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "wxid_a",
            "session_id": "",
            "scope_type": "identity",
            "source_type": "manual",
            "memory_type": "note",
            "value_json": json.dumps({}),
            "normalized_key": "k1",
            "confidence": 1.0,
            "status": "active",
            "pinned": True,
            "priority": 100,
            "sensitivity": "normal",
            "source_event_id": None,
            "source_trace_id": "",
            "original_text": "",
            "occurrence_count": 1,
            "first_seen_at": None,
            "last_seen_at": None,
            "created_at": None,
            "updated_at": None,
            "deleted_at": None,
        },
        2: {
            "id": 2,
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "wxid_a",
            "session_id": "",
            "scope_type": "identity",
            "source_type": "auto",
            "memory_type": "preference",
            "value_json": json.dumps({}),
            "normalized_key": "k2",
            "confidence": 0.8,
            "status": "active",
            "pinned": False,
            "priority": 0,
            "sensitivity": "normal",
            "source_event_id": None,
            "source_trace_id": "",
            "original_text": "",
            "occurrence_count": 1,
            "first_seen_at": None,
            "last_seen_at": None,
            "created_at": None,
            "updated_at": None,
            "deleted_at": None,
        },
        3: {
            "id": 3,
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "wxid_a",
            "session_id": "",
            "scope_type": "identity",
            "source_type": "auto",
            "memory_type": "note",
            "value_json": json.dumps(
                {"acceptance": {"status": "accepted", "history": [{"action": "accept"}]}}
            ),
            "normalized_key": "k3",
            "confidence": 1.0,
            "status": "active",
            "pinned": False,
            "priority": 0,
            "sensitivity": "normal",
            "source_event_id": None,
            "source_trace_id": "",
            "original_text": "",
            "occurrence_count": 1,
            "first_seen_at": None,
            "last_seen_at": None,
            "created_at": None,
            "updated_at": None,
            "deleted_at": None,
        },
        4: {
            "id": 4,
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "wxid_a",
            "session_id": "",
            "scope_type": "identity",
            "source_type": "auto",
            "memory_type": "note",
            "value_json": json.dumps(
                {"acceptance": {"status": "rejected", "history": [{"action": "reject"}]}}
            ),
            "normalized_key": "k4",
            "confidence": 0.2,
            "status": "pending",
            "pinned": False,
            "priority": 0,
            "sensitivity": "normal",
            "source_event_id": None,
            "source_trace_id": "",
            "original_text": "",
            "occurrence_count": 1,
            "first_seen_at": None,
            "last_seen_at": None,
            "created_at": None,
            "updated_at": None,
            "deleted_at": None,
        },
    }

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "FROM plugin_memory_item WHERE id = :id" in sql:
            row = rows.get(int(params["id"]))
            return [row] if row else []
        if (
            "SELECT id, tenant_id, channel, source_key, user_id, session_id" in sql
            and "FROM plugin_memory_item" in sql
        ):
            return list(rows.values())
        if sql.startswith("UPDATE plugin_memory_item SET"):
            item_id = int(params["id"])
            rows[item_id] = {
                **rows[item_id],
                "value_json": params["value_json"],
                "status": params["status"],
            }
            return []
        return []

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", noop)
    monkeypatch.setattr(store, "_sync_memory_graph_for_item_safe", noop)
    monkeypatch.setattr(store, "_sync_memory_vector_for_item_safe", noop)

    result = await store.backfill_legacy_acceptance(
        tenant_id="demo",
        dry_run=False,
        max_items=1,
        mark_missing_as="needs_review",
        reviewed_by="admin_backfill",
    )

    assert result["dry_run"] is False
    assert result["affected"] == 1
    assert result["ids"] == [1]
    item1_acceptance = json.loads(rows[1]["value_json"])["acceptance"]
    assert rows[1]["status"] == "pending"
    assert item1_acceptance["status"] == "needs_review"
    assert item1_acceptance["reviewed_by"] == "admin_backfill"
    assert item1_acceptance["history"][0]["action"] == "backfill"
    assert "legacy_acceptance_backfill" in item1_acceptance["history"][0]["reason"]
    assert json.loads(rows[2]["value_json"]) == {}
    assert json.loads(rows[3]["value_json"])["acceptance"]["status"] == "accepted"
    assert json.loads(rows[4]["value_json"])["acceptance"]["status"] == "rejected"


@pytest.mark.asyncio
async def test_review_memory_item_acceptance_supersedes_counterpart_when_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(_settings(), llm_service=_LLM())
    rows = {
        41: {
            "id": 41,
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "wxid_a",
            "session_id": "",
            "scope_type": "identity",
            "source_type": "auto",
            "memory_type": "preference",
            "content": "用户喜欢 Nike",
            "value_json": json.dumps(
                {"acceptance": {"status": "accepted", "score": 0.9}}, ensure_ascii=False
            ),
            "normalized_key": "preference:brand:nike",
            "confidence": 0.9,
            "status": "active",
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
        },
        42: {
            "id": 42,
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "wxid_a",
            "session_id": "",
            "scope_type": "identity",
            "source_type": "auto",
            "memory_type": "preference",
            "content": "用户喜欢 Adidas",
            "value_json": json.dumps(
                {"acceptance": {"status": "needs_review", "score": 0.71}}, ensure_ascii=False
            ),
            "normalized_key": "preference:brand:adidas",
            "confidence": 0.91,
            "status": "pending",
            "pinned": False,
            "priority": 0,
            "sensitivity": "normal",
            "source_event_id": 8,
            "source_trace_id": "trace-2",
            "original_text": "",
            "occurrence_count": 1,
            "first_seen_at": None,
            "last_seen_at": None,
            "created_at": None,
            "updated_at": None,
            "deleted_at": None,
        },
    }

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if (
            "SELECT id, tenant_id, channel, source_key, user_id, session_id" in sql
            and "FROM plugin_memory_item" in sql
        ):
            assert params is not None
            item = rows.get(int(params["id"]))
            return [item] if item else []
        if sql.startswith("UPDATE plugin_memory_item SET"):
            assert params is not None
            item_id = int(params["id"])
            rows[item_id] = {
                **rows[item_id],
                "value_json": params["value_json"],
                "status": params["status"],
            }
            return []
        return []

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", noop)
    monkeypatch.setattr(store, "_sync_memory_graph_for_item_safe", noop)
    monkeypatch.setattr(store, "_sync_memory_vector_for_item_safe", noop)

    result = await store.review_memory_item_acceptance(
        42,
        action="supersede",
        review_reason="newer preference",
        reviewed_by="admin-test",
        supersedes_item_id=41,
    )

    assert result is not None
    assert result["status"] == "active"
    assert result["acceptance_status"] == "accepted"
    assert result["value"]["acceptance"]["supersedes_item_id"] == 41
    old = store._finalize_memory_item(rows[41])
    assert old["status"] == "invalidated"
    assert old["acceptance_status"] == "superseded"
    assert old["value"]["acceptance"]["superseded_by_item_id"] == 42
    assert old["value"]["acceptance"]["history"][-1]["superseded_by_item_id"] == 42


@pytest.mark.asyncio
async def test_review_memory_item_acceptance_rejects_self_supersede(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(_settings(), llm_service=_LLM())
    row = {
        "id": 41,
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
        "session_id": "",
        "scope_type": "identity",
        "source_type": "auto",
        "memory_type": "preference",
        "content": "用户喜欢 Nike",
        "value_json": json.dumps(
            {"acceptance": {"status": "needs_review", "score": 0.71}}, ensure_ascii=False
        ),
        "normalized_key": "preference:brand:nike",
        "confidence": 0.9,
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

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if (
            "SELECT id, tenant_id, channel, source_key, user_id, session_id" in sql
            and "FROM plugin_memory_item" in sql
        ):
            return [row]
        raise AssertionError(f"unexpected SQL: {sql}")

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    with pytest.raises(ValueError, match="cannot supersede itself"):
        await store.review_memory_item_acceptance(
            41,
            action="supersede",
            review_reason="same item",
            reviewed_by="admin-test",
            supersedes_item_id=41,
        )


@pytest.mark.asyncio
async def test_review_memory_item_acceptance_does_not_cross_session_supersede(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(_settings(), llm_service=_LLM())
    rows = {
        41: {
            "id": 41,
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "wxid_a",
            "session_id": "old-session",
            "scope_type": "session",
            "source_type": "auto",
            "memory_type": "preference",
            "content": "用户喜欢 Nike",
            "value_json": json.dumps(
                {"acceptance": {"status": "accepted", "score": 0.9}}, ensure_ascii=False
            ),
            "normalized_key": "preference:brand:nike",
            "confidence": 0.9,
            "status": "active",
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
        },
        42: {
            "id": 42,
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "wxid_a",
            "session_id": "new-session",
            "scope_type": "session",
            "source_type": "auto",
            "memory_type": "preference",
            "content": "用户喜欢 Adidas",
            "value_json": json.dumps(
                {"acceptance": {"status": "needs_review", "score": 0.71}}, ensure_ascii=False
            ),
            "normalized_key": "preference:brand:adidas",
            "confidence": 0.91,
            "status": "pending",
            "pinned": False,
            "priority": 0,
            "sensitivity": "normal",
            "source_event_id": 8,
            "source_trace_id": "trace-2",
            "original_text": "",
            "occurrence_count": 1,
            "first_seen_at": None,
            "last_seen_at": None,
            "created_at": None,
            "updated_at": None,
            "deleted_at": None,
        },
    }

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if (
            "SELECT id, tenant_id, channel, source_key, user_id, session_id" in sql
            and "FROM plugin_memory_item" in sql
        ):
            assert params is not None
            item = rows.get(int(params["id"]))
            return [item] if item else []
        if sql.startswith("UPDATE plugin_memory_item SET"):
            assert params is not None
            item_id = int(params["id"])
            rows[item_id] = {
                **rows[item_id],
                "value_json": params["value_json"],
                "status": params["status"],
            }
            return []
        return []

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", noop)
    monkeypatch.setattr(store, "_sync_memory_graph_for_item_safe", noop)
    monkeypatch.setattr(store, "_sync_memory_vector_for_item_safe", noop)

    result = await store.review_memory_item_acceptance(
        42,
        action="supersede",
        review_reason="newer preference",
        reviewed_by="admin-test",
        supersedes_item_id=41,
    )

    assert result is not None
    assert result["status"] == "active"
    assert store._finalize_memory_item(rows[41])["acceptance_status"] == "accepted"


@pytest.mark.asyncio
async def test_list_memory_items_adds_duplicate_hints_without_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(_settings(), llm_service=_LLM())
    rows = []
    for item_id, content, status, acceptance_status in [
        (41, "用户喜欢 Adidas", "active", "accepted"),
        (42, "用户偏好 Adidas", "pending", "needs_review"),
    ]:
        rows.append(
            {
                "id": item_id,
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "user_id": "wxid_a",
                "session_id": "",
                "scope_type": "identity",
                "source_type": "auto",
                "memory_type": "preference",
                "content": content,
                "value_json": json.dumps(
                    {"acceptance": {"status": acceptance_status}}, ensure_ascii=False
                ),
                "normalized_key": "preference:brand:adidas",
                "confidence": 0.9,
                "status": status,
                "pinned": False,
                "priority": 0,
                "sensitivity": "normal",
                "source_event_id": None,
                "source_trace_id": "",
                "original_text": "private source body must not be exposed in hints",
                "occurrence_count": 1,
                "first_seen_at": None,
                "last_seen_at": None,
                "created_at": None,
                "updated_at": None,
                "deleted_at": None,
            }
        )

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        assert "FROM plugin_memory_item" in sql
        return rows

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    items = await store.list_memory_items(
        tenant_id="demo", channel="wechat", source_key="wxbot", user_id="wxid_a"
    )

    assert items[0]["duplicate_hint"] == {
        "count": 1,
        "ids": [42],
        "normalized_key": "preference:brand:adidas",
    }
    conflict_item = items[0]["possible_conflicts"]["items"][0]
    assert conflict_item == {
        "id": 42,
        "status": "pending",
        "acceptance_status": "needs_review",
        "normalized_key": "preference:brand:adidas",
    }
    assert "content" not in conflict_item
    assert "original_text" not in conflict_item


def test_runtime_profile_excludes_active_needs_review_acceptance_metadata() -> None:
    identity_profile = {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
        "long_term_memory": "",
        "manual_notes": "",
        "long_term_items_json": "[]",
        "message_count": 0,
        "imported_message_count": 0,
    }
    session_profile = {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "session_id": "s1",
        "user_id": "wxid_a",
        "short_term_memory": "",
        "manual_notes": "",
        "message_count": 0,
        "imported_message_count": 0,
    }
    runtime = memory_store_module._build_runtime_profile_from_items(
        identity_profile,
        session_profile,
        [
            {
                "id": 1,
                "source_type": "auto",
                "memory_type": "preference",
                "content": "用户喜欢 Adidas",
                "normalized_key": "preference:brand:adidas",
                "confidence": 0.95,
                "status": "active",
                "pinned": False,
                "priority": 0,
                "sensitivity": "normal",
                "value_json": '{"acceptance":{"status":"accepted","score":0.9}}',
            },
            {
                "id": 2,
                "source_type": "auto",
                "memory_type": "preference",
                "content": "needs_review excluded",
                "normalized_key": "preference:brand:review",
                "confidence": 0.95,
                "status": "active",
                "pinned": False,
                "priority": 0,
                "sensitivity": "normal",
                "value_json": '{"acceptance":{"status":"needs_review","score":0.7}}',
            },
        ],
        [],
    )

    assert "用户喜欢 Adidas" in runtime["long_term_memory"]
    assert "needs_review excluded" not in runtime["long_term_memory"]


def test_runtime_profile_excludes_rejected_and_expired_acceptance_metadata() -> None:
    identity_profile = {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
        "long_term_memory": "",
        "manual_notes": "",
        "long_term_items_json": "[]",
        "message_count": 0,
        "imported_message_count": 0,
    }
    session_profile = {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "session_id": "s1",
        "user_id": "wxid_a",
        "short_term_memory": "",
        "manual_notes": "",
        "message_count": 0,
        "imported_message_count": 0,
    }
    runtime = memory_store_module._build_runtime_profile_from_items(
        identity_profile,
        session_profile,
        [
            {
                "id": 1,
                "source_type": "auto",
                "memory_type": "preference",
                "content": "accepted included",
                "normalized_key": "preference:accepted",
                "confidence": 0.95,
                "status": "active",
                "pinned": False,
                "priority": 0,
                "sensitivity": "normal",
                "value_json": '{"acceptance":{"status":"accepted","score":0.9}}',
            },
            {
                "id": 2,
                "source_type": "auto",
                "memory_type": "preference",
                "content": "rejected excluded",
                "normalized_key": "preference:rejected",
                "confidence": 0.95,
                "status": "active",
                "pinned": False,
                "priority": 0,
                "sensitivity": "normal",
                "value_json": '{"acceptance":{"status":"rejected","score":0.1}}',
            },
            {
                "id": 3,
                "source_type": "auto",
                "memory_type": "preference",
                "content": "expired excluded",
                "normalized_key": "preference:expired",
                "confidence": 0.95,
                "status": "active",
                "pinned": False,
                "priority": 0,
                "sensitivity": "normal",
                "value_json": '{"acceptance":{"status":"expired","score":0.8}}',
            },
            {
                "id": 4,
                "source_type": "auto",
                "memory_type": "preference",
                "content": "superseded excluded",
                "normalized_key": "preference:superseded",
                "confidence": 0.95,
                "status": "active",
                "pinned": False,
                "priority": 0,
                "sensitivity": "normal",
                "value_json": '{"acceptance":{"status":"superseded","score":0.8}}',
            },
        ],
        [],
    )

    assert "accepted included" in runtime["long_term_memory"]
    assert "rejected excluded" not in runtime["long_term_memory"]
    assert "expired excluded" not in runtime["long_term_memory"]
    assert "superseded excluded" not in runtime["long_term_memory"]


def test_runtime_profile_excludes_pending_sensitive_llm_items() -> None:
    identity_profile = {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
        "long_term_memory": "",
        "manual_notes": "",
        "long_term_items_json": "[]",
        "message_count": 0,
        "imported_message_count": 0,
    }
    session_profile = {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "session_id": "s1",
        "user_id": "wxid_a",
        "short_term_memory": "",
        "manual_notes": "",
        "message_count": 0,
        "imported_message_count": 0,
    }
    runtime = memory_store_module._build_runtime_profile_from_items(
        identity_profile,
        session_profile,
        [
            {
                "id": 1,
                "source_type": "auto",
                "memory_type": "preference",
                "content": "用户喜欢 Adidas",
                "normalized_key": "preference:brand:adidas",
                "confidence": 0.95,
                "status": "active",
                "pinned": False,
                "priority": 0,
                "sensitivity": "normal",
            },
            {
                "id": 2,
                "source_type": "auto",
                "memory_type": "note",
                "content": "用户喜欢黑色包装",
                "normalized_key": "note:text:black_package",
                "confidence": 0.5,
                "status": "pending",
                "pinned": False,
                "priority": 0,
                "sensitivity": "normal",
            },
            {
                "id": 3,
                "source_type": "auto",
                "memory_type": "profile_fact",
                "content": "手机号 13800138000",
                "normalized_key": "profile_fact:phone:main",
                "confidence": 0.95,
                "status": "pending",
                "pinned": False,
                "priority": 0,
                "sensitivity": "pii",
            },
        ],
        [],
    )

    assert "用户喜欢 Adidas" in runtime["long_term_memory"]
    assert "用户喜欢黑色包装" not in runtime["long_term_memory"]
    assert "13800138000" not in runtime["long_term_memory"]


@pytest.mark.asyncio
async def test_llm_update_does_not_overwrite_manual_or_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(_settings(), llm_service=_LLM())
    key = _semantic_key("preference", "brand", "adidas")
    manual = {
        "id": 1,
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
        "session_id": "",
        "scope_type": "identity",
        "source_type": "manual",
        "memory_type": "preference",
        "content": "人工：用户喜欢 Adidas",
        "value_json": "{}",
        "normalized_key": key,
        "confidence": 1.0,
        "status": "active",
        "pinned": True,
        "priority": 100,
        "sensitivity": "normal",
        "source_event_id": None,
        "source_trace_id": "",
        "original_text": "",
        "occurrence_count": 1,
        "first_seen_at": None,
        "last_seen_at": None,
        "created_at": None,
        "updated_at": None,
        "deleted_at": None,
    }
    inserted = {
        **manual,
        "id": 2,
        "source_type": "auto",
        "content": "用户喜欢 Adidas",
        "status": "pending",
        "pinned": False,
    }
    calls: list[tuple[str, dict | None]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params))
        if "FROM plugin_memory_event WHERE id = :source_event_id" in sql:
            event_id = int((params or {})["source_event_id"])
            return [
                {
                    "id": event_id,
                    "source_member_id": "",
                    "source_message_id": f"event-{event_id}",
                }
            ]
        if "FROM plugin_memory_event WHERE id = ANY(:event_ids)" in sql:
            return [
                {
                    "id": int(event_id),
                    "source_member_id": "",
                    "source_message_id": f"event-{event_id}",
                }
                for event_id in (params or {}).get("event_ids", [])
            ]
        if "SELECT id FROM plugin_memory_item" in sql:
            return []
        if (
            "SELECT id, tenant_id, channel, source_key, user_id, session_id" in sql
            and "FROM plugin_memory_item" in sql
            and "normalized_key = :normalized_key" in sql
        ):
            return [manual]
        if "INSERT INTO plugin_memory_item" in sql:
            return [inserted]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    item = await store._apply_structured_memory_action(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        action={
            "op": "update",
            "memory_type": "preference",
            "content": "用户喜欢 Adidas",
            "normalized_key": key,
            "confidence": 0.95,
            "sensitivity": "normal",
            "status": "active",
            "reason": "llm update",
        },
        source_event_id=2,
        source_trace_id="trace",
        original_text="记住我喜欢 Adidas",
    )

    assert item is not None
    assert item["status"] == "pending"
    assert not any("UPDATE plugin_memory_item SET content = :content" in sql for sql, _ in calls)
    assert any(
        "INSERT INTO plugin_memory_item" in sql
        and params
        and "manual_or_pinned_conflict" in params["value_json"]
        for sql, params in calls
    )


@pytest.mark.asyncio
async def test_remember_interaction_enqueues_llm_job_after_deterministic_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(_settings(), llm_service=_LLM())
    applied = []
    enqueued = []

    async def fake_get_identity_profile(**kwargs):
        return {
            **kwargs,
            "long_term_memory": "",
            "manual_notes": "",
            "long_term_items": [],
            "message_count": 0,
            "imported_message_count": 0,
        }

    async def fake_get_session_profile(**kwargs):
        return {
            **kwargs,
            "short_term_memory": "",
            "manual_notes": "",
            "short_term_items": [],
            "message_count": 0,
            "imported_message_count": 0,
        }

    async def fake_list_memory_items(**kwargs):
        return [
            {
                "id": 1,
                "content": "用户喜欢 Adidas",
                "normalized_key": "preference:brand:adidas",
                "memory_type": "preference",
                "source_type": "auto",
                "status": "active",
                "pinned": False,
                "sensitivity": "normal",
            }
        ]

    async def fake_apply(**kwargs):
        applied.append(kwargs["action"])
        return {
            "id": len(applied),
            "scope_type": "identity",
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "wxid_a",
            "session_id": "",
        }

    async def fake_refresh(item):
        return None

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "INSERT INTO plugin_memory_event" in sql:
            return [{"id": 7}]
        return []

    async def fake_enqueue(**kwargs):
        enqueued.append(kwargs)
        return {"id": 11, "status": "pending", **kwargs}

    monkeypatch.setattr(store, "get_identity_profile", fake_get_identity_profile)
    monkeypatch.setattr(store, "get_session_profile", fake_get_session_profile)
    monkeypatch.setattr(store, "list_memory_items", fake_list_memory_items)
    monkeypatch.setattr(store, "_apply_structured_memory_action", fake_apply)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", fake_refresh)
    monkeypatch.setattr(
        store, "get_runtime_profile", lambda **kwargs: fake_get_identity_profile(**kwargs)
    )
    monkeypatch.setattr(store, "enqueue_llm_extraction_job", fake_enqueue)
    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    token = memory_store_module._ACTIVE_MUTATION_CONNECTION.set(
        SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    )
    try:
        profile = await store.remember_interaction(
            tenant_id="demo",
            channel="wechat",
            source_key="wxbot",
            user_id="wxid_a",
            session_id="s1",
            user_text="记住我喜欢 Adidas",
            assistant_text="好的",
            trace_id="trace",
        )
    finally:
        memory_store_module._ACTIVE_MUTATION_CONNECTION.reset(token)

    assert profile["user_id"] == "wxid_a"
    assert applied and applied[0]["content"] == "用户喜欢 Adidas"
    assert enqueued == [
        {
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "wxid_a",
            "session_id": "s1",
            "trace_id": "trace",
            "source_event_id": 7,
            "origin_session_kind": "private",
            "audience_scope": "private",
            "allowed_session_ids": [],
            "sensitivity_category": "normal",
            "expires_at": None,
            "source_kind": "conversation",
        }
    ]
