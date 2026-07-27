from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import orjson
from redis.asyncio import Redis

from app.common.config import Settings, get_settings

_LIST_SCAN_MULTIPLIER = 3
_LIST_SCAN_MAX_BATCHES = 5

_REPLAY_DLQ_LUA = """
local existing_fingerprint = redis.call('HGET', KEYS[3], 'fingerprint')
if existing_fingerprint then
    if existing_fingerprint ~= ARGV[1] then
        return {'CONFLICT'}
    end
    local existing_entry_id = redis.call('HGET', KEYS[3], 'entry_id')
    local existing_origin_stream = redis.call('HGET', KEYS[3], 'origin_stream')
    local existing_message_id = redis.call('HGET', KEYS[3], 'message_id')
    local existing_deleted = redis.call('HGET', KEYS[3], 'deleted')
    local existing_tenant_id = redis.call('HGET', KEYS[3], 'tenant_id')
    if not existing_entry_id or not existing_origin_stream
        or not existing_message_id or not existing_deleted
        or existing_tenant_id == false then
        return {'CORRUPT'}
    end
    return {
        'EXISTING',
        existing_entry_id,
        existing_origin_stream,
        existing_message_id,
        existing_deleted,
        existing_tenant_id
    }
end

local rows = redis.call('XRANGE', KEYS[1], ARGV[2], ARGV[2], 'COUNT', 1)
if #rows == 0 then
    return {'MISSING'}
end

local values = {}
local fields = rows[1][2]
for index = 1, #fields, 2 do
    values[fields[index]] = fields[index + 1]
end

local origin_stream = values['origin_stream'] or ''
if origin_stream == '' then
    return {'INVALID_ORIGIN'}
end
if origin_stream ~= KEYS[2] then
    return {'ORIGIN_MISMATCH'}
end

local raw_payload = values['data'] or ''
local payload_ok, payload = pcall(cjson.decode, raw_payload)
if not payload_ok or type(payload) ~= 'table' then
    return {'INVALID_PAYLOAD'}
end
local raw_headers = values['headers'] or '{}'
local headers_ok, headers = pcall(cjson.decode, raw_headers)
if not headers_ok or type(headers) ~= 'table' then
    return {'INVALID_HEADERS'}
end
local tenant_id = payload['tenant_id'] or headers['tenant_id'] or ''
headers['dlq_replayed_from'] = ARGV[2]
local reason = values['reason'] or ''
if reason ~= '' then
    headers['dlq_replay_reason'] = reason
end

local replayed_message_id = redis.call(
    'XADD', KEYS[2], '*',
    'data', raw_payload,
    'headers', cjson.encode(headers),
    'attempts', '0'
)
local deleted = '0'
if ARGV[3] == '1' then
    redis.call('XDEL', KEYS[1], ARGV[2])
    deleted = '1'
end
redis.call(
    'HSET', KEYS[3],
    'fingerprint', ARGV[1],
    'entry_id', ARGV[2],
    'origin_stream', origin_stream,
    'message_id', replayed_message_id,
    'deleted', deleted,
    'tenant_id', tenant_id
)
return {
    'PUBLISHED',
    ARGV[2],
    origin_stream,
    replayed_message_id,
    deleted,
    tenant_id
}
"""

_DELETE_DLQ_LUA = """
local existing_fingerprint = redis.call('HGET', KEYS[2], 'fingerprint')
if existing_fingerprint then
    if existing_fingerprint ~= ARGV[1] then
        return {'CONFLICT'}
    end
    local existing_entry_id = redis.call('HGET', KEYS[2], 'entry_id')
    local existing_deleted = redis.call('HGET', KEYS[2], 'deleted')
    local existing_tenant_id = redis.call('HGET', KEYS[2], 'tenant_id')
    if not existing_entry_id or existing_deleted ~= '1'
        or existing_tenant_id == false then
        return {'CORRUPT'}
    end
    return {'EXISTING', existing_entry_id, existing_deleted, existing_tenant_id}
end

local rows = redis.call('XRANGE', KEYS[1], ARGV[2], ARGV[2], 'COUNT', 1)
if #rows == 0 then
    return {'MISSING'}
end
local values = {}
local fields = rows[1][2]
for index = 1, #fields, 2 do
    values[fields[index]] = fields[index + 1]
end
local tenant_id = ''
local payload_ok, payload = pcall(cjson.decode, values['data'] or '{}')
if payload_ok and type(payload) == 'table' then
    tenant_id = payload['tenant_id'] or ''
end
if tenant_id == '' then
    local headers_ok, headers = pcall(cjson.decode, values['headers'] or '{}')
    if headers_ok and type(headers) == 'table' then
        tenant_id = headers['tenant_id'] or ''
    end
end
local deleted = redis.call('XDEL', KEYS[1], ARGV[2])
if deleted ~= 1 then
    return {'MISSING'}
end
redis.call(
    'HSET', KEYS[2],
    'fingerprint', ARGV[1],
    'entry_id', ARGV[2],
    'deleted', '1',
    'tenant_id', tenant_id
)
return {'DELETED', ARGV[2], '1', tenant_id}
"""


