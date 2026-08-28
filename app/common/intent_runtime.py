"""Shared helpers for reading a semantic intent decision.

Classifiers produce :class:`IntentDecision`. Downstream routing and plugins
only map that decision; they do not scan the original wording.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.common.intent import IntentDecision, IntentDomain, IntentOperation
from app.common.types import IntentCoarse, PreprocessedMessage, Session

SEMANTIC_INTENT_VARIABLE = "semantic_intent"
_MIN_CONFIDENCE = 0.6


def decision_from_mapping(value: Any) -> IntentDecision:
    return IntentDecision.from_dict(value)


def decision_from_pre(pre: PreprocessedMessage | None) -> IntentDecision:
    if pre is None:
        return IntentDecision()
    raw = getattr(pre, "semantic_intent", None)
    return decision_from_mapping(raw)


def decision_from_session(session: Session | None) -> IntentDecision:
    if session is None:
        return IntentDecision()
    variables = session.variables if isinstance(session.variables, Mapping) else {}
    if variables.get(SEMANTIC_INTENT_VARIABLE):
        return decision_from_mapping(variables.get(SEMANTIC_INTENT_VARIABLE))
    metadata = session.metadata if isinstance(session.metadata, Mapping) else {}
    if metadata.get(SEMANTIC_INTENT_VARIABLE):
        return decision_from_mapping(metadata.get(SEMANTIC_INTENT_VARIABLE))
    return IntentDecision()


def persist_decision(
    decision: IntentDecision,
    *,
    pre: PreprocessedMessage | None = None,
    session: Session | None = None,
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = decision.to_minimal_dict()
    if pre is not None:
        pre.semantic_intent = dict(payload)
        pre.intent_coarse = coarse_from_decision(decision)
    if session is not None:
        session.variables[SEMANTIC_INTENT_VARIABLE] = dict(payload)
    if extras is not None:
        extras["semantic_intent"] = dict(payload)
    return payload


def slot_text(decision: IntentDecision, *names: str) -> str:
    for name in names:
        value = decision.slots.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def slot_bool(decision: IntentDecision, name: str, *, default: bool = False) -> bool:
    value = decision.slots.get(name)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "on"}:
            return True
        if normalized in {"false", "0", "no", "n", "off"}:
            return False
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    return default


def slot_int(decision: IntentDecision, name: str) -> int | None:
    value = decision.slots.get(name)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def is_confident(decision: IntentDecision, *, minimum: float = _MIN_CONFIDENCE) -> bool:
    return decision.confidence >= minimum and decision.domain not in {
        IntentDomain.NONE,
        IntentDomain.UNKNOWN,
    }


def coarse_from_decision(decision: IntentDecision) -> IntentCoarse:
    if decision.domain is IntentDomain.HANDOFF and decision.action in {"", "request"}:
        return IntentCoarse.HANDOFF_REQUEST
    if decision.operation is IntentOperation.HANDOFF and decision.action not in {
        "cancel",
        "non_request",
    }:
        return IntentCoarse.HANDOFF_REQUEST
    if decision.domain is IntentDomain.COMPLAINT:
        return IntentCoarse.COMPLAINT
    if decision.domain is IntentDomain.FAQ:
        return IntentCoarse.FAQ
    if decision.domain is IntentDomain.BUSINESS:
        return IntentCoarse.BUSINESS
    if decision.domain is IntentDomain.CHITCHAT:
        return IntentCoarse.CHITCHAT
    return IntentCoarse.UNKNOWN
