from __future__ import annotations

import pytest

from app.common.identity import (
    GroupHumanIntentType,
    classify_group_human_intent,
    normalize_identity_text,
)


@pytest.mark.parametrize(
    "text",
    [
        "@bot 我要转人工",
        "麻烦人工客服",
        "请帮我找个真人客服",
        "先别转人工，还是帮我转人工吧",
        "给我转人工，不用了；还是直接转人工吧",
        "I need a human agent.",
        "Please connect me to a live agent.",
        "Can I talk to a real person?",
    ],
)
def test_group_handoff_explicit_requests(text: str) -> None:
    intent = classify_group_human_intent(text)

    assert intent.type == GroupHumanIntentType.HANDOFF_REQUEST
    assert intent.reason_code == "group_handoff_unavailable"
    assert intent.should_short_circuit is True


@pytest.mark.parametrize(
    "text",
    [
        "不要转人工",
        "给我转人工，不用了",
        "帮我转人工，算了不用了",
        "给我转人工，还是不用了",
        "帮我转人工，后来还是别转了",
        "Please connect me to a human agent, never mind.",
        "Don't transfer me to a human agent.",
    ],
)
def test_group_handoff_last_explicit_cancellation_wins(text: str) -> None:
    intent = classify_group_human_intent(text)

    assert intent.type == GroupHumanIntentType.HANDOFF_NON_REQUEST
    assert intent.reason_code == "group_handoff_non_request"
    assert intent.should_short_circuit is False


@pytest.mark.parametrize(
    "text",
    [
        "“转人工”是什么意思？",
        "他说：“给我转人工”",
        "客服说可以帮我转人工",
        "我们讨论一下转人工功能",
        "真人电影挺好看",
        'How do I say "connect me to a human agent"?',
    ],
)
def test_group_handoff_references_are_not_requests(text: str) -> None:
    intent = classify_group_human_intent(text)

    assert intent.type == GroupHumanIntentType.HANDOFF_NON_REQUEST
    assert intent.should_short_circuit is False


def test_group_handoff_request_outside_quote_still_wins() -> None:
    intent = classify_group_human_intent(
        "“给我转人工”只是示例；现在请帮我转人工",
    )

    assert intent.type == GroupHumanIntentType.HANDOFF_REQUEST


@pytest.mark.parametrize(
    "text",
    [
        "你是 AI 助手吗？如果是，请帮我转人工客服",
        "Are you an AI? Please connect me to a human agent.",
    ],
)
def test_explicit_handoff_wins_when_combined_with_identity_inquiry(
    text: str,
) -> None:
    intent = classify_group_human_intent(text)

    assert intent.type == GroupHumanIntentType.HANDOFF_REQUEST
    assert intent.reason_code == "group_handoff_unavailable"


@pytest.mark.parametrize(
    "text",
    [
        "你是真人吗？",
        "你是 AI 助手吗",
        "Are you a human?",
        "Are you an AI?",
        "What are you?",
    ],
)
def test_group_identity_questions_zh_and_en(text: str) -> None:
    intent = classify_group_human_intent(text)

    assert intent.type == GroupHumanIntentType.IDENTITY_INQUIRY
    assert intent.reason_code == "group_identity_disclosure"
    assert intent.should_short_circuit is True


def test_group_identity_normalization_handles_mentions_width_and_invisible_text() -> None:
    assert normalize_identity_text("  @bot\u3000你 是 \u200b真 人 吗？ ") == "你 是 真 人 吗?"
    assert (
        classify_group_human_intent("  @bot\u3000你 是 \u200b真 人 吗？ ").type
        == GroupHumanIntentType.IDENTITY_INQUIRY
    )
