from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import plugins.memory.store as memory_store_module
from app.common.prompting import augment_prompt_with_persona_and_memory
from app.common.types import Channel, Session
from plugins.memory.store import (
    MemoryStore,
    _extract_long_term_candidates,
    _job_idempotency_key,
    _rank_retrieved_memory_items,
    _semantic_key,
    _update_session_state,
    extract_structured_memory_actions,
)

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "memory_eval_cases.json"


def _fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _settings(**kwargs: Any) -> SimpleNamespace:
    data = {
        "memory_llm_extraction_enabled": True,
        "memory_llm_extraction_timeout_seconds": 0.2,
        "memory_llm_extraction_max_actions": 4,
        "memory_llm_extraction_min_confidence": 0.75,
        "memory_llm_extraction_job_enabled": True,
        "memory_llm_extraction_job_drain_batch_size": 5,
        "memory_llm_extraction_job_max_attempts": 2,
        "memory_llm_extraction_job_backoff_seconds": 1.0,
        "memory_llm_extraction_job_timeout_seconds": 0.2,
        "memory_llm_extraction_job_lock_ttl_seconds": 30.0,
    }
    data.update(kwargs)
    return SimpleNamespace(**data)


def _session(user_memory: dict[str, Any]) -> Session:
    session = Session(
        session_id="group-1@chatroom",
        tenant_id="demo",
        user_id="wxid_a",
        channel=Channel.WECHAT,
    )
    session.variables["user_memory"] = user_memory
    return session


async def _allow_memory_scope(_tenant_id: str, _session_id: str) -> bool:
    return True


@pytest.mark.parametrize("case", _fixture()["extraction_cases"], ids=lambda item: item["id"])
def test_eval_fixture_extraction_and_sensitivity_cases(case: dict[str, Any]) -> None:
    actions = extract_structured_memory_actions(case["text"])
    expected = case["expected"]

    assert [action["op"] for action in actions] == expected["ops"]
    active_contents = [
        action["content"]
        for action in actions
        if action["op"] in {"add", "update"}
        and action.get("status") == "active"
        and action.get("sensitivity", "normal") == "normal"
    ]
    assert active_contents == expected.get("active_contents", [])
    assert _extract_long_term_candidates(case["text"]) == [
        action for action in actions if action["op"] != "ignore"
    ]
    if "memory_type" in expected:
        assert actions[0]["memory_type"] == expected["memory_type"]
    if "status" in expected:
        assert actions[0]["status"] == expected["status"]
    if "sensitivity" in expected:
        assert actions[0]["sensitivity"] == expected["sensitivity"]


def test_explicit_remember_then_change_invalidates_old_preference() -> None:
    active_by_key: dict[str, str] = {}
    invalidated_keys: set[str] = set()

    for text in ["记住我喜欢 Adidas", "我现在不喜欢 Adidas 了，换成 Puma"]:
        for action in extract_structured_memory_actions(text):
            key = str(action["normalized_key"])
            if action["op"] == "invalidate":
                invalidated = str(action.get("invalidates_normalized_key") or key)
                invalidated_keys.add(invalidated)
                active_by_key.pop(invalidated, None)
            elif action["op"] in {"add", "update"} and action["status"] == "active":
                active_by_key[key] = str(action["content"])

    adidas_key = _semantic_key("preference", "brand", "Adidas")
    puma_key = _semantic_key("preference", "brand", "Puma")
    assert adidas_key in invalidated_keys
    assert adidas_key not in active_by_key
    assert active_by_key[puma_key] == "用户喜欢 Puma"


