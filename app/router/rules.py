"""Router rule dataclass + YAML loader + evaluator.

Rules are declarative and evaluated top-to-bottom; the first matching rule
wins. See ``config/router.yaml`` for the authoritative schema.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.common.exceptions import ConfigError
from app.common.types import (
    EmotionLabel,
    IntentCoarse,
    PreprocessedMessage,
    RouteDecision,
    RouteType,
    Session,
)

_SUPPORTED_WHEN_KEYS: frozenset[str] = frozenset(
    {
        "sensitive",
        "intent_coarse",
        "emotion",
        "faq_similarity_gte",
        "tools_available",
        "consecutive_fallbacks_gte",
    }
)


@dataclass
class Rule:
    name: str
    when: dict[str, Any] = field(default_factory=dict)
    route: RouteType = RouteType.LLM
    reason: str = ""


def load_rules(path: str | Path) -> list[Rule]:
    p = Path(path)
    if not p.is_absolute():
        # resolve relative to project root (cs-system/)
        from app.common.config import get_settings

        p = get_settings().project_root / p
    if not p.exists():
        raise ConfigError(f"router config not found: {p}")

    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:  # pragma: no cover - defensive
        raise ConfigError(f"invalid router yaml: {e}") from e

    items = raw.get("rules") or []
    if not isinstance(items, list):
        raise ConfigError("router yaml 'rules' must be a list")

    rules: list[Rule] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ConfigError(f"rule #{i} must be a mapping")
        name = item.get("name") or f"rule_{i}"
        when = item.get("when") or {}
        if not isinstance(when, dict):
            raise ConfigError(f"rule {name!r} 'when' must be a mapping")
        unknown = set(when.keys()) - _SUPPORTED_WHEN_KEYS
        if unknown:
            raise ConfigError(
                f"rule {name!r} has unsupported 'when' keys: {sorted(unknown)}"
            )
        try:
            route = RouteType(item["route"])
        except (KeyError, ValueError) as e:
            raise ConfigError(f"rule {name!r} has invalid route: {item.get('route')!r}") from e
        reason = item.get("reason") or name
        rules.append(Rule(name=name, when=dict(when), route=route, reason=reason))

    return rules


def _coerce_intent(value: Any) -> str | None:
    if isinstance(value, IntentCoarse):
        return value.value
    if isinstance(value, str):
        return value.lower()
    return None


def _coerce_emotion(value: Any) -> str | None:
    if isinstance(value, EmotionLabel):
        return value.value
    if isinstance(value, str):
        return value.lower()
    return None


def _match(rule: Rule, pre: PreprocessedMessage, signals: dict[str, Any]) -> bool:
    when = rule.when
    if not when:
        return True  # default rule

    if "sensitive" in when:
        expected = bool(when["sensitive"])
        if bool(pre.sensitive) != expected:
            return False

    if "intent_coarse" in when:
        expected = _coerce_intent(when["intent_coarse"])
        actual = pre.intent_coarse.value if pre.intent_coarse else None
        if actual != expected:
            return False

    if "emotion" in when:
        expected = _coerce_emotion(when["emotion"])
        actual = pre.emotion.value if pre.emotion else None
        if actual != expected:
            return False

    if "faq_similarity_gte" in when:
        threshold = float(when["faq_similarity_gte"])
        sim = float(signals.get("faq_similarity", 0.0) or 0.0)
        if sim < threshold:
            return False

    if "tools_available" in when:
        expected = bool(when["tools_available"])
        actual = bool(signals.get("tools_available", False))
        if actual != expected:
            return False

    if "consecutive_fallbacks_gte" in when:
        threshold = int(when["consecutive_fallbacks_gte"])
        cur = int(signals.get("consecutive_fallbacks", 0) or 0)
        if cur < threshold:
            return False

    return True


def evaluate(
    rules: list[Rule],
    pre: PreprocessedMessage,
    session: Session | None,
    signals: dict[str, Any] | None = None,
) -> RouteDecision:
    """Return the first matching rule's RouteDecision.

    ``signals`` is a free-form dict carrying runtime hints:
      - faq_similarity (float)
      - tools_available (bool)
      - consecutive_fallbacks (int)
    """
    sig = dict(signals or {})

    for rule in rules:
        if _match(rule, pre, sig):
            return RouteDecision(
                type=rule.route,
                confidence=1.0 if not rule.when else 0.9,
                reason=rule.reason,
                hints={"rule": rule.name},
            )

    # No rule matched (shouldn't happen if a default rule exists). Fall back to LLM.
    return RouteDecision(
        type=RouteType.LLM,
        confidence=0.1,
        reason="no rule matched (implicit fallback)",
        hints={"rule": "__implicit_default__"},
    )
