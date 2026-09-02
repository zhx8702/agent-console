from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

import plugins.memory.store as memory_store_module
from plugins.memory.plugin import MemoryPlugin
from plugins.memory.store import MemoryStore
from plugins.memory.store_group_graph import _group_graph_edge_quality


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
        assert "plugin_memory_event" in sql
        assert params is not None
        assert params["group_uid"] == "__group__"
        return [
            {
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "session_id": "room-a@chatroom",
                "day": date(2026, 8, 31),
                "event_count": 12,
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
        }

    monkeypatch.setattr(store, "list_imported_group_graph_targets", fake_targets)
    monkeypatch.setattr(store, "run_group_relationship_window_catchup", fake_catchup)

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
            "time_budget_seconds": 180,
            "include_llm": False,
        }
    ]


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

    result = await store.run_group_graph_auto_extract_tick(
        lookback_days=7,
        sync_missing_history=True,
        include_llm=False,
    )

    assert result["sync_missing_history"] is True
    assert result["synced"][0]["session_id"] == "room-a@chatroom"
    assert backfill_calls[0]["session_ids"] == ["room-a@chatroom"]
    assert backfill_calls[0]["days_limit"] == 7
    assert backfill_calls[0]["enqueue_llm_jobs"] is True
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
