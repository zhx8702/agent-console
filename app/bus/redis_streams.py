"""
Reliable Redis Streams implementation of the MessageBus Protocol.

Delivery guarantees:
- Consumer groups start at ``0-0`` so messages published before the first
  worker starts are still delivered.
- Stale pending messages are reclaimed before new messages are read.
- Handler failures are atomically persisted to a retry sorted set or the DLQ
  in the same Redis script that acknowledges the original stream entry.
- Retry backoff is durable and non-blocking. Due retries are atomically
  promoted back to the source stream by consumers.

The implementation remains at-least-once: a worker can finish an external
side effect and crash before its success ACK. Handlers therefore still need
their own idempotency key/inbox boundary.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any, cast

import orjson
from prometheus_client import Counter, Gauge
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.bus.base import (
    BusMessage,
    MessagePublishIdempotencyConflict,
    PermanentMessageError,
)
from app.common.config import Settings, get_settings
from app.common.logging import get_logger

logger = get_logger(__name__)

BUS_DELIVERIES = Counter(
    "cs_bus_deliveries_total",
    "Redis Stream messages handed to a handler",
    ["stream", "source"],
)
BUS_ACKS = Counter(
    "cs_bus_acks_total",
    "Redis Stream acknowledgement attempts",
    ["stream", "result"],
)
BUS_RECLAIMED = Counter(
    "cs_bus_reclaimed_total",
    "Stale Redis Stream pending messages reclaimed",
    ["stream", "group"],
)
BUS_PENDING_DELETED = Counter(
    "cs_bus_pending_deleted_total",
    "PEL entries removed by Redis because the source entry was deleted",
    ["stream", "group"],
)
BUS_FAILURE_TRANSFERS = Counter(
    "cs_bus_failure_transfers_total",
    "Handler failures transferred before acknowledgement",
    ["stream", "destination", "result"],
)
BUS_RETRY_BACKLOG = Gauge(
    "cs_bus_retry_backlog",
    "Messages durably waiting for their Redis Stream retry deadline",
    ["stream"],
)
BUS_PENDING_OLDEST_AGE = Gauge(
    "cs_bus_pending_oldest_age_seconds",
    "Age of the oldest message currently present in a Redis Stream PEL",
    ["stream", "group"],
)


# The scripts first verify that this consumer still owns the pending entry.
# They then persist the replacement and ACK while Redis is executing the
# script atomically. A client disconnect can make the result uncertain, but
# cannot expose an ACK-without-replacement state.
_TRANSFER_TO_RETRY_LUA = """
local pending = redis.call(
    'XPENDING', KEYS[1], ARGV[1], ARGV[2], ARGV[2], 1
)
if #pending == 0 then
    return {0, 0}
end
if pending[1][2] ~= ARGV[3] then
    return {-1, 0}
end
local added = redis.call('ZADD', KEYS[2], ARGV[4], ARGV[5])
local acked = redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
return {added, acked}
"""

_TRANSFER_TO_DLQ_LUA = """
local pending = redis.call(
    'XPENDING', KEYS[1], ARGV[1], ARGV[2], ARGV[2], 1
)
if #pending == 0 then
    return {'MISSING', 0}
end
if pending[1][2] ~= ARGV[3] then
    return {'STALE', 0}
end
local dlq_id = redis.call(
    'XADD', KEYS[2], '*',
    'data', ARGV[4],
    'headers', ARGV[5],
    'attempts', ARGV[6],
    'origin_stream', KEYS[1],
    'origin_id', ARGV[2],
    'reason', ARGV[7]
)
local acked = redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
return {dlq_id, acked}
"""

_PROMOTE_DUE_RETRIES_LUA = """
local members = redis.call(
    'ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, ARGV[2]
)
local promoted = {}
for _, member in ipairs(members) do
    local item = cjson.decode(member)
    local message_id = redis.call(
        'XADD', KEYS[2], '*',
        'data', item['data'],
        'headers', item['headers'],
        'attempts', item['attempts'],
        'origin_id', item['origin_id'],
        'last_error', item['last_error']
    )
    redis.call('ZREM', KEYS[1], member)
    table.insert(promoted, message_id)
