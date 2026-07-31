"""
M4 — Session Manager.

Responsibilities:
  * Load / cache / persist :class:`Session` objects.
  * Append turns to both Redis (hot) and PostgreSQL (cold).
  * Provide a distributed lock so concurrent workers cannot mutate the same
    session simultaneously.
  * Enforce the session state machine (see :mod:`app.session.state`).

Storage layout:
  * Redis key ``session:v2:ctx:{tenant_id}:{session_id}`` — hash, field
    ``blob`` holds a JSON serialization of the full :class:`Session` (last N
    turns only). TTL is the configured ``session_ttl_seconds`` (sliding on
    every write/read).
  * Redis key ``session:v2:lock:{tenant_id}:{session_id}`` — SET NX EX
    distributed lock. Token is a random hex string; only the owner releases
    via a Lua CAS script.
  * PostgreSQL tables ``sessions`` + ``turns`` — full history, written through.
"""
from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import time_ns
from typing import Any, cast
from urllib.parse import quote

import orjson
from prometheus_client import Counter
from redis.asyncio import Redis
from redis.exceptions import WatchError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.config import Settings
from app.common.context import get_tenant_id
from app.common.exceptions import SessionLockLost
from app.common.logging import get_logger
from app.common.types import ChannelId, Session, SessionState, Turn, channel_id_value
from app.infra.metrics import SESSION_LOCK_EVENTS
from app.models.session import SessionRow, TurnRow
from app.session.state import assert_can_transition

logger = get_logger(__name__)


class SessionTenantConflictError(RuntimeError):
    """Raised when a session id is already owned by another tenant."""

    code = "session_tenant_conflict"


class SessionLockLostError(SessionLockLost, RuntimeError):
    """Raised before a stale lease can commit state."""

    code = "session_lock_lost"


@dataclass(slots=True)
class SessionLease:
    tenant_id: str
    session_id: str
    token: str
    fence: int
    lost: asyncio.Event = field(default_factory=asyncio.Event)
    reported: bool = False


@dataclass(slots=True)
class _SessionTransaction:
    db: AsyncSession
    cache_sessions: dict[tuple[str, str], Session] = field(default_factory=dict)


@dataclass(slots=True)
class _SessionStage:
    sessions: dict[tuple[str, str], Session] = field(default_factory=dict)
    turns: list[tuple[str, Turn]] = field(default_factory=list)


SESSION_LOCK_LOST = Counter(
    "session_lock_lost_total",
    "Session mutations aborted after a distributed lease was lost",
)


# Lua script: atomically release the lock only if the stored token matches ours.
_LUA_RELEASE_LOCK = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


# Renew only a lock still owned by the same token.  Millisecond precision
# keeps short test TTLs useful while production normally uses whole seconds.
_LUA_RENEW_LOCK = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("pexpire", KEYS[1], ARGV[2])
else
    return 0
