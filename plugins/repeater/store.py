"""
Repeater plugin persistence.

Tables:
- plugin_repeater_config: per-session enable/cooldown config
- plugin_repeater_event: repeater trigger history for cooldown dedupe and audit
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

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
_CONFIG_COLUMNS = "tenant_id, session_id, enabled, cooldown_seconds, version, updated_at"
_ACTIVE_ADMIN_MUTATION_CONNECTION: ContextVar[AsyncConnection | None] = ContextVar(
    "repeater_admin_mutation_connection",
    default=None,
)


class RepeaterConfigVersionConflictError(RuntimeError):
    def __init__(self, *, expected: int, current: int) -> None:
        super().__init__(f"expected version {expected}, current version {current}")
        self.expected = expected
        self.current = current


@dataclass(frozen=True, slots=True)
class RepeaterConfigMutation:
    before: dict[str, Any]
    after: dict[str, Any]


async def _exec(sql: str, params: dict | None = None) -> list[dict]:
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


def _fingerprint(content: str) -> str:
    return hashlib.sha1(content.strip().encode("utf-8")).hexdigest()


class RepeaterStore:
    def __init__(self, settings: Any) -> None:
        self.settings = settings

    async def ensure_tables(self) -> None:
        await verify_runtime_schema(get_engine(), component="repeater store")
        logger.info("repeater.schema_verified")

    async def run_admin_mutation(
        self,
        *,
        identity: MutationIdentity,
        audit: MutationAudit,
        mutate: Callable[[], Awaitable[MutationChange]],
    ) -> MutationOutcome:
        """Commit config CAS, idempotency response, and audit in one transaction."""

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
        rows = await _exec(
            f"SELECT {_CONFIG_COLUMNS} "
            "FROM plugin_repeater_config "
            "WHERE tenant_id = :tid AND session_id = :sid",
            {"tid": tenant_id, "sid": session_id},
        )
        return _normalize_config(rows[0] if rows else None, tenant_id, session_id)

    @staticmethod
    def default_config(tenant_id: str, session_id: str) -> dict[str, Any]:
        return {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "enabled": False,
            "cooldown_seconds": 300,
            "version": 0,
            "updated_at": None,
        }

    async def set_config(
        self,
        tenant_id: str,
        session_id: str,
        *,
        expected_version: int,
        enabled: bool | None = None,
        cooldown_seconds: int | None = None,
    ) -> RepeaterConfigMutation:
        async with _write_connection() as conn:
            return await self.set_config_in_transaction(
                conn,
                tenant_id,
                session_id,
                expected_version=expected_version,
                enabled=enabled,
                cooldown_seconds=cooldown_seconds,
            )

    async def get_config_in_transaction(
        self,
        db: AsyncConnection | AsyncSession,
        tenant_id: str,
        session_id: str,
        *,
        for_update: bool = False,
    ) -> dict[str, Any]:
        row = await _config_row(
            db,
            tenant_id,
            session_id,
            for_update=for_update,
        )
        return _normalize_config(row, tenant_id, session_id)

    async def set_config_in_transaction(
        self,
        db: AsyncConnection | AsyncSession,
        tenant_id: str,
        session_id: str,
        *,
        expected_version: int,
        enabled: bool | None = None,
        cooldown_seconds: int | None = None,
    ) -> RepeaterConfigMutation:
        row = await _config_row(db, tenant_id, session_id, for_update=True)
        before = _normalize_config(row, tenant_id, session_id)
        current_version = int(before["version"])
        if current_version != int(expected_version):
            raise RepeaterConfigVersionConflictError(
                expected=int(expected_version),
                current=current_version,
            )
        current = dict(before)
        if enabled is not None:
            current["enabled"] = bool(enabled)
        if cooldown_seconds is not None:
            current["cooldown_seconds"] = max(1, int(cooldown_seconds))
        params = {
            "tid": tenant_id,
            "sid": session_id,
            "enabled": bool(current["enabled"]),
            "cooldown": int(current["cooldown_seconds"]),
            "expected_version": int(expected_version),
        }
        if row is None:
            result = await db.execute(
                text(
                    "INSERT INTO plugin_repeater_config "
                    "(tenant_id, session_id, enabled, cooldown_seconds, version, updated_at) "
                    "VALUES (:tid, :sid, :enabled, :cooldown, 1, NOW()) "
                    "ON CONFLICT (tenant_id, session_id) DO NOTHING "
                    f"RETURNING {_CONFIG_COLUMNS}"
                ),
                params,
            )
        else:
            result = await db.execute(
                text(
                    "UPDATE plugin_repeater_config SET enabled = :enabled, "
                    "cooldown_seconds = :cooldown, version = version + 1, "
                    "updated_at = NOW() WHERE tenant_id = :tid AND session_id = :sid "
                    "AND version = :expected_version "
                    f"RETURNING {_CONFIG_COLUMNS}"
                ),
                params,
            )
        written = result.mappings().first()
        if written is None:
            latest = await _config_row(db, tenant_id, session_id, for_update=True)
            raise RepeaterConfigVersionConflictError(
                expected=int(expected_version),
                current=int((latest or {}).get("version") or 0),
            )
        return RepeaterConfigMutation(
            before=before,
            after=_normalize_config(dict(written), tenant_id, session_id),
        )

    async def should_trigger(
        self,
        tenant_id: str,
        session_id: str,
        content_text: str,
        cooldown_seconds: int,
    ) -> bool:
        content_hash = _fingerprint(content_text)
        rows = await _exec(
            "SELECT id FROM plugin_repeater_event "
            "WHERE tenant_id = :tid AND session_id = :sid AND content_hash = :hash "
            "AND created_at >= NOW() - make_interval(secs => :cooldown) "
            "ORDER BY created_at DESC LIMIT 1",
            {
                "tid": tenant_id,
                "sid": session_id,
                "hash": content_hash,
                "cooldown": max(1, int(cooldown_seconds)),
            },
        )
        return not rows

    async def record_trigger(
        self,
        tenant_id: str,
        session_id: str,
        content_text: str,
        *,
        trace_id: str = "",
    ) -> int:
        rows = await _exec(
            "INSERT INTO plugin_repeater_event "
            "(tenant_id, session_id, content_hash, content_text, trace_id) "
            "VALUES (:tid, :sid, :hash, :text, :trace) "
            "RETURNING id",
            {
                "tid": tenant_id,
                "sid": session_id,
                "hash": _fingerprint(content_text),
                "text": content_text[:2000],
                "trace": trace_id,
            },
        )
        return rows[0]["id"]

    async def list_events(
        self,
        tenant_id: str,
        session_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if session_id:
            return await _exec(
                "SELECT id, tenant_id, session_id, content_text, trace_id, created_at "
                "FROM plugin_repeater_event "
                "WHERE tenant_id = :tid AND session_id = :sid "
                "ORDER BY created_at DESC LIMIT :lim",
                {"tid": tenant_id, "sid": session_id, "lim": limit},
            )
        return await _exec(
            "SELECT id, tenant_id, session_id, content_text, trace_id, created_at "
            "FROM plugin_repeater_event "
            "WHERE tenant_id = :tid "
            "ORDER BY created_at DESC LIMIT :lim",
            {"tid": tenant_id, "lim": limit},
        )


def _normalize_config(
    row: dict[str, Any] | None,
    tenant_id: str,
    session_id: str,
) -> dict[str, Any]:
    if row is None:
        return RepeaterStore.default_config(tenant_id, session_id)
    return {
        "tenant_id": str(row.get("tenant_id") or tenant_id),
        "session_id": str(row.get("session_id") or session_id),
        "enabled": bool(row.get("enabled")),
        "cooldown_seconds": max(1, int(row.get("cooldown_seconds") or 300)),
        "version": max(1, int(row.get("version") or 1)),
        "updated_at": row.get("updated_at"),
    }


async def _config_row(
    db: AsyncConnection | AsyncSession,
    tenant_id: str,
    session_id: str,
    *,
    for_update: bool,
) -> dict[str, Any] | None:
    suffix = " FOR UPDATE" if for_update else ""
    result = await db.execute(
        text(
            f"SELECT {_CONFIG_COLUMNS} FROM plugin_repeater_config "
            "WHERE tenant_id = :tid AND session_id = :sid"
            f"{suffix}"
        ),
        {"tid": tenant_id, "sid": session_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None
