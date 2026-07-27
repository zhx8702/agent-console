"""
Moderation data persistence — keywords, config, and event logging.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
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
from app.common.logging import get_logger
from app.infra.db import get_engine
from app.infra.runtime_schema import verify_runtime_schema

logger = get_logger(__name__)

DEFAULT_REMINDER_TEXT = "检测到命中审核关键词，请谨慎表述。"
VALID_REMINDER_MODES = {"off", "append", "replace"}
_CONFIG_COLUMNS = (
    "tenant_id, session_id, enabled, webhook_url, webhook_enabled, "
    "reminder_mode, reminder_text, version, updated_at"
)
_ACTIVE_ADMIN_MUTATION_CONNECTION: ContextVar[AsyncConnection | None] = ContextVar(
    "moderation_admin_mutation_connection",
    default=None,
)


class ModerationConfigVersionConflictError(RuntimeError):
    def __init__(self, *, expected: int, current: int) -> None:
        super().__init__(f"expected version {expected}, current version {current}")
        self.expected = expected
        self.current = current


@dataclass(frozen=True, slots=True)
class ModerationConfigMutation:
    before: dict[str, Any]
    after: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ModerationKeywordMutation:
    before: list[dict[str, Any]]
    after: list[dict[str, Any]]
    version: int


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
async def _write_connection() -> AsyncIterator[AsyncConnection]:
    active = _ACTIVE_ADMIN_MUTATION_CONNECTION.get()
    if active is not None:
        yield active
        return
    async with get_engine().begin() as conn:
        yield conn


def _clean_text(value: object, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _normalize_mode(value: object) -> str:
    mode = _clean_text(value, "off").lower() or "off"
    if mode not in VALID_REMINDER_MODES:
        return "off"
    return mode


def _default_config(tenant_id: str, session_id: str) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "session_id": session_id,
        "enabled": False,
        "webhook_url": "",
        "webhook_enabled": False,
        "reminder_mode": "off",
        "reminder_text": DEFAULT_REMINDER_TEXT,
        "version": 0,
        "updated_at": None,
    }


def _normalize_config_row(row: dict[str, Any] | None, tenant_id: str, session_id: str) -> dict[str, Any]:
    merged = dict(_default_config(tenant_id, session_id))
    if row:
        merged.update(row)
    merged["enabled"] = bool(merged.get("enabled"))
    merged["webhook_enabled"] = bool(merged.get("webhook_enabled"))
    merged["webhook_url"] = _clean_text(merged.get("webhook_url"))
    merged["reminder_mode"] = _normalize_mode(merged.get("reminder_mode"))
    merged["reminder_text"] = _clean_text(
        merged.get("reminder_text"),
        DEFAULT_REMINDER_TEXT,
    ) or DEFAULT_REMINDER_TEXT
    merged["updated_at"] = _isoformat(merged.get("updated_at"))
    merged["version"] = int(merged.get("version") or 0) if row else 0
    return merged


def _keyword_list(value: object) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        parts = value
    else:
        parts = str(value).split(",")
    cleaned: list[str] = []
    for part in parts:
        item = _clean_text(part)
        if item:
            cleaned.append(item)
    return cleaned


def _isoformat(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
    return str(value)


def _normalize_event_row(row: dict[str, Any]) -> dict[str, Any]:
    matched = _keyword_list(row.get("matched_keywords"))
    message_text = str(row.get("message_text") or "")
    row["matched_keyword_list"] = matched
    row["session_name"] = _clean_text(row.get("session_name"))
    row["sender_name"] = _clean_text(row.get("sender_name"))
    row["message_preview"] = message_text[:120]
    row["created_at"] = _isoformat(row.get("created_at"))
    return row


def _normalize_session_row(row: dict[str, Any]) -> dict[str, Any]:
    row["enabled"] = bool(row.get("enabled"))
    row["keyword_count"] = int(row.get("keyword_count") or 0)
    row["event_count"] = int(row.get("event_count") or 0)
    row["session_name"] = _clean_text(row.get("session_name")) or _clean_text(row.get("session_id"))
    row["last_event_at"] = _isoformat(row.get("last_event_at"))
    row["updated_at"] = _isoformat(row.get("updated_at"))
    row["version"] = int(row.get("version") or 0)
    return row


class ModerationStore:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            await self.ensure_tables()
            self._schema_ready = True

    async def ensure_tables(self) -> None:
        await verify_runtime_schema(get_engine(), component="moderation store")
        logger.info("moderation.schema_verified")

    async def run_admin_mutation(
        self,
        *,
        identity: MutationIdentity,
        audit: MutationAudit,
        mutate: Callable[[], Awaitable[MutationChange]],
    ) -> MutationOutcome:
        """Bind a destructive keyword mutation to its replay/audit ledger."""

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

    async def get_config(self, tenant_id: str, session_id: str) -> dict[str, Any]:
        await self._ensure_schema()
        rows = await _exec(
            f"SELECT {_CONFIG_COLUMNS} FROM plugin_moderation_config "
            "WHERE tenant_id = :tid AND session_id = :sid",
            {"tid": tenant_id, "sid": session_id},
        )
        return _normalize_config_row(rows[0] if rows else None, tenant_id, session_id)

    async def set_config(
        self,
        tenant_id: str,
        session_id: str,
        *,
        expected_version: int,
        **kwargs: Any,
    ) -> ModerationConfigMutation:
        await self._ensure_schema()
        async with _write_connection() as conn:
            row = await _config_row(conn, tenant_id, session_id, for_update=True)
            before = _normalize_config_row(row, tenant_id, session_id)
            _check_version(before, expected_version)
            next_cfg = dict(before)
            if "enabled" in kwargs:
                next_cfg["enabled"] = bool(kwargs["enabled"])
            if "webhook_url" in kwargs:
                next_cfg["webhook_url"] = _clean_text(kwargs["webhook_url"])
            if "webhook_enabled" in kwargs:
                next_cfg["webhook_enabled"] = bool(kwargs["webhook_enabled"])
            if "reminder_mode" in kwargs:
                next_cfg["reminder_mode"] = _normalize_mode(kwargs["reminder_mode"])
            if "reminder_text" in kwargs:
                next_cfg["reminder_text"] = _clean_text(
                    kwargs["reminder_text"],
                    DEFAULT_REMINDER_TEXT,
                ) or DEFAULT_REMINDER_TEXT
            written = await _write_config(
                conn,
                next_cfg,
                expected_version=expected_version,
                row_exists=row is not None,
            )
            if written is None:
                latest = await _config_row(conn, tenant_id, session_id, for_update=True)
                raise ModerationConfigVersionConflictError(
                    expected=expected_version,
                    current=int((latest or {}).get("version") or 0),
                )
            after = _normalize_config_row(written, tenant_id, session_id)
        return ModerationConfigMutation(before=before, after=after)

    async def get_keywords(
        self, tenant_id: str, session_id: str, enabled_only: bool = False
    ) -> list[dict[str, Any]]:
        rows, _version = await self.get_keywords_resource(
            tenant_id,
            session_id,
            enabled_only=enabled_only,
        )
        return rows

    async def get_keywords_resource(
        self,
        tenant_id: str,
        session_id: str,
        *,
        enabled_only: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        await self._ensure_schema()
        rows = await _exec(
            """
            SELECT COALESCE(cfg.version, 0) AS version,
                   COALESCE(
                       jsonb_agg(
                           jsonb_build_object(
                               'id', kw.id,
                               'keyword', kw.keyword,
                               'enabled', kw.enabled,
                               'created_at', kw.created_at
                           )
                           ORDER BY LOWER(kw.keyword), kw.id
                       ) FILTER (WHERE kw.id IS NOT NULL),
                       '[]'::jsonb
                   ) AS items
            FROM (
                SELECT CAST(:tid AS VARCHAR) AS tenant_id,
                       CAST(:sid AS VARCHAR) AS session_id
            ) scope
            LEFT JOIN plugin_moderation_config cfg
              ON cfg.tenant_id = scope.tenant_id
             AND cfg.session_id = scope.session_id
            LEFT JOIN plugin_moderation_keywords kw
              ON kw.tenant_id = scope.tenant_id
             AND kw.session_id = scope.session_id
             AND (:enabled_only = FALSE OR kw.enabled = TRUE)
            GROUP BY cfg.version
            """,
            {
                "tid": tenant_id,
                "sid": session_id,
                "enabled_only": bool(enabled_only),
            },
        )
        resource: dict[str, Any] = rows[0] if rows else {"version": 0, "items": []}
        items: Any = resource.get("items") or []
        if isinstance(items, str):
            items = json.loads(items)
        return _normalize_keyword_rows(list(items)), int(resource.get("version") or 0)

    async def add_keyword(
        self,
        tenant_id: str,
        session_id: str,
        keyword: str,
        *,
        expected_version: int,
    ) -> ModerationKeywordMutation:
        return await self.upsert_keywords(
            tenant_id,
            session_id,
            [{"keyword": keyword, "enabled": True}],
            replace=False,
            expected_version=expected_version,
        )

    async def upsert_keywords(
        self,
        tenant_id: str,
        session_id: str,
        entries: Iterable[dict[str, Any]],
        *,
        replace: bool = False,
        expected_version: int,
    ) -> ModerationKeywordMutation:
        await self._ensure_schema()
        cleaned: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in entries:
            keyword = _clean_text(entry.get("keyword"))
            if not keyword or keyword in seen:
                continue
            seen.add(keyword)
            cleaned.append({
                "keyword": keyword,
                "enabled": bool(entry.get("enabled", True)),
            })

        async with _write_connection() as conn:
            config_row = await _config_row(conn, tenant_id, session_id, for_update=True)
            before_config = _normalize_config_row(config_row, tenant_id, session_id)
            _check_version(before_config, expected_version)
            before = await _keyword_rows(conn, tenant_id, session_id)
            advanced = await _write_config(
                conn,
                before_config,
                expected_version=expected_version,
                row_exists=config_row is not None,
            )
            if advanced is None:
                latest = await _config_row(conn, tenant_id, session_id, for_update=True)
                raise ModerationConfigVersionConflictError(
                    expected=expected_version,
                    current=int((latest or {}).get("version") or 0),
                )
            if replace:
                await conn.execute(
                    text(
                        "DELETE FROM plugin_moderation_keywords "
                        "WHERE tenant_id = :tid AND session_id = :sid"
                    ),
                    {"tid": tenant_id, "sid": session_id},
                )
            for entry in cleaned:
                await conn.execute(
                    text(
                        "INSERT INTO plugin_moderation_keywords "
                        "(tenant_id, session_id, keyword, enabled) "
                        "VALUES (:tid, :sid, :keyword, :enabled) "
                        "ON CONFLICT (tenant_id, session_id, keyword) DO UPDATE SET "
                        "enabled = EXCLUDED.enabled"
                    ),
                    {
                        "tid": tenant_id,
                        "sid": session_id,
                        "keyword": entry["keyword"],
                        "enabled": bool(entry["enabled"]),
                    },
                )
            after = await _keyword_rows(conn, tenant_id, session_id)
        return ModerationKeywordMutation(
            before=before,
            after=after,
            version=int(advanced["version"]),
        )

    async def remove_keyword(
        self,
        tenant_id: str,
        session_id: str,
        keyword: str,
        *,
        expected_version: int,
    ) -> ModerationKeywordMutation:
        return await self.remove_keywords(
            tenant_id,
            session_id,
            [keyword],
            expected_version=expected_version,
        )

    async def remove_keywords(
        self,
        tenant_id: str,
        session_id: str,
        keywords: list[str] | None = None,
        *,
        expected_version: int,
    ) -> ModerationKeywordMutation:
        await self._ensure_schema()
        cleaned = [_clean_text(keyword) for keyword in keywords or []]
        cleaned = [keyword for keyword in cleaned if keyword]
        async with _write_connection() as conn:
            config_row = await _config_row(conn, tenant_id, session_id, for_update=True)
            before_config = _normalize_config_row(config_row, tenant_id, session_id)
            _check_version(before_config, expected_version)
            before = await _keyword_rows(conn, tenant_id, session_id)
            advanced = await _write_config(
                conn,
                before_config,
                expected_version=expected_version,
                row_exists=config_row is not None,
            )
            if advanced is None:
                latest = await _config_row(conn, tenant_id, session_id, for_update=True)
                raise ModerationConfigVersionConflictError(
                    expected=expected_version,
                    current=int((latest or {}).get("version") or 0),
                )
            if not cleaned:
                await conn.execute(
                    text(
                        "DELETE FROM plugin_moderation_keywords "
                        "WHERE tenant_id = :tid AND session_id = :sid"
                    ),
                    {"tid": tenant_id, "sid": session_id},
                )
            else:
                params: dict[str, Any] = {"tid": tenant_id, "sid": session_id}
                placeholders: list[str] = []
                for index, keyword in enumerate(cleaned):
                    key = f"keyword_{index}"
                    placeholders.append(f":{key}")
                    params[key] = keyword
                await conn.execute(
                    text(
                        "DELETE FROM plugin_moderation_keywords "
                        "WHERE tenant_id = :tid AND session_id = :sid "
                        f"AND keyword IN ({', '.join(placeholders)})"
                    ),
                    params,
                )
            after = await _keyword_rows(conn, tenant_id, session_id)
        return ModerationKeywordMutation(
            before=before,
            after=after,
            version=int(advanced["version"]),
        )

    async def match_keywords(self, tenant_id: str, session_id: str, text: str) -> list[str]:
        await self._ensure_schema()
        keywords = await self.get_keywords(tenant_id, session_id, enabled_only=True)
        text_lower = text.lower()
        return [kw["keyword"] for kw in keywords if kw["keyword"].lower() in text_lower]

    async def log_event(
        self,
        tenant_id: str,
        session_id: str,
        user_id: str,
        message_text: str,
        matched: list[str],
        trace_id: str = "",
        action: str = "flagged",
        webhook_status: str = "",
        session_name: str = "",
        sender_name: str = "",
    ) -> int:
        await self._ensure_schema()
        rows = await _exec(
            "INSERT INTO plugin_moderation_events "
            "(tenant_id, session_id, session_name, user_id, sender_name, message_text, "
            "matched_keywords, action, webhook_status, trace_id) "
            "VALUES (:tid, :sid, :session_name, :uid, :sender_name, :msg, :kws, "
            ":act, :webhook, :trc) "
            "RETURNING id",
            {
                "tid": tenant_id,
                "sid": session_id,
                "session_name": _clean_text(session_name),
                "uid": user_id,
                "sender_name": _clean_text(sender_name),
                "msg": message_text[:2000],
                "kws": ", ".join(matched),
                "act": action,
                "webhook": webhook_status,
                "trc": trace_id,
            },
        )
        return int(rows[0]["id"])

    async def update_event(
        self,
        event_id: int,
        *,
        action: str | None = None,
        webhook_status: str | None = None,
    ) -> None:
        await self._ensure_schema()
        updates: list[str] = []
        params: dict[str, object] = {"id": event_id}
        if action is not None:
            updates.append("action = :action")
            params["action"] = action
        if webhook_status is not None:
            updates.append("webhook_status = :webhook_status")
            params["webhook_status"] = webhook_status
        if not updates:
            return
        await _exec(
            f"UPDATE plugin_moderation_events SET {', '.join(updates)} WHERE id = :id",
            params,
        )

    async def get_events(
        self,
        tenant_id: str,
        session_id: str | None = None,
        *,
        action: str = "",
        webhook_status: str = "",
        keyword: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        await self._ensure_schema()
        clauses = ["tenant_id = :tid"]
        params: dict[str, Any] = {"tid": tenant_id, "lim": limit}
        if session_id:
            clauses.append("session_id = :sid")
            params["sid"] = session_id
        if action:
            clauses.append("action = :action")
            params["action"] = action
        if webhook_status:
            clauses.append("webhook_status = :webhook_status")
            params["webhook_status"] = webhook_status
        if keyword:
            clauses.append("matched_keywords ILIKE :keyword")
            params["keyword"] = f"%{_clean_text(keyword)}%"

        rows = await _exec(
            "SELECT * FROM plugin_moderation_events "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC, id DESC LIMIT :lim",
            params,
        )
        return [_normalize_event_row(row) for row in rows]

    async def list_sessions(self, tenant_id: str, limit: int = 200) -> list[dict[str, Any]]:
        await self._ensure_schema()
        rows = await _exec(
            """
            WITH session_ids AS (
                SELECT session_id
                FROM plugin_moderation_config
                WHERE tenant_id = :tid
                UNION
                SELECT session_id
                FROM plugin_moderation_keywords
                WHERE tenant_id = :tid
                UNION
                SELECT session_id
                FROM plugin_moderation_events
                WHERE tenant_id = :tid
            ),
            keyword_stats AS (
                SELECT session_id, COUNT(*)::INT AS keyword_count
                FROM plugin_moderation_keywords
                WHERE tenant_id = :tid
                GROUP BY session_id
            ),
            event_stats AS (
                SELECT session_id, COUNT(*)::INT AS event_count, MAX(created_at) AS last_event_at
                FROM plugin_moderation_events
                WHERE tenant_id = :tid
                GROUP BY session_id
            ),
            latest_event AS (
                SELECT DISTINCT ON (session_id)
                    session_id,
                    session_name,
                    created_at
                FROM plugin_moderation_events
                WHERE tenant_id = :tid
                ORDER BY session_id, created_at DESC, id DESC
            )
            SELECT
                ids.session_id,
                COALESCE(le.session_name, ids.session_id) AS session_name,
                COALESCE(cfg.enabled, FALSE) AS enabled,
                COALESCE(cfg.version, 0) AS version,
                COALESCE(ks.keyword_count, 0) AS keyword_count,
                COALESCE(es.event_count, 0) AS event_count,
                es.last_event_at,
                cfg.updated_at
            FROM session_ids ids
            LEFT JOIN plugin_moderation_config cfg
                ON cfg.tenant_id = :tid AND cfg.session_id = ids.session_id
            LEFT JOIN keyword_stats ks
                ON ks.session_id = ids.session_id
            LEFT JOIN event_stats es
                ON es.session_id = ids.session_id
            LEFT JOIN latest_event le
                ON le.session_id = ids.session_id
            ORDER BY COALESCE(es.last_event_at, cfg.updated_at) DESC NULLS LAST, ids.session_id
            LIMIT :lim
            """,
            {"tid": tenant_id, "lim": limit},
        )
        return [_normalize_session_row(row) for row in rows]


def _check_version(config: dict[str, Any], expected_version: int) -> None:
    current_version = int(config.get("version") or 0)
    if current_version != expected_version:
        raise ModerationConfigVersionConflictError(
            expected=expected_version,
            current=current_version,
        )


async def _config_row(
    conn: AsyncConnection,
    tenant_id: str,
    session_id: str,
    *,
    for_update: bool,
) -> dict[str, Any] | None:
    suffix = " FOR UPDATE" if for_update else ""
    result = await conn.execute(
        text(
            f"SELECT {_CONFIG_COLUMNS} FROM plugin_moderation_config "
            "WHERE tenant_id = :tid AND session_id = :sid"
            f"{suffix}"
        ),
        {"tid": tenant_id, "sid": session_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def _write_config(
    conn: AsyncConnection,
    config: dict[str, Any],
    *,
    expected_version: int,
    row_exists: bool,
) -> dict[str, Any] | None:
    params = {
        "tid": str(config.get("tenant_id") or ""),
        "sid": str(config.get("session_id") or ""),
        "enabled": bool(config.get("enabled")),
        "webhook_url": _clean_text(config.get("webhook_url")),
        "webhook_enabled": bool(config.get("webhook_enabled")),
        "reminder_mode": _normalize_mode(config.get("reminder_mode")),
        "reminder_text": _clean_text(
            config.get("reminder_text"),
            DEFAULT_REMINDER_TEXT,
        )
        or DEFAULT_REMINDER_TEXT,
        "expected_version": int(expected_version),
    }
    if row_exists:
        result = await conn.execute(
            text(
                "UPDATE plugin_moderation_config SET "
                "enabled = :enabled, webhook_url = :webhook_url, "
                "webhook_enabled = :webhook_enabled, reminder_mode = :reminder_mode, "
                "reminder_text = :reminder_text, version = version + 1, "
                "updated_at = NOW() "
                "WHERE tenant_id = :tid AND session_id = :sid "
                "AND version = :expected_version "
                f"RETURNING {_CONFIG_COLUMNS}"
            ),
            params,
        )
    else:
        result = await conn.execute(
            text(
                "INSERT INTO plugin_moderation_config "
                "(tenant_id, session_id, enabled, webhook_url, webhook_enabled, "
                "reminder_mode, reminder_text, version, updated_at) "
                "VALUES (:tid, :sid, :enabled, :webhook_url, :webhook_enabled, "
                ":reminder_mode, :reminder_text, 1, NOW()) "
                "ON CONFLICT (tenant_id, session_id) DO NOTHING "
                f"RETURNING {_CONFIG_COLUMNS}"
            ),
            params,
        )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def _keyword_rows(
    conn: AsyncConnection,
    tenant_id: str,
    session_id: str,
    *,
    enabled_only: bool = False,
) -> list[dict[str, Any]]:
    sql = (
        "SELECT id, keyword, enabled, created_at FROM plugin_moderation_keywords "
        "WHERE tenant_id = :tid AND session_id = :sid"
    )
    if enabled_only:
        sql += " AND enabled = TRUE"
    sql += " ORDER BY LOWER(keyword), id"
    result = await conn.execute(text(sql), {"tid": tenant_id, "sid": session_id})
    return _normalize_keyword_rows([dict(row) for row in result.mappings().all()])


def _normalize_keyword_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in rows:
        row = dict(item)
        row["enabled"] = bool(row.get("enabled"))
        row["created_at"] = _isoformat(row.get("created_at"))
        normalized.append(row)
    return normalized
