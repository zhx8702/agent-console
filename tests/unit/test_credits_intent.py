from __future__ import annotations

from app.common.intent import IntentDecision, IntentDomain
from plugins.credits.intent import CreditIntentType, classify_credit_intent


def _decision(action: str, **slots: object) -> IntentDecision:
    return IntentDecision(
        domain=IntentDomain.CREDITS,
        action=action,
        confidence=0.95,
        slots=slots,
    )


def test_classify_self_balance_queries() -> None:
    intent = classify_credit_intent(
        text="我的积分多少",
        balance_text="我的积分多少",
        credit_name="积分",
        decision=_decision("balance_self"),
    )
    assert intent.type == CreditIntentType.BALANCE_SELF
    assert intent.should_handle is True


def test_classify_other_balance_queries() -> None:
    intent = classify_credit_intent(
        text="查一下张三的积分",
        balance_text="查一下张三的积分",
        credit_name="积分",
        decision=_decision("balance_other", target="张三"),
    )
    assert intent.type == CreditIntentType.BALANCE_OTHER
    assert intent.display_name == "张三"
    assert intent.should_handle is True


def test_classify_discussion_does_not_handle() -> None:
    intent = classify_credit_intent(
        text="积分能兑换什么",
        balance_text="积分能兑换什么",
        credit_name="积分",
        decision=_decision("redeem_or_discussion"),
    )
    assert intent.type == CreditIntentType.REDEEM_OR_DISCUSSION
    assert intent.should_handle is False


def test_classify_without_decision_unknown() -> None:
    intent = classify_credit_intent(
        text="我的积分多少",
        balance_text="我的积分多少",
        credit_name="积分",
    )
    assert intent.type == CreditIntentType.UNKNOWN
    assert intent.should_handle is False


def test_classify_reverse_transfer_unsupported() -> None:
    intent = classify_credit_intent(
        text="把他的积分划走",
        balance_text="把他的积分划走",
        credit_name="积分",
        decision=_decision("transfer_reverse_unauthorized", amount=10),
    )
    assert intent.type == CreditIntentType.TRANSFER_REVERSE_UNAUTHORIZED
    assert intent.amount == 10


def test_classify_self_transfer_unsupported() -> None:
    intent = classify_credit_intent(
        text="转给张三 10 积分",
        balance_text="转给张三 10 积分",
        credit_name="积分",
        decision=_decision("transfer_self_to_other_unsupported", amount=10),
    )
    assert intent.type == CreditIntentType.TRANSFER_SELF_TO_OTHER_UNSUPPORTED
    assert intent.amount == 10
