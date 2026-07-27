from __future__ import annotations

import asyncio

import pytest

from app.common.config import Settings
from app.common.exceptions import SessionLockLost
from app.common.types import Channel, Session
from app.session.manager import (
    SessionLockLostError,
    SessionManager,
    _persisted_variables,
    _serialize_session_for_cache,
)


class _LockRedis:
    def __init__(self) -> None:
        self.value = None
        self.renewals = 0

    async def set(self, _key, value, **_kwargs):
        if self.value is not None:
            return False
        self.value = value
        return True

    async def eval(self, script, _keys, _key, token, *args):
        if "pexpire" in script:
            if self.value == token:
                self.renewals += 1
                return 1
            return 0
        if self.value == token:
            self.value = None
            return 1
        return 0


class _LosingLockRedis(_LockRedis):
    async def eval(self, script, _keys, _key, token, *args):
        if "pexpire" in script:
            # Simulate expiry/reassignment before the first renewal.
            self.value = "another-worker:999"
            return 0
        return await super().eval(script, _keys, _key, token, *args)


class _UnavailableCasRedis(_LockRedis):
    async def eval(self, script, _keys, _key, token, *args):
        if "pexpire" in script:
            self.value = "successor-worker:1000"
        raise RuntimeError("redis eval unavailable")


@pytest.mark.asyncio
async def test_session_lock_renews_until_context_exits() -> None:
    redis = _LockRedis()
    manager = SessionManager(redis, Settings(session_lock_ttl_seconds=1))  # type: ignore[arg-type]
    async with manager.lock("group@chatroom", tenant_id="demo"):
        await asyncio.sleep(0.45)
        assert redis.renewals >= 1
    assert redis.value is None


@pytest.mark.asyncio
async def test_session_lock_loss_cancels_owner_with_a_domain_error() -> None:
    redis = _LosingLockRedis()
    manager = SessionManager(redis, Settings(session_lock_ttl_seconds=1))  # type: ignore[arg-type]

    with pytest.raises(SessionLockLostError, match="session lock lost"):
        async with manager.lock("group@chatroom", tenant_id="demo"):
            await asyncio.sleep(1)

    # CAS release must never delete the replacement owner's lock.
    assert redis.value == "another-worker:999"


@pytest.mark.asyncio
async def test_session_lock_cas_failure_never_renews_or_deletes_successor() -> None:
    redis = _UnavailableCasRedis()
    manager = SessionManager(redis, Settings(session_lock_ttl_seconds=1))  # type: ignore[arg-type]

    with pytest.raises(SessionLockLostError, match="session lock lost"):
        async with manager.lock("group@chatroom", tenant_id="demo"):
            await asyncio.sleep(1)

    assert redis.value == "successor-worker:1000"


@pytest.mark.asyncio
async def test_session_lock_loss_interrupts_holder() -> None:
    class _LostRedis(_LockRedis):
        async def eval(self, script, _keys, _key, token, *args):
            if "pexpire" in script:
                self.value = "another-owner"
                return 0
            return 0

    manager = SessionManager(
        _LostRedis(), Settings(session_lock_ttl_seconds=1)
    )  # type: ignore[arg-type]
    with pytest.raises(SessionLockLost):
        async with manager.lock("group@chatroom", tenant_id="demo"):
            await asyncio.sleep(1.0)


@pytest.mark.asyncio
async def test_session_lock_loss_is_reported_when_holder_swallows_cancellation() -> None:
    redis = _LosingLockRedis()
    manager = SessionManager(redis, Settings(session_lock_ttl_seconds=1))  # type: ignore[arg-type]

    with pytest.raises(SessionLockLostError, match="session lock lost"):
        async with manager.lock("group@chatroom", tenant_id="demo"):
            try:
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                pass

    assert redis.value == "another-worker:999"


def test_group_session_snapshot_strips_private_memory_and_actor_owner() -> None:
    session = Session(
        session_id="group@chatroom",
        tenant_id="demo",
        user_id="wxid_current_actor",
        channel=Channel.WECHAT,
        variables={
            "user_memory": {"private": True},
            "group_memory": {"shared": True},
            "group_observation_context": {"recent_text": "request scoped"},
            "persona_profile": {"name": "bot"},
        },
        pii_map={"<PHONE_1>": "13800138000"},
    )
    assert _persisted_variables(session) == {"persona_profile": {"name": "bot"}}
    payload = _serialize_session_for_cache(session)
    assert '"user_id":"group@chatroom"' in payload
    assert "user_memory" not in payload
    assert "group_observation_context" not in payload
    assert "13800138000" not in payload
