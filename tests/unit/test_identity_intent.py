from __future__ import annotations

from app.common.identity import (
    GroupHumanIntentType,
    classify_group_human_intent,
    normalize_identity_text,
)
from app.common.intent import IntentDecision, IntentDomain, IntentOperation


def test_group_handoff_explicit_requests() -> None:
    intent = classify_group_human_intent(
        "我要转人工",
        decision=IntentDecision(
            domain=IntentDomain.HANDOFF,
            action="request",
            operation=IntentOperation.HANDOFF,
            confidence=0.95,
        ),
    )

    assert intent.type == GroupHumanIntentType.HANDOFF_REQUEST
    assert intent.reason_code == "group_handoff_unavailable"
    assert intent.should_short_circuit is True


def test_group_handoff_last_explicit_cancellation_wins() -> None:
    intent = classify_group_human_intent(
        "不要转人工",
        decision=IntentDecision(
            domain=IntentDomain.HANDOFF,
            action="cancel",
            confidence=0.9,
        ),
    )

    assert intent.type == GroupHumanIntentType.HANDOFF_NON_REQUEST
    assert intent.reason_code == "group_handoff_non_request"
    assert intent.should_short_circuit is False


def test_group_handoff_without_decision_is_none() -> None:
    intent = classify_group_human_intent("我要转人工")
    assert intent.type == GroupHumanIntentType.NONE


def test_group_identity_questions() -> None:
    intent = classify_group_human_intent(
        "你是真人吗？",
        decision=IntentDecision(
            domain=IntentDomain.IDENTITY,
            action="inquiry",
            confidence=0.9,
        ),
    )

    assert intent.type == GroupHumanIntentType.IDENTITY_INQUIRY
    assert intent.reason_code == "group_identity_disclosure"
    assert intent.should_short_circuit is True


def test_followup_introduce_is_not_bot_identity_short_circuit() -> None:
    intent = classify_group_human_intent(
        "介绍下",
        decision=IntentDecision(
            domain=IntentDomain.IDENTITY,
            action="inquiry",
            confidence=0.9,
        ),
    )
    assert intent.type == GroupHumanIntentType.NONE
    assert intent.should_short_circuit is False


def test_introduce_yourself_is_bot_identity_question() -> None:
    intent = classify_group_human_intent(
        "介绍一下你自己",
        decision=IntentDecision(
            domain=IntentDomain.IDENTITY,
            action="inquiry",
            confidence=0.9,
        ),
    )
    assert intent.type == GroupHumanIntentType.IDENTITY_INQUIRY


def test_group_identity_normalization_handles_mentions_width_and_invisible_text() -> None:
    assert normalize_identity_text("  @bot\u3000你 是 \u200b真 人 吗？ ") == "你 是 真 人 吗?"