@pytest.mark.asyncio
@pytest.mark.parametrize("source_type", ["auto", "explicit_user", "backfill"])
async def test_manual_pinned_memory_is_not_overwritten_or_invalidated(
    source_type: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = _semantic_key("preference", "brand", "Adidas")
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
        "content": "人工锁定：用户喜欢 Adidas",
        "value_json": "{}",
        "normalized_key": key,
        "confidence": 1.0,
        "status": "active",
        "pinned": True,
        "priority": 100,
        "sensitivity": "normal",
        "deleted_at": None,
    }
    inserted: list[dict[str, Any]] = []
    invalidated: list[int] = []
    store = MemoryStore(_settings())

    async def fake_find(**kwargs: Any) -> list[dict[str, Any]]:
        assert kwargs["normalized_key"] == key
        return [manual]

    async def fake_insert(**kwargs: Any) -> dict[str, Any]:
        inserted.append(kwargs)
        return {"id": 2, **kwargs}

    async def fake_invalidate(item_id: int, **kwargs: Any) -> None:
        invalidated.append(item_id)

    monkeypatch.setattr(store, "_find_memory_item_by_normalized_key", fake_find)
    monkeypatch.setattr(store, "_insert_or_touch_memory_item", fake_insert)
    monkeypatch.setattr(store, "_mark_memory_item_invalidated", fake_invalidate)

    item = await store._apply_structured_memory_action(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        action={
            "op": "invalidate",
            "content": "用户不再喜欢 Adidas",
            "source_type": source_type,
            "memory_type": "preference",
            "normalized_key": key,
            "confidence": 0.95,
            "status": "active",
            "sensitivity": "normal",
            "reason": "eval_conflict",
            "invalidates_normalized_key": key,
        },
        source_type_override=source_type if source_type == "backfill" else None,
    )

    assert item is not None
    assert invalidated == []
    assert inserted[0]["status"] == "pending"
    assert inserted[0]["source_type"] == source_type
    assert "manual_or_pinned_conflict" in inserted[0]["value_json"]["reason"]


def test_group_multi_user_and_source_key_fallback_isolation() -> None:
    items = [
        _item(1, "wxid_a", "wxbot", "A exact source"),
        _item(2, "wxid_a", "*", "A wildcard source"),
        _item(3, "wxid_a", "web", "A other source"),
        _item(4, "wxid_b", "wxbot", "B same group source"),
        _item(5, "wxid_a", "*", "A session memory", scope_type="session", session_id="s1"),
        _item(6, "wxid_a", "*", "A other session memory", scope_type="session", session_id="s2"),
    ]

    wxbot_ranked = _rank_retrieved_memory_items(
        items,
        source_key="wxbot",
        user_id="wxid_a",
        session_id="s1",
        has_query=False,
        limit=10,
    )
    wildcard_ranked = _rank_retrieved_memory_items(
        items,
        source_key="*",
        user_id="wxid_a",
        session_id="s1",
        has_query=False,
        limit=10,
    )

    assert {item["content"] for item in wxbot_ranked} == {
        "A exact source",
        "A wildcard source",
        "A session memory",
    }
    assert {item["content"] for item in wildcard_ranked} == {
        "A wildcard source",
        "A session memory",
    }


def test_relevant_retrieval_topk_excludes_inactive_deleted_sensitive_and_cross_scope() -> None:
    items = [
        _item(1, "wxid_a", "wxbot", "manual Adidas", source_type="manual", priority=100),
        _item(2, "wxid_a", "wxbot", "explicit Puma", source_type="explicit_user", priority=50),
        _item(3, "wxid_a", "wxbot", "pending Nike", status="pending"),
        _item(4, "wxid_a", "wxbot", "invalidated Adidas", status="invalidated"),
        _item(5, "wxid_a", "wxbot", "deleted Puma", status="deleted"),
        _item(6, "wxid_a", "wxbot", "pii phone 13800138000", sensitivity="pii"),
        _item(7, "wxid_b", "wxbot", "B Adidas"),
        _item(8, "wxid_a", "other", "other source Adidas"),
        _item(9, "wxid_a", "wxbot", "deleted_at Adidas", deleted_at="2026-05-10"),
        {**_item(10, "wxid_a", "wxbot", "review Adidas"), "value_json": {"acceptance": {"status": "needs_review", "score": 0.7}}},
    ]

    ranked = _rank_retrieved_memory_items(
        items,
        source_key="wxbot",
        user_id="wxid_a",
        session_id="s1",
        has_query=True,
        limit=2,
    )

    assert [item["content"] for item in ranked] == ["manual Adidas", "explicit Puma"]


