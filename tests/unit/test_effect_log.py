"""Unit tests for the persistent flow effect log."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.orchestrator.effect_log import EffectLogSchemaError, PostgresEffectLog
from app.orchestrator.effects import (
    EFFECT_STATUS_COMPLETED,
    EFFECT_STATUS_DRY_RUN,
    EFFECT_STATUS_DUPLICATE,
    EFFECT_STATUS_FAILED,
    EFFECT_STATUS_PREPARED,
    EFFECT_STATUS_RECORDED,
    EFFECT_STATUS_RUNNING,
    EffectClaimLost,
    EffectClaimUnavailable,
)
from tests.unit._schema_fixtures import bootstrap_effect_log_schema


@pytest.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def effect_log(factory) -> PostgresEffectLog:
    await bootstrap_effect_log_schema(factory)
    store = PostgresEffectLog(factory)
    await store.ensure_schema()
    return store


@pytest.mark.asyncio
async def test_record_persists_effect_log_row(factory, effect_log: PostgresEffectLog) -> None:
    record = await effect_log.record(
        idempotency_key="effect:demo:session-1:trace-1:wxbot:send:0",
        tenant_id="tenant-1",
        session_id="session-1",
        trace_id="trace-1",
        owner="wxbot",
        type="send_message",
        payload={"text": "hello", "targets": ["room-1"]},
    )

    assert record.status == EFFECT_STATUS_RECORDED
    assert record.dry_run is False

    async with factory() as db:
        result = await db.execute(
            text(
                """
                SELECT idempotency_key, tenant_id, session_id, trace_id, owner,
                       type, status, dry_run, payload, created_at
                FROM flow_effect_log
                """
            )
        )
        row = result.mappings().one()

    assert row["idempotency_key"] == "effect:demo:session-1:trace-1:wxbot:send:0"
    assert row["tenant_id"] == "tenant-1"
    assert row["session_id"] == "session-1"
    assert row["trace_id"] == "trace-1"
    assert row["owner"] == "wxbot"
    assert row["type"] == "send_message"
    assert row["status"] == EFFECT_STATUS_COMPLETED
    assert row["dry_run"] == 0
    assert json.loads(row["payload"]) == {"targets": ["room-1"], "text": "hello"}
    assert row["created_at"]


@pytest.mark.asyncio
async def test_record_returns_duplicate_within_same_tenant(
    factory,
    effect_log: PostgresEffectLog,
) -> None:
    first = await effect_log.record(
        idempotency_key="effect:duplicate",
        tenant_id="tenant-1",
        session_id="session-1",
        trace_id="trace-1",
        owner="wxbot",
        type="send_message",
        payload={"text": "first"},
    )
    duplicate = await effect_log.record(
        idempotency_key="effect:duplicate",
        tenant_id="tenant-1",
        session_id="session-2",
        trace_id="trace-2",
        owner="other",
        type="other_effect",
        payload={"text": "second"},
    )

    assert first.status == EFFECT_STATUS_RECORDED
    assert duplicate.status == EFFECT_STATUS_DUPLICATE
    assert duplicate.owner == "wxbot"
    assert duplicate.type == "send_message"
    assert duplicate.payload == {"text": "first"}

    async with factory() as db:
        count = await db.scalar(text("SELECT count(*) FROM flow_effect_log"))
        result = await db.execute(
            text("SELECT tenant_id, session_id, trace_id, payload FROM flow_effect_log")
        )
        row = result.mappings().one()

    assert count == 1
    assert row["tenant_id"] == "tenant-1"
    assert row["session_id"] == "session-1"
    assert row["trace_id"] == "trace-1"
    assert json.loads(row["payload"]) == {"text": "first"}


@pytest.mark.asyncio
async def test_identical_explicit_key_is_independent_across_tenants(
    factory,
    effect_log: PostgresEffectLog,
) -> None:
    first = await effect_log.record(
        idempotency_key="shared-command-id",
        tenant_id="tenant-a",
        session_id="session-a",
        trace_id="trace-a",
        owner="wxbot",
        type="enqueue_channel_reply",
        payload={"command_id": "shared-command-id", "text": "tenant a"},
    )
    second = await effect_log.record(
        idempotency_key="shared-command-id",
        tenant_id="tenant-b",
        session_id="session-b",
        trace_id="trace-b",
        owner="wxbot",
        type="enqueue_channel_reply",
        payload={"command_id": "shared-command-id", "text": "tenant b"},
    )
    duplicate_a = await effect_log.record(
        idempotency_key="shared-command-id",
        tenant_id="tenant-a",
        session_id="session-a-2",
        trace_id="trace-a-2",
        owner="wxbot",
        type="enqueue_channel_reply",
        payload={"command_id": "shared-command-id", "text": "changed"},
    )

    assert first.status == EFFECT_STATUS_RECORDED
    assert second.status == EFFECT_STATUS_RECORDED
    assert duplicate_a.status == EFFECT_STATUS_DUPLICATE
    assert first.tenant_id == "tenant-a"
    assert second.tenant_id == "tenant-b"

    async with factory() as db:
        result = await db.execute(
            text(
                """
                SELECT tenant_id, payload
                FROM flow_effect_log
                WHERE idempotency_key = 'shared-command-id'
                ORDER BY tenant_id
                """
            )
        )
        rows = result.mappings().all()

    assert [row["tenant_id"] for row in rows] == ["tenant-a", "tenant-b"]
    assert [json.loads(row["payload"])["text"] for row in rows] == [
        "tenant a",
        "tenant b",
    ]


@pytest.mark.asyncio
async def test_record_defaults_dry_run_status(effect_log: PostgresEffectLog) -> None:
    record = await effect_log.record(
        idempotency_key="effect:dry-run",
        tenant_id="tenant-1",
        session_id="session-1",
        trace_id="trace-1",
        owner="wxbot",
        type="send_message",
        payload={"text": "shadow"},
        dry_run=True,
    )

    assert record.status == EFFECT_STATUS_DRY_RUN
    assert record.dry_run is True


@pytest.mark.asyncio
async def test_list_recent_filters_and_redacts_payload(
    effect_log: PostgresEffectLog,
) -> None:
    await effect_log.record(
        idempotency_key="effect:first",
        tenant_id="tenant-1",
        session_id="session-1",
        trace_id="trace-1",
        owner="memory",
        type="save_memory",
        payload={"user_text": "secret", "assistant_text": "hidden"},
    )
    await effect_log.record(
        idempotency_key="effect:second",
        tenant_id="tenant-1",
        session_id="session-2",
        trace_id="trace-2",
        owner="wxbot",
        type="enqueue_channel_reply",
        payload={"body": "hello"},
    )

    rows = await effect_log.list_recent(tenant_id="tenant-1", owner="memory")

    assert len(rows) == 1
    assert rows[0]["idempotency_key"] == "effect:first"
    assert rows[0]["owner"] == "memory"
    assert rows[0]["type"] == "save_memory"
    assert rows[0]["payload_keys"] == ["assistant_text", "user_text"]
    assert rows[0]["payload_size"] > 0
    assert "payload" not in rows[0]


@pytest.mark.asyncio
async def test_list_recent_can_include_payload(effect_log: PostgresEffectLog) -> None:
    await effect_log.record(
        idempotency_key="effect:payload",
        tenant_id="tenant-1",
        session_id="session-1",
        trace_id="trace-1",
        owner="core",
        type="publish_outbound",
        payload={"stream": "cs:outbound"},
    )

    rows = await effect_log.list_recent(include_payload=True)

    assert rows[0]["payload"] == {"stream": "cs:outbound"}


@pytest.mark.asyncio
async def test_summarize_groups_effect_log_rows(effect_log: PostgresEffectLog) -> None:
    await effect_log.record(
        idempotency_key="effect:summary:memory",
        tenant_id="tenant-1",
        session_id="session-1",
        trace_id="trace-1",
        owner="memory",
        type="save_memory",
        payload={"user_text": "secret"},
    )
    await effect_log.record(
        idempotency_key="effect:summary:wxbot",
        tenant_id="tenant-1",
        session_id="session-2",
        trace_id="trace-2",
        owner="wxbot",
        type="enqueue_channel_reply",
        payload={"body": "hello"},
        dry_run=True,
    )

    summary = await effect_log.summarize(tenant_id="tenant-1")

    assert summary["total"] == 2
    assert {"status": EFFECT_STATUS_RECORDED, "count": 1} in summary["by_status"]
    assert {"status": EFFECT_STATUS_DRY_RUN, "count": 1} in summary["by_status"]
    assert {"owner": "memory", "count": 1} in summary["by_owner"]
    assert {"type": "save_memory", "count": 1} in summary["by_type"]
    assert {"dry_run": False, "count": 1} in summary["by_dry_run"]
    assert {
        "owner": "wxbot",
        "type": "enqueue_channel_reply",
        "status": EFFECT_STATUS_DRY_RUN,
        "dry_run": True,
        "count": 1,
    } in summary["matrix"]


@pytest.mark.asyncio
async def test_default_ensure_schema_validates_without_runtime_ddl(factory, engine) -> None:
    store = PostgresEffectLog(factory)

    with pytest.raises(EffectLogSchemaError, match="alembic_version"):
        await store.ensure_schema()

    async with engine.connect() as connection:
        table_names = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
    assert "flow_effect_log" not in table_names


@pytest.mark.asyncio
async def test_ensure_schema_accepts_newer_alembic_head(factory) -> None:
    await bootstrap_effect_log_schema(factory, revision="0017_message_reliability")
    async with factory() as db:
        assert await db.scalar(text("SELECT count(*) FROM alembic_version")) == 1

    await PostgresEffectLog(factory).ensure_schema()


@pytest.mark.asyncio
async def test_prepare_and_completed_duplicate_are_distinct_states(
    effect_log: PostgresEffectLog,
) -> None:
    prepared = await effect_log.prepare(
        idempotency_key="effect:lifecycle:completed",
        tenant_id="tenant-1",
        session_id="session-1",
        trace_id="trace-1",
        owner="core",
        type="publish_outbound",
        payload={"text": "hello"},
    )
    claimed = await effect_log.claim(
        idempotency_key=prepared.idempotency_key,
        tenant_id="tenant-1",
        session_id="session-1",
        trace_id="trace-1",
        owner="core",
        type="publish_outbound",
        claim_owner="worker-a",
        lease_seconds=30,
        payload={"text": "hello"},
    )
    completed = await effect_log.complete(
        idempotency_key=claimed.idempotency_key,
        tenant_id=claimed.tenant_id,
        claim_owner=claimed.claim_owner,
        attempt=claimed.attempt,
    )
    duplicate = await effect_log.claim(
        idempotency_key=prepared.idempotency_key,
        tenant_id="tenant-1",
        session_id="session-1",
        trace_id="trace-2",
        owner="core",
        type="publish_outbound",
        claim_owner="worker-b",
        lease_seconds=30,
        payload={"text": "changed"},
    )

    assert prepared.status == EFFECT_STATUS_PREPARED
    assert claimed.status == EFFECT_STATUS_RUNNING
    assert claimed.attempt == 1
    assert completed.status == EFFECT_STATUS_COMPLETED
    assert duplicate.status == EFFECT_STATUS_DUPLICATE
    assert duplicate.payload == {"text": "hello"}


@pytest.mark.asyncio
async def test_failed_effect_is_retryable_and_not_duplicate(
    effect_log: PostgresEffectLog,
) -> None:
    first = await effect_log.claim(
        idempotency_key="effect:lifecycle:failed",
        tenant_id="tenant-1",
        session_id="session-1",
        trace_id="trace-1",
        owner="memory",
        type="save_memory",
        claim_owner="worker-a",
        lease_seconds=30,
    )
    failed = await effect_log.fail(
        idempotency_key=first.idempotency_key,
        tenant_id=first.tenant_id,
        claim_owner=first.claim_owner,
        attempt=first.attempt,
        error="temporary backend outage",
    )
    retry = await effect_log.claim(
        idempotency_key=first.idempotency_key,
        tenant_id="tenant-1",
        session_id="session-1",
        trace_id="trace-2",
        owner="memory",
        type="save_memory",
        claim_owner="worker-b",
        lease_seconds=30,
    )

    assert failed.status == EFFECT_STATUS_FAILED
    assert retry.status == EFFECT_STATUS_RUNNING
    assert retry.attempt == 2
    assert retry.status != EFFECT_STATUS_DUPLICATE


@pytest.mark.asyncio
async def test_expired_running_claim_is_recovered_with_attempt_fencing(
    effect_log: PostgresEffectLog,
) -> None:
    started = datetime(2030, 1, 1, tzinfo=UTC)
    first = await effect_log.claim(
        idempotency_key="effect:lifecycle:crash",
        tenant_id="tenant-1",
        session_id="session-1",
        trace_id="trace-1",
        owner="wxbot",
        type="enqueue_channel_reply",
        claim_owner="crashed-worker",
        lease_seconds=10,
        now=started,
    )

    with pytest.raises(EffectClaimUnavailable):
        await effect_log.claim(
            idempotency_key=first.idempotency_key,
            tenant_id="tenant-1",
            session_id="session-1",
            trace_id="trace-2",
            owner="wxbot",
            type="enqueue_channel_reply",
            claim_owner="early-worker",
            lease_seconds=10,
            now=started + timedelta(seconds=9),
        )

    recovered = await effect_log.claim(
        idempotency_key=first.idempotency_key,
        tenant_id="tenant-1",
        session_id="session-1",
        trace_id="trace-3",
        owner="wxbot",
        type="enqueue_channel_reply",
        claim_owner="recovery-worker",
        lease_seconds=10,
        now=started + timedelta(seconds=11),
    )

    assert recovered.status == EFFECT_STATUS_RUNNING
    assert recovered.claim_owner == "recovery-worker"
    assert recovered.attempt == 2
    with pytest.raises(EffectClaimLost):
        await effect_log.complete(
            idempotency_key=first.idempotency_key,
            tenant_id=first.tenant_id,
            claim_owner=first.claim_owner,
            attempt=first.attempt,
            now=started + timedelta(seconds=12),
        )


@pytest.mark.asyncio
async def test_dry_run_completes_without_consuming_real_execution_key(
    effect_log: PostgresEffectLog,
) -> None:
    kwargs = {
        "idempotency_key": "effect:lifecycle:dry-run",
        "tenant_id": "tenant-1",
        "session_id": "session-1",
        "trace_id": "trace-1",
        "owner": "core",
        "type": "publish_outbound",
        "claim_owner": "worker-a",
        "lease_seconds": 30,
    }
    dry_run = await effect_log.claim(**kwargs, dry_run=True)
    real = await effect_log.claim(**kwargs, dry_run=False)

    assert dry_run.status == EFFECT_STATUS_DRY_RUN
    assert real.status == EFFECT_STATUS_RUNNING
    rows = await effect_log.list_recent()
    assert {row["lifecycle_status"] for row in rows} == {
        EFFECT_STATUS_COMPLETED,
        EFFECT_STATUS_RUNNING,
    }
    assert {row["status"] for row in rows} == {
        EFFECT_STATUS_DRY_RUN,
        EFFECT_STATUS_RUNNING,
    }
