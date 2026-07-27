"""
Tests for the MessageBus.

Strategy:
- Protocol-level tests run against an InMemoryBus and assert the expected
  semantics (publish, ack, DLQ, retry).
- A real Redis test is provided but skipped unless REDIS_URL is set and
  reachable.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from app.bus.base import BusMessage
from tests.unit._fakes import InMemoryBus


class _ExistingRedisGroup:
    def __init__(self) -> None:
        self.created = False

    async def xinfo_groups(self, stream: str):
        assert stream == "test:stream"
        return [{"name": "existing-group"}]

    async def xgroup_create(self, *args, **kwargs):
        _ = args, kwargs
        self.created = True
        raise AssertionError("xgroup_create should not be called when group exists")


@pytest.mark.asyncio
async def test_inmemory_publish_and_consume() -> None:
    bus = InMemoryBus()
    stream = "test:events"

    mid = await bus.publish(stream, {"hello": "world"}, partition_key="sess-1")
    assert mid
    assert len(bus.streams[stream]) == 1
    msg = bus.streams[stream][0]
    assert msg.payload == {"hello": "world"}
    assert msg.headers.get("partition_key") == "sess-1"

    received: list[BusMessage] = []

    async def handler(m: BusMessage) -> None:
        received.append(m)

    async for _ in bus.consume(stream, "g1", "c1", handler):
        pass

    assert len(received) == 1
    assert received[0].payload == {"hello": "world"}
    assert mid in bus.acked[stream] or received[0].id in bus.acked[stream]


@pytest.mark.asyncio
async def test_inmemory_handler_failure_retries_then_dlq() -> None:
    bus = InMemoryBus()
    stream = "test:flaky"
    await bus.publish(stream, {"n": 1})

    attempts = {"n": 0}

    async def handler(m: BusMessage) -> None:
        attempts["n"] += 1
        raise RuntimeError("boom")

    # Drain repeatedly until DLQ'd. Safety bound at 10 loops.
    for _ in range(10):
        async for _ in bus.consume(stream, "g", "c", handler):
            pass
        if bus.dlq:
            break

    assert bus.dlq, "message should have been DLQ'd after max attempts"
    _, reason = bus.dlq[0]
    assert "max_attempts" in reason
    # 5 attempts total before DLQ
    assert attempts["n"] == 5


@pytest.mark.asyncio
async def test_inmemory_ack_and_dlq_direct() -> None:
    bus = InMemoryBus()
    msg = BusMessage(id="1-0", stream="s", payload={"k": "v"}, headers={}, attempts=0)
    await bus.move_to_dlq(msg, reason="explicit")
    assert bus.dlq[0][1] == "explicit"

    await bus.ack("s", "g", "1-0")
    assert "1-0" in bus.acked["s"]


@pytest.mark.asyncio
async def test_redis_ensure_group_skips_create_when_group_exists() -> None:
    from app.bus.redis_streams import RedisStreamBus

    redis = _ExistingRedisGroup()
    bus = RedisStreamBus(redis)  # type: ignore[arg-type]

    await bus.ensure_group("test:stream", "existing-group")

    assert redis.created is False


# --- Real redis test (skipped by default) ------------------------------------


@pytest.mark.asyncio
async def test_redis_streams_roundtrip_if_available() -> None:
    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.skip("REDIS_URL not set; skipping real-redis test")
    try:
        from redis.asyncio import from_url
    except Exception:  # pragma: no cover
        pytest.skip("redis.asyncio not importable")
        return

    r = from_url(url, decode_responses=True)
    try:
        await r.ping()
    except Exception:
        await r.aclose()
        pytest.skip("redis not reachable")
        return

    from app.bus.redis_streams import RedisStreamBus

    stream = f"cs:test:{os.getpid()}"
    group = "tg"
    bus = RedisStreamBus(r)
    try:
        await bus.ensure_group(stream, group)
        mid = await bus.publish(stream, {"x": 1}, partition_key="p")
        assert mid

        got: list[dict[str, Any]] = []

        async def handler(m: BusMessage) -> None:
            got.append(m.payload)

        async def run() -> None:
            async for _ in bus.consume(stream, group, "c1", handler, block_ms=200):
                if got:
                    await bus.stop()
                    break

        await asyncio.wait_for(run(), timeout=5)
        assert got and got[0] == {"x": 1}
    finally:
        try:
            await r.delete(stream)
        finally:
            await r.aclose()
