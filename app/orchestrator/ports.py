"""Typed dependency boundaries for the dialog orchestrator.

The runtime deliberately depends on these small structural contracts instead
of concrete implementations.  Production services and lightweight test fakes
can therefore satisfy the same interface without dynamic ``Any`` injection.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from app.bus.base import BusMessage
from app.common.types import (
    CapabilityResult,
    Channel,
    Message,
    OutboundReply,
    PreprocessedMessage,
    RouteDecision,
    Session,
    SessionState,
    Turn,
)


class FlowSessionPort(Protocol):
    """Session operations used by core flow steps."""

    async def load(
        self,
        tenant_id: str,
        user_id: str,
        session_id: str,
        channel: Channel,
    ) -> Session: ...

    async def append_turn(self, session: Session, turn: Turn) -> None: ...

    async def set_state(self, session: Session, new_state: SessionState) -> None: ...


class SessionPort(FlowSessionPort, Protocol):
    """Full session boundary required by the transactional orchestrator."""

    def lock(
        self,
        session_id: str,
        *,
        tenant_id: str | None = None,
        acquire_timeout: float = 5.0,
        poll_interval: float = 0.05,
    ) -> AbstractAsyncContextManager[str]: ...

    def stage(self) -> AbstractAsyncContextManager[None]: ...

    def transaction(self) -> AbstractAsyncContextManager[AsyncSession]: ...

    async def flush_stage(self, db: AsyncSession) -> None: ...


class PreprocessorPort(Protocol):
    async def run(self, message: Message) -> PreprocessedMessage: ...


class RouterPort(Protocol):
    async def decide(
        self,
        pre: PreprocessedMessage,
        session: Session,
        signals: dict[str, Any] | None = None,
    ) -> RouteDecision: ...


class SafetyPort(Protocol):
    async def check_input(self, pre: PreprocessedMessage) -> bool: ...

    async def check_output(self, result: CapabilityResult) -> bool: ...


class PostprocessorPort(Protocol):
    async def run(
        self,
        result: CapabilityResult,
        session: Session,
    ) -> OutboundReply: ...


@runtime_checkable
class FaqPreviewCapabilityPort(Protocol):
    """Optional FAQ capability used to enrich router signals."""

    async def preview_match(
        self,
        pre: PreprocessedMessage,
        session: Session,
        hints: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


class OrchestratorBusPort(Protocol):
    """Message-bus surface exercised by the orchestrator and flow steps."""

    async def publish(
        self,
        stream: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        partition_key: str | None = None,
    ) -> str: ...

    async def ensure_group(self, stream: str, group: str) -> None: ...

    def consume(
        self,
        stream: str,
        group: str,
        consumer: str,
        handler: Callable[[BusMessage], Awaitable[None]],
        *,
        batch_size: int = 16,
        block_ms: int = 5_000,
    ) -> AsyncIterator[None]: ...

    async def ack(self, stream: str, group: str, message_id: str) -> None: ...

    async def move_to_dlq(self, message: BusMessage, reason: str) -> None: ...

    async def close(self) -> None: ...


class PluginRegistryPort(Protocol):
    """Registry identity exposed to worker adapters after startup wiring."""

    @property
    def loaded_plugins(self) -> Mapping[str, object]: ...
