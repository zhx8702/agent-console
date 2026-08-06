from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import plugins.wxbot.store as wxbot_store_module
from plugins.wxbot.store import WxbotStore


@pytest.mark.asyncio
async def test_ensure_tables_only_verifies_migrated_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = object()
    calls: list[tuple[object, str]] = []

    async def verify(target, *, component: str) -> None:
        calls.append((target, component))

    monkeypatch.setattr(wxbot_store_module, "get_engine", lambda: engine)
    monkeypatch.setattr(wxbot_store_module, "verify_runtime_schema", verify)

    await WxbotStore(SimpleNamespace()).ensure_tables()

    assert calls == [(engine, "wxbot store")]


@pytest.mark.asyncio
async def test_save_group_observation_is_idempotent_and_preserves_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params or {}))
        return [{"id": 9}]

    monkeypatch.setattr(wxbot_store_module, "_exec", fake_exec)

    saved = await WxbotStore(SimpleNamespace()).save_group_observation(
        tenant_id="demo",
        session_id="room@chatroom",
        message_id="msg-9",
        session_name="测试群",
        sender_wxid="wxid_a",
        sender_name="群友A",
        content="@机器人 看一下",
        mentioned_me=True,
        bot_addressed=True,
        occurred_ts=1777000000,
        metadata={"at_wxids": ["wxid_bot"], "quote_text": "上一条"},
    )

    assert saved is True
    sql, params = calls[0]
    assert sql.startswith("WITH inserted AS")
    assert "ON CONFLICT (tenant_id, session_id, message_id) DO NOTHING" in sql
    assert "INSERT INTO plugin_wxbot_group_summary_jobs" in sql
    assert "ON CONFLICT (tenant_id, session_id) DO UPDATE" in sql
    assert "claim_expires_at > NOW()" in sql
    assert "RETURNING id" in sql
    assert params["tid"] == "demo"
    assert params["sid"] == "room@chatroom"
    assert params["mid"] == "msg-9"
    assert params["mentioned_me"] is True
    assert params["bot_addressed"] is True
    assert json.loads(params["metadata"]) == {
        "at_wxids": ["wxid_bot"],
        "quote_text": "上一条",
    }
    assert params["debounce"] == 20.0
    assert params["schedule_summary"] is True


@pytest.mark.asyncio
async def test_save_group_observation_keeps_unaddressed_messages_without_scheduling_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params or {}))
        return [{"id": 10}]

    monkeypatch.setattr(wxbot_store_module, "_exec", fake_exec)

    saved = await WxbotStore(
        SimpleNamespace(
            wxbot_group_summary_enabled=True,
            wxbot_group_context_enabled=True,
            wxbot_group_summary_only_when_addressed=True,
        )
    ).save_group_observation(
        tenant_id="demo",
        session_id="room@chatroom",
        message_id="msg-10",
        content="群友之间的普通闲聊",
    )

    assert saved is True
    sql, params = calls[0]
    assert "WHERE :schedule_summary" in sql
    assert params["schedule_summary"] is False


@pytest.mark.asyncio
async def test_save_group_observation_does_not_schedule_when_summary_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params or {}))
        return [{"id": 11}]

    monkeypatch.setattr(wxbot_store_module, "_exec", fake_exec)

    saved = await WxbotStore(
        SimpleNamespace(
            wxbot_group_summary_enabled=False,
            wxbot_group_context_enabled=True,
            wxbot_group_summary_only_when_addressed=False,
        )
    ).save_group_observation(
        tenant_id="demo",
        session_id="room@chatroom",
        message_id="msg-11",
        content="@机器人 请总结",
        mentioned_me=True,
        bot_addressed=True,
    )

    assert saved is True
    assert calls[0][1]["schedule_summary"] is False


@pytest.mark.asyncio
async def test_save_group_observation_rejects_private_and_incomplete_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_exec(sql: str, params: dict | None = None) -> list[dict]:
        raise AssertionError((sql, params))

    monkeypatch.setattr(wxbot_store_module, "_exec", fail_exec)
    store = WxbotStore(SimpleNamespace())

    assert not await store.save_group_observation(
        tenant_id="demo",
        session_id="private-user",
        message_id="msg-1",
    )
    assert not await store.save_group_observation(
        tenant_id="demo",
        session_id="room@chatroom",
        message_id="",
    )