def test_session_rolling_state_tracks_and_closes_open_item() -> None:
    profile: dict[str, Any] = {}
    profile.update(
        _update_session_state(
            profile,
            session_id="s1",
            user_text="todo follow up invoice later",
            assistant_text="ok",
            created_at="2026-05-10T00:00:01",
        )
    )
    profile.update(
        _update_session_state(
            profile,
            session_id="s1",
            user_text="confirmed use concise replies",
            assistant_text="ok",
            created_at="2026-05-10T00:00:02",
        )
    )
    profile.update(
            _update_session_state(
                profile,
                session_id="s1",
                user_text="done invoice",
                assistant_text="ok",
                created_at="2026-05-10T00:00:03",
            )
    )

    assert profile["open_items"] == []
    assert len(profile["recent_turns"]) == 3
    assert any(item["kind"] == "decision" for item in profile["decisions"])
    assert any(item["kind"] == "close" for item in profile["decisions"])
    assert "Closed open item" in profile["session_summary"]


@pytest.mark.asyncio
async def test_backfill_idempotency_does_not_duplicate_events_items_or_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(_settings(), llm_service=object())
    events_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    items_by_key: dict[str, dict[str, Any]] = {}
    jobs_by_event_id: dict[int, dict[str, Any]] = {}
    session_apply_counts: list[int] = []
    identity_apply_counts: list[int] = []

    async def fake_collect_session_history(**kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "session_id": kwargs["session_id"],
                "created_at": "2026-05-10 12:00:00",
                "user_text": "记住我喜欢 Adidas",
                "assistant_text": "好的",
            },
            {
                "session_id": kwargs["session_id"],
                "created_at": "2026-05-10 12:01:00",
                "user_text": "帮我查一下",
                "assistant_text": "稍等",
            },
        ]

    async def fake_insert_backfill_event(**kwargs: Any) -> tuple[dict[str, Any], bool]:
        message = kwargs["message"]
        key = (message["created_at"], message["user_text"])
        if key in events_by_key:
            return events_by_key[key], False
        event = {
            "id": len(events_by_key) + 1,
            "trace_id": f"memory:backfill:{len(events_by_key) + 1}",
        }
        events_by_key[key] = event
        return event, True

    async def fake_apply_action(**kwargs: Any) -> dict[str, Any]:
        action = kwargs["action"]
        key = str(action["normalized_key"])
        item = items_by_key.get(key)
        if item is None:
            item = {
                "id": len(items_by_key) + 1,
                "normalized_key": key,
                "occurrence_count": 1,
                "status": action["status"],
            }
            items_by_key[key] = item
        else:
            item["occurrence_count"] += 1
        return item

    async def fake_enqueue(**kwargs: Any) -> dict[str, Any]:
        source_event_id = int(kwargs["source_event_id"])
        job = jobs_by_event_id.setdefault(
            source_event_id,
            {"id": source_event_id, "status": "pending", "source_event_id": source_event_id},
        )
        return job

    async def fake_apply_session(**kwargs: Any) -> dict[str, Any]:
        session_apply_counts.append(int(kwargs["imported_count"]))
        return {}

    async def fake_apply_identity(**kwargs: Any) -> dict[str, Any]:
        identity_apply_counts.append(int(kwargs["imported_count"]))
        return {}

    async def fake_list_session_profiles(**kwargs: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(store, "_collect_session_history", fake_collect_session_history)
    monkeypatch.setattr(store, "_insert_backfill_event", fake_insert_backfill_event)
    monkeypatch.setattr(store, "_apply_structured_memory_action", fake_apply_action)
    monkeypatch.setattr(store, "enqueue_llm_extraction_job", fake_enqueue)
    monkeypatch.setattr(store, "_apply_backfill_session_messages", fake_apply_session)
    monkeypatch.setattr(store, "_apply_backfill_identity_messages", fake_apply_identity)
    monkeypatch.setattr(store, "list_session_profiles", fake_list_session_profiles)

    kwargs = {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
        "session_ids": ["s1", "s1"],
        "enqueue_llm_jobs": True,
    }
    first = await store.backfill_from_sdk(**kwargs)
    second = await store.backfill_from_sdk(**kwargs)

    assert first["events_inserted"] == 2
    assert first["items_created"] == 1
    assert first["jobs_enqueued"] == 2
    assert second["events_inserted"] == 0
    assert second["duplicate_count"] == 2
    assert second["items_created"] == 0
    assert second["jobs_enqueued"] == 0
    assert len(events_by_key) == 2
    assert len(items_by_key) == 1
    assert len(jobs_by_event_id) == 2
    assert session_apply_counts == [2]
    assert identity_apply_counts == [2, 0]


@pytest.mark.asyncio
async def test_job_queue_idempotency_retry_and_dead_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
        "session_id": "s1",
        "trace_id": "trace-1",
        "source_event_id": 7,
    }
    assert _job_idempotency_key(**base) == _job_idempotency_key(**{**base, "trace_id": "trace-2"})
    assert _job_idempotency_key(**base) != _job_idempotency_key(**{**base, "user_id": "wxid_b"})

    class _FailingLLM:
        async def chat(self, request: Any) -> None:
            raise RuntimeError("llm failed")

    store = MemoryStore(_settings(), llm_service=_FailingLLM())
    status_updates: list[str] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        if "FROM plugin_memory_event WHERE id = :id" in sql:
            return [
                {
                    "id": 7,
                    "user_text": "记住我喜欢 Adidas",
                    "assistant_text": "好的",
                    "trace_id": "trace-1",
                }
            ]
        if "UPDATE plugin_memory_extraction_job SET" in sql and params:
            status_updates.append(str(params["status"]))
        return []

    async def fake_list_memory_items(**kwargs: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "list_memory_items", fake_list_memory_items)

    failed = await store.process_llm_extraction_job(
        _job(attempts=0, max_attempts=2),
        scope_execution_allowed=_allow_memory_scope,
    )
    dead = await store.process_llm_extraction_job(
        _job(attempts=1, max_attempts=2),
        scope_execution_allowed=_allow_memory_scope,
    )

    assert failed == "failed"
    assert dead == "dead"
    assert status_updates == ["failed", "dead"]


