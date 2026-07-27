from __future__ import annotations

from collections import deque
from types import SimpleNamespace
from typing import Any

import orjson
import pytest
from redis.exceptions import ResponseError

from app.bus.base import MessagePublishIdempotencyConflict, PermanentMessageError
from app.bus.redis_streams import (
    _PROMOTE_DUE_RETRIES_LUA,
    _PUBLISH_ONCE_LUA,
    _TRANSFER_TO_DLQ_LUA,
    _TRANSFER_TO_RETRY_LUA,
    RedisStreamBus,
    _oldest_pending_age_seconds,
)


def _fields(*, attempts: int = 0) -> dict[str, str]:
    return {
        "data": orjson.dumps({"message": "hello"}).decode(),
        "headers": orjson.dumps({"trace_id": "trace-1"}).decode(),
        "attempts": str(attempts),
    }


class _RedisDouble:
    def __init__(self) -> None:
        self.groups: list[dict[str, str]] = [{"name": "workers"}]
        self.group_creates: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.autoclaim_responses: deque[Any] = deque()
        self.read_responses: deque[Any] = deque()
        self.pending_response: list[Any] = []
        self.claim_response: list[Any] = []
        self.calls: list[str] = []
        self.eval_calls: list[tuple[str, int, tuple[Any, ...]]] = []
        self.xack_calls: list[tuple[str, str, str]] = []
        self.xreadgroup_calls = 0
        self.autoclaim_error: Exception | None = None
        self.ack_error: Exception | None = None
        self.transfer_error: Exception | None = None
        self.retry_backlog = 0
        self.publish_once_records: dict[str, tuple[str, str]] = {}
        self.stream_entries: list[tuple[str, str, str, str]] = []

    async def xinfo_groups(self, stream: str) -> list[dict[str, str]]:
        self.calls.append("xinfo_groups")
        _ = stream
        return self.groups

    async def xgroup_create(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append("xgroup_create")
        self.group_creates.append((args, kwargs))

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        self.calls.append("eval")
        self.eval_calls.append((script, numkeys, args))
        if script == _PROMOTE_DUE_RETRIES_LUA:
            return []
        if script == _PUBLISH_ONCE_LUA:
            stream, record_key, fingerprint, data, headers = args
            existing = self.publish_once_records.get(str(record_key))
            if existing is not None:
                existing_fingerprint, message_id = existing
                if existing_fingerprint != fingerprint:
                    return ["CONFLICT", message_id]
                return ["EXISTING", message_id]
            message_id = f"{len(self.stream_entries) + 1}-0"
            self.stream_entries.append(
                (str(stream), str(data), str(headers), message_id)
            )
            self.publish_once_records[str(record_key)] = (
                str(fingerprint),
                message_id,
            )
            return ["PUBLISHED", message_id]
        if self.transfer_error is not None:
            raise self.transfer_error
        if script == _TRANSFER_TO_RETRY_LUA:
            self.retry_backlog += 1
            return [1, 1]
        if script == _TRANSFER_TO_DLQ_LUA:
            return ["10-0", 1]
        raise AssertionError("unexpected script")

    async def zcard(self, key: str) -> int:
        self.calls.append("zcard")
        _ = key
        return self.retry_backlog

    async def xautoclaim(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append("xautoclaim")
        _ = args, kwargs
        if self.autoclaim_error is not None:
            raise self.autoclaim_error
        if self.autoclaim_responses:
            return self.autoclaim_responses.popleft()
        return ["0-0", [], []]

    async def xpending_range(self, *args: Any, **kwargs: Any) -> list[Any]:
        self.calls.append("xpending_range")
        _ = args, kwargs
        return self.pending_response

    async def xclaim(self, *args: Any, **kwargs: Any) -> list[Any]:
        self.calls.append("xclaim")
        _ = args, kwargs
        return self.claim_response

    async def xreadgroup(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append("xreadgroup")
        self.xreadgroup_calls += 1
        _ = args, kwargs
        if self.read_responses:
            return self.read_responses.popleft()
        return []

    async def xack(self, stream: str, group: str, message_id: str) -> int:
        self.calls.append("xack")
        self.xack_calls.append((stream, group, message_id))
        if self.ack_error is not None:
            raise self.ack_error
        return 1


def _bus(
    redis: _RedisDouble,
    *,
    max_attempts: int = 5,
) -> RedisStreamBus:
    settings = SimpleNamespace(bus_dlq_stream="test:dlq")
    return RedisStreamBus(
        redis,  # type: ignore[arg-type]
        settings,  # type: ignore[arg-type]
        max_attempts=max_attempts,
        retry_base_seconds=0,
        retry_max_seconds=0,
        pending_idle_ms=1,
        clock_ms=lambda: 1000,
    )


@pytest.mark.asyncio
async def test_consumer_group_starts_at_zero_to_include_existing_messages() -> None:
    redis = _RedisDouble()
    redis.groups = []
    bus = _bus(redis)

    await bus.ensure_group("events", "workers")

    assert redis.group_creates == [
        (("events", "workers"), {"id": "0-0", "mkstream": True})
    ]


@pytest.mark.asyncio
async def test_stale_pending_is_reclaimed_before_reading_new_messages() -> None:
    redis = _RedisDouble()
    redis.autoclaim_responses.append(
        ["0-0", [("1-0", _fields())], []]
    )
    bus = _bus(redis)
    seen: list[str] = []

    async def handler(message: Any) -> None:
        seen.append(message.id)
        await bus.stop()

    async for _ in bus.consume("events", "workers", "consumer-b", handler):
        break

    assert seen == ["1-0"]
    assert redis.xreadgroup_calls == 0
    assert redis.xack_calls == [("events", "workers", "1-0")]
    assert redis.calls.index("xautoclaim") < redis.calls.index("xack")


@pytest.mark.asyncio
async def test_xautoclaim_falls_back_to_xpending_and_xclaim() -> None:
    redis = _RedisDouble()
    redis.autoclaim_error = ResponseError("ERR unknown command 'XAUTOCLAIM'")
    redis.pending_response = [{"message_id": "2-0"}]
    redis.claim_response = [("2-0", _fields())]
    bus = _bus(redis)

    async def handler(_message: Any) -> None:
        await bus.stop()

    async for _ in bus.consume("events", "workers", "consumer-b", handler):
        break

    assert redis.xack_calls == [("events", "workers", "2-0")]
    assert redis.calls.index("xautoclaim") < redis.calls.index("xpending_range")
    assert redis.calls.index("xpending_range") < redis.calls.index("xclaim")


@pytest.mark.asyncio
async def test_handler_failure_atomically_persists_retry_before_ack() -> None:
    redis = _RedisDouble()
    redis.read_responses.append(
        [("events", [("3-0", _fields())])]
    )
    bus = _bus(redis)

    async def handler(_message: Any) -> None:
        await bus.stop()
        raise RuntimeError("boom")

    async for _ in bus.consume("events", "workers", "consumer-a", handler):
        break

    transfer_calls = [
        call for call in redis.eval_calls if call[0] == _TRANSFER_TO_RETRY_LUA
    ]
    assert len(transfer_calls) == 1
    _, numkeys, args = transfer_calls[0]
    assert numkeys == 2
    assert args[:5] == (
        "events",
        "events:retry",
        "workers",
        "3-0",
        "consumer-a",
    )
    retry_item = orjson.loads(args[-1])
    assert retry_item["attempts"] == "1"
    assert retry_item["origin_id"] == "3-0"
    # The normal XACK method must never run on a failed handler.
    assert redis.xack_calls == []
    assert _TRANSFER_TO_RETRY_LUA.index("'ZADD'") < _TRANSFER_TO_RETRY_LUA.index(
        "'XACK'"
    )


@pytest.mark.asyncio
async def test_final_failure_atomically_persists_dlq_before_ack() -> None:
    redis = _RedisDouble()
    redis.read_responses.append(
        [("events", [("4-0", _fields(attempts=4))])]
    )
    bus = _bus(redis, max_attempts=5)

    async def handler(_message: Any) -> None:
        await bus.stop()
        raise ValueError("invalid")

    async for _ in bus.consume("events", "workers", "consumer-a", handler):
        break

    transfer_calls = [
        call for call in redis.eval_calls if call[0] == _TRANSFER_TO_DLQ_LUA
    ]
    assert len(transfer_calls) == 1
    _, numkeys, args = transfer_calls[0]
    assert numkeys == 2
    assert args[:5] == (
        "events",
        "test:dlq",
        "workers",
        "4-0",
        "consumer-a",
    )
    assert args[-2:] == (5, "max_attempts:ValueError")
    assert redis.xack_calls == []
    assert _TRANSFER_TO_DLQ_LUA.index("'XADD'") < _TRANSFER_TO_DLQ_LUA.index(
        "'XACK'"
    )


@pytest.mark.asyncio
async def test_permanent_disposition_immediately_uses_atomic_dlq_and_ack() -> None:
    redis = _RedisDouble()
    redis.read_responses.append(
        [("events", [("permanent-1", _fields())])]
    )
    bus = _bus(redis, max_attempts=99)

    async def handler(_message: Any) -> None:
        await bus.stop()
        raise PermanentMessageError("permanent:invalid_domain_event")

    async for _ in bus.consume("events", "workers", "consumer-a", handler):
        break

    transfer_calls = [
        call for call in redis.eval_calls if call[0] == _TRANSFER_TO_DLQ_LUA
    ]
    assert len(transfer_calls) == 1
    assert transfer_calls[0][2][-2:] == (
        1,
        "permanent:invalid_domain_event",
    )
    assert redis.xack_calls == []


@pytest.mark.asyncio
async def test_transfer_failure_never_falls_back_to_unsafe_ack() -> None:
    redis = _RedisDouble()
    redis.transfer_error = ConnectionError("redis disconnected")
    redis.read_responses.append(
        [("events", [("5-0", _fields())])]
    )
    bus = _bus(redis)

    async def handler(_message: Any) -> None:
        await bus.stop()
        raise RuntimeError("boom")

    async for _ in bus.consume("events", "workers", "consumer-a", handler):
        break

    assert redis.xack_calls == []
    assert any(call[0] == _TRANSFER_TO_RETRY_LUA for call in redis.eval_calls)


@pytest.mark.asyncio
async def test_success_ack_failure_leaves_message_for_future_reclaim() -> None:
    redis = _RedisDouble()
    redis.ack_error = ConnectionError("redis disconnected")
    redis.read_responses.append(
        [("events", [("6-0", _fields())])]
    )
    bus = _bus(redis)

    async def handler(_message: Any) -> None:
        await bus.stop()

    async for _ in bus.consume("events", "workers", "consumer-a", handler):
        break

    assert redis.xack_calls == [("events", "workers", "6-0")]
    # No retry/DLQ transfer may occur for a successfully handled message whose
    # ACK response failed; the still-pending source entry is the durable copy.
    assert all(
        script == _PROMOTE_DUE_RETRIES_LUA
        for script, _numkeys, _args in redis.eval_calls
    )


def test_retry_promotion_is_atomic_add_then_remove() -> None:
    assert _PROMOTE_DUE_RETRIES_LUA.index("'XADD'") < _PROMOTE_DUE_RETRIES_LUA.index(
        "'ZREM'"
    )


@pytest.mark.asyncio
async def test_publish_once_reuses_message_id_without_second_stream_entry() -> None:
    redis = _RedisDouble()
    bus = _bus(redis)

    first = await bus.publish_once(
        "outbound",
        {"tenant_id": "tenant-a", "reply_id": "reply-1", "value": 1},
        idempotency_key="tenant-a:reply-1",
        headers={"trace_id": "trace-1"},
        partition_key="tenant-a:session-a",
    )
    replay = await bus.publish_once(
        "outbound",
        {"value": 1, "reply_id": "reply-1", "tenant_id": "tenant-a"},
        idempotency_key="tenant-a:reply-1",
        headers={"trace_id": "trace-1"},
        partition_key="tenant-a:session-a",
    )

    assert first == replay == "1-0"
    assert len(redis.stream_entries) == 1
    assert [call[0] for call in redis.eval_calls].count(_PUBLISH_ONCE_LUA) == 2


@pytest.mark.asyncio
async def test_publish_once_rejects_same_key_with_different_envelope() -> None:
    redis = _RedisDouble()
    bus = _bus(redis)
    await bus.publish_once(
        "outbound",
        {"tenant_id": "tenant-a", "reply_id": "reply-1", "value": 1},
        idempotency_key="tenant-a:reply-1",
    )

    with pytest.raises(
        MessagePublishIdempotencyConflict,
        match="message_publish_idempotency_conflict",
    ):
        await bus.publish_once(
            "outbound",
            {"tenant_id": "tenant-a", "reply_id": "reply-1", "value": 2},
            idempotency_key="tenant-a:reply-1",
        )

    assert len(redis.stream_entries) == 1


def test_publish_once_script_binds_identity_in_same_atomic_eval_as_xadd() -> None:
    assert "existing_fingerprint" in _PUBLISH_ONCE_LUA
    assert _PUBLISH_ONCE_LUA.index("'XADD'") < _PUBLISH_ONCE_LUA.index("'HSET'")


def test_oldest_pending_age_uses_stream_creation_time_and_clamps_clock_skew() -> None:
    assert (
        _oldest_pending_age_seconds(
            [{"message_id": "1500-2", "time_since_delivered": 25}],
            clock_ms=4000,
        )
        == 2.5
    )
    assert _oldest_pending_age_seconds([{"message_id": "5000-0"}], clock_ms=4000) == 0
    assert _oldest_pending_age_seconds([], clock_ms=4000) == 0