end
return promoted
"""

# A database outbox row can be published successfully while the database
# status update is lost. Binding the outbox identity and canonical transport
# envelope in the same Redis script as XADD closes that duplicate window. The
# idempotency record deliberately stores only a digest and Redis message id;
# message content is not duplicated into audit/debug state.
_PUBLISH_ONCE_LUA = """
local existing_fingerprint = redis.call('HGET', KEYS[2], 'fingerprint')
if existing_fingerprint then
    local existing_message_id = redis.call('HGET', KEYS[2], 'message_id')
    if not existing_message_id then
        return {'CORRUPT'}
    end
    if existing_fingerprint ~= ARGV[1] then
        return {'CONFLICT', existing_message_id}
    end
    return {'EXISTING', existing_message_id}
end

local message_id = redis.call(
    'XADD', KEYS[1], '*',
    'data', ARGV[2],
    'headers', ARGV[3],
    'attempts', '0'
)
redis.call(
    'HSET', KEYS[2],
    'fingerprint', ARGV[1],
    'message_id', message_id
)
return {'PUBLISHED', message_id}
"""


class RedisStreamBus:
    """Redis Streams-backed MessageBus with durable retry and PEL recovery."""

    def __init__(
        self,
        redis: Redis,
        settings: Settings | None = None,
        *,
        max_attempts: int | None = None,
        retry_base_seconds: float | None = None,
        retry_max_seconds: float | None = None,
        pending_idle_ms: int | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._redis = redis
        self._settings = settings or get_settings()
        self._stopping = asyncio.Event()
        self._max_attempts = max(
            1,
            int(max_attempts if max_attempts is not None else self._settings.bus_max_attempts),
        )
        self._retry_base_seconds = max(
            0.0,
            float(
                retry_base_seconds
                if retry_base_seconds is not None
                else self._settings.bus_retry_base_seconds
            ),
        )
        self._retry_max_seconds = max(
            0.0,
            float(
                retry_max_seconds
                if retry_max_seconds is not None
                else self._settings.bus_retry_max_seconds
            ),
        )
        self._pending_idle_ms = max(
            1,
            int(
                pending_idle_ms
                if pending_idle_ms is not None
                else self._settings.bus_pending_claim_idle_ms
            ),
        )
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._reclaim_cursors: dict[tuple[str, str], str] = {}
        self._xautoclaim_supported: bool | None = None

    # -- publish --------------------------------------------------------------

    async def publish(
        self,
        stream: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        partition_key: str | None = None,
    ) -> str:
        hdrs = dict(headers or {})
        if partition_key is not None:
            hdrs.setdefault("partition_key", partition_key)

        fields: dict[Any, Any] = {
            "data": orjson.dumps(payload).decode("utf-8"),
            "headers": orjson.dumps(hdrs).decode("utf-8"),
            "attempts": "0",
        }
        msg_id = await self._redis.xadd(stream, fields)
        return _as_text(msg_id)

    async def publish_once(
        self,
        stream: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        headers: dict[str, str] | None = None,
        partition_key: str | None = None,
    ) -> str:
        """Atomically publish one canonical envelope for an idempotency key.

        Redis executes the lookup, conflict check, XADD, and identity binding
        as one operation. If the caller loses the response after XADD, a
        retry returns the original stream id without appending a second entry.
        """

        normalized_stream = str(stream or "").strip()
        normalized_key = str(idempotency_key or "").strip()
        if not normalized_stream:
            raise ValueError("message_stream_required")
        if not normalized_key:
            raise ValueError("message_publish_idempotency_key_required")

        hdrs = dict(headers or {})
        if partition_key is not None:
            hdrs.setdefault("partition_key", partition_key)
        data_json = orjson.dumps(payload, option=orjson.OPT_SORT_KEYS).decode("utf-8")
        headers_json = orjson.dumps(hdrs, option=orjson.OPT_SORT_KEYS).decode("utf-8")
        fingerprint = hashlib.sha256(
            orjson.dumps(
                {
                    "stream": normalized_stream,
                    "data": data_json,
                    "headers": headers_json,
                },
                option=orjson.OPT_SORT_KEYS,
            )
        ).hexdigest()
        record_key = _publish_once_record_key(normalized_stream, normalized_key)
        result = await self._redis.eval(
            _PUBLISH_ONCE_LUA,
            2,
            normalized_stream,
            record_key,
            fingerprint,
            data_json,
            headers_json,
        )
        if not isinstance(result, (list, tuple)) or len(result) < 1:
            raise RuntimeError("message_publish_idempotency_invalid_response")
        status = _as_text(result[0])
        if status == "CONFLICT":
            raise MessagePublishIdempotencyConflict(
                "message_publish_idempotency_conflict"
            )
        if status == "CORRUPT":
            raise RuntimeError("message_publish_idempotency_record_corrupt")
        if status not in {"PUBLISHED", "EXISTING"} or len(result) < 2:
            raise RuntimeError("message_publish_idempotency_invalid_response")
        message_id = _as_text(result[1]).strip()
        if not message_id:
            raise RuntimeError("message_publish_idempotency_missing_message_id")
        return message_id

    # -- groups ---------------------------------------------------------------

    async def ensure_group(self, stream: str, group: str) -> None:
        try:
            groups = await self._redis.xinfo_groups(stream)
        except ResponseError as exc:
            if "no such key" not in str(exc).lower():
                raise
        else:
            for item in groups:
                name = item.get("name") if isinstance(item, dict) else None
                if _as_text(name or "") == group:
                    return
        try:
            # Starting at 0-0 is required for messages published before the
            # first deployment/consumer-group creation.
            await self._redis.xgroup_create(stream, group, id="0-0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" in str(exc):
                return
            raise

    # -- consume --------------------------------------------------------------

    async def consume(
        self,
        stream: str,
        group: str,
        consumer: str,
        handler: Callable[[BusMessage], Awaitable[None]],
        *,
        batch_size: int = 16,
        block_ms: int = 5_000,
    ) -> AsyncIterator[None]:
        await self.ensure_group(stream, group)
        cursor_key = (stream, group)
        self._reclaim_cursors.setdefault(cursor_key, "0-0")

        while not self._stopping.is_set():
            try:
                await self._promote_due_retries(stream, batch_size=batch_size)
                next_cursor, reclaimed, deleted_ids = await self._claim_stale_pending(
                    stream,
                    group,
                    consumer,
                    start_id=self._reclaim_cursors[cursor_key],
                    count=batch_size,
                )
                self._reclaim_cursors[cursor_key] = next_cursor
                await self._refresh_pending_age(stream, group)
            except Exception:
                logger.exception(
                    "bus.pending_recovery.failed",
                    stream=stream,
                    group=group,
                    consumer=consumer,
                )
                await asyncio.sleep(0.5)
                yield None
                continue

            if deleted_ids:
                BUS_PENDING_DELETED.labels(stream=stream, group=group).inc(
                    len(deleted_ids)
                )
                logger.warning(
                    "bus.pending_entries_deleted",
                    stream=stream,
                    group=group,
                    count=len(deleted_ids),
                )

            if reclaimed:
                BUS_RECLAIMED.labels(stream=stream, group=group).inc(len(reclaimed))
                logger.info(
                    "bus.pending_reclaimed",
                    stream=stream,
                    group=group,
                    consumer=consumer,
                    reclaimed_count=len(reclaimed),
                    next_cursor=next_cursor,
                )
                await self._process_entries(
                    stream,
                    group,
                    consumer,
                    reclaimed,
                    handler,
                    source="reclaimed",
                )
                yield None
                continue

            try:
                resp = await self._redis.xreadgroup(
                    groupname=group,
                    consumername=consumer,
                    streams={stream: ">"},
                    count=batch_size,
                    block=block_ms,
                )
            except Exception:
                logger.exception(
                    "bus.xreadgroup.failed",
                    stream=stream,
                    group=group,
                    consumer=consumer,
                )
                await asyncio.sleep(0.5)
                yield None
                continue

            if not resp:
                yield None
                continue

            response_entries = cast(
                Sequence[
                    tuple[
                        Any,
                        Sequence[tuple[Any, dict[Any, Any]]],
                    ]
                ],
                resp,
            )
            for _stream_name, entries in response_entries:
                await self._process_entries(
                    stream,
                    group,
                    consumer,
                    entries,
                    handler,
                    source="new",
                )
            yield None

    async def _process_entries(
        self,
        stream: str,
        group: str,
        consumer: str,
        entries: Sequence[tuple[Any, dict[Any, Any]]],
        handler: Callable[[BusMessage], Awaitable[None]],
        *,
        source: str,
    ) -> None:
        for msg_id, fields in entries:
            message = _decode(stream, msg_id, fields)
            BUS_DELIVERIES.labels(stream=stream, source=source).inc()
            try:
                await handler(message)
            except Exception as exc:
                try:
                    await self._handle_failure(
                        stream,
                        group,
                        consumer,
                        message,
                        exc,
                    )
                except Exception:
                    # The atomic script either persisted+ACKed or left the
                    # source entry pending. Never issue a compensating ACK.
                    logger.exception(
                        "bus.failure_transfer.failed",
                        stream=stream,
                        group=group,
                        consumer=consumer,
                        message_id=message.id,
                    )
            else:
                try:
                    await self.ack(stream, group, message.id)
                except Exception:
                    # Leave the entry in the PEL for XAUTOCLAIM. This can
                    # redeliver after a successful side effect, which is why
                    # handlers must be idempotent.
                    logger.exception(
                        "bus.ack.failed",
                        stream=stream,
                        group=group,
                        consumer=consumer,
                        message_id=message.id,
                    )

    async def _handle_failure(
        self,
        stream: str,
        group: str,
        consumer: str,
        message: BusMessage,
        exc: BaseException,
    ) -> None:
        attempts = message.attempts + 1
        logger.warning(
            "bus.handler.failed",
            stream=stream,
            group=group,
            consumer=consumer,
            message_id=message.id,
            attempts=attempts,
            error_type=exc.__class__.__name__,
        )

        if isinstance(exc, PermanentMessageError):
            try:
                await self._move_to_dlq_and_ack(
                    stream,
                    group,
                    consumer,
                    message,
                    attempts=attempts,
                    reason=exc.reason,
                )
            except Exception:
                BUS_FAILURE_TRANSFERS.labels(
                    stream=stream,
                    destination="dlq",
                    result="error",
                ).inc()
                raise
            return

        if attempts >= self._max_attempts:
            try:
                await self._move_to_dlq_and_ack(
                    stream,
                    group,
                    consumer,
                    message,
                    attempts=attempts,
                    reason=f"max_attempts:{exc.__class__.__name__}",
                )
            except Exception:
                BUS_FAILURE_TRANSFERS.labels(
                    stream=stream,
                    destination="dlq",
                    result="error",
                ).inc()
                raise
            return

        exponent = max(0, attempts)
        delay_seconds = min(
            self._retry_max_seconds,
            self._retry_base_seconds * (2**exponent),
        )
        due_at_ms = self._clock_ms() + int(delay_seconds * 1000)
        retry_key = self._retry_key(stream)
        retry_item = orjson.dumps(
            {
                "attempts": str(attempts),
                "data": orjson.dumps(message.payload).decode("utf-8"),
                "headers": orjson.dumps(message.headers).decode("utf-8"),
                "last_error": exc.__class__.__name__,
                "origin_id": message.id,
            },
            option=orjson.OPT_SORT_KEYS,
        ).decode("utf-8")

        try:
            result = await self._redis.eval(
                _TRANSFER_TO_RETRY_LUA,
                2,
                stream,
                retry_key,
                group,
                message.id,
                consumer,
                due_at_ms,
                retry_item,
            )
            _assert_transfer_ack(result, destination="retry")
        except Exception:
            BUS_FAILURE_TRANSFERS.labels(
                stream=stream,
                destination="retry",
                result="error",
            ).inc()
            raise
        BUS_FAILURE_TRANSFERS.labels(
            stream=stream,
            destination="retry",
            result="ok",
        ).inc()
        await self._refresh_retry_backlog(stream)
        logger.info(
            "bus.retry_scheduled",
            stream=stream,
            group=group,
            message_id=message.id,
            attempts=attempts,
            retry_due_at_ms=due_at_ms,
            retry_delay_ms=int(delay_seconds * 1000),
        )

    async def _move_to_dlq_and_ack(
        self,
        stream: str,
        group: str,
        consumer: str,
        message: BusMessage,
        *,
        attempts: int,
        reason: str,
    ) -> None:
        result = await self._redis.eval(
            _TRANSFER_TO_DLQ_LUA,
            2,
            stream,
            self._settings.bus_dlq_stream,
            group,
            message.id,
            consumer,
            orjson.dumps(message.payload).decode("utf-8"),
            orjson.dumps(message.headers).decode("utf-8"),
            attempts,
            reason,
        )
        _assert_transfer_ack(result, destination="dlq")
        BUS_FAILURE_TRANSFERS.labels(
            stream=stream,
            destination="dlq",
            result="ok",
        ).inc()
        logger.error(
            "bus.dlq",
            origin_stream=message.stream,
            origin_id=message.id,
            attempts=attempts,
            reason=reason,
            atomic_ack=True,
        )

    async def _promote_due_retries(self, stream: str, *, batch_size: int) -> int:
        retry_key = self._retry_key(stream)
        result = await self._redis.eval(
            _PROMOTE_DUE_RETRIES_LUA,
            2,
            retry_key,
            stream,
            self._clock_ms(),
            max(1, batch_size),
        )
        promoted = len(result or [])
        if promoted:
            logger.info(
                "bus.retries_promoted",
                stream=stream,
                promoted_count=promoted,
            )
        await self._refresh_retry_backlog(stream)
        return promoted

    async def _refresh_retry_backlog(self, stream: str) -> None:
        try:
            size = await self._redis.zcard(self._retry_key(stream))
        except Exception:
            logger.warning("bus.retry_backlog.read_failed", stream=stream)
            return
        BUS_RETRY_BACKLOG.labels(stream=stream).set(int(size))

    async def _refresh_pending_age(self, stream: str, group: str) -> None:
        try:
            rows = await self._redis.xpending_range(
                stream,
                group,
                min="-",
                max="+",
                count=1,
            )
        except Exception:
            logger.warning(
                "bus.pending_age.read_failed",
                stream=stream,
                group=group,
            )
            return
        BUS_PENDING_OLDEST_AGE.labels(stream=stream, group=group).set(
            _oldest_pending_age_seconds(rows, clock_ms=self._clock_ms())
        )

    async def _claim_stale_pending(
        self,
        stream: str,
        group: str,
        consumer: str,
        *,
        start_id: str,
        count: int,
    ) -> tuple[str, list[tuple[Any, dict[Any, Any]]], list[str]]:
        if self._xautoclaim_supported is not False:
            try:
                response = await self._redis.xautoclaim(
                    stream,
                    group,
                    consumer,
                    min_idle_time=self._pending_idle_ms,
                    start_id=start_id,
                    count=max(1, count),
                )
            except (AttributeError, NotImplementedError):
                self._xautoclaim_supported = False
                logger.warning("bus.xautoclaim.unsupported", fallback="xpending+xclaim")
            except ResponseError as exc:
                if not _is_xautoclaim_unsupported(exc):
                    raise
                self._xautoclaim_supported = False
                logger.warning("bus.xautoclaim.unsupported", fallback="xpending+xclaim")
            else:
                self._xautoclaim_supported = True
                return _parse_xautoclaim_response(response)

        pending = await self._redis.xpending_range(
            stream,
            group,
            min="-",
            max="+",
            count=max(1, count),
            idle=self._pending_idle_ms,
        )
        message_ids: list[Any] = [
            _as_text(
                item.get("message_id", "")
                if isinstance(item, dict)
                else item[0]
            )
            for item in pending
        ]
        message_ids = [item for item in message_ids if item]
        if not message_ids:
            return "0-0", [], []
        claimed = await self._redis.xclaim(
            stream,
            group,
            consumer,
            min_idle_time=self._pending_idle_ms,
            message_ids=message_ids,
        )
        entries = cast(list[tuple[Any, dict[Any, Any]]], claimed)
        return "0-0", list(entries or []), []

    # -- ack ------------------------------------------------------------------

    async def ack(self, stream: str, group: str, message_id: str) -> None:
        try:
            acknowledged = await self._redis.xack(stream, group, message_id)
        except Exception:
            BUS_ACKS.labels(stream=stream, result="error").inc()
            raise
        result = "ok" if int(acknowledged or 0) == 1 else "missing"
        BUS_ACKS.labels(stream=stream, result=result).inc()
        if result == "missing":
            logger.warning(
                "bus.ack.missing",
                stream=stream,
                group=group,
                message_id=message_id,
            )

    # -- dlq ------------------------------------------------------------------

    async def move_to_dlq(self, message: BusMessage, reason: str) -> None:
        """Persist a standalone DLQ item.

        Consumers use ``_move_to_dlq_and_ack`` instead so DLQ persistence and
        source ACK happen atomically. This method remains for direct callers
        that do not own a source consumer-group entry.
        """
        dlq_stream = self._settings.bus_dlq_stream
        fields: dict[Any, Any] = {
            "data": orjson.dumps(message.payload).decode("utf-8"),
            "headers": orjson.dumps(message.headers).decode("utf-8"),
            "attempts": str(message.attempts),
            "origin_stream": message.stream,
            "origin_id": message.id,
            "reason": reason,
        }
        await self._redis.xadd(dlq_stream, fields)
        logger.error(
            "bus.dlq",
            origin_stream=message.stream,
            origin_id=message.id,
            attempts=message.attempts,
            reason=reason,
            atomic_ack=False,
        )

    # -- close ----------------------------------------------------------------

    async def close(self) -> None:
        self._stopping.set()
        # Do NOT close the shared redis connection; it is owned by get_redis().

    async def stop(self) -> None:
        self._stopping.set()

    @staticmethod
    def _retry_key(stream: str) -> str:
        return f"{stream}:retry"


def _parse_xautoclaim_response(
    response: Any,
) -> tuple[str, list[tuple[Any, dict[Any, Any]]], list[str]]:
    if not isinstance(response, (list, tuple)) or len(response) < 2:
        raise ValueError("invalid XAUTOCLAIM response")
    next_cursor = _as_text(response[0] or "0-0")
    entries = list(response[1] or [])
    deleted = [_as_text(item) for item in (response[2] or [])] if len(response) > 2 else []
    return next_cursor, entries, deleted


def _assert_transfer_ack(result: Any, *, destination: str) -> None:
    if not isinstance(result, (list, tuple)) or len(result) < 2:
        raise RuntimeError(f"{destination}_transfer_invalid_response")
    marker = _as_text(result[0])
    acknowledged = int(result[1] or 0)
    if marker in {"-1", "STALE"}:
        raise RuntimeError(f"{destination}_transfer_lost_ownership")
    if marker == "MISSING" or acknowledged != 1:
        raise RuntimeError(f"{destination}_transfer_source_not_pending")


def _is_xautoclaim_unsupported(exc: ResponseError) -> bool:
    message = str(exc).lower()
    return (
        "unknown command" in message
        or "syntax error" in message
        or "wrong number of arguments" in message
    )


def _oldest_pending_age_seconds(rows: Sequence[Any], *, clock_ms: int) -> float:
    if not rows:
        return 0.0
    row = rows[0]
    message_id = row.get("message_id", "") if isinstance(row, dict) else row[0]
    try:
        created_ms = int(_as_text(message_id).split("-", 1)[0])
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, (int(clock_ms) - created_ms) / 1000.0)


def _as_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _publish_once_record_key(stream: str, idempotency_key: str) -> str:
    identity_digest = hashlib.sha256(
        f"{stream}\0{idempotency_key}".encode()
    ).hexdigest()
    return f"{stream}:publish-once:{identity_digest}"


def _decode(stream: str, msg_id: Any, fields: dict[Any, Any]) -> BusMessage:
    """Decode an XREADGROUP/XAUTOCLAIM entry into a BusMessage."""

    fmap = {_as_text(key): _as_text(value) for key, value in fields.items()}
    payload = orjson.loads(fmap.get("data", "{}"))
    headers_raw = fmap.get("headers", "{}")
    try:
        headers = orjson.loads(headers_raw)
    except Exception:
        headers = {}
    attempts = int(fmap.get("attempts", "0") or 0)
    return BusMessage(
        id=_as_text(msg_id),
        stream=stream,
        payload=payload,
        headers=headers,
        attempts=attempts,
    )
