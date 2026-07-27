"""Pure scoring rules for deciding whether extracted memory is safe to activate.

This module deliberately has no database or service dependencies.  Keeping the
policy here makes the acceptance decision independently testable and prevents
the persistence store from becoming the owner of product scoring rules.
"""

from __future__ import annotations

from typing import Any

PROMPT_AUTO_CONFIDENCE_MIN = 0.75
MEMORY_ACCEPTANCE_AUTO_ACCEPT_MIN = 0.78
MEMORY_ACCEPTANCE_REJECT_BELOW = 0.35


def _normalize_line(value: str) -> str:
    return " ".join(str(value or "").strip().split())


def _clamp_score(value: Any, default: float = 0.0) -> float:
    try:
        score = float(value if value is not None else default)
    except (TypeError, ValueError):
        score = default
    return max(0.0, min(score, 1.0))


def _acceptance_recommendation(value: Any) -> str:
    recommendation = str(value or "").strip().lower()
    aliases = {
        "auto_accept": "accepted",
        "accept": "accepted",
        "review": "needs_review",
        "pending": "needs_review",
        "reject": "rejected",
    }
    recommendation = aliases.get(recommendation, recommendation)
    return recommendation if recommendation in {"accepted", "needs_review", "rejected"} else ""


def _memory_acceptance_penalty_score(text_value: str, patterns: tuple[str, ...]) -> float:
    lowered = text_value.lower()
    return 1.0 if any(pattern in lowered for pattern in patterns) else 0.0


def _memory_acceptance_signals(
    *,
    action: dict[str, Any],
    source_type: str,
    memory_type: str,
    content: str,
    confidence: float,
    sensitivity: str,
    original_text: str,
    reason: str,
    has_conflict: bool = False,
) -> dict[str, float]:
    raw_scores = action.get("scores")
    raw_scores = raw_scores if isinstance(raw_scores, dict) else {}
    combined_text = f"{original_text} {content} {reason}"
    explicit_markers = ("记住", "记一下", "请记一下", "以后", "下次", "默认", "长期", "remember")
    profile_markers = ("我叫", "我的名字", "我是", "我喜欢", "我不喜欢", "我习惯", "我偏好")
    if source_type == "manual":
        explicitness = 1.0
    elif source_type == "explicit_user" or any(
        marker in combined_text for marker in explicit_markers
    ):
        explicitness = 1.0
    elif any(marker in combined_text for marker in profile_markers):
        explicitness = 0.85
    elif memory_type in {"preference", "constraint", "profile_fact"}:
        explicitness = 0.65
    else:
        explicitness = 0.35

    durability_defaults = {
        "profile_fact": 0.9,
        "preference": 0.9,
        "constraint": 0.9,
        "note": 0.55,
        "episodic": 0.45,
    }
    actionability_defaults = {
        "constraint": 0.9,
        "preference": 0.85,
        "profile_fact": 0.7,
        "note": 0.45,
        "episodic": 0.4,
    }
    source_reliability_defaults = {
        "manual": 1.0,
        "explicit_user": 0.95,
        "auto": 0.65,
        "backfill": 0.55,
    }
    joke_score = _memory_acceptance_penalty_score(
        combined_text,
        ("开玩笑", "玩笑的", "just kidding", "jk", "kidding"),
    )
    uncertainty_score = _memory_acceptance_penalty_score(
        combined_text,
        ("可能", "也许", "大概", "好像", "不确定", "maybe", "probably", "not sure", "i think"),
    )
    contradiction_score = 1.0 if has_conflict else 0.0
    if str(action.get("op") or "") == "invalidate":
        contradiction_score = max(contradiction_score, 0.35)

    signals = {
        "explicitness": explicitness,
        "evidence_strength": _clamp_score(
            action.get("evidence_strength"), _clamp_score(confidence)
        ),
        "durability": durability_defaults.get(memory_type, 0.5),
        "actionability": actionability_defaults.get(memory_type, 0.45),
        "consistency": 0.45 if contradiction_score >= 1.0 else 1.0,
        "recency": 0.8,
        "source_reliability": source_reliability_defaults.get(source_type, 0.5),
        "joke_score": joke_score,
        "uncertainty_score": uncertainty_score,
        "contradiction_score": contradiction_score,
        "sensitivity_risk": 1.0 if sensitivity != "normal" else 0.0,
    }
    for key, value in raw_scores.items():
        if key in signals:
            signals[key] = _clamp_score(value, signals[key])
    for key in ("durability", "actionability"):
        if key in action:
            signals[key] = _clamp_score(action.get(key), signals[key])
    return signals


