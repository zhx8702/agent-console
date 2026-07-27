"""Integration: session state persists to real Postgres and survives cache loss."""
from __future__ import annotations

import pytest

from tests.integration.conftest import requires_postgres, requires_redis

pytestmark = [pytest.mark.integration, requires_redis, requires_postgres]


@pytest.mark.asyncio
async def test_session_survives_cache_flush(redis_client):
    import uuid

    from app.common.config import get_settings
    from app.common.types import Channel, Role, Turn
    from app.session.manager import SessionManager

    session_id = f"se_integ_persist_{uuid.uuid4().hex[:8]}"
    sm = SessionManager(redis_client, get_settings())
    session = await sm.load(
        tenant_id="demo",
        user_id="u1",
        session_id=session_id,
        channel=Channel.WEB,
    )
    await sm.append_turn(
        session,
        Turn(session_id=session.session_id, role=Role.USER, content="hello"),
    )
    await sm.append_turn(
        session,
        Turn(session_id=session.session_id, role=Role.ASSISTANT, content="hi back"),
    )

    # Flush Redis to force a cold fetch from Postgres.
    await redis_client.flushdb()

    reloaded = await sm.load("demo", "u1", session_id, Channel.WEB)
    assert reloaded.session_id == session_id
    assert len(reloaded.turns) == 2
    assert reloaded.turns[0].role == Role.USER
    assert reloaded.turns[1].role == Role.ASSISTANT
