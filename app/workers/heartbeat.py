from __future__ import annotations

import asyncio
import os
import secrets
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote

import orjson
from prometheus_client import Counter, Gauge

from app.common.config import Settings
from app.common.logging import get_logger
from app.infra.runtime_schema import (
    RUNTIME_SCHEMA_COMPATIBILITY_LEVEL,
    RUNTIME_SCHEMA_REVISION,
)

log = get_logger(__name__)

WORKER_HEARTBEAT_LAST_TIMESTAMP = Gauge(
    "worker_heartbeat_last_timestamp_seconds",
    "Unix timestamp of the last successfully published worker heartbeat",
    ["role", "instance"],
)
WORKER_HEARTBEAT_FAILURES = Counter(
    "worker_heartbeat_publish_failures_total",
    "Failed worker heartbeat publications",
    ["role"],
)
WORKER_READY_STATE = Gauge(
    "worker_ready_state",
    "Whether a worker instance has completed critical dependency initialization",
    ["role", "instance"],
)

WorkerLifecycleState = Literal["starting", "ready", "degraded", "stopping"]


class WorkerHeartbeat:
    def __init__(
        self,
        redis: Any,
        *,
        role: str,
        instance_id: str,
        key_prefix: str,
        interval_seconds: float,
        ttl_seconds: int,
    ) -> None:
        self._redis = redis
        self.role = role.strip().lower()
        self.instance_id = instance_id.strip()
        self.interval_seconds = max(0.1, float(interval_seconds))
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.owner_token = secrets.token_hex(12)
        prefix = key_prefix.rstrip(":")
        self.key = ":".join(
            (
                prefix,
                quote(self.role, safe=""),
                quote(self.instance_id, safe=""),
                self.owner_token,
            )
        )
        # The historical key namespace is consumed by /readyz and therefore
        # contains *ready instances only*.  Lifecycle/liveness is kept in a
        # separate namespace so starting or degraded processes remain
        # observable without being mistaken for ready by older API versions.
        self.liveness_key = ":".join(
            (
                prefix,
                "liveness",
                quote(self.role, safe=""),
                quote(self.instance_id, safe=""),
                self.owner_token,
            )
        )
        self.state: WorkerLifecycleState = "starting"
        self.detail = ""
        self._state_changed_at = datetime.now(UTC)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._publish_lock = asyncio.Lock()

    @classmethod
    def from_settings(cls, redis: Any, settings: Settings) -> WorkerHeartbeat:
        return cls(
            redis,
            role=settings.app_process_role,
            instance_id=settings.resolved_worker_instance_id,
            key_prefix=settings.worker_heartbeat_key_prefix,
            interval_seconds=settings.worker_heartbeat_interval_seconds,
            ttl_seconds=settings.worker_heartbeat_ttl_seconds,
        )

    async def _publish(self) -> None:
        async with self._publish_lock:
            await self._publish_locked()

    async def _publish_locked(self) -> None:
        now = datetime.now(UTC)
        payload = orjson.dumps(
            {
                "role": self.role,
                "instance_id": self.instance_id,
                "owner_token": self.owner_token,
                "pid": os.getpid(),
                "state": self.state,
                "detail": self.detail,
                # These are the worker code's schema contract, not a sampled
                # database value.  API readiness can therefore reject a live
                # heartbeat from an older deployment even while both versions
                # temporarily share Redis during a rolling release.
                "schema_revision": RUNTIME_SCHEMA_REVISION,
                "schema_compatibility": RUNTIME_SCHEMA_COMPATIBILITY_LEVEL,
                "state_changed_at": self._state_changed_at.isoformat(),
                "heartbeat_at": now.isoformat(),
            }
        ).decode()
        # Delete readiness first on every non-ready transition.  If Redis is
        # unavailable the API's own Redis probe fails closed; once it recovers,
        # the periodic heartbeat repeats this invalidation.
        if self.state != "ready":
            await self._redis.delete(self.key)
        await self._redis.set(self.liveness_key, payload, ex=self.ttl_seconds)
        if self.state == "ready":
            await self._redis.set(self.key, payload, ex=self.ttl_seconds)
        WORKER_HEARTBEAT_LAST_TIMESTAMP.labels(
            role=self.role,
            instance=self.instance_id,
        ).set(now.timestamp())
        WORKER_READY_STATE.labels(
            role=self.role,
            instance=self.instance_id,
        ).set(1 if self.state == "ready" else 0)

    async def set_state(
        self,
        state: WorkerLifecycleState,
        *,
        detail: str = "",
    ) -> None:
        self.state = state
        self.detail = str(detail or "")[:256]
        self._state_changed_at = datetime.now(UTC)
        try:
            await self._publish()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            WORKER_HEARTBEAT_FAILURES.labels(role=self.role).inc()
            log.warning(
                "worker.heartbeat_transition_failed",
                role=self.role,
                instance=self.instance_id,
                state=state,
                error_type=exc.__class__.__name__,
            )

    async def mark_ready(self) -> None:
        await self.set_state("ready")

    async def mark_degraded(self, detail: str) -> None:
        await self.set_state("degraded", detail=detail)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._publish()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                WORKER_HEARTBEAT_FAILURES.labels(role=self.role).inc()
                log.warning(
                    "worker.heartbeat_failed",
                    role=self.role,
                    instance=self.instance_id,
                    error_type=exc.__class__.__name__,
                )
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self.interval_seconds,
                )
            except TimeoutError:
                continue

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        await self.set_state("starting")
        self._task = asyncio.create_task(
            self._run(),
            name=f"worker-heartbeat:{self.role}",
        )

    async def stop(
        self,
        *,
        final_state: WorkerLifecycleState = "stopping",
        detail: str = "",
    ) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            with suppress(asyncio.CancelledError):
                await task
        await self.set_state(final_state, detail=detail)
        WORKER_HEARTBEAT_LAST_TIMESTAMP.remove(self.role, self.instance_id)
        WORKER_READY_STATE.remove(self.role, self.instance_id)


__all__ = ["WorkerHeartbeat", "WorkerLifecycleState"]
