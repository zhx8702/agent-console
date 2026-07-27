from __future__ import annotations

import hashlib
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.admin.mutation_ledger import (
    MutationAudit,
    MutationChange,
    MutationIdentity,
    MutationOutcome,
    run_idempotent_mutation,
)
from app.agent.scopes import DEFAULT_AGENT_SCOPE
from app.channel.adapters import WECHAT_SDK_ADAPTER_ID
from app.channel.identity import (
    LEGACY_WXBOT_CONNECTION_ID,
    canonical_conversation_id,
)
from app.common.logging import get_logger
from app.infra.db import get_engine
from app.infra.runtime_schema import verify_runtime_schema

logger = get_logger(__name__)

_CONFIG_COLUMNS = (
    "tenant_id, session_id, session_name, enabled, active_start, active_end, "
    "quiet_start, quiet_end, timezone, idle_minutes, lookback_minutes, "
    "min_send_interval_minutes, max_per_day, topic_repeat_window_minutes, "
    "llm_model_tier, temperature, agent_tool_scope, version, updated_at"
)
_CONFIG_SELECT_COLUMNS = (
    "c.tenant_id, c.session_id, c.session_name, c.enabled, c.active_start, "
    "c.active_end, c.quiet_start, c.quiet_end, c.timezone, c.idle_minutes, "
    "c.lookback_minutes, c.min_send_interval_minutes, c.max_per_day, "
    "c.topic_repeat_window_minutes, c.llm_model_tier, c.temperature, "
    "c.agent_tool_scope, c.version, c.updated_at, "
    "COALESCE(s.channel, '') AS channel_id, "
    "COALESCE(NULLIF(s.metadata ->> 'adapter_id', ''), '') AS adapter_id, "
    "COALESCE(NULLIF(s.metadata ->> 'connection_id', ''), '') AS connection_id, "
    "COALESCE(NULLIF(s.metadata ->> 'external_conversation_id', ''), "
    "NULLIF(s.metadata ->> 'external_session_id', ''), '') AS external_session_id"
)
_CONFIG_FROM = (
    "plugin_group_activity_config AS c LEFT JOIN sessions AS s "
    "ON s.tenant_id = c.tenant_id AND s.session_id = c.session_id"
)
_ACTIVE_ADMIN_MUTATION_CONNECTION: ContextVar[AsyncConnection | None] = ContextVar(
    "group_activity_admin_mutation_connection",
    default=None,
)


class GroupActivityConfigVersionConflictError(RuntimeError):
    def __init__(self, *, expected: int, current: int) -> None:
        super().__init__(f"expected version {expected}, current version {current}")
        self.expected = expected
        self.current = current


@dataclass(frozen=True, slots=True)
class GroupActivityConfigMutation:
    before: dict[str, Any]
    after: dict[str, Any]


def normalize_group_activity_config_values(config: dict[str, Any]) -> dict[str, Any]:
    """Map legacy rows onto the current API contract without mutating callers.

    Migration 0019 changed the ``idle_minutes`` server default from 60 to 180,
    but existing rows retained the old value.  Current API and UI validation
    rejects those rows, so an operator cannot save an otherwise valid edit.
    Normalize every numeric field whose accepted range has tightened.  The
    normalized values are persisted by the next configuration write.
    """

    normalized = dict(config)
    normalized["idle_minutes"] = _bounded_int(
        normalized.get("idle_minutes"),
        default=180,
        minimum=180,
    )
    normalized["lookback_minutes"] = _bounded_int(
        normalized.get("lookback_minutes"),
        default=120,
        minimum=60,
    )
    normalized["min_send_interval_minutes"] = _bounded_int(
        normalized.get("min_send_interval_minutes"),
        default=180,
        minimum=60,
    )
    normalized["max_per_day"] = _bounded_int(
        normalized.get("max_per_day"),
        default=1,
        minimum=1,
        maximum=3,
    )
    normalized["topic_repeat_window_minutes"] = _bounded_int(
        normalized.get("topic_repeat_window_minutes"),
        default=1440,
        minimum=60,
        maximum=10080,
    )
    normalized["temperature"] = _bounded_float(
        normalized.get("temperature"),
        default=0.9,
        minimum=0.0,
        maximum=2.0,
    )
    return normalized


def _bounded_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int | None = None,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    parsed = max(minimum, parsed)
    return min(maximum, parsed) if maximum is not None else parsed


