from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import orjson
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.common.config import Settings, get_settings

_LIST_SCAN_MULTIPLIER = 3
_LIST_SCAN_MAX_BATCHES = 5


@dataclass
class StreamGroupSnapshot:
    name: str
    consumers: int
    pending: int
    last_delivered_id: str | None
    lag: int | None
    entries_read: int | None


@dataclass
class StreamMessage:
    id: str
    stream_key: str
    stream: str
    tenant_id: str | None
    session_id: str | None
    user_id: str | None
    trace_id: str | None
    channel: str | None
    attempts: int
    reason: str | None
    origin_stream: str | None
    origin_id: str | None
    payload: dict[str, Any]
    headers: dict[str, str]
    created_ts_ms: int | None


class StreamAdminService:
    def __init__(self, redis: Redis, settings: Settings | None = None) -> None:
        self._redis = redis
        self._settings = settings or get_settings()

    def _stream_map(self) -> dict[str, str]:
        return {
            "inbound": self._settings.bus_inbound_stream,
            "outbound": self._settings.bus_outbound_stream,
            "dlq": self._settings.bus_dlq_stream,
        }

    def resolve_stream(self, stream_key: str) -> tuple[str, str]:
        cleaned = str(stream_key or "").strip().lower()
        stream_name = self._stream_map().get(cleaned)
        if not stream_name:
            raise KeyError(cleaned)
        return cleaned, stream_name

    async def summary(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for stream_key, stream_name in self._stream_map().items():
            info = await self._stream_info(stream_name)
            groups = await self._stream_groups(stream_name)
            items.append(
                {
                    "stream_key": stream_key,
                    "stream": stream_name,
                    "length": info.get("length", 0),
                    "first_entry": info.get("first_entry"),
                    "last_entry": info.get("last_entry"),
                    "pending_total": sum(max(0, group.pending) for group in groups),
                    "groups": [self._group_to_dict(group) for group in groups],
                }
            )
        return items

    async def list_messages(
        self,
        *,
        stream_key: str,
        limit: int = 100,
        before_id: str | None = None,
        tenant_id: str | None = None,
        session_id: str | None = None,
        trace_id: str | None = None,
    ) -> tuple[list[StreamMessage], str | None]:
        resolved_key, stream_name = self.resolve_stream(stream_key)
        page_size = max(1, min(limit, 200))
        fetch_count = max(page_size, page_size * _LIST_SCAN_MULTIPLIER)
        cursor = f"({before_id}" if before_id else "+"
        items: list[StreamMessage] = []
        exhausted = False

        for _ in range(_LIST_SCAN_MAX_BATCHES):
            rows = await self._redis.xrevrange(
                stream_name,
                max=cursor,
                min="-",
                count=fetch_count,
            )
            if not rows:
                exhausted = True
                break

            for message_id, fields in rows:
                entry = _decode_stream_message(resolved_key, stream_name, message_id, fields)
                if tenant_id and entry.tenant_id != tenant_id:
                    continue
                if session_id and entry.session_id != session_id:
                    continue
                if trace_id and entry.trace_id != trace_id:
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

    async def get_message(self, *, stream_key: str, entry_id: str) -> StreamMessage | None:
        resolved_key, stream_name = self.resolve_stream(stream_key)
        rows = await self._redis.xrange(
            stream_name,
            min=entry_id,
            max=entry_id,
            count=1,
        )
        if not rows:
            return None
        message_id, fields = rows[0]
        return _decode_stream_message(resolved_key, stream_name, message_id, fields)

    async def _stream_info(self, stream_name: str) -> dict[str, Any]:
        try:
            info = await self._redis.xinfo_stream(stream_name)
        except ResponseError:
            return {"length": 0, "first_entry": None, "last_entry": None}

        first_entry = None
        raw_first = info.get("first-entry")
        if isinstance(raw_first, (list, tuple)) and raw_first:
            first_entry = _stream_id(raw_first[0])

        last_entry = None
        raw_last = info.get("last-entry")
        if isinstance(raw_last, (list, tuple)) and raw_last:
            last_entry = _stream_id(raw_last[0])

        return {
            "length": int(info.get("length") or 0),
            "first_entry": first_entry,
            "last_entry": last_entry,
        }

    async def _stream_groups(self, stream_name: str) -> list[StreamGroupSnapshot]:
        try:
            raw_groups = await self._redis.xinfo_groups(stream_name)
        except ResponseError:
            return []

        groups: list[StreamGroupSnapshot] = []
        for raw in raw_groups:
            if not isinstance(raw, dict):
                continue
            lag_raw = raw.get("lag")
            entries_read_raw = raw.get("entries-read")
            groups.append(
                StreamGroupSnapshot(
                    name=str(raw.get("name") or ""),
                    consumers=int(raw.get("consumers") or 0),
                    pending=int(raw.get("pending") or 0),
                    last_delivered_id=str(raw.get("last-delivered-id") or "") or None,
                    lag=int(lag_raw) if lag_raw not in (None, "") else None,
                    entries_read=int(entries_read_raw) if entries_read_raw not in (None, "") else None,
                )
            )
        return groups

    @staticmethod
    def _group_to_dict(group: StreamGroupSnapshot) -> dict[str, Any]:
        return {
            "name": group.name,
            "consumers": group.consumers,
            "pending": group.pending,
            "last_delivered_id": group.last_delivered_id,
            "lag": group.lag,
            "entries_read": group.entries_read,
        }


def _stream_id(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _decode_json_dict(raw: str) -> dict[str, Any]:
    try:
        decoded = orjson.loads(raw)
    except Exception:
        return {}
    if not isinstance(decoded, dict):
        return {}
    return dict(decoded)


def _decode_str_dict(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


def _stream_id_to_ts_ms(stream_id: str) -> int | None:
    text = str(stream_id or "")
    if "-" not in text:
        return None
    head = text.split("-", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


def _decode_stream_message(
    stream_key: str,
    stream_name: str,
    msg_id: Any,
    fields: dict[Any, Any],
) -> StreamMessage:
    fmap = {_stream_id(key): _stream_id(value) for key, value in fields.items()}
    payload = _decode_json_dict(fmap.get("data", "{}"))
    headers = _decode_str_dict(_decode_json_dict(fmap.get("headers", "{}")))

    attempts_raw = fmap.get("attempts", "0") or "0"
    try:
        attempts = int(attempts_raw)
    except ValueError:
        attempts = 0

    tenant_id = str(payload.get("tenant_id") or headers.get("tenant_id") or "") or None
    session_id = str(payload.get("session_id") or headers.get("session_id") or "") or None
    user_id = str(payload.get("user_id") or "") or None
    trace_id = str(payload.get("trace_id") or headers.get("trace_id") or "") or None
    channel = str(payload.get("channel") or "") or None
    message_id = _stream_id(msg_id)

    return StreamMessage(
        id=message_id,
        stream_key=stream_key,
        stream=stream_name,
        tenant_id=tenant_id,
        session_id=session_id,
        user_id=user_id,
        trace_id=trace_id,
        channel=channel,
        attempts=attempts,
        reason=fmap.get("reason") or None,
        origin_stream=fmap.get("origin_stream") or None,
        origin_id=fmap.get("origin_id") or None,
        payload=payload,
        headers=headers,
        created_ts_ms=_stream_id_to_ts_ms(message_id),
    )
