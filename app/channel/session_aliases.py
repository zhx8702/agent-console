"""Resolve operator-facing and canonical group IDs for policy lookups."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession


def collect_session_aliases(session_id: str, rows: list[dict[str, Any]]) -> list[str]:
    aliases: list[str] = []

    def add(value: Any) -> None:
        item = str(value or "").strip()
        if item and item not in aliases:
            aliases.append(item)

    add(session_id)
    for row in rows:
        add(row.get("session_id"))
        meta = row.get("metadata")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except json.JSONDecodeError:
                meta = {}
        if not isinstance(meta, dict):
            continue
        add(meta.get("external_conversation_id"))
        add(meta.get("external_session_id"))
        add(meta.get("canonical_conversation_id"))
    return aliases


def _sessions_alias_statement(dialect_name: str):
    if dialect_name == "sqlite":
        return text(
            "SELECT session_id, metadata FROM sessions WHERE tenant_id=:tid AND "
            "(session_id=:sid "
            "OR json_extract(metadata, '$.external_conversation_id')=:sid "
            "OR json_extract(metadata, '$.external_session_id')=:sid "
            "OR json_extract(metadata, '$.canonical_conversation_id')=:sid)"
        )
    return text(
        "SELECT session_id, metadata FROM sessions WHERE tenant_id=:tid AND "
        "(session_id=:sid OR metadata->>'external_conversation_id'=:sid "
        "OR metadata->>'external_session_id'=:sid "
        "OR metadata->>'canonical_conversation_id'=:sid)"
    )


def _bind_dialect_name(target: Any) -> str:
    bind = getattr(target, "bind", None)
    if bind is None and hasattr(target, "get_bind"):
        try:
            bind = target.get_bind()
        except Exception:
            bind = None
    dialect = getattr(target, "dialect", None) or getattr(bind, "dialect", None)
    return str(getattr(dialect, "name", "") or "")


def _missing_sessions_table(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "no such table" in message and "sessions" in message


async def session_policy_aliases(
    tenant_id: str,
    session_id: str,
    *,
    db: AsyncSession | None = None,
) -> list[str]:
    sid = str(session_id or "").strip()
    if not sid:
        return []
    params = {"tid": tenant_id, "sid": sid}

    async def _load(target: Any) -> list[str]:
        statement = _sessions_alias_statement(_bind_dialect_name(target))
        try:
            result = await target.execute(statement, params)
        except (OperationalError, ProgrammingError) as exc:
            if _missing_sessions_table(exc):
                return [sid]
            raise
        return collect_session_aliases(sid, [dict(row) for row in result.mappings()])

    if db is not None:
        return await _load(db)
    from app.infra.db import get_engine

    async with get_engine().begin() as conn:
        return await _load(conn)