@dataclass
class DLQMessage:
    id: str
    stream: str
    tenant_id: str | None
    origin_stream: str
    origin_id: str | None
    reason: str | None
    attempts: int
    payload: dict[str, Any]
    headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class DLQReplayResult:
    entry_id: str
    origin_stream: str
    replayed_message_id: str
    deleted: bool
    tenant_id: str = ""
    idempotent_replayed: bool = False


@dataclass(frozen=True, slots=True)
class DLQDeleteResult:
    entry_id: str
    deleted: bool
    tenant_id: str = ""
    idempotent_replayed: bool = False


class DLQReplayIdempotencyConflict(ValueError):
    """The replay key is already bound to another target or request body."""


class DLQDeleteIdempotencyConflict(ValueError):
    """The delete key is already bound to another DLQ entry."""


class DLQReplayStateError(ValueError):
    """The atomic replay record or source entry is invalid."""


class DLQAdminService:
    def __init__(self, redis: Redis, settings: Settings | None = None) -> None:
        self._redis = redis
        self._settings = settings or get_settings()

    async def list_messages(
        self,
        *,
        limit: int = 100,
        before_id: str | None = None,
        tenant_id: str | None = None,
    ) -> tuple[list[DLQMessage], str | None]:
        page_size = max(1, min(limit, 200))
        fetch_count = max(page_size, page_size * _LIST_SCAN_MULTIPLIER)
        cursor = f"({before_id}" if before_id else "+"
        items: list[DLQMessage] = []
        exhausted = False

        for _ in range(_LIST_SCAN_MAX_BATCHES):
            rows = await self._redis.xrevrange(
                self._settings.bus_dlq_stream,
                max=cursor,
                min="-",
                count=fetch_count,
            )
            if not rows:
                exhausted = True
                break

            for message_id, fields in rows:
                if fields is None:
                    continue
                entry = _decode_dlq_message(self._settings.bus_dlq_stream, message_id, fields)
                if tenant_id and entry.tenant_id != tenant_id:
                    continue
                items.append(entry)
                if len(items) >= page_size:
                    break

            if len(items) >= page_size:
                break

            if len(rows) < fetch_count:
                exhausted = True
                break

            cursor = f"({_stream_id(rows[-1][0])}"

        next_before_id = None
        if items and not exhausted:
            next_before_id = items[-1].id
        return items, next_before_id

    async def get_message(self, entry_id: str) -> DLQMessage | None:
        rows = await self._redis.xrange(
            self._settings.bus_dlq_stream,
            min=entry_id,
            max=entry_id,
            count=1,
        )
        if not rows:
            return None
        message_id, fields = rows[0]
        if fields is None:
            return None
        return _decode_dlq_message(self._settings.bus_dlq_stream, message_id, fields)

    async def replay_message(
        self,
        entry_id: str,
        *,
        idempotency_key: str,
        delete_after_replay: bool = True,
    ) -> DLQReplayResult:
        normalized_entry_id = str(entry_id or "").strip()
        if not normalized_entry_id:
            raise ValueError("valid_dlq_entry_id_required")
        normalized_key = str(idempotency_key or "").strip()
        if not normalized_key or len(normalized_key) > 128:
            raise ValueError("valid_idempotency_key_required")
        request_fingerprint = _replay_request_fingerprint(
            normalized_entry_id,
            delete_after_replay=delete_after_replay,
        )
        record_key = _replay_record_key(
            self._settings.bus_dlq_stream,
            normalized_key,
        )
        existing = await self._read_replay_record(
            record_key,
            expected_fingerprint=request_fingerprint,
        )
        if existing is not None:
            return existing

        entry = await self.get_message(normalized_entry_id)
        if entry is None:
            raise KeyError(normalized_entry_id)
        if not entry.origin_stream:
            raise ValueError("dlq_missing_origin_stream")
        if entry.origin_stream in {self._settings.bus_dlq_stream, record_key}:
            raise DLQReplayStateError("dlq_invalid_origin_stream")

        raw_result = await self._redis.eval(
            _REPLAY_DLQ_LUA,
            3,
            self._settings.bus_dlq_stream,
            entry.origin_stream,
            record_key,
            request_fingerprint,
            entry.id,
            "1" if delete_after_replay else "0",
        )
        return _decode_replay_result(raw_result)

    async def _read_replay_record(
        self,
        record_key: str,
        *,
        expected_fingerprint: str,
    ) -> DLQReplayResult | None:
        raw_record = await self._redis.hgetall(record_key)
        if not raw_record:
            return None
        record = {
            _stream_id(key): _stream_id(value)
            for key, value in raw_record.items()
        }
        fingerprint = record.get("fingerprint", "")
        if not fingerprint:
            raise DLQReplayStateError("dlq_replay_idempotency_record_corrupt")
        if fingerprint != expected_fingerprint:
            raise DLQReplayIdempotencyConflict(
                "dlq_replay_idempotency_conflict"
            )
        return _replay_result_from_fields(record, idempotent_replayed=True)

    async def delete_message(
        self,
        entry_id: str,
        *,
        idempotency_key: str,
    ) -> DLQDeleteResult:
        normalized_entry_id = str(entry_id or "").strip()
        if not normalized_entry_id:
            raise ValueError("valid_dlq_entry_id_required")
        normalized_key = str(idempotency_key or "").strip()
        if not normalized_key or len(normalized_key) > 128:
            raise ValueError("valid_idempotency_key_required")
        request_fingerprint = _delete_request_fingerprint(normalized_entry_id)
        record_key = _delete_record_key(
            self._settings.bus_dlq_stream,
            normalized_key,
        )
        raw_result = await self._redis.eval(
            _DELETE_DLQ_LUA,
            2,
            self._settings.bus_dlq_stream,
            record_key,
            request_fingerprint,
            normalized_entry_id,
        )
        return _decode_delete_result(raw_result)


