"""
CapabilityEngine protocol: shared interface for FAQ / RAG / Agent / LLM / Handoff engines.

Orchestrator holds a registry keyed by RouteType and dispatches uniformly.
"""
from __future__ import annotations

from typing import Any, Protocol

from app.common.types import CapabilityResult, PreprocessedMessage, Session


class CapabilityEngine(Protocol):
    name: str

    async def answer(
        self,
        pre: PreprocessedMessage,
        session: Session,
        hints: dict[str, Any] | None = None,
    ) -> CapabilityResult: ...
