from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any

import pytest

import plugins.memory.store as memory_store_module
from plugins.memory.plugin import MemoryPlugin
from plugins.memory.store import MemoryStore
from plugins.memory.store_group_graph import (
    _group_graph_edge_quality,
    _prefer_operator_group_session_id,
)


def test_group_graph_edge_quality_reads_safe_value_payload() -> None:
    quality = _group_graph_edge_quality(
        {
            "value": {
                "evidence_dates": ["2026-08-30", "2026-08-31"],
                "acceptance": {"score": 0.72, "reason": "window_relation"},
            }
        }
    )

    assert quality["evidence_dates"] == ["2026-08-30", "2026-08-31"]
    assert quality["acceptance_score"] == 0.72
    assert quality["acceptance_reason"] == "window_relation"


@pytest.mark.asyncio
async def test_list_imported_group_graph_targets_keeps_group_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        if "FROM sessions" in sql:
            return []
        if "FROM plugin_wxbot_group_observations" in sql:
            return []
        assert "plugin_memory_event" in sql
        assert params is not None
        assert "group_uid" not in params
        return [
            {
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "session_id": "room-a@chatroom",
                "day": date(2026, 8, 31),
                "event_count": 12,
                "last_event_id": 512,
            },
            {
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "session_id": "direct-user",
                "day": date(2026, 8, 31),
                "event_count": 9,
            },
            {
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "session_id": "room-b@chatroom",
                "day": "2026-08-30",
                "event_count": 4,
                "last_event_id": 404,
            },
        ]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    targets = await store.list_imported_group_graph_targets(lookback_days=2, max_targets=3)

    assert [item["session_id"] for item in targets] == [
        "room-a@chatroom",
        "room-b@chatroom",
    ]
    assert targets[0]["date"] == "2026-08-31"
    assert targets[1]["date"] == "2026-08-30"
    assert targets[0]["event_count"] == 12
    assert targets[0]["last_event_id"] == 512


@pytest.mark.asyncio
async def test_auto_extract_tick_skips_disabled_scope_and_runs_deterministic_catchup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    store.runtime_scope_gates_required = True
    async def allowed(tenant_id: str, session_id: str = "") -> bool:
        return tenant_id == "demo" and session_id == "room-a@chatroom"

    store.combined_history_scope_execution_allowed = allowed
    catchup_calls: list[dict[str, Any]] = []
    saved_cursors: list[dict[str, Any]] = []

    async def fake_targets(**kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "session_id": "room-a@chatroom",
                "date": "2026-08-31",
                "event_count": 8,
            },
            {
                "tenant_id": "other",
                "channel": "wechat",
                "source_key": "wxbot",
                "session_id": "room-b@chatroom",
                "date": "2026-08-31",
                "event_count": 3,
            },
        ]

    async def fake_catchup(**kwargs: Any) -> dict[str, Any]:
        catchup_calls.append(kwargs)
        return {
            "status": "completed",
            "stop_reason": "no_more_events",
            "totals": {"windows": 1, "applied": 2},
            "more_remain": False,
            "next_cursor_event_id": 500,
        }

    async def fake_load_cursor(**kwargs: Any) -> int:
        return 400

    async def fake_save_cursor(**kwargs: Any) -> None:
        saved_cursors.append(kwargs)

    monkeypatch.setattr(store, "list_imported_group_graph_targets", fake_targets)
    monkeypatch.setattr(store, "run_group_relationship_window_catchup", fake_catchup)
    monkeypatch.setattr(store, "_load_group_graph_auto_extract_cursor", fake_load_cursor)
    monkeypatch.setattr(store, "_save_group_graph_auto_extract_cursor", fake_save_cursor)

    result = await store.run_group_graph_auto_extract_tick(
        lookback_days=2,
        max_sessions=3,
        include_llm=False,
    )

    assert result["ok"] is True
    assert result["include_llm"] is False
    assert result["ran"] == 1
    assert result["skipped"] == [
        {
            "tenant_id": "other",
            "channel": "wechat",
            "source_key": "wxbot",
            "session_id": "room-b@chatroom",
            "date": "2026-08-31",
            "event_count": 3,
            "reason": "scope_disabled",
        }
    ]
    assert catchup_calls == [
        {
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "session_id": "room-a@chatroom",
            "date": "2026-08-31",
            "window_size": 50,
            "max_windows_per_run": 20,
            "cursor_event_id": 400,
            "time_budget_seconds": 180,
            "include_llm": False,
            # Targets without a source keep the legacy auto-fallback loader.
            "source": None,
            # The tick never waits on the model inline.
            "llm_mode": "enqueue",
            "llm_timeout_seconds": None,
        }
    ]
    assert saved_cursors[0]["cursor_event_id"] == 500
    assert "source" not in saved_cursors[0]
    assert result["skipped_reasons"] == {"scope_disabled": 1}
    assert result["applied"] == 2


@pytest.mark.asyncio
async def test_auto_extract_tick_defaults_to_llm_catchup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    catchup_calls: list[dict[str, Any]] = []

    async def fake_targets(**kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "session_id": "room-a@chatroom",
                "date": "2026-08-31",
                "event_count": 8,
            }
        ]

    async def fake_catchup(**kwargs: Any) -> dict[str, Any]:
        catchup_calls.append(kwargs)
        return {"status": "completed", "totals": {}, "more_remain": False}

    monkeypatch.setattr(store, "list_imported_group_graph_targets", fake_targets)
    monkeypatch.setattr(store, "run_group_relationship_window_catchup", fake_catchup)
    monkeypatch.setattr(
        store, "_load_group_graph_auto_extract_cursor", lambda **kwargs: asyncio.sleep(0, result=0)
    )

    result = await store.run_group_graph_auto_extract_tick()

    assert result["include_llm"] is True
    assert catchup_calls[0]["include_llm"] is True
    assert catchup_calls[0]["max_windows_per_run"] == 20
    assert catchup_calls[0]["time_budget_seconds"] == 180


@pytest.mark.asyncio
async def test_window_extraction_include_llm_false_does_not_call_extractor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    store.graph_extractor.config = SimpleNamespace(enabled=True)
    store.graph_extractor.llm_service = object()
    extract_calls: list[dict[str, Any]] = []

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
                    "user_text": "wxid_a: hello",
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
                    "user_text": "wxid_b: 回复: hi",
                    "assistant_text": "",
                    "trace_id": "trace-502",
                    "event_key": "event-502",
                    "created_at": "2026-05-15T08:01:00",
                },
            ]
        return []

    async def fake_extract(**kwargs: Any) -> dict[str, Any]:
        extract_calls.append(kwargs)
        return {}

    async def fake_apply(**kwargs: Any) -> dict[str, Any]:
        return {"id": 1}

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_extract_group_relationship_window_candidates", fake_extract)
    monkeypatch.setattr(store, "_apply_group_relationship_window_candidate", fake_apply)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", noop)
    monkeypatch.setattr(store, "_sync_memory_graph_for_item_safe", noop)
    monkeypatch.setattr(store, "_sync_memory_vector_for_item_safe", noop)

    result = await store.run_group_relationship_window_extraction(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="room-a@chatroom",
        date="2026-05-15",
        include_llm=False,
    )

    assert extract_calls == []
    assert result["controls"]["include_llm"] is False
    assert "llm_window_extractor" not in result["generated_from"]
    assert result["totals"]["candidates"] >= 1


@pytest.mark.asyncio
async def test_window_extraction_cancels_hung_llm_at_configured_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    store.graph_extractor.config = SimpleNamespace(enabled=True)
    store.graph_extractor.llm_service = object()
    cancelled = asyncio.Event()

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        if "FROM plugin_memory_event" in sql and "id > :cursor_event_id" in sql:
            return [
                {
                    "id": 601,
                    "user_id": "__group__",
                    "user_text": "wxid_a: hello",
                    "created_at": "2026-05-15T08:00:00",
                },
                {
                    "id": 602,
                    "user_id": "__group__",
                    "user_text": "wxid_b: 回复: hi",
                    "created_at": "2026-05-15T08:01:00",
                },
            ]
        return []

    async def hung_extract(**kwargs: Any) -> dict[str, Any]:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def fake_apply(**kwargs: Any) -> dict[str, Any]:
        return {"id": 1}

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_extract_group_relationship_window_candidates", hung_extract)
    monkeypatch.setattr(store, "_apply_group_relationship_window_candidate", fake_apply)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", noop)
    monkeypatch.setattr(store, "_sync_memory_graph_for_item_safe", noop)
    monkeypatch.setattr(store, "_sync_memory_vector_for_item_safe", noop)

    result = await store.run_group_relationship_window_extraction(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="room-a@chatroom",
        date="2026-05-15",
        include_llm=True,
        llm_timeout_seconds=1,
    )

    assert result["controls"]["llm_timeout_seconds"] == 1
    assert result["status"] == "partial"
    assert result["next_cursor_event_id"] == 602
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_auto_extract_tick_can_sync_known_sessions_before_catchup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    backfill_calls: list[dict[str, Any]] = []
    catchup_calls: list[dict[str, Any]] = []

    async def fake_sessions(**kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "session_id": "room-a@chatroom",
                "event_count": 8,
            }
        ]

    async def fake_targets(**kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "session_id": "room-a@chatroom",
                "date": "2026-08-31",
                "event_count": 8,
            }
        ]

    async def fake_backfill(**kwargs: Any) -> dict[str, Any]:
        backfill_calls.append(kwargs)
        return {"ok": True, "imported_count": 3, "events_inserted": 3}

    async def fake_catchup(**kwargs: Any) -> dict[str, Any]:
        catchup_calls.append(kwargs)
        return {"status": "completed", "totals": {}, "more_remain": False}

    async def allowed(tenant_id: str, session_id: str = "") -> bool:
        return tenant_id == "demo"

    store.combined_history_scope_execution_allowed = allowed
    monkeypatch.setattr(store, "list_known_group_graph_sessions", fake_sessions)
    monkeypatch.setattr(store, "list_imported_group_graph_targets", fake_targets)
    monkeypatch.setattr(store, "backfill_from_sdk", fake_backfill)
    monkeypatch.setattr(store, "run_group_relationship_window_catchup", fake_catchup)
    monkeypatch.setattr(
        store, "_load_group_graph_auto_extract_cursor", lambda **kwargs: asyncio.sleep(0, result=0)
    )

    result = await store.run_group_graph_auto_extract_tick(
        lookback_days=7,
        sync_missing_history=True,
        include_llm=False,
    )

    assert result["sync_missing_history"] is True
    assert result["synced"][0]["session_id"] == "room-a@chatroom"
    assert backfill_calls[0]["session_ids"] == ["room-a@chatroom"]
    assert backfill_calls[0]["days_limit"] == 7
    assert backfill_calls[0]["enqueue_llm_jobs"] is False
    assert catchup_calls[0]["session_id"] == "room-a@chatroom"


