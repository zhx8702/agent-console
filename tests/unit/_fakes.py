"""
Shared in-memory fakes for unit tests.

- InMemoryBus: implements the MessageBus Protocol for tests that don't need
  Redis. Preserves partition_key in headers and FIFO per stream.
- InMemoryRedis: minimal subset of the ``redis.asyncio.Redis`` surface we
  use from app/ingress (set NX EX, script_load, evalsha for our Lua script).
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

from app.bus.base import BusMessage


class InMemoryBus:
    """Minimal in-memory MessageBus implementation for tests."""

    def __init__(self) -> None:
        self.streams: dict[str, list[BusMessage]] = defaultdict(list)
        self.acked: dict[str, list[str]] = defaultdict(list)
        self.dlq: list[tuple[BusMessage, str]] = []
        self._seq = 0

    def _next_id(self) -> str:
        self._seq += 1
        return f"{int(time.time() * 1000)}-{self._seq}"

    async def publish(
        self,
        stream: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        partition_key: str | None = None,
    ) -> str:
        hdrs = dict(headers or {})
        if partition_key is not None:
            hdrs.setdefault("partition_key", partition_key)
        msg_id = self._next_id()
        self.streams[stream].append(
            BusMessage(id=msg_id, stream=stream, payload=dict(payload), headers=hdrs, attempts=0)
        )
        return msg_id

    async def ensure_group(self, stream: str, group: str) -> None:
        _ = (stream, group)
        return None

    async def consume(  # type: ignore[override]
        self,
        stream: str,
        group: str,
        consumer: str,
        handler: Callable[[BusMessage], Awaitable[None]],
        *,
        batch_size: int = 16,
        block_ms: int = 5_000,
    ):
        # Drain-and-yield loop. Used in tests by iterating once.
        _ = (group, consumer, block_ms)
        pending = list(self.streams[stream])
        self.streams[stream].clear()
        for msg in pending[:batch_size]:
            try:
                await handler(msg)
            except Exception as e:
                await self._handle_failure(stream, msg, e)
            else:
                self.acked[stream].append(msg.id)
            yield None

    async def _handle_failure(self, stream: str, msg: BusMessage, exc: BaseException) -> None:
        attempts = msg.attempts + 1
        if attempts >= 5:
            await self.move_to_dlq(msg, reason=f"max_attempts:{exc.__class__.__name__}")
            return
        # Re-enqueue with incremented attempts.
        self.streams[stream].append(
            BusMessage(
                id=self._next_id(),
                stream=stream,
                payload=msg.payload,
                headers=msg.headers,
                attempts=attempts,
            )
        )

    async def ack(self, stream: str, group: str, message_id: str) -> None:
        _ = group
        self.acked[stream].append(message_id)

    async def move_to_dlq(self, message: BusMessage, reason: str) -> None:
        self.dlq.append((message, reason))

    async def close(self) -> None:
        return None


class InMemoryRedis:
    """In-memory Redis subset for ingress tests.

    Supports:
    - set(key, value, nx=..., ex=...)
    - get(key)
    - script_load(script) / evalsha(sha, 1, key, *args) for the token-bucket.
    """

    def __init__(self) -> None:
        self._kv: dict[str, tuple[str, float | None]] = {}  # key -> (value, expires_at)
        self._hashes: dict[str, dict[str, str]] = {}
        self._scripts: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def _now(self) -> float:
        return time.time()

    def _expired(self, key: str) -> bool:
        entry = self._kv.get(key)
        if not entry:
            return False
        _, exp = entry
        if exp is not None and exp < self._now():
            del self._kv[key]
            return True
        return False

    async def set(
        self,
        key: str,
        value: str,
        nx: bool = False,
        ex: int | None = None,
    ) -> Any:
        async with self._lock:
            self._expired(key)
            if nx and key in self._kv:
                return None
            expires_at = (self._now() + ex) if ex else None
            self._kv[key] = (value, expires_at)
            return True

    async def get(self, key: str) -> str | None:
        async with self._lock:
            self._expired(key)
            entry = self._kv.get(key)
            return entry[0] if entry else None

    async def delete(self, *keys: str) -> int:
        async with self._lock:
            count = 0
            for key in keys:
                if key in self._kv:
                    del self._kv[key]
                    count += 1
            return count

    async def script_load(self, script: str) -> str:
        sha = f"sha_{len(self._scripts)}"
        self._scripts[sha] = script
        return sha

    async def evalsha(self, sha: str, numkeys: int, *args: Any) -> Any:
        _ = sha, numkeys
        # Hardcoded token-bucket behavior to match Lua.
        key = args[0]
        capacity = float(args[1])
        refill = float(args[2])
        now_ms = float(args[3])
        ttl = int(args[4])
        async with self._lock:
            bucket = self._hashes.setdefault(key, {})
            tokens = float(bucket.get("tokens", capacity))
            ts = float(bucket.get("ts", now_ms))
            delta_ms = max(0.0, now_ms - ts)
            refilled = (delta_ms / 1000.0) * refill
            tokens = min(capacity, tokens + refilled)
            allowed = 0
            if tokens >= 1.0:
                tokens -= 1.0
                allowed = 1
            bucket["tokens"] = str(tokens)
            bucket["ts"] = str(now_ms)
            # TTL ignored in fake; fine for tests.
            _ = ttl
            return allowed

    async def xadd(self, *args: Any, **kwargs: Any) -> str:
        _ = args, kwargs
        return "0-0"

    async def aclose(self) -> None:
        return None
