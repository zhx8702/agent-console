"""
Per-tenant daily token quota enforcement backed by Redis.

Key format: ``llm:quota:{tenant_id}:{YYYY-MM-DD}`` (UTC day bucket)
Value:      integer total tokens consumed today.

Design:
- ``reserve_tokens(tenant, estimate)`` atomically increments the counter by
  the estimated input tokens. If the new total would exceed the daily limit,
  the increment is rolled back and :class:`QuotaExceeded` is raised.
- ``commit(tenant, actual)`` adjusts the counter by the delta between the
  estimate and the real total tokens once a request has been served.
- On first write the key is given a 48-hour TTL so stale buckets expire.

The application factory passes the validated ``Settings`` value explicitly.
Direct construction retains the legacy ``TENANT_DEFAULT_DAILY_TOKENS``
environment fallback for compatibility. Set the limit to ``0`` or a negative
value to disable daily quota enforcement.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from app.common.exceptions import QuotaExceeded
from app.common.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_DAILY_TOKENS = 1_000_000
_TTL_SECONDS = 60 * 60 * 48  # 48 hours


def _today_key(tenant_id: str) -> str:
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    return f"llm:quota:{tenant_id}:{day}"


class QuotaTracker:
    """Track and enforce per-tenant daily token usage in Redis."""

    def __init__(
        self,
        redis: Any,
        *,
        default_daily_tokens: int | None = None,
        per_tenant_limits: dict[str, int] | None = None,
    ) -> None:
        self._redis = redis
        env_limit = os.getenv("TENANT_DEFAULT_DAILY_TOKENS")
        if default_daily_tokens is not None:
            self._default_limit = default_daily_tokens
        elif env_limit is not None:
            try:
                self._default_limit = int(env_limit)
            except ValueError:
                self._default_limit = _DEFAULT_DAILY_TOKENS
        else:
            self._default_limit = _DEFAULT_DAILY_TOKENS
        self._per_tenant = dict(per_tenant_limits or {})

    def limit_for(self, tenant_id: str) -> int:
        """Return the tenant daily token limit.

        A value of ``0`` or below means quota enforcement is disabled for the
        tenant while usage reconciliation can still be recorded by callers.
        """
        return self._per_tenant.get(tenant_id, self._default_limit)

    async def _incrby(self, key: str, amount: int) -> int:
        new_total = await self._redis.incrby(key, amount)
        # Best-effort TTL. Only set when the key is brand new (TTL == -1).
        try:
            ttl = await self._redis.ttl(key)
            if ttl is None or ttl < 0:
                await self._redis.expire(key, _TTL_SECONDS)
        except Exception as exc:
            logger.warning(
                "llm.quota_ttl_refresh_failed",
                error_class=exc.__class__.__name__,
            )
        return int(new_total)

    async def _decrby(self, key: str, amount: int) -> int:
        return int(await self._redis.decrby(key, amount))

    async def reserve_tokens(self, tenant_id: str, estimate: int) -> int:
        """Atomically reserve ``estimate`` tokens. Raises :class:`QuotaExceeded`.

        Returns the new total after reservation.
        """
        if estimate <= 0:
            estimate = 1
        limit = self.limit_for(tenant_id)
        if limit <= 0:
            logger.debug("llm.quota.disabled", tenant_id=tenant_id, limit=limit)
            return await self.usage(tenant_id)
        key = _today_key(tenant_id)
        new_total = await self._incrby(key, estimate)
        if new_total > limit:
            # Roll back the optimistic reservation.
            await self._decrby(key, estimate)
            logger.warning(
                "llm.quota.exceeded",
                tenant_id=tenant_id,
                limit=limit,
                would_be=new_total,
                estimate=estimate,
            )
            raise QuotaExceeded(f"daily token quota exceeded for tenant={tenant_id}")
        return new_total

    async def commit(self, tenant_id: str, actual: int, estimate: int) -> int:
        """Reconcile the reservation with the *real* usage.

        If ``actual`` > ``estimate`` the difference is added; if lower we
        subtract the refund. Returns the current total.
        """
        delta = int(actual) - int(estimate)
        if delta == 0:
            return 0
        key = _today_key(tenant_id)
        if delta > 0:
            return await self._incrby(key, delta)
        return await self._decrby(key, -delta)

    async def usage(self, tenant_id: str) -> int:
        key = _today_key(tenant_id)
        val = await self._redis.get(key)
        if val is None:
            return 0
        try:
            return int(val)
        except (TypeError, ValueError):
            return 0