def test_group_graph_auto_extract_only_starts_on_configured_roles() -> None:
    plugin = MemoryPlugin()
    plugin._ctx = SimpleNamespace(
        settings=SimpleNamespace(
            memory_group_graph_auto_extract_enabled=True,
            memory_group_graph_auto_extract_roles="scheduler",
            app_process_role="api",
        )
    )
    assert plugin._should_run_group_graph_auto_extract() is False

    plugin._ctx.settings.app_process_role = "scheduler"
    assert plugin._should_run_group_graph_auto_extract() is True

    plugin._ctx.settings.memory_group_graph_auto_extract_enabled = False
    assert plugin._should_run_group_graph_auto_extract() is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("acceptance_status", "item_status"),
    [("accepted", "active"), ("rejected", "pending")],
)
async def test_window_candidate_rerun_preserves_human_review(
    monkeypatch: pytest.MonkeyPatch,
    acceptance_status: str,
    item_status: str,
) -> None:
    store = MemoryStore(SimpleNamespace())
    inserted: list[dict[str, Any]] = []

    async def fake_find(**kwargs: Any) -> list[dict[str, Any]]:
        return [{
            "id": 91,
            "source_type": "deterministic_group_window",
            "status": item_status,
            "acceptance_status": acceptance_status,
            "value": {
                "relation": {"confidence": 0.6, "evidence_event_ids": [10]},
                "evidence_dates": ["2026-08-31"],
                "acceptance": {
                    "status": acceptance_status,
                    "reviewed_by": "human-admin",
                    "history": [{"action": acceptance_status}],
                },
            },
        }]

    async def fake_insert(**kwargs: Any) -> dict[str, Any]:
        inserted.append(kwargs)
        return {"id": 91, **kwargs}

    monkeypatch.setattr(store, "_find_memory_item_by_normalized_key", fake_find)
    monkeypatch.setattr(store, "_insert_or_touch_memory_item", fake_insert)

    await store._apply_group_relationship_window_candidate(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="__group__",
        session_id="room-a@chatroom",
        target_date="2026-08-31",
        window={"index": 2, "first_event_id": 10, "last_event_id": 20},
        candidate={
            "subject": "wxid_a",
            "subject_type": "person",
            "predicate": "knows",
            "object": "wxid_b",
            "object_type": "person",
            "confidence": 0.8,
            "evidence_event_ids": [20],
            "reason": "rerun",
            "extraction_method": "llm_group_window",
        },
    )

    assert inserted[0]["source_type"] == "deterministic_group_window"
    assert inserted[0]["status"] == item_status
    acceptance = inserted[0]["value_json"]["acceptance"]
    assert acceptance["status"] == acceptance_status
    assert acceptance["reviewed_by"] == "human-admin"
    assert acceptance["history"] == [{"action": acceptance_status}]


def _candidate(**overrides: Any) -> dict[str, Any]:
    base = {
        "subject": "wxid_a",
        "subject_type": "person",
        "predicate": "replied_to",
        "object": "wxid_b",
        "object_type": "person",
        "confidence": 0.9,
        "evidence_event_ids": [1, 8],
        "reason": "deterministic_quote_reply",
        "extraction_method": "deterministic_group_window",
        "signals": {"quote": 1, "mention": 0, "prefix_reply": 0, "co_participation": 0, "llm": 0},
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_window_candidate_direct_signal_is_accepted_on_first_sight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace(memory_group_graph_auto_accept=True))
    inserted: list[dict[str, Any]] = []

    async def fake_find(**kwargs: Any) -> list[dict[str, Any]]:
        return []

    async def fake_insert(**kwargs: Any) -> dict[str, Any]:
        inserted.append(kwargs)
        return {"id": 11, **kwargs}

    monkeypatch.setattr(store, "_find_memory_item_by_normalized_key", fake_find)
    monkeypatch.setattr(store, "_insert_or_touch_memory_item", fake_insert)

    await store._apply_group_relationship_window_candidate(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="__group__",
        session_id="room-a@chatroom",
        target_date="2026-08-31",
        window={"index": 1, "first_event_id": 1, "last_event_id": 8},
        candidate=_candidate(),
    )

    assert inserted[0]["status"] == "active"
    value = inserted[0]["value_json"]
    assert value["acceptance"]["status"] == "accepted"
    assert value["acceptance"]["reason"] == "group_window_direct_signal"
    assert value["acceptance"]["reviewed_by"] == "system/auto"
    assert value["acceptance"]["signals"]["quote"] == 1
    assert value["relation"]["signals"]["quote"] == 1
    assert 0.25 < value["relation"]["strength"] < 0.35


@pytest.mark.asyncio
async def test_window_candidate_weak_signal_waits_for_repetition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """co_participated and single-day LLM edges stay needs_review; a second day
    with evidence promotes an LLM edge to accepted and activates the item."""

    store = MemoryStore(SimpleNamespace(memory_group_graph_auto_accept=True))
    inserted: list[dict[str, Any]] = []
    existing: list[dict[str, Any]] = []

    async def fake_find(**kwargs: Any) -> list[dict[str, Any]]:
        return list(existing)

    async def fake_insert(**kwargs: Any) -> dict[str, Any]:
        inserted.append(kwargs)
        return {"id": 11, **kwargs}

    monkeypatch.setattr(store, "_find_memory_item_by_normalized_key", fake_find)
    monkeypatch.setattr(store, "_insert_or_touch_memory_item", fake_insert)
    scope = {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "__group__",
        "session_id": "room-a@chatroom",
        "window": {"index": 1, "first_event_id": 1, "last_event_id": 8},
    }

    await store._apply_group_relationship_window_candidate(
        **scope,
        target_date="2026-08-31",
        candidate=_candidate(
            predicate="co_participated",
            confidence=0.45,
            reason="deterministic_same_window_participation",
            signals={"quote": 0, "mention": 0, "prefix_reply": 0, "co_participation": 1, "llm": 0},
        ),
    )
    assert inserted[-1]["status"] == "pending"
    assert inserted[-1]["value_json"]["acceptance"]["status"] == "needs_review"
    assert inserted[-1]["value_json"]["acceptance"]["reason"] == "group_window_weak_signal"

    llm_day_one = _candidate(
        predicate="asked",
        confidence=0.88,
        reason="asked how to configure kiro2api",
        extraction_method="llm_group_window",
        signals={"quote": 0, "mention": 0, "prefix_reply": 0, "co_participation": 0, "llm": 1},
    )
    await store._apply_group_relationship_window_candidate(
        **scope, target_date="2026-08-31", candidate=llm_day_one
    )
    day_one = inserted[-1]
    assert day_one["status"] == "pending"
    assert day_one["value_json"]["acceptance"]["status"] == "needs_review"

    existing.append(
        {
            "id": 11,
            "source_type": "llm_group_window",
            "status": "pending",
            "acceptance_status": "needs_review",
            "value": day_one["value_json"],
        }
    )
    await store._apply_group_relationship_window_candidate(
        **scope,
        target_date="2026-09-01",
        candidate={**llm_day_one, "evidence_event_ids": [21]},
    )
    day_two = inserted[-1]
    value = day_two["value_json"]
    assert value["evidence_dates"] == ["2026-08-31", "2026-09-01"]
    assert value["relation"]["signals"]["llm"] == 2
    assert value["acceptance"]["status"] == "accepted"
    assert value["acceptance"]["reason"] == "group_window_llm_multi_day"
    assert value["acceptance"]["day_count"] == 2
    assert day_two["status"] == "active"
    assert value["relation"]["strength"] > day_one["value_json"]["relation"]["strength"]


@pytest.mark.asyncio
async def test_window_candidate_auto_accept_disabled_keeps_everything_in_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace(memory_group_graph_auto_accept=False))
    inserted: list[dict[str, Any]] = []

    async def fake_find(**kwargs: Any) -> list[dict[str, Any]]:
        return []

    async def fake_insert(**kwargs: Any) -> dict[str, Any]:
        inserted.append(kwargs)
        return {"id": 11, **kwargs}

    monkeypatch.setattr(store, "_find_memory_item_by_normalized_key", fake_find)
    monkeypatch.setattr(store, "_insert_or_touch_memory_item", fake_insert)

    await store._apply_group_relationship_window_candidate(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="__group__",
        session_id="room-a@chatroom",
        target_date="2026-08-31",
        window={"index": 1, "first_event_id": 1, "last_event_id": 8},
        candidate=_candidate(),
    )

    assert inserted[0]["status"] == "pending"
    assert inserted[0]["value_json"]["acceptance"]["status"] == "needs_review"


def _observation_row(
    row_id: int,
    sender: str,
    content: str,
    *,
    sender_name: str = "",
    quote_from: str = "",
    quote_sender_name: str = "",
    at_wxids: list[str] | None = None,
    bot_wxid: str = "wxid_bot",
) -> dict[str, Any]:
    return {
        "id": row_id,
        "user_id": sender,
        "user_text": f"{sender}: {content}",
        "_graph_source": "observation",
        "_observation": {
            "sender_name": sender_name,
            "quote_from": quote_from,
            "quote_sender_name": quote_sender_name,
            "quote_message_id": "",
            "at_wxids": list(at_wxids or []),
            "bot_wxid": bot_wxid,
        },
    }