@pytest.mark.asyncio
async def test_list_recent_group_observations_is_scoped_and_hydrates_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params or {}))
        return [
            {
                "id": 12,
                "tenant_id": "demo",
                "session_id": "room@chatroom",
                "message_id": "msg-12",
                "content": "你好",
                "metadata_json": '{"mentioned_names":["机器人"]}',
            }
        ]

    monkeypatch.setattr(wxbot_store_module, "_exec", fake_exec)

    rows = await WxbotStore(SimpleNamespace()).list_recent_group_observations(
        "demo",
        "room@chatroom",
        limit=999,
        before_id=20,
    )

    assert rows[0]["metadata"] == {"mentioned_names": ["机器人"]}
    assert "metadata_json" not in rows[0]
    sql, params = calls[0]
    assert "tenant_id = :tid AND session_id = :sid" in sql
    assert "id < :before_id" in sql
    assert "ORDER BY id DESC" in sql
    assert params == {
        "tid": "demo",
        "sid": "room@chatroom",
        "lim": 500,
        "before_id": 20,
    }


@pytest.mark.asyncio
async def test_participation_snapshot_uses_exact_fifteen_second_multi_party_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    now_ts = int(current.timestamp())
    observations = [
        {
            "sender_wxid": sender,
            "occurred_ts": now_ts - age,
            "is_self_sent": False,
        }
        for sender, age in (
            ("wxid_a", 2),
            ("wxid_b", 5),
            ("wxid_c", 10),
            ("wxid_a", 15),
        )
    ]
    store = WxbotStore(SimpleNamespace())

    async def recent(*args, **kwargs):
        _ = args, kwargs
        return list(observations)

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        _ = sql, params
        return [{"soft_replies_last_10m": 0, "soft_replies_last_hour": 0}]

    monkeypatch.setattr(store, "list_recent_group_observations", recent)
    monkeypatch.setattr(wxbot_store_module, "_exec", fake_exec)

    snapshot = await store.get_participation_snapshot(
        "demo",
        "room@chatroom",
        now=current,
    )
    assert snapshot["rapid_multi_party_chat"] is True

    observations[-1]["occurred_ts"] = now_ts - 16
    outside_window = await store.get_participation_snapshot(
        "demo",
        "room@chatroom",
        now=current,
    )
    assert outside_window["rapid_multi_party_chat"] is False


@pytest.mark.asyncio
async def test_list_group_observations_after_is_chronological(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params or {}))
        return [{"id": 6, "metadata_json": "not-json"}]

    monkeypatch.setattr(wxbot_store_module, "_exec", fake_exec)

    rows = await WxbotStore(SimpleNamespace()).list_group_observations_after(
        "demo",
        "room@chatroom",
        after_id=5,
        limit=25,
    )

    assert rows == [{"id": 6, "metadata": {}}]
    sql, params = calls[0]
    assert "id > :after_id" in sql
    assert "ORDER BY id ASC" in sql
    assert params["after_id"] == 5
    assert params["lim"] == 25


@pytest.mark.asyncio
async def test_list_group_observations_can_exclude_current_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params or {}))
        return []

    monkeypatch.setattr(wxbot_store_module, "_exec", fake_exec)

    await WxbotStore(SimpleNamespace()).list_group_observations(
        "demo",
        "room@chatroom",
        limit=30,
        after_id=12,
        exclude_message_id="current-msg",
    )

    sql, params = calls[0]
    assert "id > :after_id" in sql
    assert "message_id <> :exclude_mid" in sql
    assert "ORDER BY id ASC" in sql
    assert params == {
        "tid": "demo",
        "sid": "room@chatroom",
        "after_id": 12,
        "lim": 30,
        "exclude_mid": "current-msg",
    }


@pytest.mark.asyncio
async def test_list_group_observations_for_period_uses_half_open_chronological_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params or {}))
        return [
            {
                "id": 12,
                "occurred_ts": 1777000000,
                "metadata_json": '{"source":"managed"}',
            }
        ]

    monkeypatch.setattr(wxbot_store_module, "_exec", fake_exec)

    rows = await WxbotStore(SimpleNamespace()).list_group_observations_for_period(
        " demo ",
        " room@chatroom ",
        start_occurred_ts=1776960000,
        end_occurred_ts=1777046400,
        limit=75000,
    )

    assert rows == [
        {
            "id": 12,
            "occurred_ts": 1777000000,
            "metadata": {"source": "managed"},
        }
    ]
    sql, params = calls[0]
    assert "tenant_id = :tid AND session_id = :sid" in sql
    assert "occurred_ts >= :start_ts AND occurred_ts < :end_ts" in sql
    assert "ORDER BY occurred_ts ASC, id ASC LIMIT :lim" in sql
    assert params == {
        "tid": "demo",
        "sid": "room@chatroom",
        "start_ts": 1776960000,
        "end_ts": 1777046400,
        "lim": 10001,
    }


