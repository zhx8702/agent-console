from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.integration.conftest import requires_postgres


@pytest.mark.integration
@requires_postgres
@pytest.mark.asyncio
async def test_legacy_turn_writer_is_inferred_only_for_unambiguous_session() -> None:
    from app.infra.db import get_engine

    token = uuid4().hex[:16]
    session_id = f"compat-{token}"
    tenant_a = f"tenant-a-{token}"
    tenant_b = f"tenant-b-{token}"
    now = datetime.now(UTC)
    engine = get_engine()

    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await _insert_session(connection, tenant_a, session_id, now)
            await connection.execute(
                text(
                    "INSERT INTO turns "
                    "(turn_id, session_id, role, content, tool_calls, citations, "
                    "metadata, created_at) VALUES "
                    "(:turn_id, :session_id, 'user', 'legacy', '[]'::json, "
                    "'[]'::json, '{}'::json, :created_at)"
                ),
                {
                    "turn_id": f"turn-{token}",
                    "session_id": session_id,
                    "created_at": now,
                },
            )
            inferred = await connection.scalar(
                text("SELECT tenant_id FROM turns WHERE turn_id = :turn_id"),
                {"turn_id": f"turn-{token}"},
            )
            assert inferred == tenant_a

            mismatch = await connection.begin_nested()
            with pytest.raises(DBAPIError):
                await connection.execute(
                    text(
                        "INSERT INTO turns "
                        "(turn_id, tenant_id, session_id, role, content, tool_calls, "
                        "citations, metadata, created_at) VALUES "
                        "(:turn_id, :tenant_id, :session_id, 'user', 'mismatch', "
                        "'[]'::json, '[]'::json, '{}'::json, :created_at)"
                    ),
                    {
                        "turn_id": f"turn-mismatch-{token}",
                        "tenant_id": tenant_b,
                        "session_id": session_id,
                        "created_at": now,
                    },
                )
            await mismatch.rollback()

            await _insert_session(connection, tenant_b, session_id, now)
            ambiguous = await connection.begin_nested()
            with pytest.raises(DBAPIError):
                await connection.execute(
                    text(
                        "INSERT INTO turns "
                        "(turn_id, session_id, role, content, tool_calls, citations, "
                        "metadata, created_at) VALUES "
                        "(:turn_id, :session_id, 'user', 'ambiguous', '[]'::json, "
                        "'[]'::json, '{}'::json, :created_at)"
                    ),
                    {
                        "turn_id": f"turn-ambiguous-{token}",
                        "session_id": session_id,
                        "created_at": now,
                    },
                )
            await ambiguous.rollback()
        finally:
            await transaction.rollback()


async def _insert_session(connection, tenant_id: str, session_id: str, now: datetime) -> None:
    await connection.execute(
        text(
            "INSERT INTO sessions "
            "(tenant_id, session_id, user_id, channel, state, summary, variables, "
            "pii_map, metadata, fence_token, last_active_at, created_at, updated_at) "
            "VALUES (:tenant_id, :session_id, :user_id, 'web', 'idle', NULL, "
            "'{}'::json, '{}'::json, '{}'::json, 0, :now, :now, :now)"
        ),
        {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "user_id": f"user-{tenant_id}",
            "now": now,
        },
    )
