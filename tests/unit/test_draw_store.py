from __future__ import annotations

import base64
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.common.config import Settings
from plugins.draw.store import (
    DRAW_QUALITY_SIZES,
    DRAW_QUALITY_VALUES,
    DRAW_TASK_INTERRUPTED_ERROR_CODE,
    DRAW_TASK_INTERRUPTED_ERROR_MESSAGE,
    DrawApiError,
    DrawConfigError,
    DrawResult,
    DrawStore,
    DrawTaskCreate,
    _timestamp_param,
)
from tests.unit._schema_fixtures import bootstrap_draw_task_schema


def _draw_settings(tmp_path, **overrides):
    values = {
        "draw_api_url": "",
        "draw_api_edit_url": "",
        "draw_api_key": "",
        "draw_api_key_header": "Authorization",
        "draw_api_key_prefix": "Bearer ",
        "draw_api_model": "",
        "draw_api_extra_body": "",
        "draw_fallback_api_url": "",
        "draw_fallback_api_edit_url": "",
        "draw_fallback_api_key": "",
        "draw_fallback_api_key_header": "Authorization",
        "draw_fallback_api_key_prefix": "Bearer ",
        "draw_fallback_api_model": "",
        "draw_fallback_api_extra_body": "",
        "draw_storage_dir": str(tmp_path),
        "wxbot_media_base_url": "",
    }
    values.update(overrides)
    return Settings(**values)


class _SqliteResult:
    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._rows = cursor.fetchall() if cursor.description is not None else []
        self.rowcount = cursor.rowcount

    def mappings(self) -> _SqliteResult:
        return self

    def first(self) -> dict[str, object] | None:
        row = self._rows[0] if self._rows else None
        return dict(row) if row is not None else None

    def all(self) -> list[dict[str, object]]:
        return [dict(row) for row in self._rows]


class _SqliteSession:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _SqliteSession:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        _ = exc_type, exc, tb

    def get_bind(self) -> object:
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    async def execute(self, statement: object, params: dict[str, object] | None = None) -> _SqliteResult:
        cursor = self._connection.execute(str(statement), params or {})
        return _SqliteResult(cursor)

    async def commit(self) -> None:
        self._connection.commit()

    async def rollback(self) -> None:
        self._connection.rollback()


class _SqliteSessionFactory:
    def __init__(self) -> None:
        self._connection = sqlite3.connect(":memory:")
        self._connection.row_factory = sqlite3.Row

    def __call__(self) -> _SqliteSession:
        return _SqliteSession(self._connection)

    def close(self) -> None:
        self._connection.close()


@pytest.fixture
async def draw_task_store(tmp_path):
    factory = _SqliteSessionFactory()
    bootstrap_draw_task_schema(factory._connection)
    settings = _draw_settings(tmp_path, app_env="test")
    store = DrawStore(settings, session_factory=factory)
    try:
        yield store
    finally:
        await store.close()
        factory.close()


def test_draw_quality_sizes_match_gpt_image_2_constraints() -> None:
    assert tuple(DRAW_QUALITY_SIZES) == DRAW_QUALITY_VALUES == ("low", "medium", "high")
    assert DRAW_QUALITY_SIZES == {
        "low": "1024x1024",
        "medium": "2048x2048",
        "high": "3840x2160",
    }

    for size in DRAW_QUALITY_SIZES.values():
        parts = size.split("x")
        assert len(parts) == 2
        assert all(part.isdecimal() for part in parts)
        width, height = (int(part) for part in parts)
        long_edge = max(width, height)
        short_edge = min(width, height)
        total_pixels = width * height

        assert width % 16 == 0
        assert height % 16 == 0
        assert long_edge <= 3840
        assert long_edge / short_edge <= 3
        assert 655360 <= total_pixels <= 8294400


def test_draw_task_timestamp_params_are_dialect_safe() -> None:
    aware = datetime.now(UTC)
    iso = aware.isoformat()

    postgres_value = _timestamp_param(iso, dialect="postgresql")
    sqlite_value = _timestamp_param(aware, dialect="sqlite")

    assert isinstance(postgres_value, datetime)
    assert postgres_value.tzinfo is not None
    assert sqlite_value == iso


@pytest.mark.asyncio
async def test_draw_task_store_records_transitions_and_result(draw_task_store: DrawStore) -> None:
    task = await draw_task_store.create_draw_task(
        DrawTaskCreate(
            request_id="req-1",
            trace_id="trace-1:draw",
            command_type="/draw",
            tenant_id="demo",
            channel="wechat",
            session_id="room@chatroom",
            prompt="一只柴犬",
            callback_target={"channel": "wechat", "session_id": "room@chatroom"},
            source_message={"message_id": "m-1"},
        )
    )

    assert task.status == "queued"
    assert task.task_id
    assert task.command_type == "draw"

    running = await draw_task_store.mark_draw_task_running(task.task_id)
    assert running is not None
    assert running.status == "running"
    assert running.started_at

    completed = await draw_task_store.complete_draw_task(
        task.task_id,
        DrawResult(
            image_id="img_done",
            prompt="一只柴犬",
            local_path="/tmp/done.png",
            file_name="done.png",
            media_type="image/png",
            public_path="/plugins/draw/files/done.png",
            source_url="http://media.test/done.png",
        ),
    )

    assert completed is not None
    assert completed.status == "completed"
    assert completed.result_image_id == "img_done"
    assert completed.result_source_url == "http://media.test/done.png"
    assert completed.finished_at
    assert completed.retry_count == 0


@pytest.mark.asyncio
async def test_draw_task_store_reuses_existing_task_id(draw_task_store: DrawStore) -> None:
    first = await draw_task_store.create_draw_task(
        DrawTaskCreate(
            task_id="drawtask_msg_same",
            request_id="req-1",
            trace_id="trace-1:draw",
            command_type="/draw",
            tenant_id="demo",
            channel="discord",
            session_id="discord-channel-1",
            original_message_id="discord-msg-1",
            prompt="first prompt",
        )
    )
    duplicate = await draw_task_store.create_draw_task(
        DrawTaskCreate(
            task_id="drawtask_msg_same",
            request_id="req-1b",
            trace_id="trace-1b:draw",
            command_type="/draw",
            tenant_id="demo",
            channel="discord",
            session_id="discord-channel-1",
            original_message_id="discord-msg-1",
            prompt="second prompt",
        )
    )

    assert duplicate.task_id == first.task_id
    assert duplicate.trace_id == "trace-1:draw"
    assert duplicate.prompt == "first prompt"
    records = await draw_task_store.list_draw_tasks(tenant_id="demo")
    assert [record.task_id for record in records].count("drawtask_msg_same") == 1