@pytest.mark.asyncio
async def test_list_group_observations_for_period_skips_empty_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_exec(sql: str, params: dict | None = None) -> list[dict]:
        raise AssertionError((sql, params))

    monkeypatch.setattr(wxbot_store_module, "_exec", fail_exec)

    rows = await WxbotStore(SimpleNamespace()).list_group_observations_for_period(
        "demo",
        "room@chatroom",
        start_occurred_ts=1777046400,
        end_occurred_ts=1777046400,
        limit=100,
    )

    assert rows == []


@pytest.mark.asyncio
async def test_compare_and_set_group_summary_creates_version_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params or {}))
        return [
            {
                "tenant_id": "demo",
                "session_id": "room@chatroom",
                "summary_text": "大家在讨论上线时间。",
                "summary_json": '{"topics":["上线"]}',
                "last_observation_id": 10,
                "last_message_id": "msg-10",
                "message_count": 10,
                "version": 1,
            }
        ]

    monkeypatch.setattr(wxbot_store_module, "_exec", fake_exec)

    state = await WxbotStore(SimpleNamespace()).compare_and_set_group_summary_state(
        tenant_id="demo",
        session_id="room@chatroom",
        expected_version=0,
        summary_text="大家在讨论上线时间。",
        summary_payload={"topics": ["上线"]},
        last_observation_id=10,
        last_message_id="msg-10",
        message_count=10,
        session_name="测试群",
    )

    assert state is not None
    assert state["version"] == 1
    assert state["summary_payload"] == {"topics": ["上线"]}
    sql, params = calls[0]
    assert sql.startswith("INSERT INTO plugin_wxbot_group_summary_state")
    assert "ON CONFLICT (tenant_id, session_id) DO NOTHING" in sql
    assert "RETURNING tenant_id" in sql
    assert json.loads(params["summary_json"]) == {"topics": ["上线"]}


@pytest.mark.asyncio
async def test_compare_and_set_group_summary_updates_only_expected_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params or {}))
        return []

    monkeypatch.setattr(wxbot_store_module, "_exec", fake_exec)

    state = await WxbotStore(SimpleNamespace()).compare_and_set_group_summary_state(
        tenant_id="demo",
        session_id="room@chatroom",
        expected_version=3,
        summary_text="新摘要",
        summary_payload={},
        last_observation_id=30,
        last_message_id="msg-30",
        message_count=30,
    )

    assert state is None
    sql, params = calls[0]
    assert sql.startswith("UPDATE plugin_wxbot_group_summary_state")
    assert "version = version + 1" in sql
    assert "version = :expected_version" in sql
    assert "last_observation_id <= :last_observation_id" in sql
    assert params["expected_version"] == 3


@pytest.mark.asyncio
async def test_get_group_summary_state_hydrates_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        assert "FROM plugin_wxbot_group_summary_state" in sql
        assert params == {"tid": "demo", "sid": "room@chatroom"}
        return [
            {
                "tenant_id": "demo",
                "session_id": "room@chatroom",
                "summary_text": "摘要",
                "summary_json": '{"decisions":["周五发布"]}',
                "version": 2,
            }
        ]

    monkeypatch.setattr(wxbot_store_module, "_exec", fake_exec)

    state = await WxbotStore(SimpleNamespace()).get_group_summary_state(
        "demo",
        "room@chatroom",
    )

    assert state is not None
    assert state["summary_payload"] == {"decisions": ["周五发布"]}
    assert "summary_json" not in state


