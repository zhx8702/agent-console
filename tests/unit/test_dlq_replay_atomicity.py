from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import orjson
import pytest

from app.admin.dlq_service import (
    _DELETE_DLQ_LUA,
    _REPLAY_DLQ_LUA,
    DLQAdminService,
    DLQDeleteIdempotencyConflict,
    DLQReplayIdempotencyConflict,
)


class _ReplayRedisDouble:
    def __init__(self) -> None:
        self.streams: dict[str, dict[str, dict[str, str]]] = {}
        self.records: dict[str, dict[str, str]] = {}
        self.eval_calls: list[tuple[str, int, tuple[Any, ...]]] = []
        self.fail_after_delete_once = False

    def add_dlq(
        self,
        *,
        stream: str,
        entry_id: str,
        origin_stream: str,
    ) -> None:
        self.streams.setdefault(stream, {})[entry_id] = {
            "data": orjson.dumps(
                {"tenant_id": "tenant-a", "message": "not copied to idempotency"}
            ).decode(),
            "headers": orjson.dumps(
                {"tenant_id": "tenant-a", "trace_id": "trace-a"}
            ).decode(),
            "attempts": "5",
            "origin_stream": origin_stream,
            "origin_id": "source-1",
            "reason": "delivery_error",
        }

    async def xrange(
        self,
        stream: str,
        *,
        min: str,
        max: str,
        count: int,
    ) -> list[tuple[str, dict[str, str]]]:
        _ = max, count
        fields = self.streams.get(stream, {}).get(min)
        return [] if fields is None else [(min, dict(fields))]

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.records.get(key, {}))

    async def xdel(self, stream: str, entry_id: str) -> int:
        return int(self.streams.get(stream, {}).pop(entry_id, None) is not None)

    async def eval(self, script: str, numkeys: int, *args: Any) -> list[str]:
        self.eval_calls.append((script, numkeys, args))
        if script == _DELETE_DLQ_LUA:
            assert numkeys == 2
            dlq_stream, record_key, fingerprint, entry_id = map(str, args)
            existing = self.records.get(record_key)
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    return ["CONFLICT"]
                return [
                    "EXISTING",
                    existing["entry_id"],
                    existing["deleted"],
                    existing["tenant_id"],
                ]
            fields = self.streams.get(dlq_stream, {}).get(entry_id)
            if fields is None:
                return ["MISSING"]
            self.streams[dlq_stream].pop(entry_id)
            self.records[record_key] = {
                "fingerprint": fingerprint,
                "entry_id": entry_id,
                "deleted": "1",
                "tenant_id": "tenant-a",
            }
            if self.fail_after_delete_once:
                self.fail_after_delete_once = False
                raise ConnectionError("response_lost_after_atomic_delete")
            return ["DELETED", entry_id, "1", "tenant-a"]

        assert script == _REPLAY_DLQ_LUA
        assert numkeys == 3
        (
            dlq_stream,
            origin_stream,
            record_key,
            fingerprint,
            entry_id,
            delete_after,
        ) = map(str, args)
        existing = self.records.get(record_key)
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                return ["CONFLICT"]
            return [
                "EXISTING",
                existing["entry_id"],
                existing["origin_stream"],
                existing["message_id"],
                existing["deleted"],
                existing["tenant_id"],
            ]
        fields = self.streams.get(dlq_stream, {}).get(entry_id)
        if fields is None:
            return ["MISSING"]
        if fields.get("origin_stream") != origin_stream:
            return ["ORIGIN_MISMATCH"]
        message_id = f"{len(self.streams.setdefault(origin_stream, {})) + 1}-0"
        replay_headers = orjson.loads(fields["headers"])
        replay_headers["dlq_replayed_from"] = entry_id
        replay_headers["dlq_replay_reason"] = fields["reason"]
        self.streams[origin_stream][message_id] = {
            "data": fields["data"],
            "headers": orjson.dumps(replay_headers).decode(),
            "attempts": "0",
        }
        deleted = "1" if delete_after == "1" else "0"
        if deleted == "1":
            self.streams[dlq_stream].pop(entry_id, None)
        self.records[record_key] = {
            "fingerprint": fingerprint,
            "entry_id": entry_id,
            "origin_stream": origin_stream,
            "message_id": message_id,
            "deleted": deleted,
            "tenant_id": "tenant-a",
        }
        return ["PUBLISHED", entry_id, origin_stream, message_id, deleted, "tenant-a"]


@pytest.mark.asyncio
async def test_replay_is_atomic_idempotent_and_survives_source_deletion() -> None:
    redis = _ReplayRedisDouble()
    redis.add_dlq(stream="dlq", entry_id="10-0", origin_stream="outbound")
    service = DLQAdminService(
        redis,  # type: ignore[arg-type]
        SimpleNamespace(bus_dlq_stream="dlq"),  # type: ignore[arg-type]
    )

    first = await service.replay_message(
        "10-0",
        idempotency_key="replay-key-1",
        delete_after_replay=True,
    )
    replay = await service.replay_message(
        "10-0",
        idempotency_key="replay-key-1",
        delete_after_replay=True,
    )

    assert first.entry_id == replay.entry_id == "10-0"
    assert first.origin_stream == replay.origin_stream == "outbound"
    assert first.replayed_message_id == replay.replayed_message_id == "1-0"
    assert first.deleted is replay.deleted is True
    assert first.idempotent_replayed is False
    assert replay.idempotent_replayed is True
    assert len(redis.streams["outbound"]) == 1
    assert redis.streams["dlq"] == {}
    assert len(redis.eval_calls) == 1
    assert set(next(iter(redis.records.values()))) == {
        "fingerprint",
        "entry_id",
        "origin_stream",
        "message_id",
        "deleted",
        "tenant_id",
    }


