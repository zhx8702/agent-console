from __future__ import annotations

from redis.asyncio import Redis, from_url

from app.common.config import get_settings

_redis: Redis | None = None


def get_redis() -> Redis:
    global _redis
    if _redis is None:
        s = get_settings()
        # Redis Streams consumers use blocking XREADGROUP calls. Keep the socket
        # timeout comfortably above the configured block interval so an empty
        # stream poll returns normally instead of surfacing as a read timeout.
        socket_timeout = max(10.0, (int(s.bus_consume_block_ms or 5_000) / 1000.0) + 5.0)
        _redis = from_url(
            s.redis_url,
            decode_responses=True,
            encoding="utf-8",
            socket_timeout=socket_timeout,
            socket_connect_timeout=5.0,
        )
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
    _redis = None
