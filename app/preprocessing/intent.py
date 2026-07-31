"""Deterministic coarse-intent classification.

The classifier deliberately favors explicit, high-precision signals.  In
particular, a mention of a human agent, complaint, or quoted command is not by
itself treated as an instruction.
"""
from __future__ import annotations

import re
import unicodedata

from app.common.identity import GroupHumanIntentType, classify_group_human_intent
from app.common.types import IntentCoarse

_SPACE_RE = re.compile(r"\s+")
_QUOTED_TEXT_RE = re.compile(
    r"“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|《[^》]*》|\"[^\"]*\""
)
_CLAUSE_BOUNDARY_RE = re.compile(r"[,，。！？!?；;：:\n]")

_COMPLAINT_TOPIC = r"(?:投诉|举报|差评)"
_COMPLAINT_REQUEST_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        rf"(?:我要|我想(?:要)?|我需要|我(?:准备|打算)|帮我|替我|请|麻烦|"
        rf"必须|一定要|马上|现在就).{{0,10}}{_COMPLAINT_TOPIC}",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:^|[，,。！？!?；;])\s*(?:还是|那就|现在)?\s*"
        r"(?:投诉|举报)(?:你|你们|客服|商家|平台|店家|这个|此|他|她|它)"
        r"(?:吧|一下|了|[。！!？?]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:^|[，,。！？!?；;])\s*(?:投诉|举报|差评)\s*(?:[。！!？?]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:给|打|写|留)(?:你|你们|客服|商家|平台|店家|这个|此)?"
        r"(?:个|一条)?差评",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:服务|体验|态度|质量).{0,6}(?:太差|很差|糟糕|恶劣|垃圾|离谱)|"
        r"(?:太差|糟糕|恶劣|垃圾|离谱).{0,6}(?:服务|体验|态度|质量)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:i\s+(?:want|need|would\s+like|plan|intend)\s+to|"
        r"please|help\s+me(?:\s+to)?)\s+"
        r"(?:file\s+(?:a\s+)?complaint|complain|report\s+(?:this|you|them)|"
        r"leave\s+(?:a\s+)?bad\s+review)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:file|make|submit|raise|lodge)\s+(?:a\s+)?complaint\b|"
        r"\bcomplain\s+about\b|"
        r"\breport\s+(?:this|you|them|the\s+(?:agent|seller|store|service))\b|"
        r"\bleave\s+(?:a\s+)?bad\s+review\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:terrible|awful|unacceptable|worst)\s+"
        r"(?:service|support|experience|attitude|quality)\b|"
        r"\b(?:service|support|experience|attitude|quality)\s+"
        r"(?:is|was|has\s+been)\s+(?:terrible|awful|unacceptable|the\s+worst)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:^|[,.;:!?])\s*(?:complaint|complain|report\s+this)"
        r"\s*(?:[.!?]|$)",
        re.IGNORECASE,
    ),
)
_COMPLAINT_NEGATED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        rf"(?:不想|不要|别|不会|不打算|无需|不用).{{0,10}}{_COMPLAINT_TOPIC}|"
        rf"(?:撤销|取消|收回).{{0,5}}{_COMPLAINT_TOPIC}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"{_COMPLAINT_TOPIC}.{{0,8}}?"
        r"(?:算了(?:吧)?|不(?:投|举)了|取消(?:吧|了)?|撤销(?:吧|了)?|收回(?:吧|了)?)"
        r"(?=$|[\s，,。！？!?；;：:])",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:do\s+not|don['’]?t|dont|not\s+going\s+to|"
        r"i\s+(?:do\s+not|don['’]?t|dont)\s+want\s+to)\s+"
        r"(?:complain|file\s+(?:a\s+)?complaint|report|leave\s+(?:a\s+)?bad\s+review)\b|"
        r"\b(?:withdraw|cancel|drop)\s+(?:my\s+|the\s+)?complaint\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:complain|complaint|report\s+this).{0,12}?"
        r"(?:never\s+mind|cancel\s+(?:that|it)|forget\s+it|"
        r"i\s+changed\s+my\s+mind)\b",
        re.IGNORECASE,
    ),
)

_REFERENCE_CONTEXT_RE = re.compile(
    r"(?:这(?:句|个)?话|这个词|这些字|什么意思|什么含义|怎么说|如何说|"
    r"怎么写|如何写|怎么读|如何读|指的是|意味着|讨论|解释|举例|例子|"
    r"提到|原话|他说|她说|别人说|客服说)|"
    r"\b(?:what\s+does|what\s+is\s+the\s+meaning|meaning\s+of|"
    r"how\s+(?:do|can|would)\s+(?:i|you)\s+(?:say|write|spell|pronounce)|"
    r"phrase|word|term|quote|example|discuss|mentioned|said|wrote)\b",
    re.IGNORECASE,
)

_ZH_FAQ_START_RE = re.compile(
    r"^(?:(?:你好|您好|嗨|哈喽)[，,。！!\s]*)?"
    r"(?:(?:请问|想问(?:一下)?|咨询(?:一下)?)[，,：:\s]*)?"
    r"(?:怎么|如何|为什么|为何|为啥|什么是|啥是)"
)
_EN_FAQ_START_RE = re.compile(
    r"^(?:hello|hi|hey)?[\s,!.]*"
    r"(?:how(?:\s+do|\s+can|\s+should|\s+would|\s+is|\s+are)?|"
    r"why|what(?:'s|\s+is|\s+are|\s+does|\s+do)?|"
    r"could\s+you\s+explain|can\s+you\s+explain)\b",
    re.IGNORECASE,
)
_REFERENCE_QUESTION_RE = re.compile(
    r"(?:什么意思|什么含义|意味着什么|怎么说|如何说|怎么写|如何写|怎么读|如何读)"
    r"[？?]?$|"
    r"\bwhat\s+does\b.{0,80}\bmean\b|"
    r"\b(?:what\s+is\s+the\s+meaning|meaning\s+of)\b|"
    r"\bhow\s+(?:do|can|would)\s+(?:i|you)\s+(?:say|write|spell|pronounce)\b",
    re.IGNORECASE,
)

