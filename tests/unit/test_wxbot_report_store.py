from __future__ import annotations

from types import SimpleNamespace

import pytest

import plugins.wxbot.report_store as report_store_module
from plugins.wxbot.report_store import WxbotReportStoreMixin


class _ReportStore(WxbotReportStoreMixin):
    def __init__(self, *, stage_timeout_seconds: object = 240.0) -> None:
        self.settings = SimpleNamespace(
            wxbot_report_stage_timeout_seconds=stage_timeout_seconds
        )


@pytest.mark.asyncio
async def test_fresh_replica_cleanup_is_fenced_by_updated_at_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict | None]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params))
        return []

    monkeypatch.setattr(report_store_module, "_exec", fake_exec)
    store = _ReportStore(stage_timeout_seconds=720.0)

    await store.fail_stale_report_jobs()
    await store.fail_stale_self_review_jobs()

    assert len(calls) == 3
    for sql, params in calls:
        assert "updated_at < NOW() - (:stale_seconds * INTERVAL '1 second')" in sql
        assert params == {"stale_seconds": 7200.0}
    assert "status = 'running'" in calls[0][0]
    assert "delivery_status = 'sending'" in calls[1][0]
    assert "delivery_status = 'indeterminate'" in calls[1][0]
    assert "status = 'running'" in calls[2][0]


@pytest.mark.asyncio
async def test_stale_cleanup_uses_explicit_age_with_safety_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict | None]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params))
        return []

    monkeypatch.setattr(report_store_module, "_exec", fake_exec)
    store = _ReportStore()

    await store.fail_stale_report_jobs(stale_seconds=1.0)
    await store.fail_stale_self_review_jobs(stale_seconds=90.0)

    assert [params for _sql, params in calls] == [
        {"stale_seconds": 60.0},
        {"stale_seconds": 60.0},
        {"stale_seconds": 90.0},
    ]


@pytest.mark.asyncio
async def test_get_or_create_polling_does_not_renew_running_job_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        _ = params
        calls.append(sql)
        return [{"id": 1}]

    monkeypatch.setattr(report_store_module, "_exec", fake_exec)
    store = _ReportStore()

    await store.get_or_create_report_job(
        tenant_id="tenant-a",
        session_id="room-a",
        session_name="Room A",
        report_type="daily",
        period_key="2026-07-18",
        period_label="2026-07-18",
    )
    await store.get_or_create_self_review_job(
        tenant_id="tenant-a",
        session_id="room-a",
        session_name="Room A",
        period_key="2026-07-18",
        period_label="2026-07-18",
    )

    assert len(calls) == 2
    for sql in calls:
        conflict_update = sql.split("DO UPDATE SET", 1)[1]
        assert "updated_at" not in conflict_update


@pytest.mark.asyncio
async def test_report_run_attempt_reclaim_fences_late_worker_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, object] = {
        "status": "pending",
        "run_attempt": 0,
        "expired": False,
        "current_stage": "queued",
    }

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        params = params or {}
        if "run_attempt = run_attempt + 1" in sql:
            claimable = state["status"] in {"pending", "failed"} or (
                state["status"] == "running" and state["expired"] is True
            )
            if not claimable:
                return []
            state["status"] = "running"
            state["expired"] = False
            state["run_attempt"] = int(state["run_attempt"]) + 1
            state["current_stage"] = "collect_messages"
            return [{"run_attempt": state["run_attempt"]}]
        if "UPDATE plugin_wxbot_report_jobs SET" in sql:
            if (
                state["status"] != params["expected_status"]
                or state["run_attempt"] != params["expected_run_attempt"]
            ):
                return []
            state["status"] = params["status"]
            state["current_stage"] = params["current_stage"]
            return [{"id": params["id"]}]
        raise AssertionError(sql)

    monkeypatch.setattr(report_store_module, "_exec", fake_exec)
    store = _ReportStore()

    attempt_1 = await store.try_start_report_job(7)
    state["expired"] = True
    attempt_2 = await store.try_start_report_job(7)

    stale_write = await store.update_report_job(
        7,
        status="completed",
        current_stage="completed-by-stale-worker",
        expected_run_attempt=attempt_1,
        expected_status="running",
    )
    current_write = await store.update_report_job(
        7,
        status="completed",
        current_stage="completed",
        expected_run_attempt=attempt_2,
        expected_status="running",
    )

    assert (attempt_1, attempt_2) == (1, 2)
    assert stale_write is False
    assert current_write is True
    assert state["current_stage"] == "completed"


@pytest.mark.asyncio
async def test_self_review_claim_and_updates_use_status_attempt_cas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict | None]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params))
        if "run_attempt = run_attempt + 1" in sql:
            return [{"run_attempt": 4}]
        return [{"id": 11}]

    monkeypatch.setattr(report_store_module, "_exec", fake_exec)
    store = _ReportStore()

    attempt = await store.try_start_self_review_job(11, stale_seconds=120)
    updated = await store.update_self_review_job(
        11,
        status="completed",
        current_stage="completed",
        expected_run_attempt=attempt,
        expected_status="running",
    )

    assert attempt == 4
    assert updated is True
    assert "status = 'running'" in calls[0][0]
    assert "updated_at < NOW()" in calls[0][0]
    assert calls[0][1] == {"id": 11, "stale_seconds": 120.0}
    assert "run_attempt = :expected_run_attempt" in calls[1][0]
    assert "status = :expected_status" in calls[1][0]