def test_deterministic_rules_use_quote_and_mention_metadata() -> None:
    store = MemoryStore(SimpleNamespace())
    from plugins.memory.store_group_graph import GroupMemberDirectory

    directory = GroupMemberDirectory()
    # Membership knows the alias id form for 千羽 that quotes use.
    directory.add_member("qianyu_alias", "千羽")
    window = {
        "index": 1,
        "first_event_id": 1,
        "last_event_id": 6,
        "rows": [
            _observation_row(1, "wxid_qianyu", "kiro2api 配置怎么弄", sender_name="千羽"),
            _observation_row(2, "wxid_hai", "看 README", sender_name="小海",
                             quote_from="qianyu_alias", quote_sender_name="千羽"),
            _observation_row(3, "wxid_z", "@小海\u2005 那个链接失效了", sender_name="Z"),
            _observation_row(4, "wxid_hai", "@Z 修好了", sender_name="小海", at_wxids=["wxid_z"]),
            _observation_row(5, "wxid_z", "@机器人 帮我总结", sender_name="Z", at_wxids=["wxid_bot"]),
            _observation_row(6, "wxid_new", "求助", sender_name="新人",
                             quote_from="wxid_unknown_alias", quote_sender_name="不存在的人"),
        ],
    }

    candidates = store._build_deterministic_group_window_candidates(window, directory=directory)

    by_key = {(c["subject"], c["predicate"], c["object"]): c for c in candidates}
    # Quoted reply resolved through the alias → nickname → sender id chain.
    quote = by_key[("wxid_hai", "replied_to", "wxid_qianyu")]
    assert quote["confidence"] == 0.9
    assert quote["reason"] == "deterministic_quote_reply"
    assert quote["signals"]["quote"] == 1
    assert quote["evidence_event_ids"] == [2]
    # Text @昵称 resolves through the directory built from the window itself.
    text_mention = by_key[("wxid_z", "addressed", "wxid_hai")]
    assert text_mention["confidence"] == 0.72
    assert text_mention["signals"]["mention"] == 1
    # Structured at_wxids beat text matching.
    metadata_mention = by_key[("wxid_hai", "addressed", "wxid_z")]
    assert metadata_mention["confidence"] == 0.85
    assert metadata_mention["reason"] == "deterministic_at_mention_metadata"
    # Mentions of the bot itself and unresolvable quote targets are not edges.
    assert not any(c["object"] == "wxid_bot" for c in candidates)
    assert not any(c["subject"] == "wxid_new" for c in candidates)
    assert window["unresolved_targets"] == 1
    # No co_participated pairs by default even though several people spoke twice.
    assert not any(c["predicate"] == "co_participated" for c in candidates)


def test_deterministic_rules_can_opt_back_into_co_participation() -> None:
    store = MemoryStore(SimpleNamespace(memory_group_graph_co_participation_edges=True))
    window = {
        "index": 1,
        "first_event_id": 1,
        "last_event_id": 4,
        "rows": [
            {"id": 1, "user_text": "wxid_a: hello"},
            {"id": 2, "user_text": "wxid_b: hi"},
            {"id": 3, "user_text": "wxid_a: how are you"},
            {"id": 4, "user_text": "wxid_b: fine"},
        ],
    }

    candidates = store._build_deterministic_group_window_candidates(window)

    assert [(c["predicate"], c["signals"]["co_participation"]) for c in candidates] == [
        ("co_participated", 1)
    ]
    assert store._build_deterministic_group_window_candidates(
        window, co_participation_edges=False
    ) == []


@pytest.mark.asyncio
async def test_member_directory_merges_window_senders_and_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        assert "FROM plugin_wxbot_group_membership" in sql
        assert params is not None and params["sids"] == ["room-a@chatroom"]
        return [
            {"user_wxid": "wxid_hai", "user_name": "小海"},
            {"user_wxid": "hai_alias", "user_name": "小海"},
            {"user_wxid": "wxid_quiet", "user_name": "安静的人"},
        ]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    directory = await store._load_group_member_directory(
        tenant_id="demo",
        session_ids=["room-a@chatroom", "not-a-group"],
        event_rows=[_observation_row(1, "wxid_hai", "hi", sender_name="小海")],
    )

    # The sender id wins over the alias that shares the nickname.
    assert directory.resolve_id("hai_alias") == "wxid_hai"
    assert directory.resolve_id("", fallback_name="小海") == "wxid_hai"
    assert directory.resolve_name("小海 ") == "wxid_hai"
    assert directory.resolve_id("wxid_quiet") == "wxid_quiet"
    assert directory.resolve_id("wxid_nobody") is None
    assert directory.resolve_id("user:wxid_hai") == "wxid_hai"


