from __future__ import annotations

from collections import Counter

import pytest
from pydantic import ValidationError

from app.social.contracts import VoiceProfile
from app.social.reply_style import (
    NaturalReplyStyleGuard,
    ReplyStyleHistory,
    requests_detailed_answer,
)


@pytest.mark.parametrize(
    "text",
    [
        "工具执行结果：数据库迁移成功。",
        "事实核验结果为 42。",
        "安全提示：请立即联系当地急救服务。",
        "高风险操作已停止，等待人工确认。",
    ],
)
def test_ineligible_factual_tool_safety_and_high_risk_text_is_unchanged(
    text: str,
) -> None:
    result = NaturalReplyStyleGuard().apply(
        text,
        deterministic_key="preserve",
        eligible=False,
        voice_profile={"identity_disclosure": "always", "emoji_frequency": 1},
    )

    assert result.text == text
    assert result.transformed is False
    assert result.mode == "preserved"


def test_length_modes_are_deterministic_and_follow_65_25_10_split() -> None:
    guard = NaturalReplyStyleGuard()
    text = "第一句内容比较完整。第二句继续补充说明。第三句只在明确要求详细时保留。"
    modes = Counter(
        guard.apply(
            text,
            deterministic_key=f"distribution-{index}",
            eligible=True,
            source_text="请详细展开说明",
        ).mode
        for index in range(2_000)
    )

    assert 1_200 <= modes["one_sentence"] <= 1_400
    assert 430 <= modes["two_sentences"] <= 570
    assert 150 <= modes["expanded_requested"] <= 250


def test_expanded_mode_requires_an_explicit_detail_request() -> None:
    guard = NaturalReplyStyleGuard()
    text = "第一句。第二句。第三句需要详细回答才保留。"
    key = next(
        f"detail-{index}"
        for index in range(1_000)
        if guard.apply(
            text,
            deterministic_key=f"detail-{index}",
            eligible=True,
            source_text="请详细展开",
        ).mode
        == "expanded_requested"
    )

    detailed = guard.apply(
        text,
        deterministic_key=key,
        eligible=True,
        source_text="请详细展开",
    )
    ordinary = guard.apply(
        text,
        deterministic_key=key,
        eligible=True,
        source_text="随便聊聊",
    )

    assert detailed.text == text
    assert ordinary.mode == "two_sentences"
    assert len(ordinary.text) <= 70
    assert "第三句" not in ordinary.text
    assert requests_detailed_answer("麻烦一步步完整说明") is True
    assert requests_detailed_answer("随便说说") is False


def test_one_and_two_sentence_modes_enforce_exact_character_budgets() -> None:
    guard = NaturalReplyStyleGuard()
    text = "很长的一句话内容" * 20 + "。第二句话也很长" * 10 + "🙂🙂"

    for index in range(500):
        result = guard.apply(
            text,
            deterministic_key=f"length-{index}",
            eligible=True,
        )
        if result.mode == "one_sentence":
            assert len(result.text) <= 35
            assert "第二句话" not in result.text
        else:
            assert result.mode == "two_sentences"
            assert len(result.text) <= 70
            assert result.text.startswith("很长的一句话内容")
        assert result.text.rstrip("🙂").endswith("…")
        assert result.text.count("🙂") <= 1


def test_emoji_is_deterministic_capped_and_not_reused_within_twenty_messages() -> None:
    guard = NaturalReplyStyleGuard()
    key, allowed = next(
        (f"emoji-{index}", result)
        for index in range(1_000)
        if (
            result := guard.apply(
                "这个点挺有意思🙂🙂",
                deterministic_key=f"emoji-{index}",
                eligible=True,
            )
        ).emoji
    )
    repeated = guard.apply(
        "这个点挺有意思🙂🙂",
        deterministic_key=key,
        eligible=True,
        history=ReplyStyleHistory(emojis_last_20=("🙂",)),
    )

    assert allowed.text.count("🙂") == 1
    assert repeated.emoji == ""
    assert "🙂" not in repeated.text
    assert "emoji_suppressed" in repeated.reason_codes

    emitted = sum(
        bool(
            guard.apply(
                "嗯🙂",
                deterministic_key=f"emoji-rate-{index}",
                eligible=True,
                voice_profile={"emoji_frequency": 1},
            ).emoji
        )
        for index in range(2_000)
    )
    assert emitted <= 340


def test_recent_catchphrase_is_removed_and_lists_need_an_explicit_request() -> None:
    guard = NaturalReplyStyleGuard()
    repeated = guard.apply(
        "哈哈，这个思路确实可以继续看。",
        deterministic_key="catchphrase",
        eligible=True,
        history=ReplyStyleHistory(catchphrases_last_30=("哈哈",)),
    )
    flattened = guard.apply(
        "1. 第一项\n2. 第二项\n3. 第三项",
        deterministic_key="flat-list",
        eligible=True,
        source_text="你怎么看",
    )
    requested = guard.apply(
        "1. 第一项\n2. 第二项",
        deterministic_key="requested-list",
        eligible=True,
        source_text="请分点列出",
    )

    assert not repeated.text.startswith("哈哈")
    assert "catchphrase_suppressed" in repeated.reason_codes
    assert "1." not in flattened.text
    assert "2." not in flattened.text
    assert "list_flattened" in flattened.reason_codes
    assert "1." in requested.text
    assert "list_flattened" not in requested.reason_codes


def test_voice_profile_contract_caps_emoji_and_deduplicates_thirty_phrases() -> None:
    phrases = [f"短语 {index}" for index in range(30)]
    profile = VoiceProfile(
        phrase_preferences=["  短语 0  ", "短语 0", *phrases[1:]],
        emoji_frequency=0.15,
    )

    assert profile.phrase_preferences == phrases
    with pytest.raises(ValidationError):
        VoiceProfile(emoji_frequency=0.151)
    with pytest.raises(ValidationError):
        VoiceProfile(phrase_preferences=[*phrases, "第 31 条"])


def test_custom_phrase_history_and_identity_disclosure_use_the_real_guard() -> None:
    guard = NaturalReplyStyleGuard()
    custom = guard.apply(
        "这波可以，继续看看🙂",
        deterministic_key="custom-phrase",
        eligible=True,
        history=ReplyStyleHistory(catchphrases_last_30=("这波可以",)),
        voice_profile={
            "phrase_preferences": ["这波可以", "这波可以"],
            "emoji_frequency": 0,
        },
    )
    contextual = guard.apply(
        "这个思路可以继续看。",
        deterministic_key="identity-contextual",
        eligible=True,
        voice_profile={"identity_disclosure": "contextual"},
    )
    always = guard.apply(
        "这个思路可以继续看。后面再补一条。",
        deterministic_key="identity-always",
        eligible=True,
        voice_profile={"identity_disclosure": "always"},
    )

    assert not custom.text.startswith("这波可以")
    assert "catchphrase_suppressed" in custom.reason_codes
    assert not contextual.text.startswith("我是 AI 助手")
    assert contextual.identity_disclosed is False
    assert always.text.startswith("我是 AI 助手。")
    assert always.identity_disclosed is True
    assert "identity_prefix_added" in always.reason_codes
    assert len(always.text) <= 70