@pytest.mark.asyncio
async def test_draw_task_store_failed_transition_and_callback_claim_idempotency(
    draw_task_store: DrawStore,
) -> None:
    task = await draw_task_store.create_draw_task(
        DrawTaskCreate(
            request_id="req-fail",
            trace_id="trace-fail:draw",
            command_type="/draw",
            prompt="一只柴犬",
        )
    )

    failed = await draw_task_store.fail_draw_task(
        task.task_id,
        status="failed",
        error_code="upstream_request_failed",
        error_message="provider failed",
    )

    assert failed is not None
    assert failed.status == "failed"
    assert failed.error_code == "upstream_request_failed"
    assert failed.error_message == "provider failed"

    assert await draw_task_store.claim_draw_task_callback(task.task_id) is True
    assert await draw_task_store.claim_draw_task_callback(task.task_id) is False
    after_claim = await draw_task_store.get_draw_task(task.task_id)
    assert after_claim is not None
    assert after_claim.callback_sent is False
    await draw_task_store.mark_draw_task_callback_sent(task.task_id)
    assert await draw_task_store.claim_draw_task_callback(task.task_id) is False
    after = await draw_task_store.get_draw_task(task.task_id)
    assert after is not None
    assert after.callback_sent is True


@pytest.mark.asyncio
async def test_draw_task_store_lists_recent_by_status(draw_task_store: DrawStore) -> None:
    queued = await draw_task_store.create_draw_task(
        DrawTaskCreate(
            request_id="req-list-1",
            trace_id="trace-list-1:draw",
            command_type="draw",
            status="queued",
            tenant_id="demo",
            prompt="一只柴犬",
        )
    )
    running = await draw_task_store.create_draw_task(
        DrawTaskCreate(
            request_id="req-list-2",
            trace_id="trace-list-2:draw",
            command_type="draw",
            status="queued",
            tenant_id="demo",
            prompt="一只橘猫",
        )
    )
    await draw_task_store.mark_draw_task_running(running.task_id)

    records = await draw_task_store.list_draw_tasks(status="running", tenant_id="demo")

    assert [record.task_id for record in records] == [running.task_id]
    assert queued.task_id not in {record.task_id for record in records}


@pytest.mark.asyncio
async def test_draw_task_store_reserves_retry_budget_and_creates_retry_child(
    draw_task_store: DrawStore,
) -> None:
    task = await draw_task_store.create_draw_task(
        DrawTaskCreate(
            request_id="req-retry",
            trace_id="trace-retry:draw",
            command_type="draw",
            tenant_id="demo",
            channel="wechat",
            session_id="room@chatroom",
            prompt="一只柴犬",
            callback_target={"channel": "wechat", "session_id": "room@chatroom"},
            source_message={"message_id": "m-1"},
        )
    )
    await draw_task_store.fail_draw_task(
        task.task_id,
        status="failed",
        error_code="upstream_request_failed",
        error_message="provider failed",
    )

    reserved = await draw_task_store.reserve_draw_task_retry(task.task_id, max_retries=1)
    assert reserved is not None
    assert reserved.retry_count == 1

    next_run_at = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
    child = await draw_task_store.create_retry_draw_task(
        reserved,
        retry_count=reserved.retry_count,
        next_run_at=next_run_at,
    )
    assert child.task_id != task.task_id
    assert child.status == "queued"
    assert child.retry_count == 1
    assert child.next_run_at == next_run_at
    assert child.prompt == "一只柴犬"
    assert child.callback_target == {"channel": "wechat", "session_id": "room@chatroom"}
    assert child.source_message["draw_retry_parent_task_id"] == task.task_id

    over_budget = await draw_task_store.reserve_draw_task_retry(task.task_id, max_retries=1)
    assert over_budget is None


@pytest.mark.asyncio
async def test_draw_task_store_claims_due_queued_task(draw_task_store: DrawStore) -> None:
    due = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    task = await draw_task_store.create_draw_task(
        DrawTaskCreate(
            request_id="req-claim",
            trace_id="trace-claim:draw",
            command_type="draw",
            status="queued",
            prompt="到期任务",
            next_run_at=due,
        )
    )

    claimed = await draw_task_store.claim_due_draw_tasks(
        limit=2,
        lock_ttl_seconds=30,
        worker_id="worker-a",
    )

    assert [record.task_id for record in claimed] == [task.task_id]
    assert claimed[0].status == "running"
    assert claimed[0].locked_by == "worker-a"
    assert claimed[0].locked_until


@pytest.mark.asyncio
async def test_draw_task_store_scope_defer_releases_only_owned_queue_claim(
    draw_task_store: DrawStore,
) -> None:
    task = await draw_task_store.create_draw_task(
        DrawTaskCreate(
            request_id="req-scope-defer",
            trace_id="trace-scope-defer:draw",
            command_type="draw",
            status="queued",
            tenant_id="tenant-disabled",
            session_id="room@chatroom",
            prompt="作用域关闭任务",
        )
    )
    claimed = await draw_task_store.claim_due_draw_tasks(
        limit=1,
        lock_ttl_seconds=60,
        worker_id="worker-a",
    )
    assert [record.task_id for record in claimed] == [task.task_id]

    assert not await draw_task_store.defer_draw_task_claim(
        task.task_id,
        worker_id="worker-b",
    )
    assert await draw_task_store.defer_draw_task_claim(
        task.task_id,
        worker_id="worker-a",
        defer_seconds=30,
    )

    deferred = await draw_task_store.get_draw_task(task.task_id)
    assert deferred is not None
    assert deferred.status == "queued"
    assert deferred.locked_by == ""
    assert deferred.locked_until == ""
    assert deferred.next_run_at > datetime.now(UTC).isoformat()
    assert deferred.retry_count == 0
    assert await draw_task_store.claim_due_draw_tasks(
        limit=1,
        worker_id="worker-b",
    ) == []


