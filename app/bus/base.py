"""
Message bus interface.

Implementations: Redis Streams (default), can be swapped for Kafka in prod.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class BusMessage:
    id: str
    stream: str
    payload: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)
    attempts: int = 0


class PermanentMessageError(RuntimeError):
    """Tell a consumer to atomically persist this entry to its DLQ."""

    def __init__(self, reason: str) -> None:
        normalized = str(reason or "permanent:processing_failure").strip()
        super().__init__(normalized)
        self.reason = normalized


class MessagePublishIdempotencyConflict(RuntimeError):
    """An idempotency key was already bound to another transport payload."""


class MessageBus(Protocol):
    async def publish(
        self,
        stream: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        partition_key: str | None = None,
    ) -> str:
        ...

    async def ensure_group(self, stream: str, group: str) -> None:
        ...

    def consume(
        self,
        stream: str,
        group: str,
        consumer: str,
        handler: Callable[[BusMessage], Awaitable[None]],
        *,
        batch_size: int = 16,
        block_ms: int = 5_000,
    ) -> AsyncIterator[None]:
        ...

    async def ack(self, stream: str, group: str, message_id: str) -> None:
        ...

    async def move_to_dlq(self, message: BusMessage, reason: str) -> None:
        ...

    async def close(self) -> None:
        ...


@runtime_checkable
class IdempotentMessagePublisher(Protocol):
    """Transport capability required by the durable database outbox.

    Implementations must atomically bind ``idempotency_key`` to the canonical
    delivery payload. Reusing the key with the same payload returns the first
    message id; reusing it with a different payload must fail closed.
    """

    async def publish_once(
        self,
        stream: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        headers: dict[str, str] | None = None,
        partition_key: str | None = None,
    ) -> str:
        ...