end
"""


# These values are rebuilt from privacy-gated stores for each inbound turn.
# Keep this an exact allowlist: other session variables can be durable
# operator/plugin configuration and must survive a session reload.
_RUNTIME_DERIVED_VARIABLE_KEYS = frozenset(
    {
        "group_memory",
        "group_observation_context",
        "user_memory",
    }
)


def _lock_token_matches(current: object, token: str) -> bool:
    return current in {token, token.encode()}


async def _watch_compare_and_expire(
    redis: Any,
    key: str,
    token: str,
    ttl_ms: int,
) -> int:
    """Atomic CAS fallback for Redis implementations without EVAL."""

    try:
        async with redis.pipeline(transaction=True) as pipe:
            await pipe.watch(key)
            if not _lock_token_matches(await pipe.get(key), token):
                await pipe.unwatch()
                return 0
            pipe.multi()
            pipe.pexpire(key, ttl_ms)
            result = await pipe.execute()
            return int(bool(result and result[0]))
    except WatchError:
        return 0


async def _watch_compare_and_delete(redis: Any, key: str, token: str) -> int:
    """Atomic CAS delete fallback; a concurrent owner always wins safely."""

    try:
        async with redis.pipeline(transaction=True) as pipe:
            await pipe.watch(key)
            if not _lock_token_matches(await pipe.get(key), token):
                await pipe.unwatch()
                return 0
            pipe.multi()
            pipe.delete(key)
            result = await pipe.execute()
            return int(bool(result and result[0]))
    except WatchError:
        return 0


def _scope_part(value: str) -> str:
    # ``:`` is the key delimiter, so percent-encode every non-unreserved byte
    # to keep (tenant_id, session_id) pairs collision-free.
    return quote(value, safe="")


def _ctx_key(tenant_id: str, session_id: str) -> str:
    return f"session:v2:ctx:{_scope_part(tenant_id)}:{_scope_part(session_id)}"


def _lock_key(tenant_id: str, session_id: str) -> str:
    return f"session:v2:lock:{_scope_part(tenant_id)}:{_scope_part(session_id)}"


def _fence_key(tenant_id: str, session_id: str) -> str:
    return f"session:v2:fence:{_scope_part(tenant_id)}:{_scope_part(session_id)}"


def _serialize_session(session: Session) -> str:
    # mode="json" turns datetimes/enums into JSON-safe primitives.
    return cast(
        str,
        orjson.dumps(session.model_dump(mode="json")).decode("utf-8"),
    )


def _is_group_session(session: Session) -> bool:
    kind = str((session.metadata or {}).get("session_kind") or "").strip().lower()
    return kind in {"group", "chatroom", "channel", "guild"} or str(
        session.session_id or ""
    ).endswith("@chatroom")


def _persisted_variables(session: Session) -> dict[str, Any]:
    """Return durable variables without per-inbound prompt context."""
    variables = dict(session.variables or {})
    for key in _RUNTIME_DERIVED_VARIABLE_KEYS:
        variables.pop(key, None)
    return variables


def _serialize_session_for_cache(session: Session) -> str:
    snapshot = session.model_copy(deep=True)
    snapshot.variables = _persisted_variables(snapshot)
    # Placeholder-to-original mappings are needed only while post-processing
    # the current response. Persisting them would retain raw PII beyond the
    # inbound turn and could rehydrate stale secrets on a later request.
    snapshot.pii_map = {}
    if _is_group_session(snapshot):
        # A group conversation has no single durable user owner.  The active
        # actor is refreshed from every inbound event by load().
        snapshot.user_id = snapshot.session_id
    return _serialize_session(snapshot)


def _deserialize_session(blob: str | bytes) -> Session:
    raw = orjson.loads(blob)
    return cast(Session, Session.model_validate(raw))


def _refresh_loaded_session(
    session: Session,
    *,
    tenant_id: str,
    session_id: str,
    user_id: str,
    channel: ChannelId,
) -> Session:
    if session.tenant_id != tenant_id or session.session_id != session_id:
        raise SessionTenantConflictError(
            "cached_or_persisted_session_scope_mismatch:"
            f"expected={tenant_id}/{session_id}:"
            f"actual={session.tenant_id}/{session.session_id}"
        )
    session.user_id = user_id
    session.channel = channel
    return session


def _prepare_inbound_session(
    session: Session,
    *,
    tenant_id: str,
    session_id: str,
    user_id: str,
    channel: ChannelId,
) -> Session:
    """Refresh event scope and discard context that must be authorized anew."""
    session = _refresh_loaded_session(
        session,
        tenant_id=tenant_id,
        session_id=session_id,
        user_id=user_id,
        channel=channel,
    )
    for key in _RUNTIME_DERIVED_VARIABLE_KEYS:
        session.variables.pop(key, None)
    session.pii_map = {}
    return session


class SessionManager:
    """M4 façade used by the orchestrator and workers."""

    def __init__(
        self,
        redis: Redis,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._redis = redis
        self._settings = settings
        self._ttl = settings.session_ttl_seconds
        self._lock_ttl = settings.session_lock_ttl_seconds
        self._window = settings.session_window_turns
        # Tests override this; production resolves lazily so we don't bind a
        # global engine at import time.
        self._session_factory = session_factory
        self._active_lease: ContextVar[SessionLease | None] = ContextVar(
            f"session_manager_lease_{id(self)}",
            default=None,
        )
        self._active_transaction: ContextVar[_SessionTransaction | None] = (
            ContextVar(
                f"session_manager_transaction_{id(self)}",
                default=None,
            )
        )
        self._active_stage: ContextVar[_SessionStage | None] = ContextVar(
            f"session_manager_stage_{id(self)}",
            default=None,
        )
        self._local_fence = 0

    # -- factory resolution ------------------------------------------------

    def _factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is not None:
            return self._session_factory
        from app.infra.db import get_session_factory

        return get_session_factory()

    @asynccontextmanager
    async def _db(self) -> AsyncIterator[AsyncSession]:
        active = self._active_transaction.get()
        if active is not None:
            yield active.db
            return
        factory = self._factory()
        async with factory() as db:
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    @asynccontextmanager
    async def _read_db(self) -> AsyncIterator[AsyncSession]:
        """Reuse an ambient transaction for reads without owning its lifecycle."""

        active = self._active_transaction.get()
        if active is not None:
            yield active.db
            return

        factory = self._factory()
        async with factory() as db:
            yield db

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """Bind all session writes in this task to one database transaction.

        Nested users share the ambient transaction. Cache publication is
        deferred until the commit succeeds, so Redis can never expose state
        that the database later rolls back.
        """

        existing = self._active_transaction.get()
        if existing is not None:
            yield existing.db
            return

        factory = self._factory()
        pending: dict[tuple[str, str], Session] = {}
        async with factory() as db:
            state = _SessionTransaction(db=db, cache_sessions=pending)
            token = self._active_transaction.set(state)
            committed = False
            try:
                yield db
                await db.commit()
                committed = True
            except BaseException:
                await db.rollback()
                raise
            finally:
                self._active_transaction.reset(token)

        if committed:
            for session in pending.values():
                try:
                    await self._write_cache_immediate(session)
                except Exception as exc:
                    logger.warning(
                        "session.cache_publish_after_commit_failed",
                        tenant_id=session.tenant_id,
                        session_id=session.session_id,
                        error_type=exc.__class__.__name__,
                    )

    @asynccontextmanager
    async def stage(self) -> AsyncIterator[None]:
        """Stage session mutations in memory until a final short transaction."""

        existing = self._active_stage.get()
        if existing is not None:
            yield
            return

        token = self._active_stage.set(_SessionStage())
        try:
            yield
        finally:
            self._active_stage.reset(token)

    @property
    def transaction_active(self) -> bool:
        return self._active_transaction.get() is not None

    async def flush_stage(self, db: AsyncSession) -> None:
        """Write the active stage into ``db`` without committing it."""

        stage = self._active_stage.get()
        if stage is None:
            raise RuntimeError("session_stage_not_active")
        for session in stage.sessions.values():
            await self._upsert_session_row(db, session)
        for tenant_id, turn in stage.turns:
            db.add(_turn_to_row(turn, tenant_id=tenant_id))

        transaction = self._active_transaction.get()
        if transaction is not None:
            transaction.cache_sessions.update(stage.sessions)

    # -- loading -----------------------------------------------------------

    async def load(
        self,
        tenant_id: str,
        user_id: str,
        session_id: str,
        channel: ChannelId,
    ) -> Session:
        """Load the session: Redis hot path → Postgres cold path → fresh in-memory.

        A freshly created :class:`Session` is *not* persisted here; it will be
        written through on the first :meth:`append_turn` call.
        """
        stage = self._active_stage.get()
        stage_key = (tenant_id, session_id)
        if stage is not None and stage_key in stage.sessions:
            return _refresh_loaded_session(
                stage.sessions[stage_key],
                tenant_id=tenant_id,
                session_id=session_id,
                user_id=user_id,
                channel=channel,
            )

        # 1) Hot cache
        cache_key = _ctx_key(tenant_id, session_id)
        blob = await self._redis.hget(cache_key, "blob")
        if blob:
            try:
                session = _prepare_inbound_session(
                    _deserialize_session(blob),
                    tenant_id=tenant_id,
                    session_id=session_id,
                    user_id=user_id,
                    channel=channel,
                )
                # Sliding TTL on every read
                await self._redis.expire(cache_key, self._ttl)
                return session
            except SessionTenantConflictError as exc:
                # A scoped key containing another tenant/session is evidence
                # of stale or corrupt cache data.  Remove it and fail closed
                # against the database rather than mutating the cached owner.
                logger.error(
                    "session.cache_scope_mismatch",
                    tenant_id=tenant_id,
                    session_id=session_id,
                    error=str(exc),
                )
                await self._redis.delete(cache_key)
            except Exception as exc:
                logger.warning(
                    "session.cache_corrupt",
                    tenant_id=tenant_id,
                    session_id=session_id,
                    error=str(exc),
                )
                await self._redis.delete(cache_key)

        # 2) Cold path — Postgres
        async with self._read_db() as db:
            row = await self._get_session_row(db, tenant_id, session_id)
            if row is not None:
                turns = await self._load_recent_turns(db, tenant_id, session_id)
                session = _prepare_inbound_session(
                    _row_to_session(row, turns),
                    tenant_id=tenant_id,
                    session_id=session_id,
                    user_id=user_id,
                    channel=channel,
                )
                await self._write_cache(session)
                return session

        # 3) New session in memory — persisted lazily on first append_turn.
        session = Session(
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            channel=channel,
            state=SessionState.IDLE,
        )
        return session

    async def _get_session_row(
        self,
        db: AsyncSession,
        tenant_id: str,
        session_id: str,
    ) -> SessionRow | None:
        stmt = (
            select(SessionRow)
            .where(
                SessionRow.tenant_id == tenant_id,
                SessionRow.session_id == session_id,
            )
            .limit(1)
        )
        return cast(
            SessionRow | None,
            (await db.execute(stmt)).scalar_one_or_none(),
        )

    async def _load_recent_turns(
        self,
        db: AsyncSession,
        tenant_id: str,
        session_id: str,
    ) -> list[Turn]:
        stmt = (
            select(TurnRow)
            .where(
                TurnRow.tenant_id == tenant_id,
                TurnRow.session_id == session_id,
            )
            .order_by(TurnRow.created_at.desc())
            .limit(self._window)
        )
        res = await db.execute(stmt)
        rows = list(res.scalars().all())
        rows.reverse()  # chronological order
        return [_row_to_turn(r) for r in rows]

    # -- writes ------------------------------------------------------------

    async def append_turn(self, session: Session, turn: Turn) -> None:
        """Append a turn, trim the window, write through to DB + cache."""
        if turn.session_id != session.session_id:
            raise ValueError(
                "turn_session_mismatch:"
                f"turn={turn.session_id}:session={session.session_id}"
            )

        session.turns.append(turn)
        if len(session.turns) > self._window:
            session.turns = session.turns[-self._window :]

        stage = self._active_stage.get()
        if stage is not None:
            await self._assert_active_lease(session)
            stage.sessions[(session.tenant_id, session.session_id)] = session
            stage.turns.append((session.tenant_id, turn))
            return

        async with self._db() as db:
            await self._upsert_session_row(db, session)
            db.add(_turn_to_row(turn, tenant_id=session.tenant_id))

        await self._write_cache(session)

    async def save(self, session: Session) -> None:
        """Persist session metadata (state / variables / pii_map) + cache."""
        stage = self._active_stage.get()
        if stage is not None:
            await self._assert_active_lease(session)
            stage.sessions[(session.tenant_id, session.session_id)] = session
            return
        async with self._db() as db:
            await self._upsert_session_row(db, session)
        await self._write_cache(session)

    async def _upsert_session_row(
        self, db: AsyncSession, session: Session
    ) -> SessionRow:
        lease = await self._assert_active_lease(session)
        row = await self._get_session_row(
            db,
            session.tenant_id,
            session.session_id,
        )
        if row is None:
            row = SessionRow(
                session_id=session.session_id,
                tenant_id=session.tenant_id,
                user_id=session.session_id if _is_group_session(session) else session.user_id,
                channel=channel_id_value(session.channel),
                state=session.state.value,
                summary=session.summary,
                variables=_persisted_variables(session),
                pii_map={},
                meta=dict(session.metadata),
                fence_token=lease.fence if lease is not None else 0,
                last_active_at=session.last_active_at,
            )
            db.add(row)
        elif lease is not None:
            result = await db.execute(
                update(SessionRow)
                .where(
                    SessionRow.tenant_id == session.tenant_id,
                    SessionRow.session_id == session.session_id,
                    SessionRow.fence_token <= lease.fence,
                )
                .values(
                    user_id=(
                        session.session_id
                        if _is_group_session(session)
                        else session.user_id
                    ),
                    channel=channel_id_value(session.channel),
                    state=session.state.value,
                    summary=session.summary,
                    variables=_persisted_variables(session),
                    pii_map={},
                    meta=dict(session.metadata),
                    last_active_at=session.last_active_at,
                    fence_token=lease.fence,
                )
            )
            if int(getattr(result, "rowcount", 0) or 0) != 1:
                lease.lost.set()
                lease.reported = True
                SESSION_LOCK_LOST.inc()
                raise SessionLockLostError(
                    "session write rejected by fencing token:"
                    f"{session.tenant_id}/{session.session_id}/{lease.fence}"
                )
            row.fence_token = lease.fence
        else:
            row.user_id = session.session_id if _is_group_session(session) else session.user_id
            row.channel = channel_id_value(session.channel)
            row.state = session.state.value
            row.summary = session.summary
            row.variables = _persisted_variables(session)
            row.pii_map = {}
            row.meta = dict(session.metadata)
            row.last_active_at = session.last_active_at
        return row

    async def _write_cache(self, session: Session) -> None:
        await self._assert_active_lease(session)
        active = self._active_transaction.get()
        if active is not None:
            active.cache_sessions[(session.tenant_id, session.session_id)] = session
            return
        await self._write_cache_immediate(session)

    async def _write_cache_immediate(self, session: Session) -> None:
        await self._assert_active_lease(session)
        key = _ctx_key(session.tenant_id, session.session_id)
        await self._redis.hset(key, "blob", _serialize_session_for_cache(session))
        await self._redis.expire(key, self._ttl)

    async def _assert_active_lease(
        self,
        session: Session,
    ) -> SessionLease | None:
        lease = self._active_lease.get()
        if lease is None:
            return None
        if (
            lease.tenant_id != session.tenant_id
            or lease.session_id != session.session_id
        ):
            raise SessionLockLostError(
                "active lease does not match session scope:"
                f"{lease.tenant_id}/{lease.session_id}:"
                f"{session.tenant_id}/{session.session_id}"
            )
        if lease.lost.is_set():
            lease.reported = True
            raise SessionLockLostError(
                f"session lease already lost:{session.tenant_id}/{session.session_id}"
            )

        getter = getattr(self._redis, "get", None)
        if callable(getter):
            try:
                current = await getter(_lock_key(session.tenant_id, session.session_id))
            except Exception as exc:
                lease.lost.set()
                lease.reported = True
                SESSION_LOCK_LOST.inc()
                raise SessionLockLostError("session lease ownership check failed") from exc
            if current not in {lease.token, lease.token.encode()}:
                lease.lost.set()
                lease.reported = True
                SESSION_LOCK_LOST.inc()
                raise SessionLockLostError(
                    f"session lease ownership changed:{session.tenant_id}/{session.session_id}"
                )
        return lease

    # -- state machine -----------------------------------------------------

    async def set_state(self, session: Session, new_state: SessionState) -> None:
        """Validate then persist a state transition; writes an audit log line."""
        if session.state == new_state:
            return
        assert_can_transition(session.state, new_state)
        old = session.state
        session.state = new_state
        await self.save(session)
        logger.info(
            "session.state_transition",
            session_id=session.session_id,
            tenant_id=session.tenant_id,
            from_state=old.value,
            to_state=new_state.value,
        )

    # -- distributed lock --------------------------------------------------

    async def _next_fence_token(self, tenant_id: str, session_id: str) -> int:
        increment = getattr(self._redis, "incr", None)
        if callable(increment):
            try:
                return int(await increment(_fence_key(tenant_id, session_id)))
            except Exception as exc:
                raise RuntimeError("session fence allocation failed") from exc
        # Minimal Redis test doubles may not implement INCR. This fallback is
        # process-local and is never used by the production Redis client.
        self._local_fence = max(self._local_fence + 1, time_ns())
        return self._local_fence

    @asynccontextmanager
    async def lock(
        self,
        session_id: str,
        *,
        tenant_id: str | None = None,
        acquire_timeout: float = 5.0,
        poll_interval: float = 0.05,
    ) -> AsyncIterator[str]:
        """Acquire a distributed lock for a tenant-scoped ``session_id``.

        Implementation: ``SET key token NX EX lock_ttl``. Release is a Lua CAS
        so only the owner can delete. Raises :class:`TimeoutError` if the lock
        can't be acquired within ``acquire_timeout`` seconds.

        Existing orchestrator call sites remain compatible because they set
        the tenant context before acquiring the lock.  Direct callers should
        pass ``tenant_id`` explicitly.
        """
        resolved_tenant_id = tenant_id or get_tenant_id()
        if not resolved_tenant_id:
            raise ValueError("tenant_id_required_for_session_lock")

        key = _lock_key(resolved_tenant_id, session_id)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + acquire_timeout
        owner_token = secrets.token_hex(16)
        token = ""
        fence = 0

        while True:
            fence = await self._next_fence_token(resolved_tenant_id, session_id)
            token = f"{owner_token}:{fence}"
            ok = await self._redis.set(key, token, nx=True, ex=self._lock_ttl)
            if ok:
                break
            if loop.time() >= deadline:
                raise TimeoutError(
                    f"could not acquire session lock for {session_id} within "
                    f"{acquire_timeout}s"
                )
            await asyncio.sleep(poll_interval)

        lease = SessionLease(
            tenant_id=resolved_tenant_id,
            session_id=session_id,
            token=token,
            fence=fence,
        )
        context_token = self._active_lease.set(lease)
        owner_task = asyncio.current_task()

        async def renew() -> None:
            interval = max(0.1, float(self._lock_ttl) / 3.0)
            ttl_ms = max(1000, int(float(self._lock_ttl) * 1000))
            try:
                while True:
                    await asyncio.sleep(interval)
                    try:
                        renewed = await self._redis.eval(
                            _LUA_RENEW_LOCK, 1, key, token, ttl_ms
                        )
                    except Exception as exc:
                        # Renewal must stay a single compare-and-expire
                        # operation. A GET followed by EXPIRE can extend a
                        # successor's lease after ownership changes, so an
                        # unavailable CAS primitive is treated as lease loss.
                        logger.warning(
                            "session.lock_renew_eval_failed",
                            tenant_id=resolved_tenant_id,
                            session_id=session_id,
                            error=str(exc),
                        )
                        try:
                            renewed = await _watch_compare_and_expire(
                                self._redis,
                                key,
                                token,
                                ttl_ms,
                            )
                        except Exception as fallback_exc:
                            logger.error(
                                "session.lock_renew_cas_fallback_failed",
                                tenant_id=resolved_tenant_id,
                                session_id=session_id,
                                error_class=fallback_exc.__class__.__name__,
                            )
                            renewed = 0
                    if not renewed:
                        lease.lost.set()
                        SESSION_LOCK_LOST.inc()
                        SESSION_LOCK_EVENTS.labels(event="lost").inc()
                        logger.error(
                            "session.lock_lost",
                            tenant_id=resolved_tenant_id,
                            session_id=session_id,
                            fence=fence,
                        )
                        if owner_task is not None and not owner_task.done():
                            owner_task.cancel("session lock lost")
                        return
            except asyncio.CancelledError:
                raise

        renewal_task = asyncio.create_task(renew(), name=f"session-lock-renew:{session_id}")

        try:
            yield token
            if lease.lost.is_set() and not lease.reported:
                lease.reported = True
                raise SessionLockLostError(
                    f"session lock lost:{resolved_tenant_id}/{session_id}/{fence}"
                )
        except asyncio.CancelledError as exc:
            if lease.lost.is_set():
                lease.reported = True
                raise SessionLockLostError(
                    f"session lock lost:{resolved_tenant_id}/{session_id}/{fence}"
                ) from exc
            raise
        finally:
            renewal_task.cancel()
            try:
                await renewal_task
            except asyncio.CancelledError:
                pass
            try:
                # Use EVAL (not EVALSHA + register_script) so the CAS works on
                # any Redis-compatible backend (including in-process fakes
                # that may not implement script caching).
                await self._redis.eval(_LUA_RELEASE_LOCK, 1, key, token)
            except Exception as exc:
                # Never fall back to GET+DELETE: ownership can change between
                # those operations. Leaving the key to expire is fail-safe.
                logger.warning(
                    "session.lock_release_eval_failed",
                    tenant_id=resolved_tenant_id,
                    session_id=session_id,
                    error=str(exc),
                )
                try:
                    await _watch_compare_and_delete(self._redis, key, token)
                except Exception as fallback_exc:
                    logger.error(
                        "session.lock_release_cas_fallback_failed",
                        tenant_id=resolved_tenant_id,
                        session_id=session_id,
                        error_class=fallback_exc.__class__.__name__,
                    )
            self._active_lease.reset(context_token)


# -- row <-> pydantic helpers ----------------------------------------------


def _row_to_session(row: SessionRow, turns: list[Turn]) -> Session:
    metadata = dict(row.meta or {})
    return Session(
        session_id=row.session_id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        channel=row.channel,
        adapter_id=str(metadata.get("adapter_id") or ""),
        connection_id=str(metadata.get("connection_id") or ""),
        conversation_id=str(metadata.get("conversation_id") or row.session_id),
        external_conversation_id=str(
            metadata.get("external_conversation_id") or row.session_id
        ),
        canonical_conversation_id=str(
            metadata.get("canonical_conversation_id") or row.session_id
        ),
        external_user_id=str(metadata.get("external_user_id") or row.user_id),
        external_participant_id=str(
            metadata.get("external_participant_id") or row.user_id
        ),
        canonical_participant_id=str(
            metadata.get("canonical_participant_id") or row.user_id
        ),
        state=SessionState(row.state),
        summary=row.summary,
        variables=dict(row.variables or {}),
        pii_map=dict(row.pii_map or {}),
        turns=turns,
        last_active_at=row.last_active_at,
        metadata=metadata,
    )


def _row_to_turn(row: TurnRow) -> Turn:
    from app.common.types import Citation, Role, ToolCall

    tool_calls = [ToolCall.model_validate(tc) for tc in (row.tool_calls or [])]
    citations = [Citation.model_validate(c) for c in (row.citations or [])]
    return Turn(
        turn_id=row.turn_id,
        session_id=row.session_id,
        role=Role(row.role),
        content=row.content,
        tool_calls=tool_calls,
        citations=citations,
        trace_id=row.trace_id,
        created_at=row.created_at,
        metadata=dict(row.meta or {}),
    )


def _turn_to_row(turn: Turn, *, tenant_id: str) -> TurnRow:
    return TurnRow(
        turn_id=turn.turn_id,
        tenant_id=tenant_id,
        session_id=turn.session_id,
        role=turn.role.value,
        content=turn.content,
        tool_calls=[tc.model_dump(mode="json") for tc in turn.tool_calls],
        citations=[c.model_dump(mode="json") for c in turn.citations],
        trace_id=turn.trace_id,
        meta=dict(turn.metadata),
        created_at=turn.created_at,
    )


# Exported for tests/introspection.
__all__ = [
    "SessionLease",
    "SessionLockLost",
    "SessionLockLostError",
    "SessionManager",
    "SessionTenantConflictError",
]


# Silence the unused import warning for the _db context manager helper type.
_ = Any
