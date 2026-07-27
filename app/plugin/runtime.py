from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from typing import Any

from app.common.capability import CapabilityEngine
from app.common.exceptions import CapabilityError
from app.common.logging import get_logger
from app.common.types import CapabilityResult, PreprocessedMessage, Session

logger = get_logger(__name__)

DEFAULT_SESSION_GATE_TIMEOUT_SECONDS = 1.0
MAX_SESSION_GATE_TIMEOUT_SECONDS = 30.0

SessionExecutionGate = Callable[[str, Session], Awaitable[bool]]


class CapabilityOwnerExecutionDenied(CapabilityError):
    """Raised when a plugin capability is disabled at its final execution hop."""

    code = "capability_owner_execution_denied"

    def __init__(self, owner: str, *, reason: str = "owner_execution_denied") -> None:
        self.owner = str(owner or "").strip()
        self.reason = str(reason or "owner_execution_denied").strip()
        super().__init__(self.code)


class GatedCapabilityEngine:
    """Fail-closed runtime wrapper for a plugin-owned capability engine."""

    def __init__(
        self,
        owner: str,
        delegate: CapabilityEngine,
        session_gate: SessionExecutionGate | None,
        *,
        gate_timeout_seconds: float = DEFAULT_SESSION_GATE_TIMEOUT_SECONDS,
    ) -> None:
        normalized_owner = str(owner or "").strip()
        if not normalized_owner:
            raise ValueError("capability owner cannot be empty")
        self._owner = normalized_owner
        self._delegate = delegate
        self._session_gate = session_gate
        self._gate_timeout_seconds = _bounded_gate_timeout(gate_timeout_seconds)

    @property
    def name(self) -> str:
        return str(getattr(self._delegate, "name", self._owner) or self._owner)

    @property
    def owner(self) -> str:
        return self._owner

    @property
    def delegate(self) -> CapabilityEngine:
        return self._delegate

    async def answer(
        self,
        pre: PreprocessedMessage,
        session: Session,
        hints: dict[str, Any] | None = None,
    ) -> CapabilityResult:
        if not await self._execution_allowed(session):
            raise CapabilityOwnerExecutionDenied(self._owner)
        result = await self._delegate.answer(pre, session, hints)
        if not await self._execution_allowed(session):
            raise CapabilityOwnerExecutionDenied(
                self._owner,
                reason="owner_disabled_after_capability",
            )
        return result

    async def _execution_allowed(self, session: Session) -> bool:
        if self._owner == "core":
            return True
        if self._session_gate is None:
            logger.warning(
                "plugin.capability_owner_gate_denied",
                owner=self._owner,
                tenant_id=session.tenant_id,
                session_id=session.session_id,
                reason="missing_gate",
            )
            return False
        try:
            result = await asyncio.wait_for(
                self._session_gate(self._owner, session),
                timeout=self._gate_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            logger.warning(
                "plugin.capability_owner_gate_denied",
                owner=self._owner,
                tenant_id=session.tenant_id,
                session_id=session.session_id,
                reason="timeout",
            )
            return False
        except Exception as exc:
            logger.warning(
                "plugin.capability_owner_gate_denied",
                owner=self._owner,
                tenant_id=session.tenant_id,
                session_id=session.session_id,
                reason="error",
                error_class=exc.__class__.__name__,
            )
            return False
        if isinstance(result, bool):
            return result
        logger.warning(
            "plugin.capability_owner_gate_denied",
            owner=self._owner,
            tenant_id=session.tenant_id,
            session_id=session.session_id,
            reason="invalid_result",
        )
        return False


def _bounded_gate_timeout(value: float) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        timeout = DEFAULT_SESSION_GATE_TIMEOUT_SECONDS
    if not math.isfinite(timeout) or timeout <= 0:
        timeout = DEFAULT_SESSION_GATE_TIMEOUT_SECONDS
    return min(timeout, MAX_SESSION_GATE_TIMEOUT_SECONDS)


__all__ = [
    "CapabilityOwnerExecutionDenied",
    "GatedCapabilityEngine",
    "SessionExecutionGate",
]
