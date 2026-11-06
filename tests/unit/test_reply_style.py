from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.social.contracts import VoiceProfile
from app.social.reply_style import (
    NaturalReplyStyleGuard,
    ReplyStyleHistory,
    is_agree_echo,
    requests_detailed_answer,
    soften_repeated_trailing_tics,
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


def test_non_persona_replies_keep_full_model_text() -> None:
    guard = NaturalReplyStyleGuard()
    text = "第一句内容比较完整。第二句继续补充说明。第三句只在明确要求详细时保留。"

    ordinary = guard.apply(
        text,
        deterministic_key="no-persona-long",
        eligible=True,
        source_text="随便聊聊",
    )
    detailed = guard.apply(
        text,
        deterministic_key="no-persona-detail",
        eligible=True,
        source_text="请详细展开",
    )
    long_text = "很长的一句话内容" * 20 + "。第二句话也很长" * 10

    long_result = guard.apply(
        long_text,
        deterministic_key="no-persona-very-long",
        eligible=True,
    )

    assert ordinary.mode == "natural"
    assert ordinary.text == text
    assert "length_shaped" not in ordinary.reason_codes
    assert detailed.text == text
    assert long_result.text == long_text
    assert len(long_result.text) > 70
    assert requests_detailed_answer("麻烦一步步完整说明") is True
    assert requests_detailed_answer("随便说说") is False


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


def test_agree_echo_catches_restatement_but_keeps_new_context() -> None:
    source = "还是错怪了codex了，他的computer use真的是独一档的"

    assert is_agree_echo("是啊，computer use这块确实独一档，错怪了就对了 hhh", source)
    assert is_agree_echo("对啊 hhh", source)
    assert is_agree_echo("是啊，还是错怪了codex了，他的computer use真的是独一档的", source)
    assert not is_agree_echo(
        "是啊，computer use确实猛，不过这次炸的是容量，歇一会再试",
        source,
    )
    assert not is_agree_echo(
        "computer use确实猛，不过这次炸的是容量，歇一会再试",
        source,
    )
    assert not is_agree_echo(
        "淦，连鹈鹕都画不明白，跟luna有啥区别",
        "画个鹈鹕都翻车了",
    )


def test_trailing_hhh_stays_once_then_is_stripped_when_recent() -> None:
    first = soften_repeated_trailing_tics("是啊，wg就是香 hhh")
    repeated = soften_repeated_trailing_tics(
        "是啊，反射通了就完了 hhh",
        recent_catchphrases=("hhh",),
    )
    middle = soften_repeated_trailing_tics(
        "hhh 这种气氛才带",
        recent_catchphrases=("hhh",),
    )

    assert first == ("是啊，wg就是香 hhh", "hhh", False)
    assert repeated == ("是啊，反射通了就完了", "hhh", True)
    assert middle == ("hhh 这种气氛才带", "", False)


def test_style_guard_records_and_strips_repeated_trailing_tics() -> None:
    guard = NaturalReplyStyleGuard()
    kept = guard.apply(
        "是啊，mt6000可以 hhh",
        deterministic_key="tic-keep",
        eligible=True,
        preserve_persona_style=True,
    )
    stripped = guard.apply(
        "是啊，手搓就是香 hhh",
        deterministic_key="tic-strip",
        eligible=True,
        preserve_persona_style=True,
        history=ReplyStyleHistory(catchphrases_last_30=("hhh",)),
    )

    assert kept.text.endswith("hhh")
    assert kept.catchphrase == "hhh"
    assert "trailing_tic_suppressed" not in kept.reason_codes
    assert stripped.text == "是啊，手搓就是香"
    assert "trailing_tic_suppressed" in stripped.reason_codes


def test_short_replies_inline_newlines_with_punctuation() -> None:
    guard = NaturalReplyStyleGuard()
    result = guard.apply(
        "数字世界一零一壹，加减乘除一土一洋\n对上没\nhhh",
        deterministic_key="couplet-inline",
        eligible=True,
        preserve_persona_style=True,
    )

    assert result.mode == "persona"
    assert "\n" not in result.text
    assert result.text == "数字世界一零一壹，加减乘除一土一洋。对上没，hhh"
    assert "short_lines_inlined" in result.reason_codes
    assert "length_shaped" not in result.reason_codes


def test_long_replies_keep_line_breaks() -> None:
    guard = NaturalReplyStyleGuard()
    text = (
        "这一段把整件事的背景、条件和限制都说清楚，已经明显超过短回复的一行容量了。\n"
        "接下来再补一层操作步骤和注意点，方便对方可读，而不是挤在同一行里发。\n"
        "最后把风险和回滚也写上，避免只剩一句结论。"
    )
    result = guard.apply(
        text,
        deterministic_key="persona-keep-long",
        eligible=True,
        preserve_persona_style=True,
    )

    assert result.mode == "persona"
    assert "\n" in result.text
    assert result.text.splitlines()[0].startswith("这一段把整件事的背景")
    assert "short_lines_inlined" not in result.reason_codes
    assert "length_shaped" not in result.reason_codes