@pytest.mark.asyncio
async def test_expired_sending_delivery_becomes_indeterminate_and_cannot_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"delivery_status": "sending", "expired": True, "delivery_attempt": 1}

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        _ = params
        if "delivery_status = 'indeterminate'" in sql:
            if state["delivery_status"] == "sending" and state["expired"] is True:
                state["delivery_status"] = "indeterminate"
                state["expired"] = False
                return [{"id": 9}]
            return []
        if "delivery_attempt = delivery_attempt + 1" in sql:
            if state["delivery_status"] not in {"pending", "failed"}:
                return []
            state["delivery_status"] = "sending"
            state["delivery_attempt"] = int(state["delivery_attempt"]) + 1
            return [{"delivery_attempt": state["delivery_attempt"]}]
        raise AssertionError(sql)

    monkeypatch.setattr(report_store_module, "_exec", fake_exec)
    store = _ReportStore()

    assert await store.try_start_report_delivery(9, stale_seconds=120) is None
    assert await store.try_start_report_delivery(9, stale_seconds=120) is None
    assert state == {
        "delivery_status": "indeterminate",
        "expired": False,
        "delivery_attempt": 1,
    }


@pytest.mark.asyncio
async def test_delivery_ack_transitions_are_fenced_by_attempt_and_sdk_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict | None]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params))
        return [{"id": 9}]

    monkeypatch.setattr(report_store_module, "_exec", fake_exec)
    store = _ReportStore()

    assert await store.mark_report_delivery_queued(
        9,
        delivery_attempt=3,
        sdk_outbound_id=71,
    )
    assert await store.touch_report_delivery_check(
        9,
        delivery_attempt=3,
        sdk_outbound_id=71,
        error="still queued",
    )
    assert await store.mark_report_delivery_terminal(
        9,
        delivery_attempt=3,
        sdk_outbound_id=71,
        status="sent",
    )

    queued_sql, queued_params = calls[0]
    assert "delivery_status = 'sending'" in queued_sql
    assert "delivery_status = 'queued'" in queued_sql
    assert "delivery_attempt = :delivery_attempt" in queued_sql
    assert queued_params == {
        "id": 9,
        "delivery_attempt": 3,
        "sdk_outbound_id": 71,
    }

    for sql, params in calls[1:]:
        assert "delivery_status = 'queued'" in sql
        assert "delivery_attempt = :delivery_attempt" in sql
        assert "sdk_outbound_id = :sdk_outbound_id" in sql
        assert params is not None
        assert params["delivery_attempt"] == 3
        assert params["sdk_outbound_id"] == 71
    assert calls[1][1] == {
        "id": 9,
        "delivery_attempt": 3,
        "sdk_outbound_id": 71,
        "error": "still queued",
    }
    assert calls[2][1] == {
        "id": 9,
        "delivery_attempt": 3,
        "sdk_outbound_id": 71,
        "status": "sent",
        "is_sent": True,
        "error": "",
    }
    assert "delivery_checked_at = NOW()" in calls[1][0]
    assert "CASE WHEN :is_sent THEN NOW() ELSE NULL END" in calls[2][0]


@pytest.mark.asyncio
async def test_delivery_terminal_rejects_retryable_status_without_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_exec(sql: str, params: dict | None = None) -> list[dict]:
        raise AssertionError((sql, params))

    monkeypatch.setattr(report_store_module, "_exec", fail_exec)
    store = _ReportStore()

    with pytest.raises(ValueError, match="sent or indeterminate"):
        await store.mark_report_delivery_terminal(
            9,
            delivery_attempt=3,
            sdk_outbound_id=71,
            status="failed",
        )


@pytest.mark.asyncio
async def test_reconcile_listing_is_tenant_scoped_bounded_and_hydrated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict | None]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params))
        return [
            {
                "id": 9,
                "report_json": "{}",
                "run_attempt": 2,
                "delivery_attempt": 3,
                "sdk_outbound_id": "71",
            }
        ]

    monkeypatch.setattr(report_store_module, "_exec", fake_exec)
    store = _ReportStore()

    rows = await store.list_report_deliveries_to_reconcile("tenant-a", limit=5001)

    assert rows[0]["sdk_outbound_id"] == 71
    assert rows[0]["delivery_attempt"] == 3
    sql, params = calls[0]
    assert "tenant_id = :tid" in sql
    assert "delivery_status = 'queued'" in sql
    assert "sdk_outbound_id IS NOT NULL" in sql
    assert "delivery_checked_at ASC NULLS FIRST" in sql
    assert params == {"tid": "tenant-a", "limit": 1000}


@pytest.mark.asyncio
async def test_delivery_claim_clears_previous_ack_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        _ = params
        calls.append(sql)
        if "delivery_status = 'indeterminate'" in sql:
            return []
        return [{"delivery_attempt": 4}]

    monkeypatch.setattr(report_store_module, "_exec", fake_exec)
    store = _ReportStore()

    assert await store.try_start_report_delivery(9) == 4
    claim_sql = calls[1]
    assert "sdk_outbound_id = NULL" in claim_sql
    assert "delivery_queued_at = NULL" in claim_sql
    assert "delivery_checked_at = NULL" in claim_sql
    assert "delivered_at = NULL" in claim_sql
    assert "delivery_status IN ('pending', 'failed')" in claim_sql
    assert "'queued'" not in claim_sql
