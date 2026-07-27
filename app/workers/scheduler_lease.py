from __future__ import annotations

import asyncio
import secrets
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from prometheus_client import Counter, Gauge

from app.common.logging import get_logger

log = get_logger(__name__)

_RENEW = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("pexpire", KEYS[1], ARGV[2])
else
    return 0
end
"""

_RELEASE = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

SCHEDULER_OWNER = Gauge(
    "scheduler_leader_owner",
    "Whether this process currently owns the scheduler leader lease",
)
SCHEDULER_LEASE_LOST = Counter(
    "scheduler_leader_lease_lost_total",
    "Scheduler leader leases lost while the process was active",
)


class SchedulerLeaseLostError(RuntimeError):
    code = "scheduler_lease_lost"


@dataclass(slots=True)
class SchedulerLeaderLease:
    redis: Any
    key: str
    ttl_seconds: int
    acquire_timeout_seconds: float
    poll_interval_seconds: float
    token: str = field(default_factory=lambda: secrets.token_hex(24))
    lost: asyncio.Event = field(default_factory=asyncio.Event)
    _renewal_task: asyncio.Task[None] | None = field(default=None, init=False)
    _owned: bool = field(default=False, init=False)

    async def acquire(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.acquire_timeout_seconds
        while True:
            acquired = await self.redis.set(
                self.key,
                self.token,
                nx=True,
                ex=self.ttl_seconds,
            )
            if acquired:
                self._owned = True
                self.lost.clear()
                SCHEDULER_OWNER.set(1)
                self._renewal_task = asyncio.create_task(
                    self._renew(),
                    name="scheduler-leader-renewal",
                )
                log.info(
                    "scheduler.lease_acquired",
                    key=self.key,
                    ttl_seconds=self.ttl_seconds,
                )
                return
            if loop.time() >= deadline:
                raise TimeoutError(
                    "scheduler leader lease was not acquired within "
                    f"{self.acquire_timeout_seconds}s"
                )
            await asyncio.sleep(self.poll_interval_seconds)

    async def _renew(self) -> None:
        interval = max(0.5, float(self.ttl_seconds) / 3.0)
        ttl_ms = int(self.ttl_seconds * 1000)
        try:
            while self._owned:
                await asyncio.sleep(interval)
                try:
                    renewed = await self.redis.eval(
                        _RENEW,
                        1,
                        self.key,
                        self.token,
                        ttl_ms,
                    )
                except Exception as exc:
                    # Ownership and TTL extension must be one atomic Redis
                    # operation. A GET+EXPIRE fallback can extend a successor's
                    # key after expiry, creating two leaders. Fail closed.
                    log.error(
                        "scheduler.lease_renew_eval_failed",
                        error_type=exc.__class__.__name__,
                    )
                    renewed = 0
                if renewed:
                    continue
                self._owned = False
                self.lost.set()
                SCHEDULER_OWNER.set(0)
                SCHEDULER_LEASE_LOST.inc()
                log.error("scheduler.lease_lost", key=self.key)
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._owned = False
            self.lost.set()
            SCHEDULER_OWNER.set(0)
            SCHEDULER_LEASE_LOST.inc()
            log.error(
                "scheduler.lease_renew_failed",
                key=self.key,
                error_type=exc.__class__.__name__,
            )

    async def release(self) -> None:
        renewal_task = self._renewal_task
        self._renewal_task = None
        if renewal_task is not None:
            renewal_task.cancel()
            with suppress(asyncio.CancelledError):
                await renewal_task

        if self._owned:
            try:
                await self.redis.eval(
                    _RELEASE,
                    1,
                    self.key,
                    self.token,
                )
            except Exception as exc:
                # Never emulate compare-and-delete with separate GET/DELETE
                # calls. The bounded TTL safely releases an unreachable lease.
                log.warning(
                    "scheduler.lease_release_eval_failed",
                    key=self.key,
                    error_type=exc.__class__.__name__,
                )
        self._owned = False
        SCHEDULER_OWNER.set(0)
        log.info("scheduler.lease_released", key=self.key)


__all__ = ["SchedulerLeaderLease", "SchedulerLeaseLostError"]