def _stream_id(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _replay_request_fingerprint(
    entry_id: str,
    *,
    delete_after_replay: bool,
) -> str:
    canonical_request = orjson.dumps(
        {
            "operation": "dlq_replay_v1",
            "entry_id": str(entry_id or "").strip(),
            "delete_after_replay": bool(delete_after_replay),
        },
        option=orjson.OPT_SORT_KEYS,
    )
    return hashlib.sha256(canonical_request).hexdigest()


def _replay_record_key(dlq_stream: str, idempotency_key: str) -> str:
    return _dlq_mutation_record_key(dlq_stream, idempotency_key)


def _dlq_mutation_record_key(dlq_stream: str, idempotency_key: str) -> str:
    identity_digest = hashlib.sha256(
        f"{dlq_stream}\0{idempotency_key}".encode()
    ).hexdigest()
    return f"{dlq_stream}:mutation-idempotency:{identity_digest}"


def _delete_request_fingerprint(entry_id: str) -> str:
    canonical_request = orjson.dumps(
        {
            "operation": "dlq_delete_v1",
            "entry_id": str(entry_id or "").strip(),
        },
        option=orjson.OPT_SORT_KEYS,
    )
    return hashlib.sha256(canonical_request).hexdigest()


def _delete_record_key(dlq_stream: str, idempotency_key: str) -> str:
    return _dlq_mutation_record_key(dlq_stream, idempotency_key)


def _decode_delete_result(raw_result: Any) -> DLQDeleteResult:
    if not isinstance(raw_result, (list, tuple)) or not raw_result:
        raise DLQReplayStateError("dlq_delete_invalid_response")
    values = [_stream_id(value) for value in raw_result]
    status = values[0]
    if status == "MISSING":
        raise KeyError("dlq_message_not_found")
    if status == "CONFLICT":
        raise DLQDeleteIdempotencyConflict("dlq_delete_idempotency_conflict")
    if status == "CORRUPT":
        raise DLQReplayStateError("dlq_delete_idempotency_record_corrupt")
    if status not in {"DELETED", "EXISTING"} or len(values) != 4:
        raise DLQReplayStateError("dlq_delete_invalid_response")
    entry_id = values[1].strip()
    if not entry_id or values[2] != "1":
        raise DLQReplayStateError("dlq_delete_idempotency_record_corrupt")
    return DLQDeleteResult(
        entry_id=entry_id,
        deleted=True,
        tenant_id=values[3].strip(),
        idempotent_replayed=status == "EXISTING",
    )


def _decode_replay_result(raw_result: Any) -> DLQReplayResult:
    if not isinstance(raw_result, (list, tuple)) or not raw_result:
        raise DLQReplayStateError("dlq_replay_invalid_response")
    values = [_stream_id(value) for value in raw_result]
    status = values[0]
    if status == "MISSING":
        raise KeyError("dlq_message_not_found")
    if status == "CONFLICT":
        raise DLQReplayIdempotencyConflict("dlq_replay_idempotency_conflict")
    if status == "CORRUPT":
        raise DLQReplayStateError("dlq_replay_idempotency_record_corrupt")
    invalid_statuses = {
        "INVALID_ORIGIN": "dlq_missing_origin_stream",
        "ORIGIN_MISMATCH": "dlq_origin_stream_changed",
        "INVALID_PAYLOAD": "dlq_payload_invalid",
        "INVALID_HEADERS": "dlq_headers_invalid",
    }
    if status in invalid_statuses:
        raise DLQReplayStateError(invalid_statuses[status])
    if status not in {"PUBLISHED", "EXISTING"} or len(values) != 6:
        raise DLQReplayStateError("dlq_replay_invalid_response")
    return _replay_result_from_fields(
        {
            "entry_id": values[1],
            "origin_stream": values[2],
            "message_id": values[3],
            "deleted": values[4],
            "tenant_id": values[5],
        },
        idempotent_replayed=status == "EXISTING",
    )


def _replay_result_from_fields(
    fields: dict[str, str],
    *,
    idempotent_replayed: bool,
) -> DLQReplayResult:
    entry_id = fields.get("entry_id", "").strip()
    origin_stream = fields.get("origin_stream", "").strip()
    message_id = fields.get("message_id", "").strip()
    deleted_value = fields.get("deleted", "")
    if (
        not entry_id
        or not origin_stream
        or not message_id
        or deleted_value not in {"0", "1"}
    ):
        raise DLQReplayStateError("dlq_replay_idempotency_record_corrupt")
    return DLQReplayResult(
        entry_id=entry_id,
        origin_stream=origin_stream,
        replayed_message_id=message_id,
        deleted=deleted_value == "1",
        tenant_id=fields.get("tenant_id", "").strip(),
        idempotent_replayed=idempotent_replayed,
    )


def _decode_str_dict(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _decode_json_dict(raw: str) -> dict[str, Any]:
    try:
        decoded = orjson.loads(raw)
    except Exception:
        return {}
    if not isinstance(decoded, dict):
        return {}
    return dict(decoded)


def _decode_dlq_message(stream: str, msg_id: Any, fields: dict[Any, Any]) -> DLQMessage:
    fmap = {_stream_id(k): _stream_id(v) for k, v in fields.items()}
    payload = _decode_json_dict(fmap.get("data", "{}"))
    headers = _decode_str_dict(_decode_json_dict(fmap.get("headers", "{}")))
    tenant_id = str(payload.get("tenant_id") or headers.get("tenant_id") or "") or None

    attempts_raw = fmap.get("attempts", "0") or "0"
    try:
        attempts = int(attempts_raw)
    except ValueError:
        attempts = 0

    return DLQMessage(
        id=_stream_id(msg_id),
        stream=stream,
        tenant_id=tenant_id,
        origin_stream=fmap.get("origin_stream", ""),
        origin_id=fmap.get("origin_id"),
        reason=fmap.get("reason"),
        attempts=attempts,
        payload=payload,
        headers=headers,
    )