@pytest.mark.asyncio
async def test_claim_group_summary_job_uses_skip_locked_and_expired_lease_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params or {}))
        return [
            {
                "tenant_id": "demo",
                "session_id": "room@chatroom",
                "status": "running",
                "claimed_by": "summary-worker-1",
                "claim_token": params["claim_token"] if params else "",
                "claimed_through_observation_id": 42,
            }
        ]

    monkeypatch.setattr(wxbot_store_module, "_exec", fake_exec)

    job = await WxbotStore(SimpleNamespace()).claim_group_summary_job(
        "summary-worker-1",
        lock_ttl_seconds=90,
    )

    assert job is not None
    assert job["claimed_through_observation_id"] == 42
    assert len(job["claim_token"]) == 32
    sql, params = calls[0]
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "claim_expires_at IS NULL OR claim_expires_at <= NOW()" in sql
    assert "claimed_through_observation_id = job.requested_through_observation_id" in sql
    assert params["worker_id"] == "summary-worker-1"
    assert params["lock_ttl"] == 90.0


@pytest.mark.asyncio
async def test_defer_group_summary_job_releases_claim_without_spending_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params or {}))
        return [{"tenant_id": "demo"}]

    monkeypatch.setattr(wxbot_store_module, "_exec", fake_exec)

    deferred = await WxbotStore(SimpleNamespace()).defer_group_summary_job(
        tenant_id="demo",
        session_id="room@chatroom",
        worker_id="summary-worker-1",
        claim_token="claim-1",
        defer_seconds=45,
    )

    assert deferred is True
    sql, params = calls[0]
    assert "status = 'pending'" in sql
    assert "attempt_count = GREATEST(attempt_count - 1, 0)" in sql
    assert "claimed_by = ''" in sql
    assert "claim_token = ''" in sql
    assert "claim_expires_at = NULL" in sql
    assert "claim_expires_at > NOW()" in sql
    assert params == {
        "tid": "demo",
        "sid": "room@chatroom",
        "worker_id": "summary-worker-1",
        "claim_token": "claim-1",
        "defer_seconds": 45.0,
    }


@pytest.mark.asyncio
async def test_complete_group_summary_job_commits_summary_and_requeues_new_tail_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params or {}))
        return [{"n": 1}]

    monkeypatch.setattr(wxbot_store_module, "_exec", fake_exec)

    await WxbotStore(SimpleNamespace()).complete_group_summary_job(
        tenant_id="demo",
        session_id="room@chatroom",
        covered_observation_id=42,
        summary_text="群里决定周五发布。",
        worker_id="summary-worker-1",
        claim_token="claim-1",
    )

    sql, params = calls[0]
    assert sql.startswith("WITH owned AS")
    assert "FOR UPDATE" in sql
    assert "job.claim_expires_at > NOW()" in sql
    assert "INSERT INTO plugin_wxbot_group_summary_state" in sql
    assert "owned.previous_message_count +" in sql
    assert "observation.id > owned.previous_observation_id" in sql
    assert "version = plugin_wxbot_group_summary_state.version + 1" in sql
    assert "requested_through_observation_id > :covered_id" in sql
    assert "THEN 'pending' ELSE 'completed'" in sql
    assert "FROM owned JOIN saved USING (tenant_id, session_id)" in sql
    assert params["covered_id"] == 42
    assert params["claim_token"] == "claim-1"


@pytest.mark.asyncio
async def test_fail_group_summary_job_releases_only_owned_lease_with_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params or {}))
        return []

    monkeypatch.setattr(wxbot_store_module, "_exec", fake_exec)

    await WxbotStore(SimpleNamespace()).fail_group_summary_job(
        tenant_id="demo",
        session_id="room@chatroom",
        error="模型暂时不可用",
        worker_id="summary-worker-1",
        retry_backoff_seconds=45,
        claim_token="claim-1",
    )

    sql, params = calls[0]
    assert "status = 'failed'" in sql
    assert "claim_expires_at = NULL" in sql
    assert "claim_expires_at > NOW()" in sql
    assert "claimed_by = :worker_id" in sql
    assert "claim_token = :claim_token" in sql
    assert params["backoff"] == 45.0


@pytest.mark.asyncio
async def test_prune_group_observations_only_deletes_old_covered_rows_outside_recent_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params or {}))
        return [{"n": 17}]

    monkeypatch.setattr(wxbot_store_module, "_exec", fake_exec)

    deleted = await WxbotStore(SimpleNamespace()).prune_group_observations(
        retention_days=30,
        keep_recent=200,
    )

    assert deleted == 17
    sql, params = calls[0]
    assert "plugin_wxbot_group_summary_state AS state" in sql
    assert "observation.id <= state.last_observation_id" in sql
    assert "observation.received_at < NOW()" in sql
    assert "ranked.recent_rank > :keep_recent" in sql
    assert params == {"retention_days": 30, "keep_recent": 200}