@pytest.mark.asyncio
async def test_draw_task_store_locked_task_is_not_claimed_twice(draw_task_store: DrawStore) -> None:
    task = await draw_task_store.create_draw_task(
        DrawTaskCreate(
            request_id="req-lock",
            trace_id="trace-lock:draw",
            command_type="draw",
            status="queued",
            prompt="锁定任务",
        )
    )

    first = await draw_task_store.claim_due_draw_tasks(
        limit=1,
        lock_ttl_seconds=60,
        worker_id="worker-a",
    )
    second = await draw_task_store.claim_due_draw_tasks(
        limit=1,
        lock_ttl_seconds=60,
        worker_id="worker-b",
    )

    assert [record.task_id for record in first] == [task.task_id]
    assert second == []


@pytest.mark.asyncio
async def test_draw_task_store_expired_lock_can_be_claimed(draw_task_store: DrawStore) -> None:
    task = await draw_task_store.create_draw_task(
        DrawTaskCreate(
            request_id="req-expired-lock",
            trace_id="trace-expired-lock:draw",
            command_type="draw",
            status="queued",
            prompt="过期锁任务",
        )
    )
    await draw_task_store.claim_due_draw_tasks(
        limit=1,
        lock_ttl_seconds=1,
        worker_id="worker-a",
    )
    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    async with draw_task_store._db() as db:
        await db.execute(
            """
            UPDATE plugin_draw_task
            SET locked_until = :locked_until
            WHERE task_id = :task_id
            """,
            {"locked_until": expired, "task_id": task.task_id},
        )

    claimed = await draw_task_store.claim_due_draw_tasks(
        limit=1,
        lock_ttl_seconds=60,
        worker_id="worker-b",
    )

    assert [record.task_id for record in claimed] == [task.task_id]
    assert claimed[0].locked_by == "worker-b"


@pytest.mark.asyncio
async def test_draw_task_store_inline_claim_respects_active_queued_lock(
    draw_task_store: DrawStore,
) -> None:
    task = await draw_task_store.create_draw_task(
        DrawTaskCreate(
            request_id="req-inline-lock",
            trace_id="trace-inline-lock:draw",
            command_type="draw",
            status="queued",
            prompt="锁定内联任务",
        )
    )
    locked_until = (datetime.now(UTC) + timedelta(seconds=60)).isoformat()
    async with draw_task_store._db() as db:
        await db.execute(
            """
            UPDATE plugin_draw_task
            SET locked_until = :locked_until,
                locked_by = :locked_by
            WHERE task_id = :task_id
            """,
            {
                "locked_until": locked_until,
                "locked_by": "worker-a",
                "task_id": task.task_id,
            },
        )

    claimed = await draw_task_store.claim_draw_task_for_execution(
        task.task_id,
        lock_ttl_seconds=60,
        worker_id="inline-runner",
    )

    assert claimed is not None
    assert claimed.status == "queued"
    assert claimed.locked_by == "worker-a"


@pytest.mark.asyncio
async def test_draw_task_store_inline_claim_can_take_expired_running_lock(
    draw_task_store: DrawStore,
) -> None:
    task = await draw_task_store.create_draw_task(
        DrawTaskCreate(
            request_id="req-inline-expired",
            trace_id="trace-inline-expired:draw",
            command_type="draw",
            status="queued",
            prompt="过期运行任务",
        )
    )
    first = await draw_task_store.claim_due_draw_tasks(
        limit=1,
        lock_ttl_seconds=1,
        worker_id="worker-a",
    )
    assert [record.task_id for record in first] == [task.task_id]
    expired = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    async with draw_task_store._db() as db:
        await db.execute(
            """
            UPDATE plugin_draw_task
            SET locked_until = :locked_until
            WHERE task_id = :task_id
            """,
            {"locked_until": expired, "task_id": task.task_id},
        )

    claimed = await draw_task_store.claim_draw_task_for_execution(
        task.task_id,
        lock_ttl_seconds=60,
        worker_id="inline-runner",
    )

    assert claimed is not None
    assert claimed.status == "running"
    assert claimed.locked_by == "inline-runner"


@pytest.mark.asyncio
async def test_draw_task_store_records_callback_error_without_marking_sent(
    draw_task_store: DrawStore,
) -> None:
    task = await draw_task_store.create_draw_task(
        DrawTaskCreate(
            request_id="req-callback-error",
            trace_id="trace-callback-error:draw",
            command_type="draw",
            prompt="一只柴犬",
        )
    )

    await draw_task_store.mark_draw_task_callback_error(
        task.task_id,
        callback_error="send failed",
    )

    record = await draw_task_store.get_draw_task(task.task_id)
    assert record is not None
    assert record.callback_sent is False
    assert record.callback_error == "send failed"


@pytest.mark.asyncio
async def test_draw_task_store_scope_release_preserves_retryable_callback(
    draw_task_store: DrawStore,
) -> None:
    task = await draw_task_store.create_draw_task(
        DrawTaskCreate(
            request_id="req-callback-scope-release",
            trace_id="trace-callback-scope-release:draw",
            command_type="draw",
            status="completed",
            tenant_id="tenant-toggle",
            session_id="room@chatroom",
            prompt="回调重试",
        )
    )
    assert await draw_task_store.claim_draw_task_callback(task.task_id)
    assert await draw_task_store.release_draw_task_callback_claim(
        task.task_id,
        reason="scope_execution_denied",
    )

    released = await draw_task_store.get_draw_task(task.task_id)
    assert released is not None
    assert released.callback_sent is False
    assert released.callback_error == "scope_execution_denied"
    assert await draw_task_store.claim_draw_task_callback(task.task_id)


@pytest.mark.asyncio
async def test_draw_task_store_callback_claim_retries_after_error(
    draw_task_store: DrawStore,
) -> None:
    task = await draw_task_store.create_draw_task(
        DrawTaskCreate(
            request_id="req-callback-retry",
            trace_id="trace-callback-retry:draw",
            command_type="draw",
            prompt="一只柴犬",
        )
    )

    assert await draw_task_store.claim_draw_task_callback(task.task_id) is True
    assert await draw_task_store.claim_draw_task_callback(task.task_id) is False

    await draw_task_store.mark_draw_task_callback_error(
        task.task_id,
        callback_error="send failed",
    )

    record = await draw_task_store.get_draw_task(task.task_id)
    assert record is not None
    assert record.callback_sent is False
    assert record.callback_error == "send failed"
    assert await draw_task_store.claim_draw_task_callback(task.task_id) is True


