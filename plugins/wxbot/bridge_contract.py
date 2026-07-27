from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from app.social import ParticipationPolicy
from plugins.wxbot.store import normalize_group_participation_policy

CURSOR_REDIS_KEY_PREFIX = "wxbot:bridge:ingest_cursor"


LEGACY_CURSOR_REDIS_KEY_PREFIX = "wxbot:bridge:legacy_ingest_cursor"


EVENT_CURSOR_REDIS_KEY_PREFIX = "wxbot:bridge:event_cursor"


INBOUND_DEDUPE_KEY_PREFIX = "wxbot:bridge:inbound"


LEADER_KEY_PREFIX = "wxbot:bridge:leader"


STATUS_KEY_PREFIX = "wxbot:bridge:status"


LEADER_TTL_SECONDS = 30


STATUS_TTL_SECONDS = 90


LEADER_RETRY_SECONDS = 5.0


STATUS_PUBLISH_INTERVAL_SECONDS = 2.0


CURSOR_RECONCILE_INTERVAL_SECONDS = 5.0


SELF_HEAL_COOLDOWN_SECONDS = 30.0


SELF_HEAL_RECURRENCE_THRESHOLD = 2


CURSOR_LAG_THRESHOLD = 5


CURSOR_STALL_CHECKS = 2


MEMBER_EVENT_TYPES = {"group.member.joined", "group.member.left"}


MEDIA_READY_EVENT_TYPE = "message.media.ready"


_IMAGE_PREVIEW_VARIANTS = ("preview",)


_IMAGE_THUMBNAIL_VARIANTS = ("thumbnail",)


_REQUIRED_TASK_NAMES = {
    "wxbot-bridge-ingest",
    "wxbot-bridge-events",
    "wxbot-bridge-send",
    "wxbot-bridge-pending-media",
    "wxbot-bridge-cursor-reconcile",
}


_STREAM_TASK_NAMES = {"wxbot-bridge-ingest", "wxbot-bridge-events"}


_SLASH_COMMAND_RE = re.compile(r"^\s*(?:@\S+[\s\u2005\u00a0]+)*\s*/[^\s]+")


REPLY_CLAIM_LEASE_SECONDS = 45.0


REPLY_MAX_ATTEMPTS = 3


REPLY_DRAIN_LIMIT = 50


_SDK_JSON_CONTENT_TYPES = (
    "application/json",
    "application/problem+json",
    "text/plain",
)


_SDK_MAX_JSON_BYTES = 16 * 1024 * 1024


_SDK_MAX_SSE_BYTES = 64 * 1024 * 1024


_SDK_SSE_CONNECTION_TIMEOUT_SECONDS = 300.0


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _as_utc_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    parsed = value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(parsed, datetime):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _send_time_participation_policy(
    session_policy: dict[str, Any],
    *,
    force_send: bool = False,
) -> ParticipationPolicy:
    values = normalize_group_participation_policy(session_policy.get("participation_policy"))
    enabled = force_send or str(session_policy.get("effective_mode") or "off") in {
        "all",
        "contains",
    }
    try:
        return ParticipationPolicy(
            enabled=enabled,
            threshold=int(values["threshold"]),
            quiet_start_hour=int(values["quiet_start_hour"]),
            quiet_end_hour=int(values["quiet_end_hour"]),
            timezone=str(values["timezone"]),
            max_soft_replies_10m=int(values["max_soft_replies_10m"]),
            max_soft_replies_hour=int(values["max_soft_replies_hour"]),
            max_bot_ratio_last_40=float(values["max_bot_ratio_last_40"]),
            max_consecutive_bot_messages=int(values["max_consecutive_bot_messages"]),
        )
    except (TypeError, ValueError):
        return ParticipationPolicy(enabled=False)


def _connection_scoped_key(prefix: str, tenant_id: str, connection_id: str = "") -> str:
    connection = str(connection_id or "").strip()
    if connection and connection != "legacy-wechat-default":
        return f"{prefix}:{_key_part(tenant_id)}:{_key_part(connection)}"
    return f"{prefix}:{_key_part(tenant_id)}"


def _key_part(value: str) -> str:
    return quote(str(value or ""), safe="@._-")


def _status_key(tenant_id: str, connection_id: str = "") -> str:
    return _connection_scoped_key(STATUS_KEY_PREFIX, tenant_id, connection_id)


def _leader_key(tenant_id: str, connection_id: str = "") -> str:
    return _connection_scoped_key(LEADER_KEY_PREFIX, tenant_id, connection_id)


def _cursor_key(tenant_id: str, connection_id: str = "") -> str:
    return _connection_scoped_key(CURSOR_REDIS_KEY_PREFIX, tenant_id, connection_id)


def _legacy_cursor_key(tenant_id: str, connection_id: str = "") -> str:
    return _connection_scoped_key(LEGACY_CURSOR_REDIS_KEY_PREFIX, tenant_id, connection_id)


def _event_cursor_key(tenant_id: str, connection_id: str = "") -> str:
    return _connection_scoped_key(EVENT_CURSOR_REDIS_KEY_PREFIX, tenant_id, connection_id)


def _partition_key(
    tenant_id: str,
    session_id: str,
    connection_id: str = "",
) -> str:
    connection = str(connection_id or "").strip()
    if connection and connection != "legacy-wechat-default":
        return f"{_key_part(tenant_id)}:{_key_part(connection)}:{_key_part(session_id)}"
    return f"{_key_part(tenant_id)}:{_key_part(session_id)}"


def _parse_json_dict(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _decode_leader_payload(raw: str | None) -> dict[str, Any]:
    payload = _parse_json_dict(raw)
    if payload:
        return payload
    token = str(raw or "").strip()
    if not token:
        return {}
    return {
        "token": token,
        "instance_id": "",
        "process_role": "",
        "host": "",
        "pid": None,
        "updated_at": None,
    }


def _cursor_diagnostics(
    existing: dict[str, Any],
    *,
    stream_mode: str,
    cursor: int,
    legacy_cursor: int,
    event_cursor: int,
) -> dict[str, Any]:
    diagnostics = dict(existing)
    diagnostics.update(
        {
            "cursor": cursor,
            "legacy_cursor": legacy_cursor,
            "event_cursor": event_cursor,
        }
    )
    if not any(key in diagnostics for key in ("max_stream_id", "max_inbound_id", "max_event_id")):
        return diagnostics

    max_stream_id = int(diagnostics.get("max_stream_id") or 0)
    max_inbound_id = int(diagnostics.get("max_inbound_id") or 0)
    max_event_id = int(diagnostics.get("max_event_id") or 0)
    stream_lag = max(0, max_stream_id - cursor)
    legacy_lag = max(0, max_inbound_id - legacy_cursor)
    event_lag = max(0, max_event_id - event_cursor)
    active_lags = [stream_lag, legacy_lag, event_lag]
    if stream_mode == "unified" or (max_stream_id > 0 and cursor >= max_stream_id):
        active_lags = [stream_lag]
        diagnostics["lag_mode"] = "unified"
    elif diagnostics.get("lag_mode") == "legacy":
        active_lags = [legacy_lag, event_lag]
    diagnostics.update(
        {
            "stream_lag": stream_lag,
            "legacy_lag": legacy_lag,
            "event_lag": event_lag,
            "max_lag": max(active_lags),
        }
    )
    return diagnostics
