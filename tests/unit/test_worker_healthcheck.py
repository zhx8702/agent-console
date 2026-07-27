from __future__ import annotations

from fnmatch import fnmatch

import pytest

from app.common.config import Settings
from app.workers.healthcheck import worker_is_ready
from app.workers.readiness import RoleReadiness


class FakeRedis:
    def __init__(self, ttls: dict[str, int], *, fail: bool = False) -> None:
        self._ttls = ttls
        self._fail = fail
        self.pattern: str | None = None

    async def scan_iter(self, *, match: str, count: int):
        assert count == 20
        self.pattern = match
        if self._fail:
            raise ConnectionError("redis unavailable")
        for key in self._ttls:
            if fnmatch(key, match):
                yield key

    async def ttl(self, key: str) -> int:
        return self._ttls[key]


async def _dependencies_ready(
    role: str,
    _settings: Settings,
    _redis: object,
) -> RoleReadiness:
    return RoleReadiness(
        role=role,
        checks={"redis": True, "db": True},
        required=("redis", "db"),
        errors=(),
    )


@pytest.mark.asyncio
async def test_worker_healthcheck_accepts_live_ready_heartbeat() -> None:
    redis = FakeRedis({"agent-console:worker:heartbeat:inbound:worker-a:token": 10})
    settings = Settings(app_env="test", worker_instance_id="worker-a")

    assert await worker_is_ready(
        "inbound",
        settings=settings,
        redis=redis,
        dependency_probe=_dependencies_ready,
    )
    assert redis.pattern == "agent-console:worker:heartbeat:inbound:worker-a:*"


@pytest.mark.asyncio
async def test_worker_healthcheck_rejects_expired_or_unknown_role() -> None:
    redis = FakeRedis(
        {"agent-console:worker:heartbeat:scheduler:container-a-42:token": 0}
    )
    settings = Settings(app_env="test")

    assert not await worker_is_ready(
        "scheduler",
        settings=settings,
        redis=redis,
        hostname="container-a",
        dependency_probe=_dependencies_ready,
    )
    assert not await worker_is_ready("api", settings=settings, redis=redis)


@pytest.mark.asyncio
async def test_worker_healthcheck_fails_closed_when_redis_is_unavailable() -> None:
    settings = Settings(app_env="test")

    assert not await worker_is_ready(
        "wxbot_bridge",
        settings=settings,
        redis=FakeRedis({}, fail=True),
        hostname="container-a",
        dependency_probe=_dependencies_ready,
    )


@pytest.mark.asyncio
async def test_worker_healthcheck_does_not_accept_another_replica() -> None:
    redis = FakeRedis(
        {
            "agent-console:worker:heartbeat:inbound:container-b-7:token": 10,
        }
    )
    settings = Settings(app_env="test")

    assert not await worker_is_ready(
        "inbound",
        settings=settings,
        redis=redis,
        hostname="container-a",
        dependency_probe=_dependencies_ready,
    )
    assert redis.pattern == "agent-console:worker:heartbeat:inbound:container-a-*:*"


@pytest.mark.asyncio
async def test_worker_healthcheck_rejects_live_heartbeat_when_role_dependency_is_down() -> None:
    redis = FakeRedis({"agent-console:worker:heartbeat:outbound:worker-a:token": 10})
    settings = Settings(app_env="test", worker_instance_id="worker-a")

    async def db_down(
        role: str,
        _settings: Settings,
        _redis: object,
    ) -> RoleReadiness:
        return RoleReadiness(
            role=role,
            checks={"redis": True, "db": False},
            required=("redis", "db"),
            errors=("db_unreachable",),
        )

    assert not await worker_is_ready(
        "outbound",
        settings=settings,
        redis=redis,
        dependency_probe=db_down,
    )
