from __future__ import annotations

import asyncio
import json

import pytest

from app.infra.runtime_schema import (
    RUNTIME_SCHEMA_COMPATIBILITY_LEVEL,
    RUNTIME_SCHEMA_REVISION,
)
from app.workers.heartbeat import WorkerHeartbeat


class _HeartbeatRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expiries: dict[str, int] = {}
        self.published = asyncio.Event()
        self.history: list[tuple[str, str]] = []

    async def set(self, key, value, *, ex):
        self.values[str(key)] = str(value)
        self.expiries[str(key)] = int(ex)
        self.history.append((str(key), str(value)))
        self.published.set()
        return True

    async def delete(self, key):
        self.values.pop(str(key), None)
        self.expiries.pop(str(key), None)
        return 1


@pytest.mark.asyncio
async def test_worker_heartbeat_separates_liveness_from_readiness_state() -> None:
    redis = _HeartbeatRedis()
    heartbeat = WorkerHeartbeat(
        redis,
        role="inbound",
        instance_id="worker/a",
        key_prefix="heartbeat",
        interval_seconds=0.02,
        ttl_seconds=3,
    )

    await heartbeat.start()
    await asyncio.wait_for(redis.published.wait(), timeout=0.5)

    assert heartbeat.key.startswith("heartbeat:inbound:worker%2Fa:")
    assert heartbeat.liveness_key.startswith(
        "heartbeat:liveness:inbound:worker%2Fa:"
    )
    assert heartbeat.key not in redis.values
    payload = json.loads(redis.values[heartbeat.liveness_key])
    assert payload["role"] == "inbound"
    assert payload["instance_id"] == "worker/a"
    assert payload["owner_token"] == heartbeat.owner_token
    assert payload["state"] == "starting"
    assert payload["schema_revision"] == RUNTIME_SCHEMA_REVISION
    assert payload["schema_compatibility"] == RUNTIME_SCHEMA_COMPATIBILITY_LEVEL
    assert redis.expiries[heartbeat.liveness_key] == 3

    await heartbeat.mark_ready()
    ready_payload = json.loads(redis.values[heartbeat.key])
    assert ready_payload["state"] == "ready"
    assert ready_payload["schema_revision"] == RUNTIME_SCHEMA_REVISION
    assert ready_payload["schema_compatibility"] == RUNTIME_SCHEMA_COMPATIBILITY_LEVEL
    assert redis.expiries[heartbeat.key] == 3

    await heartbeat.mark_degraded("dependency_lost")
    assert heartbeat.key not in redis.values
    degraded_payload = json.loads(redis.values[heartbeat.liveness_key])
    assert degraded_payload["state"] == "degraded"
    assert degraded_payload["detail"] == "dependency_lost"

    await heartbeat.mark_ready()

    await heartbeat.stop()
    assert heartbeat.key not in redis.values
    stopping_payload = json.loads(redis.values[heartbeat.liveness_key])
    assert stopping_payload["state"] == "stopping"
    assert redis.expiries[heartbeat.liveness_key] == 3
