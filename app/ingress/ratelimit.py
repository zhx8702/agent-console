"""
Token-bucket rate limiter backed by Redis Lua for atomicity.

Each tenant has a bucket with capacity = qps and a refill rate of qps
tokens/second. One EVAL call both consumes a token and tells us whether the
request is allowed. The bucket state is stored as a hash with fields
`tokens` (float) and `ts` (ms since epoch).
"""
from __future__ import annotations

from redis.asyncio import Redis

# KEYS[1] = bucket key
# ARGV[1] = capacity (tokens)
# ARGV[2] = refill_rate (tokens/second)
# ARGV[3] = now_ms
# ARGV[4] = ttl_seconds
# Returns 1 if allowed, 0 if rate limited.
_LUA = """
local bucket = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])

local data = redis.call('HMGET', bucket, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])

if tokens == nil then
  tokens = capacity
  ts = now
end

local delta_ms = math.max(0, now - ts)
local refilled = (delta_ms / 1000.0) * refill
tokens = math.min(capacity, tokens + refilled)

local allowed = 0
if tokens >= 1.0 then
  tokens = tokens - 1.0
  allowed = 1
end

redis.call('HMSET', bucket, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', bucket, ttl)
return allowed
"""


class TokenBucketRateLimiter:
    """Per-tenant token bucket, Redis Lua-backed."""

    def __init__(self, redis: Redis, *, capacity: int, refill_per_second: float | None = None) -> None:
        self._redis = redis
        self._capacity = capacity
        self._refill = refill_per_second if refill_per_second is not None else float(capacity)
        self._script_sha: str | None = None

    async def _ensure_script(self) -> str:
        if self._script_sha is None:
            self._script_sha = await self._redis.script_load(_LUA)
        return self._script_sha

    async def allow(self, tenant_id: str, *, now_ms: int) -> bool:
        sha = await self._ensure_script()
        key = f"ratelimit:{tenant_id}"
        ttl = max(60, int(self._capacity / max(self._refill, 1.0)) * 4)
        try:
            result = await self._redis.evalsha(
                sha,
                1,
                key,
                str(self._capacity),
                str(self._refill),
                str(now_ms),
                str(ttl),
            )
        except Exception:
            # Script may have been flushed; reload and retry once.
            self._script_sha = None
            sha = await self._ensure_script()
            result = await self._redis.evalsha(
                sha,
                1,
                key,
                str(self._capacity),
                str(self._refill),
                str(now_ms),
                str(ttl),
            )
        return int(result) == 1