@pytest.mark.asyncio
async def test_graph_fact_query_pushes_filters_into_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    seen: list[tuple[str, dict[str, Any]]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        seen.append((sql, dict(params or {})))
        if sql.startswith("SELECT COUNT(*)"):
            return [{"count": 1752}]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    await store.list_memory_graph_facts(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_ids=["53876528317@chatroom", "cx1:c:abc@chatroom"],
        status="active",
        limit=500,
        predicates=["replied_to", "addressed"],
        min_confidence=0.6,
        from_date="2026-08-16",
        to_date="2026-09-15",
        default_accepted_only=True,
        order_by_strength=True,
    )
    sql, params = seen[0]
    assert "LEFT JOIN plugin_memory_item backing ON backing.id = fact.memory_item_id" in sql
    assert "fact.predicate = ANY(:predicates)" in sql
    assert params["predicates"] == ["replied_to", "addressed"]
    assert "fact.confidence >= :min_confidence" in sql and params["min_confidence"] == 0.6
    assert "= 'accepted' AND backing.status = 'active' AND backing.deleted_at IS NULL" in sql
    assert "'last_seen_date'" in sql and ">= :from_date" in sql and params["from_date"] == "2026-08-16"
    assert "'first_seen_date'" in sql and "<= :to_date" in sql and params["to_date"] == "2026-09-15"
    assert "ORDER BY COALESCE((NULLIF(backing.value_json, '')::jsonb #>> '{relation,strength}')::float, 0) DESC" in sql
    assert sql.rstrip().endswith("LIMIT :lim") and params["lim"] == 500
    # Explicit statuses replace the default accepted-only clause.
    seen.clear()
    await store.list_memory_graph_facts(
        tenant_id="demo", acceptance_statuses=["needs_review", "expired"], limit=10
    )
    sql, params = seen[0]
    assert "= ANY(:acceptance_statuses)" in sql
    assert params["acceptance_statuses"] == ["needs_review", "expired"]
    assert "backing.deleted_at IS NULL" not in sql

    total = await store.count_memory_graph_facts(
        tenant_id="demo", channel="wechat", session_ids=["room-a@chatroom"], predicates=["replied_to"]
    )
    assert total == 1752
    count_sql, count_params = seen[-1]
    assert count_sql.startswith("SELECT COUNT(*) AS count FROM plugin_memory_fact fact")
    assert "backing.session_id = ANY(:sids)" in count_sql and count_params["sids"] == ["room-a@chatroom"]


def _entity_row(entity_id: int, name: str, entity_type: str = "person") -> dict[str, Any]:
    return {
        "id": entity_id, "tenant_id": "demo", "channel": "wechat", "source_key": "wxbot",
        "user_id": "__group__", "entity_type": entity_type, "name": name, "normalized_name": name.lower(),
        "aliases_json": "[]", "confidence": 0.9, "status": "active",
        "created_at": "2026-09-07T08:00:00", "updated_at": "2026-09-07T09:00:00",
    }


def _fact_row(fact_id: int, subject: int, obj: int, predicate: str, item_id: int) -> dict[str, Any]:
    return {
        "id": fact_id, "tenant_id": "demo", "channel": "wechat", "source_key": "wxbot", "user_id": "__group__",
        "subject_entity_id": subject, "subject_name": f"e{subject}", "predicate": predicate,
        "object_entity_id": obj, "object_name": f"e{obj}", "object_value": "", "memory_item_id": item_id,
        "source_event_id": None, "confidence": 0.9 if predicate == "replied_to" else 0.45, "status": "active",
        "valid_at": "2026-09-07T08:36:21", "invalid_at": None,
        "created_at": "2026-09-07T08:36:21", "updated_at": "2026-09-07T09:20:57",
    }


def _item_row(item_id: int, predicate: str, dates: list[str], observation_ids: list[int], strength: float) -> dict[str, Any]:
    value = {
        "kind": "group_window_relation",
        "evidence_dates": dates,
        "first_seen_date": min(dates),
        "last_seen_date": max(dates),
        "evidence_source": "observation",
        "source_event_ids": [],
        "source_observation_ids": observation_ids,
        "relation": {
            "predicate": predicate,
            "evidence_event_ids": [],
            "evidence_observation_ids": observation_ids,
            "signals": {"quote": len(observation_ids) if predicate == "replied_to" else 0, "mention": 0,
                        "prefix_reply": 0, "co_participation": 0 if predicate == "replied_to" else 1, "llm": 0},
            "strength": strength,
        },
        "acceptance": {"status": "accepted", "reason": "group_window_direct_signal"},
    }
    return {
        "id": item_id, "tenant_id": "demo", "channel": "wechat", "source_key": "wxbot", "user_id": "__group__",
        "session_id": "53876528317@chatroom", "scope_type": "session", "source_type": "deterministic_group_window",
        "memory_type": "note", "content": "Group window relation", "value_json": json.dumps(value),
        "normalized_key": f"group-window-rel:{predicate}:{item_id}", "confidence": 0.9, "status": "active",
        "pinned": False, "priority": 0, "sensitivity": "normal", "source_event_id": None, "source_trace_id": "",
        "original_text": "", "occurrence_count": 1, "first_seen_at": None, "last_seen_at": None,
        "created_at": "2026-09-07T08:36:21", "updated_at": "2026-09-07T09:20:57", "deleted_at": None,
    }


@pytest.mark.asyncio
async def test_graph_read_reports_evidence_dates_merges_pairs_and_backfills_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    seen: list[str] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        params = params or {}
        seen.append(sql)
        if "FROM sessions" in sql:
            return _ALIAS_ROWS
        if sql.startswith("SELECT COUNT(*)"):
            return [{"count": 2780}]
        if "FROM plugin_memory_entity entity" in sql and "entity.id = ANY(:ids)" in sql:
            # Endpoints the recency-ordered entity page did not include.
            assert set(params["ids"]) == {2, 3}
            return [_entity_row(2, "千羽"), _entity_row(3, "小海")]
        if "FROM plugin_memory_entity entity" in sql:
            return [_entity_row(1, "洋白")]
        if "FROM plugin_memory_fact fact" in sql:
            return [
                _fact_row(10, 1, 2, "co_participated", 100),
                _fact_row(11, 2, 1, "co_participated", 101),
                _fact_row(12, 1, 3, "replied_to", 102),
            ]
        if "FROM plugin_memory_item WHERE id = ANY" in sql:
            return [
                _item_row(100, "co_participated", ["2026-08-09", "2026-09-04"], [154604, 154626], 0.2835),
                _item_row(101, "co_participated", ["2026-08-16"], [95955], 0.2835),
                _item_row(102, "replied_to", ["2026-08-20", "2026-08-21", "2026-09-02"], [7001, 7002, 7003, 7004], 0.7364),
            ]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    graph = await store.get_group_relationship_graph(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="cx1:c:abc@chatroom",
        from_="2026-08-16T00:00:00",
        limit=500,
    )

    edges = {edge["type"]: edge for edge in graph["edges"]}
    assert set(edges) == {"co_participated", "replied_to"}
    assert graph["counts"] == {"nodes": 3, "edges": 2}
    # A→B and B→A co-participation rows became one undirected edge.
    pair = edges["co_participated"]
    assert pair["mirror_ids"] == ["fact:11"]
    assert pair["evidence_count"] == 3
    assert pair["evidence_dates"] == ["2026-08-09", "2026-08-16", "2026-09-04"]
    assert pair["first_seen"] == "2026-08-09" and pair["last_seen"] == "2026-09-04"
    # Evidence dates drive first/last_seen; the extraction time is separate.
    reply = edges["replied_to"]
    assert reply["first_seen"] == "2026-08-20"
    assert reply["last_seen"] == "2026-09-02"
    assert reply["extracted_at"] == "2026-09-07T09:20:57"
    assert reply["evidence_count"] == 4
    assert reply["evidence_day_count"] == 3
    assert reply["strength"] == 0.7364
    assert reply["signals"]["quote"] == 4
    assert reply["evidence_source"] == "observation"
    # Endpoint 3 (小海) only existed through the by-id backfill.
    assert {node["display_label"] for node in graph["nodes"]} == {"洋白", "千羽", "小海"}
    assert graph["page"] == {
        "limit": 500,
        "total": 2780,
        "truncated": True,
        "order": "strength_desc",
        "next_cursor": None,
    }
    fact_sql = next(sql for sql in seen if "FROM plugin_memory_fact fact" in sql and "COUNT" not in sql)
    assert "= 'accepted' AND backing.status = 'active'" in fact_sql
    assert ">= :from_date" in fact_sql


@pytest.mark.asyncio
async def test_graph_read_time_filter_uses_evidence_dates_not_extraction_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        if "FROM sessions" in sql:
            return []
        if "FROM plugin_memory_entity entity" in sql:
            return [_entity_row(1, "洋白"), _entity_row(3, "小海")]
        if "FROM plugin_memory_fact fact" in sql and "COUNT" not in sql:
            return [_fact_row(12, 1, 3, "replied_to", 102)]
        if "FROM plugin_memory_item WHERE id = ANY" in sql:
            return [_item_row(102, "replied_to", ["2026-08-20", "2026-08-21"], [7001, 7002], 0.4866)]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    scope = {"tenant_id": "demo", "channel": "wechat", "source_key": "wxbot", "session_id": "53876528317@chatroom"}

    # Extracted on 2026-09-07 but observed 08-20..08-21: an August window keeps it...
    august = await store.get_group_relationship_graph(**scope, from_="2026-08-01", to="2026-08-31T23:59:59")
    assert [edge["id"] for edge in august["edges"]] == ["fact:12"]
    # ...and a September-only window drops it even though the fact row is from September.
    september = await store.get_group_relationship_graph(**scope, from_="2026-09-01")
    assert september["edges"] == []


def test_llm_candidates_must_resolve_people_to_real_participants() -> None:
    """Shapes observed from the production model: terms typed as `person`."""

    store = MemoryStore(SimpleNamespace())
    from plugins.memory.store_group_graph import GroupMemberDirectory

    directory = GroupMemberDirectory()
    directory.add_member("wxid_quiet", "安静的人")
    participants = ["wxid_a", "wxid_b", "q813235683"]
    payload = {
        "relations": [
            # Real participant -> real participant: kept as is.
            {"subject": "wxid_a", "predicate": "answered", "object": "q813235683",
             "object_type": "person", "confidence": 0.8, "evidence_event_ids": [1, 7]},
            # A term the model typed as a person becomes a topic for mention-like predicates.
            {"subject": "wxid_a", "predicate": "mentioned", "object": "缩水",
             "object_type": "person", "confidence": 0.9, "evidence_event_ids": [2, 7]},
            # ...but a person-only predicate with a phantom person is dropped.
            {"subject": "wxid_a", "predicate": "replied_to", "object": "gpt",
             "object_type": "person", "confidence": 0.9, "evidence_event_ids": [3, 7]},
            # Unknown subject: dropped.
            {"subject": "somebody", "predicate": "asked", "object": "wxid_b",
             "object_type": "person", "confidence": 0.9, "evidence_event_ids": [4, 7]},
            # Directory member who did not speak in this window still resolves.
            {"subject": "wxid_b", "predicate": "addressed", "object": "安静的人",
             "object_type": "person", "confidence": 0.7, "evidence_event_ids": [5, 7]},
            # Explicit topic objects are untouched.
            {"subject": "wxid_b", "predicate": "interested_in", "object": "并发",
             "object_type": "topic", "confidence": 0.9, "evidence_event_ids": [6, 7]},
        ]
    }

    candidates, skipped = store._parse_group_window_candidates(
        payload,
        allowed_event_ids={1, 2, 3, 4, 5, 6, 7},
        participants=participants,
        directory=directory,
    )

    assert skipped == 2
    assert [(c["subject"], c["predicate"], c["object"], c["object_type"]) for c in candidates] == [
        ("wxid_a", "answered", "q813235683", "person"),
        ("wxid_a", "mentioned", "缩水", "topic"),
        ("wxid_b", "addressed", "wxid_quiet", "person"),
        ("wxid_b", "interested_in", "并发", "topic"),
    ]
    assert all(c["signals"]["llm"] == 2 for c in candidates)

    # Without participant context (legacy callers) nothing is resolved or dropped.
    legacy, legacy_skipped = store._parse_group_window_candidates(
        payload, allowed_event_ids={1, 2, 3, 4, 5, 6, 7}
    )
    assert legacy_skipped == 0
    assert len(legacy) == 6


def test_llm_candidates_drop_noise_shapes_seen_in_production() -> None:
    """One-off claims, rule-layer predicates, 'the group' and junk terms are not stored."""

    store = MemoryStore(SimpleNamespace())
    participants = ["wxid_a", "wxid_b"]
    payload = {
        "relations": [
            # Single supporting message: not stored, so it cannot clog the review queue.
            {"subject": "wxid_a", "predicate": "mentioned", "object": "苹果",
             "object_type": "topic", "confidence": 0.95, "evidence_event_ids": [1]},
            # The rule layer owns co-participation.
            {"subject": "wxid_a", "predicate": "co_participated", "object": "wxid_b",
             "object_type": "person", "confidence": 0.9, "evidence_event_ids": [1, 2]},
            # "asked the group" is not an edge to anything.
            {"subject": "wxid_a", "predicate": "asked", "object": "group",
             "object_type": "group", "confidence": 0.9, "evidence_event_ids": [1, 2]},
            {"subject": "wxid_a", "predicate": "asked", "object": "大家",
             "object_type": "topic", "confidence": 0.9, "evidence_event_ids": [1, 2]},
            # Bare numbers/times, single characters and sentences are not terms.
            {"subject": "wxid_a", "predicate": "mentioned", "object": "9点",
             "object_type": "topic", "confidence": 0.9, "evidence_event_ids": [1, 2]},
            {"subject": "wxid_a", "predicate": "mentioned", "object": "x",
             "object_type": "topic", "confidence": 0.9, "evidence_event_ids": [1, 2]},
            {"subject": "wxid_a", "predicate": "reported_issue",
             "object": "elderly users keep forgetting their accounts and passwords again",
             "object_type": "topic", "confidence": 0.9, "evidence_event_ids": [1, 2]},
            # An unknown id-shaped "person" is a hallucinated member: dropped, not
            # turned into a topic called "wxid_ghost".
            {"subject": "wxid_a", "predicate": "mentioned", "object": "wxid_ghost",
             "object_type": "person", "confidence": 0.9, "evidence_event_ids": [1, 2]},
            # Kept: a real term with two supporting messages.
            {"subject": "wxid_a", "predicate": "interested_in", "object": "DeepSeek",
             "object_type": "tool", "confidence": 0.9, "evidence_event_ids": [1, 2]},
            {"subject": "wxid_b", "predicate": "mentioned", "object": "苹果5x",
             "object_type": "topic", "confidence": 0.9, "evidence_event_ids": [2, 3]},
        ]
    }

    candidates, skipped = store._parse_group_window_candidates(
        payload,
        allowed_event_ids={1, 2, 3},
        participants=participants,
    )

    assert skipped == 8
    assert [(c["subject"], c["predicate"], c["object"], c["object_type"]) for c in candidates] == [
        ("wxid_a", "interested_in", "DeepSeek", "tool"),
        ("wxid_b", "mentioned", "苹果5x", "topic"),
    ]


@pytest.mark.asyncio
async def test_llm_prompt_excludes_rule_layer_predicates_and_asks_for_terms() -> None:
    requests: list[Any] = []

    class _Llm:
        async def chat(self, request: Any) -> Any:
            requests.append(request)
            return SimpleNamespace(content='{"relations": []}')

    store = MemoryStore(SimpleNamespace(), llm_service=_Llm())
    store.graph_extractor.llm_service = _Llm()

    await store._extract_group_relationship_window_candidates(
        tenant_id="default",
        trace_id="group-window:test",
        target_date="2026-09-16",
        session_id="53876528317@chatroom",
        event_ids=[1, 2],
        transcript="[1] a: hi\n[2] b: hello",
    )

    assert len(requests) == 1
    system = str(requests[0].system)
    assert "co_participated" not in system
    assert "replied_to" in system
    assert "at least two different messages" in system
    assert "Chinese chat -> Chinese term" in system


@pytest.mark.asyncio
async def test_window_llm_request_uses_configured_model_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[Any] = []

    class _Llm:
        async def chat(self, request: Any) -> Any:
            requests.append(request)
            return SimpleNamespace(content='{"relations": []}')

    store = MemoryStore(SimpleNamespace(), llm_service=_Llm())
    store.graph_extractor.llm_service = _Llm()

    await store._extract_group_relationship_window_candidates(
        tenant_id="demo", trace_id="t", target_date="2026-09-14",
        session_id="room-a@chatroom", event_ids=[1, 2], transcript="[event_id=1] a: hi",
    )
    assert requests[-1].model_tier == "tier-1"

    store.settings = SimpleNamespace(memory_group_graph_llm_model_tier="tier-2")
    await store._extract_group_relationship_window_candidates(
        tenant_id="demo", trace_id="t", target_date="2026-09-14",
        session_id="room-a@chatroom", event_ids=[1, 2], transcript="[event_id=1] a: hi",
    )
    assert requests[-1].model_tier == "tier-2"


@pytest.mark.asyncio
async def test_window_catchup_gives_every_window_the_full_llm_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The budget bounds how many windows run; it is no longer divided by max_windows."""

    store = MemoryStore(SimpleNamespace(memory_group_graph_llm_timeout_seconds=60))
    calls: list[dict[str, Any]] = []

    async def fake_window(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "status": "completed",
            "totals": {"events": 50, "windows": 1, "candidates": 1, "applied": 1, "skipped": 0},
            "next_cursor_event_id": 100 * len(calls),
            "more_remain": len(calls) < 3,
            "source": "observation",
        }

    monkeypatch.setattr(store, "run_group_relationship_window_extraction", fake_window)

    result = await store.run_group_relationship_window_catchup(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="room-a@chatroom",
        date="2026-09-14",
        time_budget_seconds=120,
        max_windows_per_run=20,
        include_llm=True,
    )

    # Old formula would have handed the model 120 // 20 = 6 seconds.
    assert [call["llm_timeout_seconds"] for call in calls] == [60, 60, 60]
    assert all(call["llm_mode"] == "inline" for call in calls)
    assert result["controls"]["llm_timeout_seconds"] == 60
    assert result["controls"]["llm_mode"] == "inline"
    assert result["stop_reason"] == "no_more_events"

    calls.clear()
    await store.run_group_relationship_window_catchup(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="room-a@chatroom",
        date="2026-09-14",
        time_budget_seconds=120,
        include_llm=True,
        llm_timeout_seconds=45,
        llm_mode="enqueue",
    )
    assert calls[0]["llm_timeout_seconds"] == 45
    assert calls[0]["llm_mode"] == "enqueue"
    # Enqueue mode does not need one-window batches; it walks up to 10 at a time.
    assert calls[0]["max_windows"] == 10


@pytest.mark.asyncio
async def test_window_extraction_enqueue_mode_records_one_job_per_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace(memory_group_graph_auto_accept=True))
    store.graph_extractor.config = SimpleNamespace(enabled=True)
    store.graph_extractor.llm_service = object()
    inserts: list[dict[str, Any]] = []
    existing_job_keys: set[str] = set()
    extract_calls: list[dict[str, Any]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        params = params or {}
        if "FROM sessions" in sql or "FROM plugin_wxbot_group_membership" in sql:
            return []
        if "FROM plugin_wxbot_group_observations" in sql:
            cursor = int(params.get("cursor_event_id") or 0)
            rows = [
                {
                    "id": 7000 + index,
                    "tenant_id": "demo",
                    "session_id": "room-a@chatroom",
                    "sender_wxid": f"wxid_{index % 3}",
                    "sender_name": f"member {index % 3}",
                    "content": f"message {index}",
                    "occurred_ts": 1757808000 + index * 30,
                    "is_self_sent": False,
                    "metadata_json": "{}",
                }
                for index in range(15)
            ]
            return [row for row in rows if row["id"] > cursor][: int(params.get("lim") or 100)]
        if sql.startswith("INSERT INTO plugin_memory_extraction_job"):
            assert "ON CONFLICT (idempotency_key) DO NOTHING" in sql
            key = str(params["job_key"])
            inserts.append(dict(params))
            if key in existing_job_keys:
                return []
            existing_job_keys.add(key)
            return [{"id": len(existing_job_keys)}]
        return []

    async def fake_extract(**kwargs: Any) -> dict[str, Any]:
        extract_calls.append(kwargs)
        return {"relations": []}

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_extract_group_relationship_window_candidates", fake_extract)

    first = await store.run_group_relationship_window_extraction(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="room-a@chatroom",
        date="2026-09-14",
        window_size=10,
        max_windows=5,
        include_llm=True,
        llm_mode="enqueue",
        source="observation",
    )

    assert extract_calls == []
    assert first["llm_jobs_enqueued"] == 2
    assert first["status"] == "completed"
    assert [window["llm_job"] for window in first["windows"]] == ["enqueued", "enqueued"]
    assert "llm_window_jobs" in first["generated_from"]
    assert "llm_window_extractor" not in first["generated_from"]
    assert all(params["trace"].startswith("group-window-llm:") for params in inserts)
    assert all(params["group_uid"] == "__group__" for params in inserts)
    payload = json.loads(inserts[0]["result_json"])
    assert payload["kind"] == "group_window_llm"
    assert payload["scope"]["source"] == "observation"
    assert payload["window"] == {
        "index": 1,
        "first_event_id": 7000,
        "last_event_id": 7009,
        "event_count": 10,
        "window_size": 10,
    }

    second = await store.run_group_relationship_window_extraction(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="room-a@chatroom",
        date="2026-09-14",
        window_size=10,
        max_windows=5,
        include_llm=True,
        llm_mode="enqueue",
        source="observation",
    )

    # Same windows again: idempotent, nothing new queued, still not "skipped".
    assert second["llm_jobs_enqueued"] == 0
    assert [window["llm_job"] for window in second["windows"]] == ["exists", "exists"]
    assert second["status"] == "completed"


@pytest.mark.asyncio
async def test_run_group_window_llm_jobs_processes_claimed_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(
        SimpleNamespace(
            memory_group_graph_llm_timeout_seconds=60,
            memory_group_graph_llm_jobs_per_tick=6,
        )
    )
    store.graph_extractor.config = SimpleNamespace(enabled=True)
    store.graph_extractor.llm_service = object()
    finished: list[dict[str, Any]] = []
    window_calls: list[dict[str, Any]] = []

    def job(job_id: int, first: int, last: int, count: int, attempts: int = 0) -> dict[str, Any]:
        return {
            "id": job_id,
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "session_id": "room-a@chatroom",
            "attempts": attempts,
            "max_attempts": 3,
            "result_json": json.dumps(
                {
                    "kind": "group_window_llm",
                    "scope": {
                        "tenant_id": "demo",
                        "channel": "wechat",
                        "source_key": "wxbot",
                        "session_id": "room-a@chatroom",
                        "date": "2026-09-14",
                        "source": "observation",
                    },
                    "window": {
                        "index": 1,
                        "first_event_id": first,
                        "last_event_id": last,
                        "event_count": count,
                        "window_size": 50,
                    },
                }
            ),
        }

    async def fake_claim(**kwargs: Any) -> list[dict[str, Any]]:
        assert kwargs["limit"] == 6
        return [job(1, 7000, 7049, 50), job(2, 7050, 7099, 50), job(3, 7100, 7120, 21, attempts=2)]

    async def fake_finish(job_row: dict[str, Any], **kwargs: Any) -> None:
        finished.append({"id": job_row["id"], **kwargs})

    async def fake_window(**kwargs: Any) -> dict[str, Any]:
        window_calls.append(kwargs)
        first = kwargs["cursor_event_id"] + 1
        if first == 7050:
            return {
                "status": "partial",
                "totals": {"events": 50, "windows": 1, "candidates": 0, "applied": 0, "skipped": 1},
                "llm_failures": 1,
            }
        if first == 7100:
            raise RuntimeError("provider down")
        return {
            "status": "completed",
            "totals": {"events": 50, "windows": 1, "candidates": 4, "applied": 4, "skipped": 0},
            "llm_failures": 0,
            "signal_counts": {"llm": 4},
        }

    monkeypatch.setattr(store, "_claim_group_window_llm_jobs", fake_claim)
    monkeypatch.setattr(store, "_finish_group_window_llm_job", fake_finish)
    monkeypatch.setattr(store, "run_group_relationship_window_extraction", fake_window)

    summary = await store.run_group_window_llm_jobs()

    assert summary["claimed"] == 3
    assert summary["succeeded"] == 1
    assert summary["failed"] == 1
    assert summary["dead"] == 1
    assert summary["applied"] == 4
    assert summary["controls"]["llm_timeout_seconds"] == 60
    # Each window is re-run alone, LLM only, with the full timeout.
    assert all(call["deterministic"] is False for call in window_calls)
    assert all(call["llm_mode"] == "inline" for call in window_calls)
    assert all(call["include_llm"] is True for call in window_calls)
    assert all(call["llm_timeout_seconds"] == 60 for call in window_calls)
    assert all(call["max_windows"] == 1 for call in window_calls)
    assert all(call["source"] == "observation" for call in window_calls)
    assert [call["cursor_event_id"] for call in window_calls] == [6999, 7049, 7099]
    assert [call["window_size"] for call in window_calls] == [50, 50, 21]
    by_id = {item["id"]: item for item in finished}
    assert by_id[1]["status"] == "succeeded"
    assert by_id[1]["result"]["totals"]["applied"] == 4
    assert by_id[2]["status"] == "failed"
    assert by_id[2]["retry_after_seconds"] == 600
    assert by_id[3]["status"] == "dead"
    assert "provider down" in by_id[3]["error"]


@pytest.mark.asyncio
async def test_run_group_window_llm_jobs_skips_when_disabled_or_llm_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace(memory_group_graph_llm_jobs_per_tick=0))
    store.graph_extractor.config = SimpleNamespace(enabled=True)
    store.graph_extractor.llm_service = object()
    claimed = False

    async def fake_claim(**kwargs: Any) -> list[dict[str, Any]]:
        nonlocal claimed
        claimed = True
        return []

    monkeypatch.setattr(store, "_claim_group_window_llm_jobs", fake_claim)

    assert (await store.run_group_window_llm_jobs())["stop_reason"] == "disabled"
    assert claimed is False

    store.graph_extractor.llm_service = None
    assert (await store.run_group_window_llm_jobs(limit=3))["stop_reason"] == "llm_unavailable"
    assert claimed is False


@pytest.mark.asyncio
async def test_group_window_job_claim_and_finish_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    seen: list[tuple[str, dict[str, Any]]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        seen.append((sql, dict(params or {})))
        if sql.startswith("WITH candidate AS"):
            return [{"id": 5, "result_json": "{}"}]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    jobs = await store._claim_group_window_llm_jobs(limit=4, lock_ttl_seconds=150)
    assert [item["id"] for item in jobs] == [5]
    claim_sql, claim_params = seen[0]
    assert "source_trace_id LIKE :trace_prefix" in claim_sql
    assert claim_params["trace_prefix"] == "group-window-llm:%"
    assert claim_params["lock_ttl"] == 150
    assert "FOR UPDATE SKIP LOCKED" in claim_sql

    await store._finish_group_window_llm_job(
        jobs[0], status="failed", error="boom", retry_after_seconds=600
    )
    finish_sql, finish_params = seen[1]
    assert finish_sql.startswith("UPDATE plugin_memory_extraction_job SET")
    assert "attempts = attempts + 1" in finish_sql
    assert finish_params["status"] == "failed"
    assert finish_params["retry_after"] == 600
    assert finish_params["error"] == "boom"


@pytest.mark.asyncio
async def test_auto_extract_tick_runs_queued_llm_jobs_after_catchup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    order: list[str] = []

    async def fake_targets(**kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "session_id": "room-a@chatroom",
                "date": "2026-09-14",
                "event_count": 8,
                "source": "observation",
                "last_observation_id": 9,
            }
        ]

    async def fake_catchup(**kwargs: Any) -> dict[str, Any]:
        order.append("catchup")
        assert kwargs["llm_mode"] == "enqueue"
        assert kwargs["llm_timeout_seconds"] == 90
        return {"status": "completed", "totals": {"applied": 2}, "more_remain": False, "next_cursor_event_id": 9}

    async def fake_jobs(**kwargs: Any) -> dict[str, Any]:
        order.append("llm_jobs")
        assert kwargs == {"limit": 4, "llm_timeout_seconds": 90}
        return {"claimed": 2, "succeeded": 2, "failed": 0, "dead": 0, "deferred": 0, "applied": 5, "stop_reason": "completed"}

    monkeypatch.setattr(store, "list_imported_group_graph_targets", fake_targets)
    monkeypatch.setattr(store, "run_group_relationship_window_catchup", fake_catchup)
    monkeypatch.setattr(store, "run_group_window_llm_jobs", fake_jobs)
    monkeypatch.setattr(
        store, "_load_group_graph_auto_extract_cursor", lambda **kwargs: asyncio.sleep(0, result=0)
    )
    monkeypatch.setattr(
        store, "_save_group_graph_auto_extract_cursor", lambda **kwargs: asyncio.sleep(0)
    )

    result = await store.run_group_graph_auto_extract_tick(
        llm_jobs_per_tick=4,
        llm_timeout_seconds=90,
    )

    assert order == ["catchup", "llm_jobs"]
    assert result["llm_jobs"]["succeeded"] == 2
    assert result["applied"] == 7

    order.clear()
    await store.run_group_graph_auto_extract_tick(include_llm=False, llm_timeout_seconds=90)
    assert order == ["catchup"]


@pytest.mark.asyncio
async def test_auto_accept_pending_group_window_relations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    seen: list[str] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        seen.append(sql)
        if "FROM sessions" in sql:
            return [{"session_id": "room-a@chatroom"}]
        if sql.strip().startswith("UPDATE plugin_memory_item"):
            assert params is not None
            assert params["sids"] == ["room-a@chatroom"]
            assert "group_window_auto_accept" in sql
            return [{"id": 21}, {"id": 22}]
        if sql.strip().startswith("UPDATE plugin_memory_fact"):
            return [{"id": 201}, {"id": 202}]
        if sql.strip().startswith("UPDATE plugin_memory_entity"):
            return [{"id": 11}]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    result = await store.auto_accept_pending_group_window_relations(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="room-a@chatroom",
    )

    assert any(sql.strip().startswith("UPDATE plugin_memory_item") for sql in seen)
    assert result["accepted"] == 2
    assert result["facts_activated"] == 2
    assert result["entities_activated"] == 1
    assert result["failed"] == 0


@pytest.mark.asyncio
async def test_auto_extract_cursor_round_trips_in_durable_job_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    state: dict[str, str] = {}
    writes: list[tuple[str, dict[str, Any]]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        params = params or {}
        writes.append((sql, params))
        if sql.startswith("SELECT result_json"):
            payload = state.get(str(params["cursor_key"]))
            return [{"result_json": payload}] if payload else []
        if sql.startswith("INSERT INTO plugin_memory_extraction_job"):
            state[str(params["cursor_key"])] = str(params["result_json"])
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    scope = {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "session_id": "room-a@chatroom",
        "target_date": "2026-08-31",
    }
    assert await store._load_group_graph_auto_extract_cursor(**scope) == 0
    await store._save_group_graph_auto_extract_cursor(**scope, cursor_event_id=731)
    assert await store._load_group_graph_auto_extract_cursor(**scope) == 731
    insert_sql, insert_params = next(
        (sql, params) for sql, params in writes if sql.startswith("INSERT INTO")
    )
    assert "ON CONFLICT (idempotency_key) DO UPDATE" in insert_sql
    assert "< :cursor_event_id" in insert_sql
    assert insert_params["cursor_event_id"] == 731


@pytest.mark.asyncio
async def test_auto_extract_skips_completed_front_target_without_starving_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    catchup_sessions: list[str] = []

    async def fake_targets(**kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "tenant_id": "demo", "channel": "wechat", "source_key": "wxbot",
                "session_id": "room-a@chatroom", "date": "2026-08-31",
                "event_count": 10, "last_event_id": 100,
            },
            {
                "tenant_id": "demo", "channel": "wechat", "source_key": "wxbot",
                "session_id": "room-b@chatroom", "date": "2026-08-31",
                "event_count": 10, "last_event_id": 200,
            },
        ]

    async def fake_load_cursor(**kwargs: Any) -> int:
        return 100 if kwargs["session_id"] == "room-a@chatroom" else 150

    async def fake_catchup(**kwargs: Any) -> dict[str, Any]:
        catchup_sessions.append(kwargs["session_id"])
        return {
            "status": "completed",
            "stop_reason": "no_more_events",
            "totals": {"windows": 1},
            "more_remain": False,
            "next_cursor_event_id": 200,
        }

    async def fake_save_cursor(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(store, "list_imported_group_graph_targets", fake_targets)
    monkeypatch.setattr(store, "_load_group_graph_auto_extract_cursor", fake_load_cursor)
    monkeypatch.setattr(store, "_save_group_graph_auto_extract_cursor", fake_save_cursor)
    monkeypatch.setattr(store, "run_group_relationship_window_catchup", fake_catchup)

    result = await store.run_group_graph_auto_extract_tick(max_sessions=1)

    assert catchup_sessions == ["room-b@chatroom"]
    assert result["skipped"][0]["reason"] == "up_to_date"
    assert result["results"][0]["cursor_event_id"] == 150
    assert result["results"][0]["next_cursor_event_id"] == 200


@pytest.mark.asyncio
async def test_window_catchup_hard_stops_a_hung_window_at_time_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    cancelled = asyncio.Event()

    async def hung_window(**kwargs: Any) -> dict[str, Any]:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(store, "run_group_relationship_window_extraction", hung_window)

    result = await store.run_group_relationship_window_catchup(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="room-a@chatroom",
        date="2026-08-31",
        time_budget_seconds=1,
    )

    assert result["status"] == "partial"
    assert result["stop_reason"] == "time_budget_reached"
    assert result["more_remain"] is True
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_group_graph_auto_extract_task_start_is_unique() -> None:
    plugin = MemoryPlugin()
    plugin._ctx = SimpleNamespace(
        settings=SimpleNamespace(
            memory_group_graph_auto_extract_enabled=True,
            memory_group_graph_auto_extract_roles="scheduler",
            app_process_role="scheduler",
        )
    )
    plugin._store = object()  # type: ignore[assignment]
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_loop() -> None:
        started.set()
        await release.wait()

    plugin._group_graph_auto_extract_loop = fake_loop  # type: ignore[method-assign]
    plugin._ensure_group_graph_auto_extract_task()
    first_task = plugin._group_graph_auto_extract_task
    plugin._ensure_group_graph_auto_extract_task()

    assert plugin._group_graph_auto_extract_task is first_task
    assert first_task is not None
    await started.wait()
    release.set()
    await first_task


def test_prefer_operator_group_session_id_skips_connection_scoped_ids() -> None:
    assert (
        _prefer_operator_group_session_id(
            [
                "cx1:c:abc@chatroom",
                "53876528317@chatroom",
            ],
            "cx1:c:abc@chatroom",
        )
        == "53876528317@chatroom"
    )


@pytest.mark.asyncio
async def test_list_known_sessions_include_recent_runtime_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        if "FROM plugin_memory_event" in sql:
            return []
        if "FROM plugin_wxbot_group_observations" in sql:
            return [
                {
                    "tenant_id": "demo",
                    "session_id": "cx1:c:abc@chatroom",
                    "event_count": 41,
                    "last_seen_ts": 1757900000,
                }
            ]
        if "updated_at >=" in sql:
            return [
                {
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "session_id": "cx1:c:abc@chatroom",
                }
            ]
        return [
            {
                "session_id": "cx1:c:abc@chatroom",
                "external_id": "53876528317@chatroom",
                "external_session_id": "",
                "canonical_id": "cx1:c:abc@chatroom",
            }
        ]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    sessions = await store.list_known_group_graph_sessions(lookback_days=7, max_sessions=5)

    # Live observations count as activity for a group that has no imported
    # memory events; the runtime alias collapses onto the operator id.
    assert sessions == [
        {
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "session_id": "53876528317@chatroom",
            "event_count": 41,
        }
    ]


@pytest.mark.asyncio
async def test_list_imported_targets_collapse_runtime_session_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        if "FROM sessions" in sql:
            return [
                {
                    "session_id": "cx1:c:abc@chatroom",
                    "external_id": "53876528317@chatroom",
                    "external_session_id": "",
                    "canonical_id": "cx1:c:abc@chatroom",
                }
            ]
        if "FROM plugin_wxbot_group_observations" in sql:
            return []
        return [
            {
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "session_id": "cx1:c:abc@chatroom",
                "day": date(2026, 9, 7),
                "event_count": 9,
                "last_event_id": 900,
            },
            {
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "session_id": "53876528317@chatroom",
                "day": date(2026, 9, 7),
                "event_count": 3,
                "last_event_id": 700,
            },
        ]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    targets = await store.list_imported_group_graph_targets(lookback_days=2, max_targets=5)

    assert targets == [
        {
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "session_id": "53876528317@chatroom",
            "date": "2026-09-07",
            "event_count": 12,
            "last_event_id": 900,
            "last_observation_id": 0,
            "memory_event_count": 12,
            "observation_count": 0,
            "source": "memory_event",
        }
    ]


@pytest.mark.asyncio
async def test_load_group_relationship_events_falls_back_to_live_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    seen: list[dict[str, Any]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        seen.append({"sql": sql, "params": dict(params or {})})
        if "FROM sessions" in sql:
            return [
                {
                    "session_id": "cx1:c:abc@chatroom",
                    "external_id": "53876528317@chatroom",
                    "external_session_id": "",
                    "canonical_id": "cx1:c:abc@chatroom",
                }
            ]
        if "user_id = :uid" in sql:
            return []
        return [{"id": 11, "user_text": "sender:alice\nhello", "created_at": "2026-09-07T00:00:00"}]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    rows = await store._load_group_relationship_events(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="53876528317@chatroom",
        user_id_scope="__group__",
        start_at=date(2026, 9, 7),
        end_at=date(2026, 9, 8),
        columns="id, user_text, created_at",
    )

    assert [row["id"] for row in rows] == [11]
    live_query = next(item for item in seen if "FROM plugin_memory_event" in item["sql"] and "user_id = :uid" not in item["sql"])
    assert live_query["params"]["sids"] == [
        "53876528317@chatroom",
        "cx1:c:abc@chatroom",
    ]


@pytest.mark.asyncio
async def test_load_group_relationship_events_uses_group_observations_when_memory_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    seen: list[str] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        seen.append(sql)
        if "FROM sessions" in sql:
            return [
                {
                    "session_id": "cx1:c:abc@chatroom",
                    "external_id": "53876528317@chatroom",
                    "external_session_id": "",
                    "canonical_id": "cx1:c:abc@chatroom",
                }
            ]
        if "FROM plugin_memory_event" in sql:
            return []
        if "FROM plugin_wxbot_group_observations" in sql:
            assert params is not None
            assert "53876528317@chatroom" in params["sids"]
            assert "cx1:c:abc@chatroom" in params["sids"]
            return [
                {
                    "id": 88,
                    "tenant_id": "demo",
                    "session_id": "cx1:c:abc@chatroom",
                    "sender_wxid": "wxid_a",
                    "content": "hello",
                    "occurred_ts": 1757203200,
                }
            ]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    rows = await store._load_group_relationship_events(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="53876528317@chatroom",
        user_id_scope="__group__",
        start_at=datetime(2026, 8, 9),
        end_at=datetime(2026, 8, 10),
        columns="id, user_text, created_at",
    )

    assert [row["id"] for row in rows] == [88]
    assert rows[0]["_graph_source"] == "observation"
    assert rows[0]["user_text"].startswith("wxid_a:")
    assert any("plugin_wxbot_group_observations" in sql for sql in seen)


@pytest.mark.asyncio
async def test_group_relationship_graph_queries_bind_requested_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        calls.append((sql, dict(params or {})))
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    await store.get_group_relationship_graph(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="53876528317@chatroom",
        limit=10,
    )

    entity_sql, entity_params = next(
        (sql, params) for sql, params in calls if "FROM plugin_memory_entity entity" in sql
    )
    fact_sql, fact_params = next(
        (sql, params) for sql, params in calls if "FROM plugin_memory_fact fact" in sql
    )
    assert "scope_item.session_id = :sid" in entity_sql
    assert entity_params["sid"] == "53876528317@chatroom"
    assert "scope_item.session_id = :sid" in fact_sql
    assert fact_params["sid"] == "53876528317@chatroom"


_ALIAS_ROWS = [
    {
        "session_id": "cx1:c:abc@chatroom",
        "external_id": "53876528317@chatroom",
        "external_session_id": "",
        "canonical_id": "cx1:c:abc@chatroom",
    }
]


@pytest.mark.asyncio
async def test_list_imported_targets_include_observation_only_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Days that only exist in the live observation stream become targets."""

    store = MemoryStore(SimpleNamespace())
    seen_sql: list[str] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        seen_sql.append(sql)
        if "FROM sessions" in sql:
            return _ALIAS_ROWS
        if "FROM plugin_memory_event" in sql:
            return [
                {
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "session_id": "53876528317@chatroom",
                    "day": date(2026, 9, 6),
                    "event_count": 5,
                    "last_event_id": 60,
                }
            ]
        if "FROM plugin_wxbot_group_observations" in sql:
            assert params is not None
            assert isinstance(params["start_ts"], int)
            return [
                {
                    "tenant_id": "demo",
                    "session_id": "cx1:c:abc@chatroom",
                    "day": date(2026, 9, 7),
                    "event_count": 1200,
                    "last_event_id": 154626,
                },
                {
                    "tenant_id": "demo",
                    "session_id": "cx1:c:abc@chatroom",
                    "day": date(2026, 9, 6),
                    "event_count": 900,
                    "last_event_id": 153000,
                },
            ]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    targets = await store.list_imported_group_graph_targets(lookback_days=3, max_targets=5)

    assert any("plugin_wxbot_group_observations" in sql for sql in seen_sql)
    assert [(item["date"], item["source"]) for item in targets] == [
        ("2026-09-07", "observation"),
        ("2026-09-06", "memory_event"),
    ]
    observation_day = targets[0]
    assert observation_day["session_id"] == "53876528317@chatroom"
    assert observation_day["event_count"] == 1200
    assert observation_day["last_observation_id"] == 154626
    assert observation_day["last_event_id"] == 0
    # A day with imported events keeps memory events as its source even when
    # observations also exist for it.
    mixed_day = targets[1]
    assert mixed_day["memory_event_count"] == 5
    assert mixed_day["observation_count"] == 900
    assert mixed_day["last_event_id"] == 60
    assert mixed_day["last_observation_id"] == 153000


@pytest.mark.asyncio
async def test_auto_extract_tick_runs_observation_targets_with_their_own_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    catchup_calls: list[dict[str, Any]] = []
    cursor_loads: list[dict[str, Any]] = []
    cursor_saves: list[dict[str, Any]] = []

    async def fake_targets(**kwargs: Any) -> list[dict[str, Any]]:
        base = {"tenant_id": "demo", "channel": "wechat", "source_key": "wxbot"}
        return [
            {
                **base,
                "session_id": "room-a@chatroom",
                "date": "2026-09-07",
                "event_count": 1200,
                "last_event_id": 0,
                "last_observation_id": 154626,
                "source": "observation",
            },
            {
                **base,
                "session_id": "room-a@chatroom",
                "date": "2026-09-06",
                "event_count": 300,
                "last_event_id": 0,
                "last_observation_id": 153000,
                "source": "observation",
            },
        ]

    async def fake_load_cursor(**kwargs: Any) -> int:
        cursor_loads.append(kwargs)
        return 153000 if kwargs["target_date"] == "2026-09-06" else 154000

    async def fake_save_cursor(**kwargs: Any) -> None:
        cursor_saves.append(kwargs)

    async def fake_catchup(**kwargs: Any) -> dict[str, Any]:
        catchup_calls.append(kwargs)
        return {
            "status": "completed",
            "stop_reason": "no_more_events",
            "totals": {"windows": 2, "applied": 7},
            "more_remain": False,
            "next_cursor_event_id": 154626,
            "source": "observation",
        }

    monkeypatch.setattr(store, "list_imported_group_graph_targets", fake_targets)
    monkeypatch.setattr(store, "_load_group_graph_auto_extract_cursor", fake_load_cursor)
    monkeypatch.setattr(store, "_save_group_graph_auto_extract_cursor", fake_save_cursor)
    monkeypatch.setattr(store, "run_group_relationship_window_catchup", fake_catchup)

    result = await store.run_group_graph_auto_extract_tick(include_llm=False)

    # The cursor is looked up per source so memory-event and observation ids
    # never get compared with each other.
    assert all(call["source"] == "observation" for call in cursor_loads)
    assert catchup_calls == [
        {
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "session_id": "room-a@chatroom",
            "date": "2026-09-07",
            "window_size": 50,
            "max_windows_per_run": 20,
            "cursor_event_id": 154000,
            "time_budget_seconds": 180,
            "include_llm": False,
            "source": "observation",
            "llm_mode": "enqueue",
            "llm_timeout_seconds": None,
        }
    ]
    assert cursor_saves == [
        {
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "session_id": "room-a@chatroom",
            "target_date": "2026-09-07",
            "source": "observation",
            "cursor_event_id": 154626,
        }
    ]
    assert result["skipped_reasons"] == {"up_to_date": 1}
    assert result["applied"] == 7
    assert result["ran"] == 1


@pytest.mark.asyncio
async def test_auto_extract_cursor_is_scoped_to_its_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    state: dict[str, str] = {}

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        params = params or {}
        if sql.startswith("SELECT result_json"):
            payload = state.get(str(params["cursor_key"]))
            return [{"result_json": payload}] if payload else []
        if sql.startswith("INSERT INTO plugin_memory_extraction_job"):
            assert params["cursor_source"] in {"memory_event", "observation"}
            assert "<> :cursor_source" in sql
            state[str(params["cursor_key"])] = str(params["result_json"])
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    scope = {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "session_id": "room-a@chatroom",
        "target_date": "2026-09-07",
    }

    await store._save_group_graph_auto_extract_cursor(**scope, cursor_event_id=731)

    # Legacy cursors (no source) count as memory-event cursors.
    assert await store._load_group_graph_auto_extract_cursor(**scope) == 731
    assert await store._load_group_graph_auto_extract_cursor(**scope, source="memory_event") == 731
    assert await store._load_group_graph_auto_extract_cursor(**scope, source="observation") == 0

    await store._save_group_graph_auto_extract_cursor(
        **scope, cursor_event_id=154626, source="observation"
    )

    assert await store._load_group_graph_auto_extract_cursor(**scope, source="observation") == 154626
    assert await store._load_group_graph_auto_extract_cursor(**scope, source="memory_event") == 0


@pytest.mark.asyncio
async def test_load_group_relationship_events_honours_pinned_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    seen: list[str] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        seen.append(sql)
        if "FROM sessions" in sql:
            return _ALIAS_ROWS
        if "FROM plugin_wxbot_group_observations" in sql:
            return [
                {
                    "id": 88,
                    "tenant_id": "demo",
                    "session_id": "cx1:c:abc@chatroom",
                    "sender_wxid": "wxid_a",
                    "content": "hello",
                    "occurred_ts": 1757203200,
                }
            ]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    scope = {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "session_id": "53876528317@chatroom",
        "user_id_scope": "__group__",
        "start_at": datetime(2026, 9, 7),
        "end_at": datetime(2026, 9, 8),
        "columns": "id, user_text, created_at",
    }

    observation_rows = await store._load_group_relationship_events(**scope, source="observation")

    assert [row["id"] for row in observation_rows] == [88]
    assert not any("FROM plugin_memory_event" in sql for sql in seen)

    seen.clear()
    memory_rows = await store._load_group_relationship_events(**scope, source="memory_event")

    assert memory_rows == []
    assert any("FROM plugin_memory_event" in sql for sql in seen)
    assert not any("FROM plugin_wxbot_group_observations" in sql for sql in seen)


@pytest.mark.asyncio
async def test_window_extraction_keeps_observation_evidence_on_its_own_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Observation ids are stored as observation evidence instead of being dropped."""

    store = MemoryStore(SimpleNamespace(memory_group_graph_auto_accept=True))
    inserted: list[dict[str, Any]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        if "FROM sessions" in sql:
            return []
        if "FROM plugin_wxbot_group_observations" in sql:
            return [
                {
                    "id": 5001,
                    "tenant_id": "demo",
                    "session_id": "room-a@chatroom",
                    "sender_wxid": "wxid_a",
                    "content": "问一下 kiro2api 的配置",
                    "occurred_ts": 1757203200,
                },
                {
                    "id": 5002,
                    "tenant_id": "demo",
                    "session_id": "room-a@chatroom",
                    "sender_wxid": "wxid_b",
                    "content": "回复: 看 README",
                    "occurred_ts": 1757203260,
                },
            ]
        return []

    async def fake_find(**kwargs: Any) -> list[dict[str, Any]]:
        return []

    async def fake_insert(**kwargs: Any) -> dict[str, Any]:
        inserted.append(kwargs)
        return {"id": len(inserted), **kwargs}

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_find_memory_item_by_normalized_key", fake_find)
    monkeypatch.setattr(store, "_insert_or_touch_memory_item", fake_insert)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", noop)
    monkeypatch.setattr(store, "_sync_memory_graph_for_item_safe", noop)
    monkeypatch.setattr(store, "_sync_memory_vector_for_item_safe", noop)

    result = await store.run_group_relationship_window_extraction(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="room-a@chatroom",
        date="2026-09-07",
        include_llm=False,
        source="observation",
    )

    assert result["source"] == "observation"
    assert result["generated_from"] == [
        "plugin_wxbot_group_observations",
        "deterministic_window_participants",
    ]
    assert result["totals"]["applied"] == 1
    value = inserted[0]["value_json"]
    assert value["evidence_source"] == "observation"
    assert value["source_event_ids"] == []
    assert value["source_observation_ids"] == [5001, 5002]
    assert value["relation"]["predicate"] == "replied_to"
    assert value["relation"]["evidence_event_ids"] == []
    assert value["relation"]["evidence_observation_ids"] == [5001, 5002]
    assert value["window"]["source"] == "observation"
    # Nothing here may be mistaken for a plugin_memory_event id.
    assert inserted[0]["source_event_id"] is None


def test_group_graph_edge_quality_reports_observation_evidence() -> None:
    quality = _group_graph_edge_quality(
        {
            "value": {
                "evidence_dates": ["2026-09-04", "2026-08-09"],
                "first_seen_date": "2026-08-09",
                "last_seen_date": "2026-09-04",
                "evidence_source": "observation",
                "source_observation_ids": [154604, 154610, 154626],
                "relation": {
                    "evidence_event_ids": [],
                    "evidence_observation_ids": [154604, 154610, 154626],
                },
                "acceptance": {"score": 0.45},
            }
        }
    )

    assert quality["evidence_event_count"] == 0
    assert quality["evidence_observation_count"] == 3
    assert quality["evidence_day_count"] == 2
    assert quality["evidence_source"] == "observation"
    assert quality["first_seen_date"] == "2026-08-09"
    assert quality["last_seen_date"] == "2026-09-04"


@pytest.mark.asyncio
async def test_window_stats_expand_runtime_session_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    requested_sessions: list[str | None] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, Any]]:
        if "FROM sessions" in sql:
            return _ALIAS_ROWS
        return []

    async def fake_items(**kwargs: Any) -> list[dict[str, Any]]:
        requested_sessions.append(kwargs.get("session_id"))
        if kwargs.get("session_id") != "53876528317@chatroom":
            return []
        return [
            {
                "id": 3417,
                "status": "active",
                "value_json": (
                    '{"kind":"group_window_relation","date":"2026-09-04",'
                    '"evidence_source":"observation",'
                    '"source_observation_ids":[154604,154626],'
                    '"relation":{"predicate":"co_participated","evidence_observation_ids":[154604,154626]},'
                    '"window":{"first_event_id":154604,"last_event_id":154626},'
                    '"acceptance":{"status":"accepted"}}'
                ),
            }
        ]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_list_memory_acceptance_audit_items", fake_items)

    stats = await store.get_group_relationship_window_stats(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="cx1:c:abc@chatroom",
    )

    assert set(requested_sessions) == {"cx1:c:abc@chatroom", "53876528317@chatroom"}
    assert stats["scope"]["session_ids"] == ["cx1:c:abc@chatroom", "53876528317@chatroom"]
    assert stats["totals"]["items"] == 1
    assert stats["totals"]["accepted"] == 1
    assert stats["totals"]["observations"] == 2
    assert stats["totals"]["events"] == 0
    assert stats["evidence_source_counts"] == {"observation": 1}
    assert stats["predicate_counts"] == {"co_participated": 1}


@pytest.mark.asyncio
async def test_scope_gate_denial_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryStore(SimpleNamespace())
    store.runtime_scope_gates_required = True
    logged: list[tuple[str, dict[str, Any]]] = []

    class _Logger:
        def debug(self, event: str, **kwargs: Any) -> None:
            logged.append((event, kwargs))

        def info(self, event: str, **kwargs: Any) -> None:
            logged.append((event, kwargs))

        def warning(self, event: str, **kwargs: Any) -> None:
            logged.append((event, kwargs))

    monkeypatch.setattr("plugins.memory.store_group_graph.logger", _Logger())

    assert await store._group_graph_auto_extract_scope_allowed("demo", "room-a@chatroom") is False
    assert logged[-1][0] == "memory.group_graph_auto_extract_scope_denied"
    assert logged[-1][1]["reason"] == "scope_gate_unavailable"

    async def denied(tenant_id: str, session_id: str = "") -> bool:
        return False

    store.combined_history_scope_execution_allowed = denied

    assert await store._group_graph_auto_extract_scope_allowed("demo", "room-a@chatroom") is False
    assert logged[-1][1]["reason"] == "plugin_scope_disabled"
