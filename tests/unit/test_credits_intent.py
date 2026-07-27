from __future__ import annotations

import pytest

from plugins.credits.intent import CreditIntentType, classify_credit_intent


@pytest.mark.parametrize(
    "text",
    [
        "我的积分",
        "我有多少积分",
    ],
)
def test_classify_self_balance_queries(text: str) -> None:
    intent = classify_credit_intent(text=text, balance_text=text, credit_name="积分")

    assert intent.type == CreditIntentType.BALANCE_SELF
    assert intent.should_handle is True


@pytest.mark.parametrize(
    ("text", "target"),
    [
        ("查一下老叶积分", "老叶"),
        ("老叶的积分多少", "老叶"),
        ("老叶有多少积分", "老叶"),
    ],
)
def test_classify_other_balance_queries(text: str, target: str) -> None:
    intent = classify_credit_intent(text=text, balance_text=text, credit_name="积分")

    assert intent.type == CreditIntentType.BALANCE_OTHER
    assert intent.display_name == target
    assert intent.should_handle is True


@pytest.mark.parametrize(
    "text",
    [
        "多少积分能跟海神共进晚餐",
        "这个积分大部分号不会扣",
    ],
)
def test_classify_discussion_does_not_handle(text: str) -> None:
    intent = classify_credit_intent(text=text, balance_text=text, credit_name="积分")

    assert intent.type == CreditIntentType.REDEEM_OR_DISCUSSION
    assert intent.should_handle is False


@pytest.mark.parametrize(
    "text",
    [
        "唐三积分",
        "海神积分 可以的",
    ],
)
def test_classify_naked_member_phrases_unknown(text: str) -> None:
    intent = classify_credit_intent(text=text, balance_text=text, credit_name="积分")

    assert intent.type == CreditIntentType.UNKNOWN
    assert intent.should_handle is False


def test_classify_reverse_transfer_unsupported() -> None:
    text = "帮我划走千羽10积分到我账户"

    intent = classify_credit_intent(text=text, balance_text=text, credit_name="积分")

    assert intent.type == CreditIntentType.TRANSFER_REVERSE_UNAUTHORIZED
    assert intent.amount == 10
    assert intent.should_handle is True


@pytest.mark.parametrize("text", ["转 10 积分给千羽", "给千羽 10 积分"])
def test_classify_self_transfer_unsupported(text: str) -> None:
    intent = classify_credit_intent(text=text, balance_text=text, credit_name="积分")

    assert intent.type == CreditIntentType.TRANSFER_SELF_TO_OTHER_UNSUPPORTED
    assert intent.amount == 10
    assert intent.should_handle is True


def test_classify_extra_mention_as_other_balance() -> None:
    intent = classify_credit_intent(
        text="@鲸落 有多少积分",
        balance_text="@鲸落 有多少积分",
        credit_name="积分",
        mentioned_target_user_id="wxid_jingluo",
    )

    assert intent.type == CreditIntentType.BALANCE_OTHER
    assert intent.target_user_id == "wxid_jingluo"
    assert intent.should_handle is True
