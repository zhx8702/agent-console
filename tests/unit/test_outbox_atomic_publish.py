from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bus.redis_streams import _PUBLISH_ONCE_LUA, RedisStreamBus
from app.models.reliability import MessageOutboxRow
from app.reliability.message_store import MessageOutboxRelay, MessageReliabilityStore


class _AtomicRedisDouble:
    def __init__(self) -> None:
        self.records: dict[str, tuple[str, str]] = {}
        self.entries: list[tuple[str, str, str, str]] = []

    async def eval(self, script: str, numkeys: int, *args: Any) -> list[str]:
        assert script == _PUBLISH_ONCE_LUA
        assert numkeys == 2
        stream, record_key, fingerprint, data, headers = map(str, args)
        existing = self.records.get(record_key)
        if existing is not None:
            existing_fingerprint, message_id = existing
            if existing_fingerprint != fingerprint:
                return ["CONFLICT", message_id]
            return ["EXISTING", message_id]
        message_id = f"{len(self.entries) + 1}-0"
        self.entries.append((stream, data, headers, message_id))
        self.records[record_key] = (fingerprint, message_id)
        return ["PUBLISHED", message_id]


@pytest.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(MessageOutboxRow.__table__.create)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_publish_success_mark_failure_then_lease_replay_adds_one_stream_entry(
    factory,
) -> None:
    store = MessageReliabilityStore(factory)
    payload = {
        "reply_id": "reply-atomic-1",
        "tenant_id": "tenant-a",
        "session_id": "room-a",
        "trace_id": "trace-a",
        "segments": [{"type": "text", "content": "answer"}],
    }
    async with factory() as db:
        async with db.begin():
            await store.enqueue(
                db,
                stream="outbound",
                payload=payload,
                headers={"trace_id": "trace-a"},
                partition_key="tenant-a:room-a",
            )

    redis = _AtomicRedisDouble()
    bus = RedisStreamBus(
        redis,  # type: ignore[arg-type]
        SimpleNamespace(bus_dlq_stream="dlq"),  # type: ignore[arg-type]
        max_attempts=5,
        retry_base_seconds=0,
        retry_max_seconds=0,
        pending_idle_ms=1,
    )
    relay = MessageOutboxRelay(store, bus, worker_id="relay-a")
    mark_published = relay._mark_published
    mark_calls = 0

    async def fail_first_mark(*args: Any, **kwargs: Any) -> None:
        nonlocal mark_calls
        mark_calls += 1
        if mark_calls == 1:
            raise ConnectionError("database response lost after Redis publish")
        await mark_published(*args, **kwargs)

    relay._mark_published = fail_first_mark  # type: ignore[method-assign]

    assert await relay.drain_once() == 0
    assert len(redis.entries) == 1
    first_message_id = redis.entries[0][3]

    async with factory() as db:
        row = (await db.execute(select(MessageOutboxRow))).scalar_one()
        assert row.status == "publishing"
        row.lease_until = datetime(2000, 1, 1, tzinfo=UTC)
        row.available_at = datetime(2000, 1, 1, tzinfo=UTC)
        await db.commit()

    assert await relay.drain_once() == 1
    assert len(redis.entries) == 1
    async with factory() as db:
        row = (await db.execute(select(MessageOutboxRow))).scalar_one()
    assert row.status == "published"
    assert row.published_message_id == first_message_id
    assert row.attempts == 2


@pytest.mark.asyncio
async def test_outbox_relay_fails_closed_for_non_idempotent_transport(factory) -> None:
    class _UnsafeBus:
        def __init__(self) -> None:
            self.publish_calls = 0

        async def publish(self, *_args: Any, **_kwargs: Any) -> str:
            self.publish_calls += 1
            return "unsafe-1"

    store = MessageReliabilityStore(factory)
    async with factory() as db:
        async with db.begin():
            await store.enqueue(
                db,
                stream="outbound",
                payload={
                    "reply_id": "reply-unsafe-1",
                    "tenant_id": "tenant-a",
                    "session_id": "room-a",
                },
                headers=None,
                partition_key=None,
            )
    unsafe_bus = _UnsafeBus()
    relay = MessageOutboxRelay(
        store,
        unsafe_bus,  # type: ignore[arg-type]
        worker_id="relay-a",
    )

    assert await relay.drain_once() == 0
    assert unsafe_bus.publish_calls == 0
    async with factory() as db:
        row = (await db.execute(select(MessageOutboxRow))).scalar_one()
    assert row.status == "pending"
    assert row.last_error.startswith(
        "RuntimeError:outbox_transport_idempotency_required"
    )