@pytest.mark.asyncio
async def test_draw_task_store_forced_callback_error_updates_sent_task(
    draw_task_store: DrawStore,
) -> None:
    task = await draw_task_store.create_draw_task(
        DrawTaskCreate(
            request_id="req-forced-callback-error",
            trace_id="trace-forced-callback-error:draw",
            command_type="draw",
            prompt="一只柴犬",
        )
    )
    await draw_task_store.mark_draw_task_callback_sent(task.task_id)

    await draw_task_store.mark_draw_task_callback_error(
        task.task_id,
        callback_error="forced resend failed",
        force=True,
    )

    record = await draw_task_store.get_draw_task(task.task_id)
    assert record is not None
    assert record.callback_sent is True
    assert record.callback_error == "forced resend failed"


@pytest.mark.asyncio
async def test_draw_task_store_recovers_stale_queued_and_running_tasks(
    draw_task_store: DrawStore,
) -> None:
    old = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    queued = await draw_task_store.create_draw_task(
        DrawTaskCreate(
            request_id="req-stale-queued",
            trace_id="trace-stale-queued:draw",
            command_type="draw",
            status="queued",
            prompt="旧的队列任务",
            created_at=old,
        )
    )
    running = await draw_task_store.create_draw_task(
        DrawTaskCreate(
            request_id="req-stale-running",
            trace_id="trace-stale-running:draw",
            command_type="draw",
            status="running",
            prompt="旧的运行任务",
            created_at=old,
        )
    )

    recovered = await draw_task_store.recover_stale_tasks(stale_seconds=60)

    assert {record.task_id for record in recovered} == {queued.task_id, running.task_id}
    for task_id in {queued.task_id, running.task_id}:
        record = await draw_task_store.get_draw_task(task_id)
        assert record is not None
        assert record.status == "interrupted"
        assert record.error_code == DRAW_TASK_INTERRUPTED_ERROR_CODE
        assert record.error_message == DRAW_TASK_INTERRUPTED_ERROR_MESSAGE
        assert record.callback_sent is False


@pytest.mark.asyncio
async def test_draw_task_store_scope_gates_single_stale_recovery(
    draw_task_store: DrawStore,
) -> None:
    old = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    task = await draw_task_store.create_draw_task(
        DrawTaskCreate(
            request_id="req-stale-single",
            trace_id="trace-stale-single:draw",
            command_type="draw",
            status="running",
            tenant_id="tenant-enabled",
            session_id="room@chatroom",
            prompt="单条恢复",
            created_at=old,
        )
    )

    recovered = await draw_task_store.recover_stale_draw_task(
        task.task_id,
        stale_seconds=60,
    )
    assert recovered is not None
    assert recovered.status == "interrupted"
    assert recovered.error_code == DRAW_TASK_INTERRUPTED_ERROR_CODE

    fresh = await draw_task_store.create_draw_task(
        DrawTaskCreate(
            request_id="req-fresh-single",
            trace_id="trace-fresh-single:draw",
            command_type="draw",
            status="running",
            tenant_id="tenant-enabled",
            session_id="room@chatroom",
            prompt="新任务",
        )
    )
    assert (
        await draw_task_store.recover_stale_draw_task(
            fresh.task_id,
            stale_seconds=60,
        )
        is None
    )


@pytest.mark.asyncio
async def test_draw_task_store_scope_defer_does_not_refresh_stale_heartbeat(
    draw_task_store: DrawStore,
) -> None:
    old = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    task = await draw_task_store.create_draw_task(
        DrawTaskCreate(
            request_id="req-stale-scope-defer",
            trace_id="trace-stale-scope-defer:draw",
            command_type="draw",
            status="running",
            tenant_id="tenant-disabled",
            session_id="room@chatroom",
            prompt="禁用租户旧任务",
            created_at=old,
        )
    )
    before = await draw_task_store.get_draw_task(task.task_id)
    assert before is not None

    assert await draw_task_store.defer_stale_draw_task(
        task.task_id,
        stale_seconds=60,
        defer_seconds=30,
    )
    deferred = await draw_task_store.get_draw_task(task.task_id)
    assert deferred is not None
    assert deferred.status == "queued"
    assert deferred.error_code == ""
    assert deferred.heartbeat_at == before.heartbeat_at
    assert deferred.next_run_at > datetime.now(UTC).isoformat()
    assert await draw_task_store.list_stale_draw_tasks(stale_seconds=60) == []


@pytest.mark.asyncio
async def test_draw_task_store_does_not_recover_fresh_or_callback_sent_tasks(
    draw_task_store: DrawStore,
) -> None:
    old = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    fresh = await draw_task_store.create_draw_task(
        DrawTaskCreate(
            request_id="req-fresh",
            trace_id="trace-fresh:draw",
            command_type="draw",
            status="queued",
            prompt="新的队列任务",
        )
    )
    already_sent = await draw_task_store.create_draw_task(
        DrawTaskCreate(
            request_id="req-sent",
            trace_id="trace-sent:draw",
            command_type="draw",
            status="queued",
            prompt="已回调任务",
            created_at=old,
        )
    )
    await draw_task_store.mark_draw_task_callback_sent(already_sent.task_id)

    stale = await draw_task_store.list_stale_draw_tasks(stale_seconds=60)
    recovered = await draw_task_store.recover_stale_tasks(stale_seconds=60)

    assert stale == []
    assert recovered == []
    fresh_record = await draw_task_store.get_draw_task(fresh.task_id)
    assert fresh_record is not None
    assert fresh_record.status == "queued"
    sent_record = await draw_task_store.get_draw_task(already_sent.task_id)
    assert sent_record is not None
    assert sent_record.status == "queued"
    assert sent_record.callback_sent is True