def _bounded_float(
    value: Any,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        parsed = default
    if not math.isfinite(parsed):
        parsed = default
    return min(maximum, max(minimum, parsed))


async def _exec(
    sql: str,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    async with _write_connection() as conn:
        result = await conn.execute(text(sql), params or {})
        if result.returns_rows:
            return [dict(row._mapping) for row in result.fetchall()]
        return []


@asynccontextmanager
async def _read_connection() -> AsyncIterator[AsyncConnection]:
    active = _ACTIVE_ADMIN_MUTATION_CONNECTION.get()
    if active is not None:
        yield active
        return
    async with get_engine().connect() as conn:
        yield conn


@asynccontextmanager
async def _write_connection() -> AsyncIterator[AsyncConnection]:
    active = _ACTIVE_ADMIN_MUTATION_CONNECTION.get()
    if active is not None:
        yield active
        return
    async with get_engine().begin() as conn:
        yield conn


def prompt_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_group_activity_identity(
    settings: Any,
    *,
    tenant_id: str,
    session_id: str,
    connection_id: str = "",
    adapter_id: str = "",
    external_session_id: str = "",
) -> dict[str, str]:
    """Return one coherent wxbot group identity or fail closed.

    The process-wide legacy SDK is deliberately inferred only for its configured
    default tenant and historical, non-namespaced chatroom IDs. Managed
    connections must carry the provider's raw conversation ID so the canonical
    owner can be recomputed instead of trusting event metadata at face value.
    """

    tenant = str(tenant_id or "").strip()
    sid = str(session_id or "").strip()
    connection = str(connection_id or "").strip()
    adapter = str(adapter_id or "").strip().lower()
    external = str(external_session_id or "").strip()
    default_tenant = str(
        getattr(settings, "wxbot_default_tenant_id", "default") or "default"
    ).strip()

    if not tenant or not sid or not sid.endswith("@chatroom"):
        raise ValueError("channel_identity_mismatch")
    if not connection:
        if tenant == default_tenant and not sid.startswith("cx1:"):
            connection = LEGACY_WXBOT_CONNECTION_ID
        else:
            raise ValueError("connection_identity_unavailable")

    if connection == LEGACY_WXBOT_CONNECTION_ID:
        if tenant != default_tenant:
            raise ValueError("legacy_wxbot_history_tenant_unavailable")
        if sid.startswith("cx1:"):
            raise ValueError("channel_identity_mismatch")
        adapter = adapter or WECHAT_SDK_ADAPTER_ID
        external = external or sid
        if external != sid:
            raise ValueError("channel_identity_mismatch")
    else:
        if not external:
            raise ValueError("external_session_identity_unavailable")
        if external == sid:
            raise ValueError("channel_identity_mismatch")

    if not adapter:
        raise ValueError("adapter_identity_unavailable")
    if adapter != WECHAT_SDK_ADAPTER_ID:
        raise ValueError("wxbot_adapter_identity_mismatch")
    try:
        expected_session_id = canonical_conversation_id(connection, external)
    except ValueError as exc:
        raise ValueError("channel_identity_incomplete") from exc
    if expected_session_id != sid:
        raise ValueError("channel_identity_mismatch")
    return {
        "adapter_id": adapter,
        "connection_id": connection,
        "external_session_id": external,
    }


class GroupActivityStore:
    def __init__(self, settings: Any) -> None:
        self.settings = settings

    async def ensure_tables(self) -> None:
        await verify_runtime_schema(get_engine(), component="group activity store")
        logger.info("group_activity.schema_verified")

    @contextmanager
    def independent_runtime_connections(self) -> Iterator[None]:
        """Keep concurrent scheduler workers off one inherited admin connection."""

        token = _ACTIVE_ADMIN_MUTATION_CONNECTION.set(None)
        try:
            yield
        finally:
            _ACTIVE_ADMIN_MUTATION_CONNECTION.reset(token)

    async def run_admin_mutation(
        self,
        *,
        identity: MutationIdentity,
        audit: MutationAudit,
        mutate: Callable[[], Awaitable[MutationChange]],
    ) -> MutationOutcome:
        """Run an admin-triggered send in the durable mutation transaction."""

        async with get_engine().begin() as conn:
            token = _ACTIVE_ADMIN_MUTATION_CONNECTION.set(conn)
            try:
                return await run_idempotent_mutation(
                    conn,
                    identity=identity,
                    audit=audit,
                    mutate=mutate,
                )
            finally:
                _ACTIVE_ADMIN_MUTATION_CONNECTION.reset(token)

    def default_config(self, tenant_id: str, session_id: str) -> dict[str, Any]:
        return {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "session_name": "",
            "enabled": False,
            "active_start": "08:00",
            "active_end": "17:00",
            "quiet_start": "23:00",
            "quiet_end": "08:00",
            "timezone": "Asia/Shanghai",
            "idle_minutes": 180,
            "lookback_minutes": 120,
            "min_send_interval_minutes": 180,
            "max_per_day": 1,
            "topic_repeat_window_minutes": 1440,
            "llm_model_tier": "tier-2",
            "temperature": 0.9,
            "agent_tool_scope": DEFAULT_AGENT_SCOPE,
            "channel_id": "",
            "adapter_id": "",
            "connection_id": "",
            "external_session_id": "",
            "version": 0,
            "updated_at": None,
        }

    async def get_config(self, tenant_id: str, session_id: str) -> dict[str, Any]:
        async with _read_connection() as conn:
            row = await _config_row(conn, tenant_id, session_id, for_update=False)
        if row:
            return row
        return self.default_config(tenant_id, session_id)

    async def set_config(
        self,
        tenant_id: str,
        session_id: str,
        *,
        expected_version: int,
        session_name: str | None = None,
        enabled: bool | None = None,
        active_start: str | None = None,
        active_end: str | None = None,
        quiet_start: str | None = None,
        quiet_end: str | None = None,
        timezone: str | None = None,
        idle_minutes: int | None = None,
        lookback_minutes: int | None = None,
        min_send_interval_minutes: int | None = None,
        max_per_day: int | None = None,
        topic_repeat_window_minutes: int | None = None,
        llm_model_tier: str | None = None,
        temperature: float | None = None,
        agent_tool_scope: str | None = None,
    ) -> GroupActivityConfigMutation:
        updates = {
            "session_name": session_name,
            "enabled": enabled,
            "active_start": active_start,
            "active_end": active_end,
            "quiet_start": quiet_start,
            "quiet_end": quiet_end,
            "timezone": timezone,
            "idle_minutes": idle_minutes,
            "lookback_minutes": lookback_minutes,
            "min_send_interval_minutes": min_send_interval_minutes,
            "max_per_day": max_per_day,
            "topic_repeat_window_minutes": topic_repeat_window_minutes,
            "llm_model_tier": llm_model_tier,
            "temperature": temperature,
            "agent_tool_scope": agent_tool_scope,
        }
        async with _write_connection() as conn:
            row = await _config_row(conn, tenant_id, session_id, for_update=True)
            before = row or self.default_config(tenant_id, session_id)
            current_version = int(before.get("version") or 0)
            if current_version != expected_version:
                raise GroupActivityConfigVersionConflictError(
                    expected=expected_version,
                    current=current_version,
                )
            current = dict(before)
            for key, value in updates.items():
                if value is not None:
                    current[key] = value
            params = _config_params(current, expected_version=expected_version)
            if row is None:
                result = await conn.execute(
                    text(
                        "INSERT INTO plugin_group_activity_config "
                        "(tenant_id, session_id, session_name, enabled, active_start, active_end, "
                        "quiet_start, quiet_end, timezone, idle_minutes, lookback_minutes, "
                        "min_send_interval_minutes, max_per_day, topic_repeat_window_minutes, "
                        "llm_model_tier, temperature, agent_tool_scope, version, updated_at) "
                        "VALUES (:tid, :sid, :session_name, :enabled, :active_start, :active_end, "
                        ":quiet_start, :quiet_end, :timezone, :idle_minutes, :lookback_minutes, "
                        ":min_interval, :max_per_day, :topic_repeat_window_minutes, "
                        ":llm_model_tier, :temperature, :agent_tool_scope, 1, NOW()) "
                        "ON CONFLICT (tenant_id, session_id) DO NOTHING "
                        f"RETURNING {_CONFIG_COLUMNS}"
                    ),
                    params,
                )
            else:
                result = await conn.execute(
                    text(
                        "UPDATE plugin_group_activity_config SET "
                        "session_name = :session_name, enabled = :enabled, "
                        "active_start = :active_start, active_end = :active_end, "
                        "quiet_start = :quiet_start, quiet_end = :quiet_end, "
                        "timezone = :timezone, idle_minutes = :idle_minutes, "
                        "lookback_minutes = :lookback_minutes, "
                        "min_send_interval_minutes = :min_interval, "
                        "max_per_day = :max_per_day, "
                        "topic_repeat_window_minutes = :topic_repeat_window_minutes, "
                        "llm_model_tier = :llm_model_tier, temperature = :temperature, "
                        "agent_tool_scope = :agent_tool_scope, version = version + 1, "
                        "updated_at = NOW() "
                        "WHERE tenant_id = :tid AND session_id = :sid "
                        "AND version = :expected_version "
                        f"RETURNING {_CONFIG_COLUMNS}"
                    ),
                    params,
                )
            written = result.mappings().first()
            if written is None:
                latest = await _config_row(conn, tenant_id, session_id, for_update=True)
                raise GroupActivityConfigVersionConflictError(
                    expected=expected_version,
                    current=int((latest or {}).get("version") or 0),
                )
            after = await _config_row(conn, tenant_id, session_id, for_update=False)
            if after is None:  # pragma: no cover - guarded by RETURNING above
                raise RuntimeError("group_activity_config_write_not_visible")
        return GroupActivityConfigMutation(before=dict(before), after=after)

    async def upsert_candidate(
        self,
        tenant_id: str,
        session_id: str,
        *,
        session_name: str = "",
        connection_id: str = "",
        adapter_id: str = "",
        external_session_id: str = "",
    ) -> dict[str, Any]:
        identity = normalize_group_activity_identity(
            self.settings,
            tenant_id=tenant_id,
            session_id=session_id,
            connection_id=connection_id,
            adapter_id=adapter_id,
            external_session_id=external_session_id,
        )
        current = await self.get_config(tenant_id, session_id)
        name = str(session_name or current.get("session_name") or "")[:256]
        await _exec(
            "INSERT INTO plugin_group_activity_config "
            "(tenant_id, session_id, session_name, enabled, version, updated_at) "
            "VALUES (:tid, :sid, :session_name, FALSE, 1, NOW()) "
            "ON CONFLICT (tenant_id, session_id) DO UPDATE SET "
            "session_name = CASE WHEN :session_name <> '' THEN :session_name "
            "ELSE plugin_group_activity_config.session_name END, "
            "version = CASE WHEN :session_name <> '' AND "
            "plugin_group_activity_config.session_name IS DISTINCT FROM :session_name "
            "THEN plugin_group_activity_config.version + 1 "
            "ELSE plugin_group_activity_config.version END, "
            "updated_at = CASE WHEN :session_name <> '' AND "
            "plugin_group_activity_config.session_name IS DISTINCT FROM :session_name "
            "THEN NOW() ELSE plugin_group_activity_config.updated_at END",
            {"tid": tenant_id, "sid": session_id, "session_name": name},
        )
        # The hook copies this normalized identity into ctx.session before the
        # orchestrator stages its user turn. Config reads join that canonical
        # session owner; overlay it here so the immediate result is consistent.
        config = await self.get_config(tenant_id, session_id)
        config.update(identity)
        return config

    async def list_configs(
        self,
        tenant_id: str,
        *,
        enabled: bool | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if enabled is None:
            rows = await _exec(
                f"SELECT {_CONFIG_SELECT_COLUMNS} FROM {_CONFIG_FROM} "
                "WHERE c.tenant_id = :tid "
                "ORDER BY c.updated_at DESC LIMIT :lim",
                {"tid": tenant_id, "lim": max(1, min(limit, 500))},
            )
        else:
            rows = await _exec(
                f"SELECT {_CONFIG_SELECT_COLUMNS} FROM {_CONFIG_FROM} "
                "WHERE c.tenant_id = :tid AND c.enabled = :enabled "
                "ORDER BY c.updated_at DESC LIMIT :lim",
                {
                    "tid": tenant_id,
                    "enabled": bool(enabled),
                    "lim": max(1, min(limit, 500)),
                },
            )
        return [normalize_group_activity_config_values(row) for row in rows]

    async def list_enabled_configs(self, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = await _exec(
            f"SELECT {_CONFIG_SELECT_COLUMNS} FROM {_CONFIG_FROM} "
            "WHERE c.enabled = TRUE "
            "ORDER BY c.updated_at DESC LIMIT :lim",
            {"lim": max(1, min(limit, 1000))},
        )
        return [normalize_group_activity_config_values(row) for row in rows]

    async def try_create_event(
        self,
        *,
        tenant_id: str,
        session_id: str,
        session_name: str,
        slot_key: str,
        last_user_message_ts: int,
        message_count: int,
        trace_id: str,
    ) -> dict[str, Any] | None:
        rows = await _exec(
            "INSERT INTO plugin_group_activity_event "
            "(tenant_id, session_id, session_name, slot_key, status, last_user_message_ts, message_count, trace_id, updated_at) "
            "VALUES (:tid, :sid, :session_name, :slot_key, 'pending', :last_ts, :message_count, :trace_id, NOW()) "
            "ON CONFLICT (tenant_id, session_id, slot_key) DO NOTHING "
            "RETURNING id, tenant_id, session_id, session_name, slot_key, status, trace_id",
            {
                "tid": tenant_id,
                "sid": session_id,
                "session_name": session_name[:256],
                "slot_key": slot_key,
                "last_ts": int(last_user_message_ts or 0),
                "message_count": int(message_count or 0),
                "trace_id": trace_id,
            },
        )
        return rows[0] if rows else None

    async def try_start_event(self, event_id: int) -> dict[str, Any] | None:
        rows = await _exec(
            "UPDATE plugin_group_activity_event SET status = 'running', updated_at = NOW() "
            "WHERE id = :id AND status = 'pending' "
            "RETURNING id, tenant_id, session_id, session_name, slot_key, status, trace_id",
            {"id": int(event_id)},
        )
        return rows[0] if rows else None

    async def complete_event(
        self,
        event_id: int,
        *,
        generated_text: str,
        reply_queue_id: int | None,
        command_id: str,
        prompt_text: str,
    ) -> dict[str, Any] | None:
        rows = await _exec(
            "UPDATE plugin_group_activity_event SET status = 'completed', generated_text = :text, "
            "reply_queue_id = :reply_queue_id, command_id = :command_id, prompt_hash = :prompt_hash, "
            "reason_code = 'queued', updated_at = NOW(), completed_at = NOW() WHERE id = :id "
            "RETURNING id, status, reason_code, reply_queue_id, generated_text, command_id",
            {
                "id": int(event_id),
                "text": generated_text[:1000],
                "reply_queue_id": reply_queue_id,
                "command_id": command_id,
                "prompt_hash": prompt_hash(prompt_text),
            },
        )
        return rows[0] if rows else None

    async def mark_event(
        self,
        event_id: int,
        *,
        status: str,
        reason_code: str = "",
        error: str = "",
        generated_text: str = "",
    ) -> None:
        completed_expr = "NOW()" if status in {"completed", "skipped", "failed"} else "NULL"
        await _exec(
            "UPDATE plugin_group_activity_event SET status = :status, reason_code = :reason_code, "
            "error = :error, "
            "generated_text = CASE WHEN :generated_text <> '' THEN :generated_text ELSE generated_text END, "
            f"updated_at = NOW(), completed_at = {completed_expr} WHERE id = :id",
            {
                "id": int(event_id),
                "status": str(status or "failed")[:16],
                "reason_code": str(reason_code or "")[:64],
                "error": str(error or "")[:2000],
                "generated_text": str(generated_text or "")[:1000],
            },
        )

    async def recent_event_exists(
        self,
        tenant_id: str,
        session_id: str,
        *,
        minutes: int,
    ) -> bool:
        rows = await _exec(
            "SELECT id FROM plugin_group_activity_event "
            "WHERE tenant_id = :tid AND session_id = :sid "
            "AND status IN ('pending', 'running', 'completed') "
            "AND created_at >= NOW() - make_interval(mins => :minutes) "
            "ORDER BY created_at DESC LIMIT 1",
            {"tid": tenant_id, "sid": session_id, "minutes": max(1, int(minutes))},
        )
        return bool(rows)

    async def count_completed_today(self, tenant_id: str, session_id: str, *, timezone: str) -> int:
        rows = await _exec(
            "SELECT COUNT(*) AS count FROM plugin_group_activity_event "
            "WHERE tenant_id = :tid AND session_id = :sid AND status = 'completed' "
            "AND (created_at AT TIME ZONE :tz)::date = (NOW() AT TIME ZONE :tz)::date",
            {"tid": tenant_id, "sid": session_id, "tz": timezone or "Asia/Shanghai"},
        )
        return int((rows[0] if rows else {}).get("count") or 0)

    async def has_completed_event(self, tenant_id: str, session_id: str) -> bool:
        rows = await _exec(
            "SELECT id FROM plugin_group_activity_event "
            "WHERE tenant_id = :tid AND session_id = :sid AND status = 'completed' "
            "ORDER BY completed_at DESC NULLS LAST LIMIT 1",
            {"tid": tenant_id, "sid": session_id},
        )
        return bool(rows)

    async def latest_completed_event(
        self,
        tenant_id: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        rows = await _exec(
            "SELECT id, last_user_message_ts, completed_at FROM plugin_group_activity_event "
            "WHERE tenant_id = :tid AND session_id = :sid AND status = 'completed' "
            "ORDER BY completed_at DESC NULLS LAST, id DESC LIMIT 1",
            {"tid": tenant_id, "sid": session_id},
        )
        return rows[0] if rows else None

    async def list_recent_generated_texts(
        self,
        tenant_id: str,
        session_id: str,
        *,
        minutes: int,
        limit: int = 20,
    ) -> list[str]:
        rows = await _exec(
            "SELECT generated_text FROM plugin_group_activity_event "
            "WHERE tenant_id = :tid AND session_id = :sid AND status = 'completed' "
            "AND generated_text <> '' "
            "AND completed_at >= NOW() - make_interval(mins => :minutes) "
            "ORDER BY completed_at DESC LIMIT :lim",
            {
                "tid": tenant_id,
                "sid": session_id,
                "minutes": max(1, int(minutes)),
                "lim": max(1, min(int(limit), 100)),
            },
        )
        return [str(row.get("generated_text") or "") for row in rows if row.get("generated_text")]

    async def list_events(
        self,
        tenant_id: str,
        *,
        session_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id = :tid"]
        params: dict[str, Any] = {"tid": tenant_id, "lim": max(1, min(limit, 200))}
        if session_id:
            conditions.append("session_id = :sid")
            params["sid"] = session_id
        if status:
            conditions.append("status = :status")
            params["status"] = status
        where = " AND ".join(conditions)
        return await _exec(
            "SELECT id, tenant_id, session_id, session_name, slot_key, status, last_user_message_ts, "
            "message_count, generated_text, reply_queue_id, command_id, trace_id, reason_code, error, "
            "created_at, updated_at, completed_at FROM plugin_group_activity_event "
            f"WHERE {where} ORDER BY created_at DESC LIMIT :lim",
            params,
        )


def _config_params(config: dict[str, Any], *, expected_version: int) -> dict[str, Any]:
    config = normalize_group_activity_config_values(config)
    return {
        "tid": str(config.get("tenant_id") or ""),
        "sid": str(config.get("session_id") or ""),
        "session_name": str(config.get("session_name") or "")[:256],
        "enabled": bool(config.get("enabled")),
        "active_start": str(config.get("active_start") or "08:00"),
        "active_end": str(config.get("active_end") or "17:00"),
        "quiet_start": str(config.get("quiet_start") or "23:00"),
        "quiet_end": str(config.get("quiet_end") or "08:00"),
        "timezone": str(config.get("timezone") or "Asia/Shanghai"),
        "idle_minutes": int(config.get("idle_minutes") or 180),
        "lookback_minutes": int(config.get("lookback_minutes") or 120),
        "min_interval": int(config.get("min_send_interval_minutes") or 180),
        "max_per_day": int(config.get("max_per_day") or 1),
        "topic_repeat_window_minutes": int(
            config.get("topic_repeat_window_minutes") or 1440
        ),
        "llm_model_tier": str(config.get("llm_model_tier") or "tier-2"),
        "temperature": float(
            0.9 if config.get("temperature") is None else config["temperature"]
        ),
        "agent_tool_scope": str(config.get("agent_tool_scope") or DEFAULT_AGENT_SCOPE),
        "expected_version": int(expected_version),
    }


async def _config_row(
    conn: AsyncConnection,
    tenant_id: str,
    session_id: str,
    *,
    for_update: bool,
) -> dict[str, Any] | None:
    suffix = " FOR UPDATE OF c" if for_update else ""
    result = await conn.execute(
        text(
            f"SELECT {_CONFIG_SELECT_COLUMNS} FROM {_CONFIG_FROM} "
            "WHERE c.tenant_id = :tid AND c.session_id = :sid"
            f"{suffix}"
        ),
        {"tid": tenant_id, "sid": session_id},
    )
    row = result.mappings().first()
    return normalize_group_activity_config_values(dict(row)) if row is not None else None
