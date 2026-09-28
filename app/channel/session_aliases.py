"""Resolve operator-facing and canonical group IDs for policy lookups."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
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
        if not isinstance(meta, dict):
            continue
        add(meta.get("external_conversation_id"))
        add(meta.get("external_session_id"))
        add(meta.get("canonical_conversation_id"))
    return aliases


async def session_policy_aliases(
    tenant_id: str,
    session_id: str,
    *,
    db: AsyncSession | None = None,
) -> list[str]:
    sid = str(session_id or "").strip()
    if not sid:
        return []
    statement = text(
        "SELECT session_id, metadata FROM sessions WHERE tenant_id=:tid AND "
        "(session_id=:sid OR metadata->>'external_conversation_id'=:sid "
        "OR metadata->>'external_session_id'=:sid "
        "OR metadata->>'canonical_conversation_id'=:sid)"
    )
    params = {"tid": tenant_id, "sid": sid}
    if db is not None:
        result = await db.execute(statement, params)
        return collect_session_aliases(sid, [dict(row) for row in result.mappings()])
    from app.infra.db import get_engine

    async with get_engine().begin() as conn:
        result = await conn.execute(statement, params)
        return collect_session_aliases(sid, [dict(row) for row in result.mappings()])