@pytest.mark.asyncio
async def test_draw_task_store_stale_recovery_is_idempotent(draw_task_store: DrawStore) -> None:
    old = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    task = await draw_task_store.create_draw_task(
        DrawTaskCreate(
            request_id="req-idempotent",
            trace_id="trace-idempotent:draw",
            command_type="draw",
            status="queued",
            prompt="旧任务",
            created_at=old,
        )
    )

    first = await draw_task_store.recover_stale_tasks(stale_seconds=60)
    await draw_task_store.mark_draw_task_callback_sent(task.task_id)
    second = await draw_task_store.recover_stale_tasks(stale_seconds=60)

    assert [record.task_id for record in first] == [task.task_id]
    assert second == []


@pytest.mark.asyncio
async def test_draw_task_store_retries_recovered_interrupted_callback_after_failure(
    draw_task_store: DrawStore,
) -> None:
    old = (datetime.now(UTC) - timedelta(seconds=120)).isoformat()
    task = await draw_task_store.create_draw_task(
        DrawTaskCreate(
            request_id="req-retry-interrupted",
            trace_id="trace-retry-interrupted:draw",
            command_type="draw",
            status="running",
            prompt="旧任务",
            created_at=old,
        )
    )

    first = await draw_task_store.recover_stale_tasks(stale_seconds=60)
    assert [record.task_id for record in first] == [task.task_id]

    await draw_task_store.claim_draw_task_callback(task.task_id)
    await draw_task_store.mark_draw_task_callback_error(
        task.task_id,
        callback_error="send failed",
    )

    retry = await draw_task_store.recover_stale_tasks(stale_seconds=60)
    assert [record.task_id for record in retry] == [task.task_id]
    assert retry[0].status == "interrupted"
    assert retry[0].error_code == DRAW_TASK_INTERRUPTED_ERROR_CODE


@pytest.mark.asyncio
async def test_draw_task_store_does_not_overwrite_interrupted_terminal_state(
    draw_task_store: DrawStore,
) -> None:
    task = await draw_task_store.create_draw_task(
        DrawTaskCreate(
            request_id="req-terminal",
            trace_id="trace-terminal:draw",
            command_type="draw",
            status="queued",
            prompt="旧任务",
        )
    )
    interrupted = await draw_task_store.fail_draw_task(
        task.task_id,
        status="interrupted",
        error_code=DRAW_TASK_INTERRUPTED_ERROR_CODE,
        error_message=DRAW_TASK_INTERRUPTED_ERROR_MESSAGE,
    )
    assert interrupted is not None
    assert interrupted.status == "interrupted"

    running = await draw_task_store.mark_draw_task_running(task.task_id)
    completed = await draw_task_store.complete_draw_task(
        task.task_id,
        DrawResult(
            image_id="img_late",
            prompt="旧任务",
            local_path="/tmp/late.png",
            file_name="late.png",
            media_type="image/png",
            public_path="/plugins/draw/files/late.png",
        ),
    )
    failed = await draw_task_store.fail_draw_task(
        task.task_id,
        status="failed",
        error_code="upstream_request_failed",
        error_message="late failure",
    )

    for record in (running, completed, failed):
        assert record is not None
        assert record.status == "interrupted"
        assert record.error_code == DRAW_TASK_INTERRUPTED_ERROR_CODE
        assert record.error_message == DRAW_TASK_INTERRUPTED_ERROR_MESSAGE
        assert record.result_image_id == ""