def _memory_acceptance_score(signals: dict[str, float]) -> float:
    positive = (
        signals["explicitness"] * 0.18
        + signals["evidence_strength"] * 0.18
        + signals["durability"] * 0.16
        + signals["actionability"] * 0.14
        + signals["consistency"] * 0.14
        + signals["recency"] * 0.08
        + signals["source_reliability"] * 0.12
    )
    penalty = (
        signals["joke_score"] * 0.45
        + signals["uncertainty_score"] * 0.30
        + signals["contradiction_score"] * 0.35
        + signals["sensitivity_risk"] * 0.60
    )
    return round(_clamp_score(positive - penalty), 3)


def _build_memory_acceptance_metadata(
    *,
    action: dict[str, Any],
    source_type: str,
    memory_type: str,
    content: str,
    confidence: float,
    sensitivity: str,
    original_text: str,
    reason: str,
    has_conflict: bool = False,
    auto_accept_min: float = MEMORY_ACCEPTANCE_AUTO_ACCEPT_MIN,
    reject_below: float = MEMORY_ACCEPTANCE_REJECT_BELOW,
) -> dict[str, Any]:
    extraction_confidence = _clamp_score(
        action.get("extraction_confidence"), _clamp_score(confidence)
    )
    signals = _memory_acceptance_signals(
        action=action,
        source_type=source_type,
        memory_type=memory_type,
        content=content,
        confidence=extraction_confidence,
        sensitivity=sensitivity,
        original_text=original_text,
        reason=reason,
        has_conflict=has_conflict,
    )
    score = _memory_acceptance_score(signals)
    recommendation = _acceptance_recommendation(action.get("acceptance_recommendation"))
    if signals["sensitivity_risk"] > 0:
        status = "needs_review"
        status_reason = "sensitive_memory_requires_review"
    elif signals["joke_score"] >= 0.8:
        status = "rejected"
        status_reason = "joke_or_nonfactual_memory"
    elif signals["uncertainty_score"] >= 0.8 or signals["contradiction_score"] >= 0.8:
        status = "needs_review"
        status_reason = "uncertain_or_conflicting_memory"
    elif score >= _clamp_score(auto_accept_min, MEMORY_ACCEPTANCE_AUTO_ACCEPT_MIN):
        status = "accepted"
        status_reason = "acceptance_score_auto_accept"
    elif score < _clamp_score(reject_below, MEMORY_ACCEPTANCE_REJECT_BELOW):
        status = "rejected"
        status_reason = "acceptance_score_reject"
    else:
        status = "needs_review"
        status_reason = "acceptance_score_needs_review"

    if recommendation == "rejected" and status != "accepted":
        status = "rejected"
        status_reason = "llm_recommended_reject"
    elif recommendation == "needs_review" and status == "accepted":
        status = "needs_review"
        status_reason = "llm_recommended_review"
    elif (
        recommendation == "accepted"
        and status == "needs_review"
        and score >= PROMPT_AUTO_CONFIDENCE_MIN
        and signals["sensitivity_risk"] == 0.0
        and signals["joke_score"] < 0.8
        and signals["uncertainty_score"] < 0.8
        and signals["contradiction_score"] < 0.8
    ):
        status = "accepted"
        status_reason = "llm_recommended_accept"

    acceptance_reason = _normalize_line(str(action.get("acceptance_reason") or status_reason))[:240]
    return {
        "status": status,
        "score": score,
        "reason": acceptance_reason,
        "signals": {key: round(value, 3) for key, value in signals.items()},
        "extraction_confidence": extraction_confidence,
        "recommendation": recommendation,
    }


def _memory_status_for_acceptance(acceptance_status: str, *, sensitivity: str) -> str:
    if acceptance_status == "accepted" and sensitivity == "normal":
        return "active"
    if acceptance_status == "superseded":
        return "invalidated"
    if acceptance_status == "expired":
        return "archived"
    return "pending"


def _acceptance_status_for_review_action(action: str) -> str:
    action = str(action or "").strip().lower()
    if action == "accept":
        return "accepted"
    if action in {"reject", "mark_joke"}:
        return "rejected"
    if action == "expire":
        return "expired"
    if action == "supersede":
        return "superseded"
    return "needs_review"
