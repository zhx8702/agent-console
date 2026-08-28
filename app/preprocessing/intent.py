"""Map a semantic intent decision onto the coarse router label."""

from __future__ import annotations

from app.common.intent import IntentDecision
from app.common.intent_runtime import coarse_from_decision, decision_from_mapping
from app.common.types import IntentCoarse


def classify_intent(
    text: str = "",
    *,
    decision: IntentDecision | dict | None = None,
) -> IntentCoarse:
    """Return the router coarse label.

    Text is ignored.  Without a semantic decision the label is unknown so
    wording cannot invent a route.
    """

    _ = text
    if decision is None:
        return IntentCoarse.UNKNOWN
    parsed = decision if isinstance(decision, IntentDecision) else decision_from_mapping(decision)
    return coarse_from_decision(parsed)