@pytest.mark.asyncio
async def test_draw_store_generate_checks_storage_before_upstream_request(monkeypatch, tmp_path) -> None:
    upstream_called = False

    def responder(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_called
        upstream_called = True
        return httpx.Response(500)

    settings = _draw_settings(tmp_path,
        draw_api_url="http://127.0.0.1:18080/v1/images/generations",
        draw_api_key="sk-test",
        draw_api_model="gpt-image-2",
    )
    store = DrawStore(settings)
    store._client = httpx.AsyncClient(transport=httpx.MockTransport(responder))

    def fail_storage() -> None:
        raise DrawConfigError("DRAW_STORAGE_DIR 不可写")

    monkeypatch.setattr(store, "_ensure_storage_dir", fail_storage)

    with pytest.raises(DrawConfigError):
        await store.generate_image("赛博朋克城市夜景", trace_id="trace-storage")

    assert upstream_called is False
    await store.close()


@pytest.mark.asyncio
async def test_draw_store_reference_edit_checks_storage_before_upstream_request(
    monkeypatch,
    tmp_path,
) -> None:
    upstream_called = False

    def responder(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_called
        upstream_called = True
        return httpx.Response(500)

    settings = _draw_settings(tmp_path,
        draw_api_url="http://127.0.0.1:18080/v1/images/generations",
        draw_api_key="sk-test",
        draw_api_model="gpt-image-2",
    )
    store = DrawStore(settings)
    store._client = httpx.AsyncClient(transport=httpx.MockTransport(responder))

    def fail_storage() -> None:
        raise DrawConfigError("DRAW_STORAGE_DIR 不可写")

    monkeypatch.setattr(store, "_ensure_storage_dir", fail_storage)

    with pytest.raises(DrawConfigError):
        await store.edit_reference_image(
            image_url="http://media.test/source.png",
            prompt="改成水彩",
            trace_id="trace-storage",
        )

    assert upstream_called is False
    await store.close()


@pytest.mark.asyncio
async def test_draw_store_downloads_relative_image_url_from_gpt2api(
    tmp_path,
) -> None:
    captured: dict[str, object] = {}
    png_bytes = b"\x89PNG\r\n\x1a\nfake-png"

    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            captured["post_url"] = str(request.url)
            captured["post_headers"] = dict(request.headers)
            captured["post_body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"data": [{"url": "/p/img/task-1/0?exp=1&sig=test"}]},
            )
        if request.method == "GET":
            captured["get_url"] = str(request.url)
            return httpx.Response(
                200,
                content=png_bytes,
                headers={"content-type": "image/png"},
            )
        return httpx.Response(405)

    settings = _draw_settings(tmp_path,
        draw_api_url="http://127.0.0.1:18080/v1/images/generations",
        draw_api_key="sk-test",
        draw_api_model="gpt-image-2",
    )
    store = DrawStore(settings)
    store._client = httpx.AsyncClient(transport=httpx.MockTransport(responder))

    result = await store.generate_image("赛博朋克城市夜景", trace_id="trace-xyz")

    assert captured["post_url"] == "http://127.0.0.1:18080/v1/images/generations"
    assert captured["post_body"] == {
        "prompt": "赛博朋克城市夜景",
        "model": "gpt-image-2",
        "quality": "low",
        "size": "1024x1024",
    }
    assert captured["post_headers"]["authorization"] == "Bearer sk-test"
    assert captured["get_url"] == "http://127.0.0.1:18080/p/img/task-1/0?exp=1&sig=test"
    assert result.media_type == "image/png"
    assert result.image_id.startswith("img_")
    assert result.file_name.endswith(".png")
    assert result.public_path.endswith(result.file_name)
    assert result.source_url == "http://127.0.0.1:18080/p/img/task-1/0?exp=1&sig=test"
    assert tmp_path.joinpath(result.file_name).read_bytes() == png_bytes
    listed = store.list_images()
    assert listed[0].image_id == result.image_id
    assert listed[0].file_name == result.file_name

    await store.close()


@pytest.mark.asyncio
async def test_draw_store_does_not_forward_api_key_to_cross_origin_image(
    tmp_path,
) -> None:
    requests: list[httpx.Request] = []
    png_bytes = b"\x89PNG\r\n\x1a\ncross-origin-png"

    def responder(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={"data": [{"url": "https://cdn.example.test/image.png"}]},
            )
        return httpx.Response(
            200,
            content=png_bytes,
            headers={"content-type": "image/png"},
        )

    settings = _draw_settings(
        tmp_path,
        draw_api_url="https://draw.example.test/v1/images/generations",
        draw_api_key="sk-draw-secret",
        draw_api_model="gpt-image-2",
    )
    store = DrawStore(settings)
    store._client = httpx.AsyncClient(transport=httpx.MockTransport(responder))

    result = await store.generate_image("跨域下载测试", trace_id="trace-cross-origin")

    assert len(requests) == 2
    assert requests[0].headers["authorization"] == "Bearer sk-draw-secret"
    assert str(requests[1].url) == "https://cdn.example.test/image.png"
    assert "authorization" not in requests[1].headers
    assert tmp_path.joinpath(result.file_name).read_bytes() == png_bytes
    await store.close()


@pytest.mark.asyncio
async def test_draw_store_rejects_generation_post_redirect_without_second_request(
    tmp_path,
) -> None:
    requests: list[httpx.Request] = []

    def responder(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            307,
            headers={"location": "https://attacker.example.test/collect"},
        )

    settings = _draw_settings(
        tmp_path,
        draw_api_url="https://draw.example.test/v1/images/generations",
        draw_api_key="sk-draw-secret",
        draw_api_model="gpt-image-2",
    )
    store = DrawStore(settings)
    store._client = httpx.AsyncClient(transport=httpx.MockTransport(responder))

    with pytest.raises(DrawApiError, match="绘图接口请求失败"):
        await store.generate_image("重定向测试", trace_id="trace-redirect")

    assert len(requests) == 1
    assert requests[0].headers["authorization"] == "Bearer sk-draw-secret"
    await store.close()


@pytest.mark.asyncio
async def test_draw_store_falls_back_to_secondary_endpoint_when_primary_times_out(
    tmp_path,
) -> None:
    captured: dict[str, object] = {}
    png_bytes = b"\x89PNG\r\n\x1a\nfallback-png"

    def primary_responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("primary timeout", request=request)

    image_b64 = base64.b64encode(png_bytes).decode("ascii")

    def fallback_responder(request: httpx.Request) -> httpx.Response:
        captured["fallback_url"] = str(request.url)
        captured["fallback_headers"] = dict(request.headers)
        captured["fallback_body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"created": 1, "data": [{"b64_json": image_b64}]},
        )

    settings = _draw_settings(tmp_path,
        draw_api_url="http://127.0.0.1:8081/v1/images/generations",
        draw_api_key="sk-primary",
        draw_api_model="gpt-image-2",
        draw_fallback_api_url="https://fallback-image.example.test/v1/images/generations",
        draw_fallback_api_key="sk-fallback",
        draw_fallback_api_model="gpt-image-2",
        draw_fallback_api_timeout_seconds=240.0,
    )
    store = DrawStore(settings)
    store._client = httpx.AsyncClient(transport=httpx.MockTransport(primary_responder))
    store._clients["fallback"] = httpx.AsyncClient(transport=httpx.MockTransport(fallback_responder))

    result = await store.generate_image("一只戴墨镜的橘猫", trace_id="trace-fallback")

    assert captured["fallback_url"] == "https://fallback-image.example.test/v1/images/generations"
    assert captured["fallback_body"] == {
        "prompt": "一只戴墨镜的橘猫",
        "model": "gpt-image-2",
        "quality": "low",
        "size": "1024x1024",
    }
    assert captured["fallback_headers"]["authorization"] == "Bearer sk-fallback"
    assert result.media_type == "image/png"
    assert tmp_path.joinpath(result.file_name).read_bytes() == png_bytes

    await store.close()


@pytest.mark.asyncio
async def test_draw_store_accepts_openai_compatible_base_url(tmp_path) -> None:
    captured: dict[str, object] = {}
    png_bytes = b"\x89PNG\r\n\x1a\nbase-url-png"
    image_b64 = base64.b64encode(png_bytes).decode("ascii")

    def responder(request: httpx.Request) -> httpx.Response:
        captured["post_url"] = str(request.url)
        captured["post_body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"created": 1, "data": [{"b64_json": image_b64}]},
        )

    settings = _draw_settings(tmp_path,
        draw_api_url="https://primary-image.example.test/v1",
        draw_api_key="sk-test",
        draw_api_model="gpt-image-2",
        draw_api_extra_body='{"size":"2048x2048","quality":"medium","background":"opaque","output_format":"png","n":1,"stream":true}',
    )
    store = DrawStore(settings)
    store._client = httpx.AsyncClient(transport=httpx.MockTransport(responder))

    result = await store.generate_image("一只可爱的柴犬", trace_id="trace-base-url")

    assert captured["post_url"] == "https://primary-image.example.test/v1/images/generations"
    assert captured["post_body"] == {
        "size": "1024x1024",
        "background": "opaque",
        "output_format": "png",
        "n": 1,
        "stream": True,
        "prompt": "一只可爱的柴犬",
        "model": "gpt-image-2",
        "quality": "low",
    }
    assert tmp_path.joinpath(result.file_name).read_bytes() == png_bytes

    await store.close()


@pytest.mark.asyncio
async def test_draw_store_accepts_openai_compatible_domain_as_base_url(tmp_path) -> None:
    captured: dict[str, object] = {}
    png_bytes = b"\x89PNG\r\n\x1a\ndomain-png"
    image_b64 = base64.b64encode(png_bytes).decode("ascii")

    def responder(request: httpx.Request) -> httpx.Response:
        captured["post_url"] = str(request.url)
        return httpx.Response(200, json={"data": [{"b64_json": image_b64}]})

    settings = _draw_settings(tmp_path,
        draw_api_url="https://primary-image.example.test",
        draw_api_key="sk-test",
        draw_api_model="gpt-image-2",
    )
    store = DrawStore(settings)
    store._client = httpx.AsyncClient(transport=httpx.MockTransport(responder))

    await store.generate_image("樱花树下的柴犬", trace_id="trace-domain")

    assert captured["post_url"] == "https://primary-image.example.test/v1/images/generations"

    await store.close()


@pytest.mark.asyncio
async def test_draw_store_decodes_streaming_image_response(tmp_path) -> None:
    png_bytes = b"\x89PNG\r\n\x1a\nstream-png"
    image_b64 = base64.b64encode(png_bytes).decode("ascii")

    def responder(request: httpx.Request) -> httpx.Response:
        _ = request
        stream_body = "\n".join(
            [
                ": keepalive",
                "",
                'data: {"type":"progress","message":"running"}',
                "",
                f'data: {{"created":1,"data":[{{"b64_json":"{image_b64}"}}]}}',
                "",
                'data: {"type":"progress","message":"finalizing"}',
                "",
                "data: [DONE]",
                "",
            ]
        )
        return httpx.Response(
            200,
            content=stream_body.encode("utf-8"),
            headers={"content-type": "text/event-stream"},
        )

    settings = _draw_settings(tmp_path,
        draw_api_url="https://fallback-image.example.test/v1",
        draw_api_key="sk-test",
        draw_api_model="gpt-image-2",
        draw_api_extra_body='{"stream":true}',
    )
    store = DrawStore(settings)
    store._client = httpx.AsyncClient(transport=httpx.MockTransport(responder))

    result = await store.generate_image("流式保活测试", trace_id="trace-stream")

    assert tmp_path.joinpath(result.file_name).read_bytes() == png_bytes

    await store.close()


@pytest.mark.asyncio
async def test_draw_store_streaming_progress_message_is_not_media_type(tmp_path) -> None:
    png_bytes = b"\x89PNG\r\n\x1a\nstream-png"
    image_b64 = base64.b64encode(png_bytes).decode("ascii")

    def responder(request: httpx.Request) -> httpx.Response:
        _ = request
        stream_body = "\n".join(
            [
                'data: {"type":"progress","message":"image/jpeg"}',
                "",
                f'data: {{"created":1,"data":[{{"b64_json":"{image_b64}"}}]}}',
                "",
                "data: [DONE]",
                "",
            ]
        )
        return httpx.Response(
            200,
            content=stream_body.encode("utf-8"),
            headers={"content-type": "text/event-stream"},
        )

    settings = _draw_settings(
        tmp_path,
        draw_api_url="https://fallback-image.example.test/v1",
        draw_api_key="sk-test",
        draw_api_model="gpt-image-2",
        draw_api_extra_body='{"stream":true}',
    )
    store = DrawStore(settings)
    store._client = httpx.AsyncClient(transport=httpx.MockTransport(responder))

    result = await store.generate_image("流式进度测试", trace_id="trace-stream-progress")

    assert result.media_type == "image/png"
    assert tmp_path.joinpath(result.file_name).read_bytes() == png_bytes

    await store.close()


@pytest.mark.asyncio
async def test_draw_store_edits_image_by_id_with_multipart_payload(tmp_path) -> None:
    captured: dict[str, object] = {}
    source_bytes = b"\x89PNG\r\n\x1a\nsource-png"
    edited_bytes = b"\x89PNG\r\n\x1a\nedited-png"
    source_b64 = base64.b64encode(source_bytes).decode("ascii")
    edited_b64 = base64.b64encode(edited_bytes).decode("ascii")

    def responder(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/v1/images/generations"):
            return httpx.Response(
                200,
                json={"created": 1, "data": [{"b64_json": source_b64}]},
            )
        if str(request.url).endswith("/v1/images/edits"):
            captured["edit_url"] = str(request.url)
            captured["edit_headers"] = dict(request.headers)
            captured["edit_body"] = request.content
            return httpx.Response(
                200,
                json={"created": 2, "data": [{"b64_json": edited_b64}]},
            )
        return httpx.Response(404)

    settings = _draw_settings(tmp_path,
        draw_api_url="http://127.0.0.1:18080/v1/images/generations",
        draw_api_key="sk-test",
        draw_api_model="gpt-image-2",
    )
    store = DrawStore(settings)
    store._client = httpx.AsyncClient(transport=httpx.MockTransport(responder))

    source = await store.generate_image("原图", trace_id="trace-src")
    edited = await store.edit_image(
        source.image_id,
        "把这张图变成梵高星空风格的油画",
        trace_id="trace-edit",
    )

    assert captured["edit_url"] == "http://127.0.0.1:18080/v1/images/edits"
    assert "multipart/form-data" in captured["edit_headers"]["content-type"]
    assert b'name="prompt"' in captured["edit_body"]
    assert b'name="quality"' in captured["edit_body"]
    assert b"low" in captured["edit_body"]
    assert "把这张图变成梵高星空风格的油画".encode() in captured["edit_body"]
    assert b'name="image"; filename="' in captured["edit_body"]
    assert source_bytes in captured["edit_body"]
    assert edited.source_image_id == source.image_id
    assert tmp_path.joinpath(edited.file_name).read_bytes() == edited_bytes
    assert store.resolve_image_id(edited.image_id).source_image_id == source.image_id

    await store.close()


@pytest.mark.asyncio
async def test_draw_store_passes_requested_quality(tmp_path) -> None:
    captured: dict[str, object] = {}
    png_bytes = b"\x89PNG\r\n\x1a\nquality-png"
    image_b64 = base64.b64encode(png_bytes).decode("ascii")

    def responder(request: httpx.Request) -> httpx.Response:
        captured["post_body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"data": [{"b64_json": image_b64}]})

    settings = _draw_settings(tmp_path,
        draw_api_url="https://fallback-image.example.test/v1",
        draw_api_key="sk-test",
        draw_api_model="gpt-image-2",
    )
    store = DrawStore(settings)
    store._client = httpx.AsyncClient(transport=httpx.MockTransport(responder))

    await store.generate_image("高质量图片", trace_id="trace-quality", quality="high")

    assert captured["post_body"]["quality"] == "high"
    assert captured["post_body"]["size"] == "3840x2160"

    await store.close()


@pytest.mark.asyncio
async def test_draw_store_quality_overrides_extra_body_size(tmp_path) -> None:
    captured: dict[str, object] = {}
    png_bytes = b"\x89PNG\r\n\x1a\nmedium-size-png"
    image_b64 = base64.b64encode(png_bytes).decode("ascii")

    def responder(request: httpx.Request) -> httpx.Response:
        captured["post_body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"data": [{"b64_json": image_b64}]})

    settings = _draw_settings(tmp_path,
        draw_api_url="https://fallback-image.example.test/v1",
        draw_api_key="sk-test",
        draw_api_model="gpt-image-2",
        draw_api_extra_body='{"size":"4096x4096","stream":true}',
    )
    store = DrawStore(settings)
    store._client = httpx.AsyncClient(transport=httpx.MockTransport(responder))

    await store.generate_image("中等质量图片", trace_id="trace-medium", quality="medium")

    assert captured["post_body"]["quality"] == "medium"
    assert captured["post_body"]["size"] == "2048x2048"
    assert captured["post_body"]["stream"] is True

    await store.close()


@pytest.mark.asyncio
async def test_draw_store_edits_reference_image_url_with_multipart_payload(tmp_path) -> None:
    captured: dict[str, object] = {}
    source_bytes = b"\x89PNG\r\n\x1a\nquoted-png"
    edited_bytes = b"\x89PNG\r\n\x1a\nedited-quoted-png"
    edited_b64 = base64.b64encode(edited_bytes).decode("ascii")

    def responder(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "http://127.0.0.1:5080/images/hash-601/601.png":
            captured["source_get_url"] = str(request.url)
            captured["source_get_headers"] = dict(request.headers)
            return httpx.Response(
                200,
                content=source_bytes,
                headers={"content-type": "image/png"},
            )
        if str(request.url).endswith("/v1/images/edits"):
            captured["edit_url"] = str(request.url)
            captured["edit_headers"] = dict(request.headers)
            captured["edit_body"] = request.content
            return httpx.Response(
                200,
                json={"created": 2, "data": [{"b64_json": edited_b64}]},
            )
        return httpx.Response(404)

    settings = _draw_settings(tmp_path,
        draw_api_url="http://127.0.0.1:18080/v1/images/generations",
        draw_api_key="sk-test",
        draw_api_model="gpt-image-2",
    )
    store = DrawStore(settings)
    store._client = httpx.AsyncClient(transport=httpx.MockTransport(responder))

    edited = await store.edit_reference_image(
        image_url="http://127.0.0.1:5080/images/hash-601/601.png",
        image_path=r"C:\Users\Example\AppData\Local\Programs\wx-bot-client\data\images\hash-601\601.png",
        prompt="把这张图变成梵高星空风格的油画",
        trace_id="trace-edit-ref",
        source_label="quote:quoted-image",
    )

    assert captured["source_get_url"] == "http://127.0.0.1:5080/images/hash-601/601.png"
    assert captured["source_get_headers"]["accept"] == "image/*,*/*"
    assert captured["edit_url"] == "http://127.0.0.1:18080/v1/images/edits"
    assert "multipart/form-data" in captured["edit_headers"]["content-type"]
    assert b'name="prompt"' in captured["edit_body"]
    assert "把这张图变成梵高星空风格的油画".encode() in captured["edit_body"]
    assert b'name="image"; filename="601.png"' in captured["edit_body"]
    assert source_bytes in captured["edit_body"]
    assert edited.source_image_id == "quote:quoted-image"
    assert tmp_path.joinpath(edited.file_name).read_bytes() == edited_bytes
    assert store.resolve_image_id(edited.image_id).source_image_id == "quote:quoted-image"

    await store.close()


@pytest.mark.asyncio
async def test_draw_store_waits_for_reference_preview_before_thumbnail_fallback(tmp_path) -> None:
    captured: dict[str, object] = {"source_get_urls": []}
    source_bytes = b"\x89PNG\r\n\x1a\npreview-png"
    edited_bytes = b"\x89PNG\r\n\x1a\nedited-preview-png"
    edited_b64 = base64.b64encode(edited_bytes).decode("ascii")

    def responder(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            captured["source_get_urls"].append(str(request.url))
            if str(request.url) == "http://127.0.0.1:5080/images/hash-601/601_preview.jpg":
                return httpx.Response(
                    200,
                    content=source_bytes,
                    headers={"content-type": "image/jpeg"},
                )
            return httpx.Response(404)
        if str(request.url).endswith("/v1/images/edits"):
            captured["edit_body"] = request.content
            return httpx.Response(
                200,
                json={"created": 2, "data": [{"b64_json": edited_b64}]},
            )
        return httpx.Response(404)

    settings = _draw_settings(tmp_path,
        draw_api_url="http://127.0.0.1:18080/v1/images/generations",
        draw_api_key="sk-test",
        draw_api_model="gpt-image-2",
        wxbot_preview_wait_seconds=0.01,
        wxbot_preview_poll_interval_seconds=0.01,
    )
    store = DrawStore(settings)
    store._client = httpx.AsyncClient(transport=httpx.MockTransport(responder))

    await store.edit_reference_image(
        image_url="http://127.0.0.1:5080/images/hash-601/601_thumbnail.jpg",
        prompt="改成水彩",
        trace_id="trace-edit-ref",
        source_label="quote:quoted-image",
    )

    assert captured["source_get_urls"] == [
        "http://127.0.0.1:5080/images/hash-601/601_preview.jpg"
    ]
    assert b'name="image"; filename="601_preview.jpg"' in captured["edit_body"]
    assert source_bytes in captured["edit_body"]

    await store.close()