@pytest.mark.asyncio
async def test_replay_same_key_with_different_request_fails_closed() -> None:
    redis = _ReplayRedisDouble()
    redis.add_dlq(stream="dlq", entry_id="10-0", origin_stream="outbound")
    service = DLQAdminService(
        redis,  # type: ignore[arg-type]
        SimpleNamespace(bus_dlq_stream="dlq"),  # type: ignore[arg-type]
    )
    await service.replay_message(
        "10-0",
        idempotency_key="replay-key-1",
        delete_after_replay=False,
    )

    with pytest.raises(
        DLQReplayIdempotencyConflict,
        match="dlq_replay_idempotency_conflict",
    ):
        await service.replay_message(
            "10-0",
            idempotency_key="replay-key-1",
            delete_after_replay=True,
        )

    assert len(redis.streams["outbound"]) == 1
    assert len(redis.eval_calls) == 1


@pytest.mark.asyncio
async def test_replay_concurrent_same_key_publishes_once() -> None:
    redis = _ReplayRedisDouble()
    redis.add_dlq(stream="dlq", entry_id="10-0", origin_stream="outbound")
    service = DLQAdminService(
        redis,  # type: ignore[arg-type]
        SimpleNamespace(bus_dlq_stream="dlq"),  # type: ignore[arg-type]
    )

    first, replay = await asyncio.gather(
        service.replay_message(
            "10-0",
            idempotency_key="replay-key-1",
            delete_after_replay=False,
        ),
        service.replay_message(
            "10-0",
            idempotency_key="replay-key-1",
            delete_after_replay=False,
        ),
    )

    assert first.replayed_message_id == replay.replayed_message_id
    assert sorted([first.idempotent_replayed, replay.idempotent_replayed]) == [False, True]
    assert len(redis.streams["outbound"]) == 1


def test_replay_lua_contains_add_delete_and_idempotency_record_in_one_script() -> None:
    assert "existing_fingerprint" in _REPLAY_DLQ_LUA
    assert "'XADD'" in _REPLAY_DLQ_LUA
    assert "'XDEL'" in _REPLAY_DLQ_LUA
    assert "'HSET'" in _REPLAY_DLQ_LUA


@pytest.mark.asyncio
async def test_delete_is_concurrently_idempotent_and_binds_key_to_entry() -> None:
    redis = _ReplayRedisDouble()
    redis.add_dlq(stream="dlq", entry_id="10-0", origin_stream="outbound")
    redis.add_dlq(stream="dlq", entry_id="11-0", origin_stream="outbound")
    service = DLQAdminService(
        redis,  # type: ignore[arg-type]
        SimpleNamespace(bus_dlq_stream="dlq"),  # type: ignore[arg-type]
    )

    first, replay = await asyncio.gather(
        service.delete_message("10-0", idempotency_key="delete-key-1"),
        service.delete_message("10-0", idempotency_key="delete-key-1"),
    )

    assert first.entry_id == replay.entry_id == "10-0"
    assert sorted([first.idempotent_replayed, replay.idempotent_replayed]) == [False, True]
    assert "10-0" not in redis.streams["dlq"]
    assert "11-0" in redis.streams["dlq"]

    with pytest.raises(
        DLQDeleteIdempotencyConflict,
        match="dlq_delete_idempotency_conflict",
    ):
        await service.delete_message("11-0", idempotency_key="delete-key-1")

    assert "11-0" in redis.streams["dlq"]


@pytest.mark.asyncio
async def test_delete_recovers_when_response_is_lost_after_atomic_commit() -> None:
    redis = _ReplayRedisDouble()
    redis.add_dlq(stream="dlq", entry_id="10-0", origin_stream="outbound")
    redis.fail_after_delete_once = True
    service = DLQAdminService(
        redis,  # type: ignore[arg-type]
        SimpleNamespace(bus_dlq_stream="dlq"),  # type: ignore[arg-type]
    )

    with pytest.raises(ConnectionError, match="response_lost_after_atomic_delete"):
        await service.delete_message("10-0", idempotency_key="delete-key-1")

    replay = await service.delete_message("10-0", idempotency_key="delete-key-1")
    assert replay.deleted is True
    assert replay.idempotent_replayed is True
    assert redis.streams["dlq"] == {}


@pytest.mark.asyncio
async def test_dlq_key_cannot_be_rebound_between_replay_and_delete() -> None:
    redis = _ReplayRedisDouble()
    redis.add_dlq(stream="dlq", entry_id="10-0", origin_stream="outbound")
    service = DLQAdminService(
        redis,  # type: ignore[arg-type]
        SimpleNamespace(bus_dlq_stream="dlq"),  # type: ignore[arg-type]
    )
    await service.replay_message(
        "10-0",
        idempotency_key="shared-dlq-key-1",
        delete_after_replay=False,
    )

    with pytest.raises(
        DLQDeleteIdempotencyConflict,
        match="dlq_delete_idempotency_conflict",
    ):
        await service.delete_message(
            "10-0",
            idempotency_key="shared-dlq-key-1",
        )

    assert "10-0" in redis.streams["dlq"]


def test_delete_lua_keeps_delete_and_durable_result_in_one_script() -> None:
    assert "existing_fingerprint" in _DELETE_DLQ_LUA
    assert "'XRANGE'" in _DELETE_DLQ_LUA
    assert "'XDEL'" in _DELETE_DLQ_LUA
    assert "'HSET'" in _DELETE_DLQ_LUA