def test_prompt_injection_order_budget_and_sensitive_filtering() -> None:
    ordered_markers = _fixture()["prompt_order"]
    prompt = augment_prompt_with_persona_and_memory(
        "base",
        _session(
            {
                "session_summary": "Recent context: current request overrides old memory.",
                "open_items": [{"text": "todo check invoice"}],
                "decisions": [{"text": "confirmed use concise replies"}],
                "recent_turns": [{"user_text": "刚刚改了预算", "assistant_text": "收到"}],
                "short_term": "用户最近说：刚刚改了预算",
                "manual_notes": "手机号 13800138000 不应通过 fallback 注入",
                "memory_items": {
                    "identity": [
                        {
                            "source_type": "manual",
                            "status": "active",
                            "confidence": 1.0,
                            "sensitivity": "normal",
                            "content": "人工标记为 VIP",
                        },
                        {
                            "source_type": "explicit_user",
                            "status": "active",
                            "confidence": 0.95,
                            "sensitivity": "normal",
                            "pinned": True,
                            "content": "以后默认发顺丰",
                        },
                        {
                            "source_type": "auto",
                            "status": "pending",
                            "confidence": 0.9,
                            "sensitivity": "pii",
                            "content": "手机号 13800138000",
                        },
                        {
                            "source_type": "auto",
                            "status": "active",
                            "confidence": 0.95,
                            "sensitivity": "sensitive",
                            "content": "api key sk-test-token",
                        },
                    ],
                    "session": [
                        {
                            "source_type": "manual",
                            "status": "active",
                            "confidence": 1.0,
                            "sensitivity": "normal",
                            "content": "当前会话只处理发票",
                        }
                    ],
                },
                "relevant_memory_items": [
                    {
                        "source_type": "auto",
                        "status": "active",
                        "confidence": 0.9,
                        "sensitivity": "normal",
                        "content": "用户喜欢 Adidas 相关内容",
                    },
                    {
                        "source_type": "auto",
                        "status": "active",
                        "confidence": 0.9,
                        "sensitivity": "pii",
                        "content": "收货地址 上海市浦东新区测试路1号",
                    },
                ],
                "relevant_graph_facts": [
                    {
                        "memory_item_id": 8,
                        "subject_name": "用户",
                        "predicate": "prefers_response_style",
                        "object_value": "默认简洁中文回复",
                    }
                ],
                "relevant_graph_episodes": [
                    {
                        "memory_item_ids": [9],
                        "title": "用户确认发票流程",
                        "summary": "当前会话需要先核对发票信息",
                    }
                ],
                "memory_graph_budget_chars": 300,
            }
        ),
        memory_intro="memory",
        memory_budget_chars=700,
    )

    positions = [prompt.index(marker) for marker in ordered_markers]
    assert positions == sorted(positions)
    assert "当前用户本轮明确表达优先于历史记忆" in prompt
    assert "人工记忆、置顶记忆和用户明确要求记住的内容优先于自动记忆" in prompt
    assert "人工标记为 VIP" in prompt
    assert "以后默认发顺丰" in prompt
    assert "当前会话只处理发票" in prompt
    assert "手机号 13800138000" not in prompt
    assert "sk-test-token" not in prompt
    assert "上海市浦东新区测试路1号" not in prompt
    assert "相关内容" in prompt
    assert len(prompt[prompt.index("memory") :]) < 1200


