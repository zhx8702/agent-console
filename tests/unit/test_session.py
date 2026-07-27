"""Unit tests for the Session Manager (M4)."""
from __future__ import annotations

import asyncio

import fakeredis.aioredis
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.common.config import Settings
from app.common.types import Channel, Role, SessionState, Turn
from app.models.session import SessionRow, TurnRow
from app.session.manager import SessionLockLostError, SessionManager
from app.session.state import assert_can_transition, can_transition

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine():
    """Fresh in-memory SQLite engine with only session/turn tables created.

    We intentionally avoid ``Base.metadata.create_all`` because other ORM
    models in this project use Postgres-only types (e.g. ``ARRAY``) which the
    SQLite dialect can't compile.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [SessionRow.__table__, TurnRow.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: [t.create(sync_conn) for t in tables])
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield r
    finally:
        await r.aclose()


@pytest.fixture
def settings() -> Settings:
    # Small window to exercise trimming cheaply.
    return Settings(
        session_window_turns=3,
        session_ttl_seconds=60,
        session_lock_ttl_seconds=5,
    )


@pytest.fixture
def manager(redis, settings, factory) -> SessionManager:
    return SessionManager(redis=redis, settings=settings, session_factory=factory)


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


def test_state_transitions_legal():
    assert can_transition(SessionState.IDLE, SessionState.CHATTING)
    assert can_transition(SessionState.CHATTING, SessionState.ESCALATED)
    assert can_transition(SessionState.AWAITING_INFO, SessionState.CHATTING)
    # Self transition is a no-op and always allowed.
    assert can_transition(SessionState.CHATTING, SessionState.CHATTING)


def test_state_transitions_illegal():
    # CLOSED is terminal; any exit is illegal.
    assert not can_transition(SessionState.CLOSED, SessionState.CHATTING)
    assert not can_transition(SessionState.CLOSED, SessionState.ESCALATED)
    with pytest.raises(ValueError):
        assert_can_transition(SessionState.CLOSED, SessionState.CHATTING)


# ---------------------------------------------------------------------------
# load() and new-session creation
# ---------------------------------------------------------------------------


async def test_load_creates_new_session_when_missing(manager):
    session = await manager.load(
        tenant_id="demo",
        user_id="u1",
        session_id="se_new_01",
        channel=Channel.WEB,
    )
    assert session.session_id == "se_new_01"
    assert session.state == SessionState.IDLE
    assert session.turns == []


async def test_load_hits_cache_after_write(manager, redis):
    session = await manager.load("demo", "u1", "se_cache_01", Channel.WEB)
    turn = Turn(
        session_id=session.session_id,
        role=Role.USER,
        content="hello",
    )
    await manager.append_turn(session, turn)

    # Underlying Redis should have the hot blob.
    blob = await redis.hget("session:v2:ctx:demo:se_cache_01", "blob")
    assert blob is not None

    # Second load should use the cache (identical content)
    reloaded = await manager.load("demo", "u1", "se_cache_01", Channel.WEB)
    assert len(reloaded.turns) == 1
    assert reloaded.turns[0].content == "hello"


async def test_load_falls_back_to_db_when_cache_empty(manager, redis):
    session = await manager.load("demo", "u1", "se_db_01", Channel.WEB)
    for i in range(2):
        await manager.append_turn(
            session,
            Turn(session_id=session.session_id, role=Role.USER, content=f"m{i}"),
        )

    # Purge the hot cache
    await redis.delete("session:v2:ctx:demo:se_db_01")

    reloaded = await manager.load("demo", "u1", "se_db_01", Channel.WEB)
    assert len(reloaded.turns) == 2
    contents = [t.content for t in reloaded.turns]
    assert contents == ["m0", "m1"]


# ---------------------------------------------------------------------------
# append_turn
# ---------------------------------------------------------------------------


async def test_append_turn_persists_to_db(manager, factory):
    session = await manager.load("demo", "u1", "se_persist_01", Channel.WEB)
    await manager.append_turn(
        session,
        Turn(session_id=session.session_id, role=Role.USER, content="hi"),
    )

    # Verify via a raw DB session
    async with factory() as db:
        row = await db.get(
            SessionRow,
            {"tenant_id": "demo", "session_id": "se_persist_01"},
        )
        assert row is not None
        assert row.state == SessionState.IDLE.value
        turn_rows = (await db.execute(select(TurnRow))).scalars().all()
        assert len(turn_rows) == 1
        assert turn_rows[0].tenant_id == "demo"
        assert turn_rows[0].content == "hi"


async def test_same_session_id_is_isolated_between_tenants(manager, factory):
    session = await manager.load("tenant-a", "u1", "shared-id", Channel.WEB)
    await manager.append_turn(
        session,
        Turn(session_id=session.session_id, role=Role.USER, content="private"),
    )

    other = await manager.load("tenant-b", "u2", "shared-id", Channel.WEB)
    assert other.turns == []
    await manager.append_turn(
        other,
        Turn(session_id=other.session_id, role=Role.USER, content="other"),
    )

    tenant_a = await manager.load("tenant-a", "u1", "shared-id", Channel.WEB)
    tenant_b = await manager.load("tenant-b", "u2", "shared-id", Channel.WEB)
    assert [turn.content for turn in tenant_a.turns] == ["private"]
    assert [turn.content for turn in tenant_b.turns] == ["other"]

    async with factory() as db:
        rows = (
            await db.execute(
                select(SessionRow)
                .where(SessionRow.session_id == "shared-id")
                .order_by(SessionRow.tenant_id)
            )
        ).scalars().all()
        assert [row.tenant_id for row in rows] == ["tenant-a", "tenant-b"]


async def test_append_turn_rejects_a_different_session_id(manager):
    session = await manager.load("tenant-a", "u1", "session-a", Channel.WEB)

    with pytest.raises(ValueError, match="turn_session_mismatch"):
        await manager.append_turn(
            session,
            Turn(session_id="session-b", role=Role.USER, content="wrong target"),
        )


async def test_append_turn_trims_to_window(manager):
    session = await manager.load("demo", "u1", "se_trim_01", Channel.WEB)
    # Window is 3 via fixture; append 5.
    for i in range(5):
        await manager.append_turn(
            session,
            Turn(session_id=session.session_id, role=Role.USER, content=f"t{i}"),
        )
    assert len(session.turns) == 3
    assert [t.content for t in session.turns] == ["t2", "t3", "t4"]


# ---------------------------------------------------------------------------
# set_state
# ---------------------------------------------------------------------------


async def test_set_state_valid(manager, factory):
    session = await manager.load("demo", "u1", "se_state_01", Channel.WEB)
    # Need to persist first (set_state calls save() which upserts the row).
    await manager.set_state(session, SessionState.CHATTING)
    assert session.state == SessionState.CHATTING

    from app.models.session import SessionRow

    async with factory() as db:
        row = await db.get(
            SessionRow,
            {"tenant_id": "demo", "session_id": "se_state_01"},
        )
        assert row is not None
        assert row.state == SessionState.CHATTING.value


async def test_set_state_invalid_raises(manager):
    session = await manager.load("demo", "u1", "se_state_02", Channel.WEB)
    # Drive the session through CHATTING -> CLOSED, then attempt to reopen.
    await manager.set_state(session, SessionState.CHATTING)
    await manager.set_state(session, SessionState.CLOSED)
    with pytest.raises(ValueError):
        await manager.set_state(session, SessionState.CHATTING)  # CLOSED is terminal


# ---------------------------------------------------------------------------
# Distributed lock
# ---------------------------------------------------------------------------


async def test_lock_exclusive(manager):
    held = asyncio.Event()
    released = asyncio.Event()

    async def holder():
        async with manager.lock(
            "se_lock_01",
            tenant_id="demo",
            acquire_timeout=1.0,
        ):
            held.set()
            await released.wait()

    task = asyncio.create_task(holder())
    await held.wait()

    # Another acquisition with a tiny timeout should fail.
    with pytest.raises(TimeoutError):
        async with manager.lock(
            "se_lock_01",
            tenant_id="demo",
            acquire_timeout=0.2,
        ):
            pass

    released.set()
    await task


async def test_lock_released_allows_reacquire(manager):
    async with manager.lock(
        "se_lock_02",
        tenant_id="demo",
        acquire_timeout=1.0,
    ):
        pass
    # After release, a fresh acquisition succeeds immediately.
    async with manager.lock(
        "se_lock_02",
        tenant_id="demo",
        acquire_timeout=1.0,
    ):
        pass


async def test_reacquired_lock_gets_a_strictly_newer_fence_token(manager):
    async with manager.lock(
        "se_fence_monotonic",
        tenant_id="demo",
        acquire_timeout=1.0,
    ) as first_token:
        first_fence = int(first_token.rsplit(":", 1)[1])

    async with manager.lock(
        "se_fence_monotonic",
        tenant_id="demo",
        acquire_timeout=1.0,
    ) as second_token:
        second_fence = int(second_token.rsplit(":", 1)[1])

    assert second_fence > first_fence


async def test_stale_fence_cannot_overwrite_newer_persisted_state(manager, factory):
    session = await manager.load("demo", "u1", "se_stale_fence", Channel.WEB)
    await manager.save(session)

    # Model a newer worker having already committed fence 100.  The current
    # Redis lease starts at a lower token and must be rejected by the database,
    # even while that stale worker still believes it owns its Redis key.
    async with factory() as db:
        await db.execute(
            update(SessionRow)
            .where(
                SessionRow.tenant_id == "demo",
                SessionRow.session_id == "se_stale_fence",
            )
            .values(fence_token=100, summary="newer state")
        )
        await db.commit()

    session.summary = "stale overwrite"
    async with manager.lock(
        "se_stale_fence",
        tenant_id="demo",
        acquire_timeout=1.0,
    ):
        with pytest.raises(SessionLockLostError):
            await manager.save(session)

    async with factory() as db:
        row = (
            await db.execute(
                select(SessionRow).where(
                    SessionRow.tenant_id == "demo",
                    SessionRow.session_id == "se_stale_fence",
                )
            )
        ).scalar_one()
        assert row.fence_token == 100
        assert row.summary == "newer state"


async def test_same_session_id_has_independent_tenant_locks(manager):
    async with manager.lock(
        "shared-lock",
        tenant_id="tenant-a",
        acquire_timeout=1.0,
    ):
        async with manager.lock(
            "shared-lock",
            tenant_id="tenant-b",
            acquire_timeout=1.0,
        ):
            pass
