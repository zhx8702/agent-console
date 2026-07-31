"""Router rule dataclass + YAML loader + evaluator.

Rules are declarative and evaluated top-to-bottom; the first matching rule
wins. See ``config/router.yaml`` for the authoritative schema.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from numbers import Real
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
        "faq_matched",
        "faq_verdict",
        "faq_similarity_gte",
        "tool_intent_matched",
        "tools_available",
        "consecutive_fallbacks_gte",
    }
)

_FAQ_VERDICTS: frozenset[str] = frozenset(
    {"CLEAR", "AMBIGUOUS", "INSUFFICIENT", "LOW"}
)
_CONSECUTIVE_FALLBACKS_KEY = "consecutive_fallbacks"


@dataclass
class Rule:
    name: str
    when: dict[str, Any] = field(default_factory=dict)
    route: RouteType = RouteType.LLM
    reason: str = ""


def _strict_bool(value: Any) -> bool | None:
    return value if type(value) is bool else None


def _finite_float(
    value: Any,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    result = float(value)
    if not math.isfinite(result):
        return None
    if minimum is not None and result < minimum:
        return None
    if maximum is not None and result > maximum:
        return None
    return result


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or not numeric.is_integer() or numeric < 0:
        return None
    return int(numeric)


def _normalize_faq_verdict(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    verdict = value.strip().upper()
    return verdict if verdict in _FAQ_VERDICTS else None


def normalize_router_signals(signals: dict[str, Any] | None) -> dict[str, Any]:
    """Return routing signals with strict, fail-closed scalar types.

    Unknown keys are retained for forward-compatible diagnostics, while known
    routing keys are removed when malformed. In particular, strings such as
    ``"false"`` are never treated as booleans and non-finite numbers can never
    satisfy numeric rules.
    """

    raw = dict(signals) if isinstance(signals, dict) else {}
    normalized = dict(raw)

    bool_keys = (
        "faq_matched",
        "tool_intent_matched",
        "tools_available",
        "policy_allowed",
    )
    for key in bool_keys:
        value = _strict_bool(raw.get(key))
        if value is None:
            normalized.pop(key, None)
        else:
            normalized[key] = value

    similarity = _finite_float(
        raw.get("faq_similarity"),
        minimum=0.0,
        maximum=1.0,
    )
    if similarity is None:
        normalized.pop("faq_similarity", None)
    else:
        normalized["faq_similarity"] = similarity

    verdict = _normalize_faq_verdict(raw.get("faq_verdict"))
    if verdict is None:
        normalized.pop("faq_verdict", None)
    else:
        normalized["faq_verdict"] = verdict

    for key in ("consecutive_fallbacks", "effective_tool_count"):
        value = _non_negative_int(raw.get(key))
        if value is None:
            normalized.pop(key, None)
        else:
            normalized[key] = value

    # ``tools_available`` was historically the regex/tool-intent signal. Keep
    # that input working only when the canonical signal is absent and the
    # legacy value is an actual boolean. New producers set both signals and a
    # preflight overwrites tools_available with effective availability.
    if (
        "tool_intent_matched" not in raw
        and normalized.get("tools_available") is True
    ):
        normalized["tool_intent_matched"] = True

    return normalized


def _normalize_when(name: str, when: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, raw_value in when.items():
        if key in {
            "sensitive",
            "faq_matched",
            "tool_intent_matched",
            "tools_available",
        }:
            value = _strict_bool(raw_value)
            if value is None:
                raise ConfigError(f"rule {name!r} condition {key!r} must be a boolean")
            normalized[key] = value
            continue

        if key == "intent_coarse":
            value = _coerce_intent(raw_value)
            try:
                normalized[key] = IntentCoarse(value).value
            except (TypeError, ValueError) as exc:
                raise ConfigError(
                    f"rule {name!r} condition 'intent_coarse' is invalid: {raw_value!r}"
                ) from exc
            continue

        if key == "emotion":
            value = _coerce_emotion(raw_value)
            try:
                normalized[key] = EmotionLabel(value).value
            except (TypeError, ValueError) as exc:
                raise ConfigError(
                    f"rule {name!r} condition 'emotion' is invalid: {raw_value!r}"
                ) from exc
            continue

        if key == "faq_verdict":
            value = _normalize_faq_verdict(raw_value)
            if value is None:
                raise ConfigError(
                    f"rule {name!r} condition 'faq_verdict' is invalid: {raw_value!r}"
                )
            normalized[key] = value
            continue

        if key == "faq_similarity_gte":
            value = _finite_float(raw_value, minimum=0.0, maximum=1.0)
            if value is None:
                raise ConfigError(
                    f"rule {name!r} condition 'faq_similarity_gte' "
                    "must be a finite number between 0 and 1"
                )
            normalized[key] = value
            continue

        if key == "consecutive_fallbacks_gte":
            value = _non_negative_int(raw_value)
            if value is None:
                raise ConfigError(
                    f"rule {name!r} condition 'consecutive_fallbacks_gte' "
                    "must be a non-negative integer"
                )
            normalized[key] = value
            continue

        # Unsupported keys are rejected before this helper is called.
        normalized[key] = raw_value
    return normalized


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
    names: set[str] = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ConfigError(f"rule #{i} must be a mapping")
        name = item.get("name") or f"rule_{i}"
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"rule #{i} 'name' must be a non-empty string")
        name = name.strip()
        if name in names:
            raise ConfigError(f"duplicate router rule name: {name!r}")
        names.add(name)
        when = item.get("when", {})
        if not isinstance(when, dict):
            raise ConfigError(f"rule {name!r} 'when' must be a mapping")
        unknown = set(when.keys()) - _SUPPORTED_WHEN_KEYS
        if unknown:
            raise ConfigError(
                f"rule {name!r} has unsupported 'when' keys: {sorted(unknown)}"
            )
        when = _normalize_when(name, when)
        try:
            route = RouteType(item["route"])
        except (KeyError, ValueError) as e:
            raise ConfigError(f"rule {name!r} has invalid route: {item.get('route')!r}") from e
        reason = item.get("reason") or name
        if not isinstance(reason, str) or not reason.strip():
            raise ConfigError(f"rule {name!r} 'reason' must be a non-empty string")
        rules.append(Rule(name=name, when=dict(when), route=route, reason=reason))

    defaults = [index for index, rule in enumerate(rules) if not rule.when]
    if len(defaults) != 1:
        raise ConfigError(
            "router rules must contain exactly one unconditional default rule"
        )
    if defaults[0] != len(rules) - 1:
        raise ConfigError("the unconditional default router rule must be last")

    return rules


def _coerce_intent(value: Any) -> str | None:
    if isinstance(value, IntentCoarse):
        return value.value
    if isinstance(value, str):
        return value.strip().lower()
    return None


def _coerce_emotion(value: Any) -> str | None:
    if isinstance(value, EmotionLabel):
        return value.value
    if isinstance(value, str):
        return value.strip().lower()
    return None


def _match(rule: Rule, pre: PreprocessedMessage, signals: dict[str, Any]) -> bool:
    when = rule.when
    if not when:
        return True  # default rule

    if "sensitive" in when:
        expected = _strict_bool(when["sensitive"])
        if expected is None:
            return False
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

    if "faq_matched" in when:
        expected = _strict_bool(when["faq_matched"])
        actual = _strict_bool(signals.get("faq_matched"))
        if expected is None or actual != expected:
            return False

    if "faq_verdict" in when:
        expected = _normalize_faq_verdict(when["faq_verdict"])
        actual = _normalize_faq_verdict(signals.get("faq_verdict"))
        if expected is None or actual != expected:
            return False

    if "faq_similarity_gte" in when:
        threshold = _finite_float(
            when["faq_similarity_gte"],
            minimum=0.0,
            maximum=1.0,
        )
        sim = _finite_float(
            signals.get("faq_similarity"),
            minimum=0.0,
            maximum=1.0,
        )
        if threshold is None or sim is None or sim < threshold:
            return False

    if "tool_intent_matched" in when:
        expected = _strict_bool(when["tool_intent_matched"])
        actual = _strict_bool(signals.get("tool_intent_matched"))
        if expected is None or actual != expected:
            return False

    if "tools_available" in when:
        expected = _strict_bool(when["tools_available"])
        actual = _strict_bool(signals.get("tools_available"))
        if expected is None or actual != expected:
            return False

    if "consecutive_fallbacks_gte" in when:
        threshold = _non_negative_int(when["consecutive_fallbacks_gte"])
        cur = _non_negative_int(signals.get("consecutive_fallbacks"))
        if threshold is None or cur is None or cur < threshold:
            return False

    return True


def _is_group_session(session: Session) -> bool:
    kind = str((session.metadata or {}).get("session_kind") or "").strip().lower()
    return kind in {"group", "chatroom", "channel", "guild"} or str(
        session.session_id or ""
    ).endswith("@chatroom")


def _with_session_fallback_count(
    signals: dict[str, Any],
    session: Session | None,
) -> dict[str, Any]:
    if session is None:
        return signals
    hydrated = dict(signals)
    if _is_group_session(session):
        # A shared group-level counter must never trigger automatic takeover.
        hydrated.pop(_CONSECUTIVE_FALLBACKS_KEY, None)
        return hydrated
    if _CONSECUTIVE_FALLBACKS_KEY not in session.variables:
        return hydrated
    persisted = _non_negative_int(
        session.variables.get(_CONSECUTIVE_FALLBACKS_KEY)
    )
    if persisted is None:
        hydrated.pop(_CONSECUTIVE_FALLBACKS_KEY, None)
    else:
        hydrated[_CONSECUTIVE_FALLBACKS_KEY] = persisted
    return hydrated


def _confidence_for_match(
    rule: Rule,
    signals: dict[str, Any],
) -> tuple[float, str]:
    if not rule.when:
        return 0.25, "unconditional_default"
    if "sensitive" in rule.when:
        return 0.99, "deterministic_safety_signal"
    if rule.when.get("intent_coarse") == IntentCoarse.HANDOFF_REQUEST.value:
        return 0.97, "explicit_handoff_intent"
    if "faq_matched" in rule.when:
        score = _finite_float(
            signals.get("faq_similarity"),
            minimum=0.0,
            maximum=1.0,
        )
        if score is not None:
            return max(0.80, score), "faq_engine_match_and_score"
        return 0.90, "faq_engine_match_and_verdict"
    if {
        "tool_intent_matched",
        "tools_available",
    }.issubset(rule.when):
        if _non_negative_int(signals.get("effective_tool_count")) is not None:
            return 0.92, "tool_intent_and_effective_preflight"
        return 0.82, "tool_intent_and_legacy_availability"
    if "consecutive_fallbacks_gte" in rule.when:
        return 0.90, "emotion_and_persisted_fallback_history"
    if "intent_coarse" in rule.when:
        return 0.80, "coarse_intent_rule"
    return 0.75, "matched_rule_conditions"


def evaluate(
    rules: list[Rule],
    pre: PreprocessedMessage,
    session: Session | None,
    signals: dict[str, Any] | None = None,
) -> RouteDecision:
    """Return the first matching rule's RouteDecision.

    Known scalar signals are normalized fail-closed before evaluation. FAQ
    routing uses the FAQ engine's ``faq_matched``/``faq_verdict`` decision;
    ``faq_similarity`` remains diagnostic evidence, not a policy threshold.
    """
    sig = normalize_router_signals(signals)
    sig = _with_session_fallback_count(sig, session)

    for rule in rules:
        if _match(rule, pre, sig):
            confidence, confidence_basis = _confidence_for_match(rule, sig)
            return RouteDecision(
                type=rule.route,
                confidence=confidence,
                reason=rule.reason,
                hints={
                    "rule": rule.name,
                    "confidence_basis": confidence_basis,
                    "matched_conditions": sorted(rule.when),
                },
            )

    # No rule matched (shouldn't happen if a default rule exists). Fall back to LLM.
    return RouteDecision(
        type=RouteType.LLM,
        confidence=0.1,
        reason="no rule matched (implicit fallback)",
        hints={
            "rule": "__implicit_default__",
            "confidence_basis": "missing_configured_default",
            "matched_conditions": [],
        },
    )
