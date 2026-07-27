"""
Thin adapters that bridge concrete module APIs to the async Protocols the
orchestrator expects. Modules were implemented by separate teams and picked
slightly different shapes (sync vs. async; tuple returns vs. bool); these
adapters normalize those differences in one place.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.common.types import CapabilityResult, PreprocessedMessage, RouteDecision, Session

if TYPE_CHECKING:
    from app.router.engine import Router
    from app.safety.service import SafetyService


class AsyncRouterAdapter:
    """Wrap the sync Router to present the orchestrator's async API."""

    def __init__(self, router: Router) -> None:
        self._r = router

    async def decide(
        self,
        pre: PreprocessedMessage,
        session: Session,
        signals: dict[str, object] | None = None,
    ) -> RouteDecision:
        return self._r.decide(pre, session, signals=signals)


class AsyncSafetyAdapter:
    """
    Orchestrator wants ``(async) -> bool`` where True = safe.
    SafetyService returns ``(blocked: bool, reason: str | None)``.
    check_output in the underlying service takes a text string; the
    orchestrator passes a CapabilityResult, so we extract reply_text here.
    """

    def __init__(self, safety: SafetyService) -> None:
        self._s = safety

    async def check_input(self, pre: PreprocessedMessage) -> bool:
        blocked, _reason = self._s.check_input(pre)
        return not blocked

    async def check_output(self, result: CapabilityResult) -> bool:
        blocked, _reason = self._s.check_output(result.reply_text or "")
        return not blocked
