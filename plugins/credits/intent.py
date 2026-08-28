"""Map a semantic intent decision onto credits operations."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.common.intent import IntentDecision, IntentDomain
from app.common.intent_runtime import is_confident, slot_int, slot_text


class CreditIntentType(str, Enum):
    BALANCE_SELF = "balance_self"
    BALANCE_OTHER = "balance_other"
    RANK = "rank"
    CHECKIN_STATUS = "checkin_status"
    CHECKIN_ACTION = "checkin_action"
    TRANSFER_SELF_TO_OTHER_UNSUPPORTED = "transfer_self_to_other_unsupported"
    TRANSFER_REVERSE_UNAUTHORIZED = "transfer_reverse_unauthorized"
    REDEEM_OR_DISCUSSION = "redeem_or_discussion"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CreditIntent:
    type: CreditIntentType
    confidence: float
    reason: str
    should_handle: bool
    target_user_id: str = ""
    display_name: str = ""
    amount: int | None = None


_ACTION_TYPES = {
    "balance_self": (CreditIntentType.BALANCE_SELF, "self_balance_query", True),
    "balance_other": (CreditIntentType.BALANCE_OTHER, "named_target_balance_query", True),
    "rank": (CreditIntentType.RANK, "rank_query", True),
    "checkin_status": (CreditIntentType.CHECKIN_STATUS, "checkin_status_query", True),
    "checkin_action": (CreditIntentType.CHECKIN_ACTION, "checkin_action", True),
    "transfer_self_to_other_unsupported": (
        CreditIntentType.TRANSFER_SELF_TO_OTHER_UNSUPPORTED,
        "self_transfer_unsupported",
        True,
    ),
    "transfer_reverse_unauthorized": (
        CreditIntentType.TRANSFER_REVERSE_UNAUTHORIZED,
        "reverse_transfer_unsupported",
        True,
    ),
    "redeem_or_discussion": (
        CreditIntentType.REDEEM_OR_DISCUSSION,
        "redeem_or_discussion",
        False,
    ),
}


def contains_credit_term(text: str, credit_name: str) -> bool:
    terms = [token for token in {"积分", "余额", credit_name.strip()} if token]
    return any(term in text for term in terms)


def extract_other_credit_query_target(text: str, credit_name: str) -> str:
    _ = text, credit_name
    return ""


def is_credit_rank_query(text: str, credit_name: str) -> bool:
    _ = text, credit_name
    return False


def is_self_query(text: str) -> bool:
    _ = text
    return False


def classify_credit_intent(
    *,
    text: str = "",
    balance_text: str = "",
    credit_name: str = "积分",
    mentioned_target_user_id: str = "",
    decision: IntentDecision | None = None,
) -> CreditIntent:
    _ = text, balance_text, credit_name
    if decision is None or decision.domain is not IntentDomain.CREDITS:
        return CreditIntent(
            type=CreditIntentType.UNKNOWN,
            confidence=0.0,
            reason="not_matched",
            should_handle=False,
        )
    if not is_confident(decision):
        return CreditIntent(
            type=CreditIntentType.UNKNOWN,
            confidence=decision.confidence,
            reason="low_confidence",
            should_handle=False,
        )
    mapped = _ACTION_TYPES.get(decision.action)
    if mapped is None:
        return CreditIntent(
            type=CreditIntentType.UNKNOWN,
            confidence=decision.confidence,
            reason="not_matched",
            should_handle=False,
        )
    intent_type, reason, should_handle = mapped
    target = slot_text(decision, "target", "display_name")
    target_user_id = slot_text(decision, "target_user_id") or mentioned_target_user_id
    if intent_type is CreditIntentType.BALANCE_OTHER and mentioned_target_user_id:
        target_user_id = mentioned_target_user_id
        reason = "mentioned_target_balance_query"
    return CreditIntent(
        type=intent_type,
        confidence=decision.confidence,
        reason=reason,
        should_handle=should_handle,
        target_user_id=target_user_id,
        display_name=target,
        amount=slot_int(decision, "amount"),
    )
