"""
Pipeline helpers for the orchestrator.

The full M3 pipeline (load → preprocess → route → capability → safety →
postprocess → publish) is implemented as a single async method in
:mod:`app.orchestrator.engine` to keep observability straightforward — each
step is wrapped in its own OpenTelemetry span so we can trace without needing
a full step-framework here.

This module exposes a lightweight :class:`PipelineContext` dataclass so the
orchestrator can pass intermediate results around cleanly (and so tests can
introspect them), plus a :class:`PipelineStep` Protocol for future extension
points (e.g. plugins that want to hook into the chain).

``extras`` remains the compatibility surface for existing hooks. New flow
work should prefer ``signals`` for cross-step facts, ``effects`` for
auditable side effects, and ``scratch`` for owner-local temporary state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.common.types import (
    CapabilityResult,
    InboundEvent,
    OutboundReply,
    PreprocessedMessage,
    RouteDecision,
    Session,
)


@dataclass
class PipelineContext:
    """Mutable state carried through the orchestrator pipeline."""

    event: InboundEvent
    trace_id: str
    session: Session | None = None
    pre: PreprocessedMessage | None = None
    route: RouteDecision | None = None
    result: CapabilityResult | None = None
    reply: OutboundReply | None = None
    signals: dict[str, Any] = field(default_factory=dict)
    effects: list[Any] = field(default_factory=list)
    scratch: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)


class PipelineStep(Protocol):
    """Optional extension point — not required by the default orchestrator."""

    name: str

    async def run(self, ctx: PipelineContext) -> None: ...