def _item(
    item_id: int,
    user_id: str,
    source_key: str,
    content: str,
    *,
    source_type: str = "auto",
    status: str = "active",
    sensitivity: str = "normal",
    scope_type: str = "identity",
    session_id: str = "",
    priority: int = 0,
    deleted_at: Any = None,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": source_key,
        "user_id": user_id,
        "session_id": session_id,
        "scope_type": scope_type,
        "source_type": source_type,
        "memory_type": "note",
        "content": content,
        "value_json": "{}",
        "normalized_key": f"note:text:{item_id}",
        "confidence": 0.95,
        "status": status,
        "pinned": source_type == "manual",
        "priority": priority,
        "sensitivity": sensitivity,
        "source_event_id": None,
        "source_trace_id": "",
        "original_text": content,
        "occurrence_count": 1,
        "first_seen_at": f"2026-05-10T00:00:{item_id:02d}",
        "last_seen_at": f"2026-05-10T00:00:{item_id:02d}",
        "created_at": f"2026-05-10T00:00:{item_id:02d}",
        "updated_at": f"2026-05-10T00:00:{item_id:02d}",
        "deleted_at": deleted_at,
        "match_count": 1 if "Adidas" in content or "Puma" in content else 0,
    }


def _job(**kwargs: Any) -> dict[str, Any]:
    data = {
        "id": 9,
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
        "session_id": "s1",
        "source_event_id": 7,
        "source_trace_id": "trace-1",
        "status": "running",
        "attempts": 0,
        "max_attempts": 2,
    }
    data.update(kwargs)
    return data
