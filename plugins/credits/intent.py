"""
Lightweight intent classifier for natural-language credits queries.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


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


_SELF_QUERY_RE = re.compile(r"(我|我的|我还|我又|我现在)")
_RANK_QUERY_RE = re.compile(r"(第几|排名|排行|名次)")
_CHECKIN_QUERY_RE = re.compile(r"(签到.*(了吗|没|情况|状态)|今天.*签到了吗|今日.*签到了吗)")
_CHECKIN_ACTION_RE = re.compile(r"^(?:签到|打卡|签个到|我要签到|我来签到|帮我签到|给我签到)$")
_OTHER_MEMBER_CREDIT_QUERY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^(?:帮我)?(?:查一下|查查|查询|查|看下|看看|看一下)\s*"
        r"(?P<target>@?[^，,。？！?：:\s的]{1,40})\s*(?:的)?(?:积分|余额)"
    ),
    re.compile(
        r"^(?P<target>@?[^，,。？！?：:\s的]{1,40})\s*的\s*"
        r"(?:积分|余额)\s*(?:有多少|还有多少|多少|几|剩多少|还剩多少|余额多少|积分多少)"
    ),
    re.compile(
        r"^(?P<target>@?[^，,。？！?：:\s的]{1,40})\s*"
        r"(?:有多少|还有多少|剩多少|还剩多少)\s*(?:积分|余额)"
    ),
    re.compile(
        r"^(?P<target>@?[^，,。？！?：:\s的]{1,40})\s*"
        r"(?:积分|余额)\s*(?:有多少|还有多少|多少|几|剩多少|还剩多少|余额多少|积分多少)"
    ),
)
_CREDIT_REVERSE_TRANSFER_RE = re.compile(
    r"(划走|扣(?:别人|他|她|人)|从[^，,。？！?：:\s]{1,40}(?:转|划|扣).*(?:给我|到我(?:的)?(?:账户|账号|帐户)?))"
)
_CREDIT_SELF_TRANSFER_RE = re.compile(
    r"("
    r"(?:转|转给|给|送|赠送)\s*@?[^，,。？！?：:\s]{1,40}\s*\d+\s*(?:积分|余额)"
    r"|(?:转|送|赠送)\s*\d+\s*(?:积分|余额)\s*(?:给|到|转给)\s*@?[^，,。？！?：:\s]{1,40}"
    r"|\d+\s*(?:积分|余额)\s*(?:给|到|转给|送给)\s*@?[^，,。？！?：:\s]{1,40}"
    r")"
)
_CREDIT_REDEEM_OR_DISCUSSION_RE = re.compile(
    r"(兑换|换|买|花|消费|共进晚餐|一晚|不会扣|大部分|质量)"
)
_OTHER_MEMBER_QUERY_EXCLUDED_TARGETS = {
    "我",
    "我的",
    "我还",
    "我又",
    "我现在",
    "自己",
    "本人",
    "他",
    "她",
    "它",
    "谁",
    "大家",
    "群里",
    "本群",
    "这个群",
}
BARE_CREDIT_BALANCE_PHRASES = {
    "积分",
    "积分余额",
    "查积分",
    "查询积分",
    "查一下积分",
    "看看积分",
    "看积分",
}


def contains_credit_term(text: str, credit_name: str) -> bool:
    terms = [token for token in {"积分", "余额", credit_name.strip()} if token]
    return any(term in text for term in terms)


def _is_credit_balance_query(text: str, credit_name: str) -> bool:
    if not text or text.startswith("/"):
        return False
    terms = [token for token in {"积分", "余额", credit_name.strip()} if token]
    if not any(term in text for term in terms):
        return False
    compact = re.sub(r"\s+", "", text)
    if _SELF_QUERY_RE.search(text):
        if compact in {f"我的{term}" for term in terms}:
            return True
        return any(token in text for token in ("多少", "几", "还有", "还剩", "剩", "查", "看看", "余额"))
    if compact in BARE_CREDIT_BALANCE_PHRASES:
        return True
    return "积分" in text and "余额" in text


def _is_unsupported_reverse_credit_transfer(text: str, credit_name: str) -> bool:
    if not text or text.startswith("/") or not contains_credit_term(text, credit_name):
        return False
    compact = re.sub(r"\s+", "", text)
    if "到我账户" in compact or "到我的账户" in compact or "到我账号" in compact or "到我的账号" in compact:
        return bool(re.search(r"(划走|扣|转)", compact))
    return bool(_CREDIT_REVERSE_TRANSFER_RE.search(compact))


def _is_unsupported_self_credit_transfer(text: str, credit_name: str) -> bool:
    if not text or text.startswith("/") or not contains_credit_term(text, credit_name):
        return False
    compact = re.sub(r"\s+", "", text)
    if _is_unsupported_reverse_credit_transfer(compact, credit_name):
        return False
    return bool(_CREDIT_SELF_TRANSFER_RE.search(compact))


def _is_credit_redeem_or_discussion(text: str, credit_name: str) -> bool:
    if not text or text.startswith("/") or not contains_credit_term(text, credit_name):
        return False
    return bool(_CREDIT_REDEEM_OR_DISCUSSION_RE.search(text))


def _clean_credit_query_target(value: str) -> str:
    cleaned = str(value or "").strip()
    cleaned = cleaned.strip("@ \t\r\n\"'“”‘’「」[]【】()（）")
    cleaned = re.sub(r"^(?:帮我)?(?:查一下|查查|查询|看下|看看|看一下)", "", cleaned).strip()
    cleaned = cleaned.rstrip("的")
    return cleaned.strip("@ \t\r\n\"'“”‘’「」[]【】()（）")


def extract_other_credit_query_target(text: str, credit_name: str) -> str:
    value = str(text or "").strip()
    if not value or value.startswith("/") or not contains_credit_term(value, credit_name):
        return ""
    for pattern in _OTHER_MEMBER_CREDIT_QUERY_PATTERNS:
        match = pattern.search(value)
        if not match:
            continue
        target = _clean_credit_query_target(match.group("target"))
        if (
            target
            and target not in _OTHER_MEMBER_QUERY_EXCLUDED_TARGETS
            and not target.startswith("我")
        ):
            return target
    return ""


def is_credit_rank_query(text: str, credit_name: str) -> bool:
    if not text or text.startswith("/"):
        return False
    if _RANK_QUERY_RE.search(text):
        return contains_credit_term(text, credit_name) or bool(_SELF_QUERY_RE.search(text))
    terms = [token for token in {"积分", "余额", credit_name.strip()} if token]
    return any(term in text for term in terms) and "榜" in text


def is_self_query(text: str) -> bool:
    return bool(_SELF_QUERY_RE.search(text))


def _is_credit_checkin_query(text: str) -> bool:
    if not text or text.startswith("/"):
        return False
    return bool(_CHECKIN_QUERY_RE.search(text))


def _is_credit_checkin_action(text: str) -> bool:
    if not text or text.startswith("/"):
        return False
    return bool(_CHECKIN_ACTION_RE.fullmatch(text.strip()))


def _extract_amount(text: str) -> int | None:
    match = re.search(r"(\d+)\s*(?:积分|余额)", text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def classify_credit_intent(
    *,
    text: str,
    balance_text: str,
    credit_name: str,
    mentioned_target_user_id: str = "",
) -> CreditIntent:
    raw_text = str(text or "").strip()
    query_text = str(balance_text or raw_text).strip()
    credit_label = str(credit_name or "").strip() or "积分"

    if _is_unsupported_reverse_credit_transfer(query_text, credit_label):
        return CreditIntent(
            type=CreditIntentType.TRANSFER_REVERSE_UNAUTHORIZED,
            confidence=0.96,
            reason="reverse_transfer_unsupported",
            should_handle=True,
            amount=_extract_amount(query_text),
        )
    if _is_unsupported_self_credit_transfer(query_text, credit_label):
        return CreditIntent(
            type=CreditIntentType.TRANSFER_SELF_TO_OTHER_UNSUPPORTED,
            confidence=0.94,
            reason="self_transfer_unsupported",
            should_handle=True,
            amount=_extract_amount(query_text),
        )

    if is_credit_rank_query(raw_text, credit_label):
        return CreditIntent(
            type=CreditIntentType.RANK,
            confidence=0.9,
            reason="rank_query",
            should_handle=True,
        )
    if _is_credit_checkin_query(raw_text):
        return CreditIntent(
            type=CreditIntentType.CHECKIN_STATUS,
            confidence=0.9,
            reason="checkin_status_query",
            should_handle=True,
        )
    if _is_credit_checkin_action(raw_text):
        return CreditIntent(
            type=CreditIntentType.CHECKIN_ACTION,
            confidence=0.92,
            reason="checkin_action",
            should_handle=True,
        )

    if _is_credit_redeem_or_discussion(query_text, credit_label):
        return CreditIntent(
            type=CreditIntentType.REDEEM_OR_DISCUSSION,
            confidence=0.82,
            reason="redeem_or_discussion",
            should_handle=False,
        )

    compact_query_text = re.sub(r"\s+", "", query_text)
    if mentioned_target_user_id and contains_credit_term(raw_text or query_text, credit_label):
        return CreditIntent(
            type=CreditIntentType.BALANCE_OTHER,
            confidence=0.9,
            reason="mentioned_target_balance_query",
            should_handle=True,
            target_user_id=mentioned_target_user_id,
        )

    if compact_query_text not in BARE_CREDIT_BALANCE_PHRASES:
        target = extract_other_credit_query_target(raw_text, credit_label)
        if target:
            return CreditIntent(
                type=CreditIntentType.BALANCE_OTHER,
                confidence=0.88,
                reason="named_target_balance_query",
                should_handle=True,
                display_name=target,
            )

    if _is_credit_balance_query(query_text, credit_label):
        return CreditIntent(
            type=CreditIntentType.BALANCE_SELF,
            confidence=0.88,
            reason="self_balance_query",
            should_handle=True,
        )

    return CreditIntent(
        type=CreditIntentType.UNKNOWN,
        confidence=0.0,
        reason="not_matched",
        should_handle=False,
    )