_ZH_BUSINESS_WORDS = (
    "订单",
    "发货",
    "退款",
    "退货",
    "换货",
    "物流",
    "快递",
    "配送",
    "到货",
    "包裹",
    "账单",
    "付款",
    "支付",
    "充值",
    "扣款",
    "发票",
    "售后",
    "保修",
    "账户",
    "账号",
    "登录",
    "密码",
    "会员",
    "订阅",
    "优惠券",
)
_EN_BUSINESS_RE = re.compile(
    r"\b(?:refunds?|shipments?|shipping|delivery|deliveries|tracking|"
    r"packages?|parcels?|invoices?|billing|payments?|charged|checkout|"
    r"accounts?|logins?|passwords?|subscriptions?|memberships?|coupons?|"
    r"warrant(?:y|ies)|after[-\s]?sales)\b|"
    r"\b(?:my|the|an?|your)\s+orders?\b|"
    r"\border\s+(?:number|status|history|details?|issue|problem)\b|"
    r"\b(?:track|cancel)\s+(?:my|the|an?)?\s*orders?\b|"
    r"\b(?:return|exchange)\s+(?:an?\s+|the\s+|my\s+)?"
    r"(?:item|product|purchase|order)\b",
    re.IGNORECASE,
)

_SOCIAL_ONLY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^\s*(?:你好吗|最近怎么样|在吗|吃了吗)[呀啊吗嘛呢？?!！。\s]*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*(?:(?:hello|hi|hey)[\s,!.]+)?"
        r"(?:how\s+are\s+you|how['’]?s\s+it\s+going|what['’]?s\s+up|"
        r"nice\s+to\s+meet\s+you)[?!. \s]*$",
        re.IGNORECASE,
    ),
)
_ZH_CHITCHAT_RE = re.compile(
    r"(?:你好|您好|哈喽|嗨|早上好|早安|午安|下午好|晚上好|晚安|"
    r"谢谢|多谢|谢啦|辛苦了|再见|拜拜|回头见)"
)
_EN_CHITCHAT_RE = re.compile(
    r"\b(?:hello|hi|hey|good\s+(?:morning|afternoon|evening|night)|"
    r"thanks|thank\s+you|goodbye|bye|see\s+you|take\s+care)\b",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or ""))
    return _SPACE_RE.sub(" ", value).strip()


def _mask_quoted_text(text: str) -> str:
    def _mask(match: re.Match[str]) -> str:
        value = match.group(0)
        if len(value) < 2:
            return value
        return f"{value[0]}{'_' * (len(value) - 2)}{value[-1]}"

    return _QUOTED_TEXT_RE.sub(_mask, text)


def _match_clause(text: str, match: re.Match[str]) -> str:
    left = 0
    right = len(text)
    for boundary in _CLAUSE_BOUNDARY_RE.finditer(text):
        if boundary.end() <= match.start():
            left = boundary.end()
            continue
        if boundary.start() >= match.end():
            right = boundary.start()
            break
    return text[left:right]


def _last_non_reference_match(
    patterns: tuple[re.Pattern[str], ...],
    text: str,
) -> re.Match[str] | None:
    matches = [
        match
        for pattern in patterns
        for match in pattern.finditer(text)
        if not _REFERENCE_CONTEXT_RE.search(_match_clause(text, match))
    ]
    return max(matches, key=lambda match: (match.end(), match.start()), default=None)


def _last_match(
    patterns: tuple[re.Pattern[str], ...],
    text: str,
) -> re.Match[str] | None:
    matches = [
        match
        for pattern in patterns
        for match in pattern.finditer(text)
    ]
    return max(matches, key=lambda match: (match.end(), match.start()), default=None)


def _complaint_requested(text: str) -> bool:
    scan_text = _mask_quoted_text(text)
    request_match = _last_non_reference_match(_COMPLAINT_REQUEST_PATTERNS, scan_text)
    if request_match is None:
        return False
    negated_match = _last_match(_COMPLAINT_NEGATED_PATTERNS, scan_text)
    return negated_match is None or request_match.end() > negated_match.end()


def classify_intent(text: str) -> IntentCoarse:
    """Return one stable coarse intent without model or network calls."""

    normalized = _normalize(text)
    if not normalized:
        return IntentCoarse.UNKNOWN

    human_intent = classify_group_human_intent(normalized)
    if human_intent.type == GroupHumanIntentType.HANDOFF_REQUEST:
        return IntentCoarse.HANDOFF_REQUEST

    if _complaint_requested(normalized):
        return IntentCoarse.COMPLAINT

    if any(pattern.search(normalized) for pattern in _SOCIAL_ONLY_PATTERNS):
        return IntentCoarse.CHITCHAT

    if (
        _ZH_FAQ_START_RE.search(normalized)
        or _EN_FAQ_START_RE.search(normalized)
        or _REFERENCE_QUESTION_RE.search(normalized)
    ):
        return IntentCoarse.FAQ

    if any(word in normalized for word in _ZH_BUSINESS_WORDS):
        return IntentCoarse.BUSINESS
    if _EN_BUSINESS_RE.search(normalized):
        return IntentCoarse.BUSINESS

    if _ZH_CHITCHAT_RE.search(normalized) or _EN_CHITCHAT_RE.search(normalized):
        return IntentCoarse.CHITCHAT
    return IntentCoarse.UNKNOWN
