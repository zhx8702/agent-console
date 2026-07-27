"""Integration test: Redis Streams bus end-to-end."""
from __future__ import annotations

import asyncio

import pytest

from tests.integration.conftest import requires_redis

pytestmark = [pytest.mark.integration, requires_redis]


@pytest.mark.asyncio
async def test_publish_consume_ack(redis_client):
    from app.bus.base import BusMessage
    from app.bus.redis_streams import RedisStreamBus
    from app.common.config import get_settings

    bus = RedisStreamBus(redis_client, get_settings())
    stream = "test:stream:ok"
    group = "test-group"
    await bus.ensure_group(stream, group)

    received: list[BusMessage] = []

    async def handler(msg: BusMessage) -> None:
        received.append(msg)

    await bus.publish(stream, {"n": 1}, headers={"h": "v"}, partition_key="p1")
    await bus.publish(stream, {"n": 2}, partition_key="p2")

    async def _consume():
        async for _ in bus.consume(stream, group, "c1", handler, batch_size=4, block_ms=500):
            if len(received) >= 2:
                return

    await asyncio.wait_for(_consume(), timeout=3.0)
    assert len(received) == 2
    assert received[0].payload["n"] == 1
    assert received[1].payload["n"] == 2


@pytest.mark.asyncio
async def test_message_published_before_group_creation_is_not_skipped(redis_client):
    from app.bus.base import BusMessage
    from app.bus.redis_streams import RedisStreamBus
    from app.common.config import get_settings

    bus = RedisStreamBus(redis_client, get_settings())
    stream = "test:stream:preexisting"
    group = "late-workers"
    await bus.publish(stream, {"n": 1})

    received: list[BusMessage] = []

    async def handler(msg: BusMessage) -> None:
        received.append(msg)
        await bus.stop()

    async for _ in bus.consume(stream, group, "c1", handler, block_ms=100):
        break

    assert [item.payload for item in received] == [{"n": 1}]


@pytest.mark.asyncio
async def test_crashed_consumer_pending_entry_is_reclaimed(redis_client):
    from app.bus.base import BusMessage
    from app.bus.redis_streams import RedisStreamBus
    from app.common.config import get_settings

    settings = get_settings()
    stream = "test:stream:reclaim"
    group = "reclaim-workers"
    first_bus = RedisStreamBus(redis_client, settings)
    await first_bus.ensure_group(stream, group)
    message_id = await first_bus.publish(stream, {"n": 7})

    # Simulate a process crash after XREADGROUP but before handler/ACK.
    claimed = await redis_client.xreadgroup(
        groupname=group,
        consumername="crashed-worker",
        streams={stream: ">"},
        count=1,
    )
    assert claimed
    assert claimed[0][1][0][0] == message_id

    await asyncio.sleep(0.01)
    recovery_bus = RedisStreamBus(redis_client, settings, pending_idle_ms=1)
    received: list[BusMessage] = []

    async def handler(msg: BusMessage) -> None:
        received.append(msg)
        await recovery_bus.stop()

    async for _ in recovery_bus.consume(
        stream,
        group,
        "recovery-worker",
        handler,
        block_ms=100,
    ):
        break

    assert [item.id for item in received] == [message_id]
    pending = await redis_client.xpending(stream, group)
    assert pending["pending"] == 0


@pytest.mark.asyncio
async def test_failed_message_retry_is_durable_before_source_ack(redis_client):
    from app.bus.redis_streams import RedisStreamBus, _decode
    from app.common.config import get_settings

    settings = get_settings()
    stream = "test:stream:atomic-retry"
    group = "atomic-workers"
    bus = RedisStreamBus(
        redis_client,
        settings,
        retry_base_seconds=1,
        retry_max_seconds=30,
        clock_ms=lambda: 1_000,
    )
    await bus.ensure_group(stream, group)
    await bus.publish(stream, {"n": 9})
    response = await redis_client.xreadgroup(
        groupname=group,
        consumername="c1",
        streams={stream: ">"},
        count=1,
    )
    raw_id, raw_fields = response[0][1][0]
    message = _decode(stream, raw_id, raw_fields)

    await bus._handle_failure(
        stream,
        group,
        "c1",
        message,
        RuntimeError("boom"),
    )

    pending = await redis_client.xpending(stream, group)
    assert pending["pending"] == 0
    assert await redis_client.zcard(f"{stream}:retry") == 1

    # A fresh process can promote the durable retry after its deadline.
    restarted_bus = RedisStreamBus(
        redis_client,
        settings,
        clock_ms=lambda: 3_001,
    )
    assert await restarted_bus._promote_due_retries(stream, batch_size=10) == 1
    assert await redis_client.zcard(f"{stream}:retry") == 0
    assert await redis_client.xlen(stream) == 2


@pytest.mark.asyncio
async def test_publish_once_is_atomic_under_concurrent_retries(redis_client):
    from app.bus.base import MessagePublishIdempotencyConflict
    from app.bus.redis_streams import RedisStreamBus
    from app.common.config import get_settings

    bus = RedisStreamBus(redis_client, get_settings())
    stream = "test:stream:publish-once"
    results = await asyncio.gather(
        *[
            bus.publish_once(
                stream,
                {"tenant_id": "tenant-a", "reply_id": "reply-1", "n": 1},
                idempotency_key="tenant-a:reply-1",
                headers={"trace_id": "trace-1"},
                partition_key="tenant-a:room-a",
            )
            for _ in range(12)
        ]
    )

    assert len(set(results)) == 1
    assert await redis_client.xlen(stream) == 1
    with pytest.raises(MessagePublishIdempotencyConflict):
        await bus.publish_once(
            stream,
            {"tenant_id": "tenant-a", "reply_id": "reply-1", "n": 2},
            idempotency_key="tenant-a:reply-1",
            headers={"trace_id": "trace-1"},
            partition_key="tenant-a:room-a",
        )
    assert await redis_client.xlen(stream) == 1


@pytest.mark.asyncio
async def test_retry_and_dlq_on_persistent_failure(redis_client):
    from app.bus.base import BusMessage
    from app.bus.redis_streams import RedisStreamBus
    from app.common.config import get_settings

    bus = RedisStreamBus(
        redis_client,
        get_settings(),
        retry_base_seconds=0.01,
        retry_max_seconds=0.01,
        pending_idle_ms=10,
    )
    stream = "test:stream:dlq"
    dlq = get_settings().bus_dlq_stream
    group = "test-group"
    await bus.ensure_group(stream, group)

    attempts = {"n": 0}

    async def always_fail(_msg: BusMessage) -> None:
        attempts["n"] += 1
        raise RuntimeError("boom")

    await bus.publish(stream, {"n": 42}, partition_key="p")

    async def _consume():
        async for _ in bus.consume(
            stream, group, "c1", always_fail, batch_size=4, block_ms=200
        ):
            # Max 5 attempts, then DLQ. Exit after we've seen at least 5 retries and DLQ has data.
            dlq_len = await redis_client.xlen(dlq)
            if dlq_len > 0:
                return

    await asyncio.wait_for(_consume(), timeout=5.0)
    assert attempts["n"] >= 5
    dlq_len = await redis_client.xlen(dlq)
    assert dlq_len >= 1
