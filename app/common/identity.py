
"""Deterministic AI-identity and human-support intent handling.

These rules intentionally sit outside model prompts: persona instructions and
untrusted chat text must not be able to override identity transparency or make
the product claim that a human handoff exists when it does not.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

AI_IDENTITY_DISCLOSURE = "我是 AI 助手，不是真人。"
GROUP_HANDOFF_UNAVAILABLE = (
    "我目前无法直接转接人工，也不会把整个群切换为人工接管；"
    "如需人工帮助，请联系群管理员。"
)


class GroupHumanIntentType(StrEnum):
    NONE = "none"
    IDENTITY_INQUIRY = "identity_inquiry"
    HANDOFF_REQUEST = "handoff_request"
    HANDOFF_NON_REQUEST = "handoff_non_request"


@dataclass(frozen=True)
class GroupHumanIntent:
    type: GroupHumanIntentType
    reason_code: str
    normalized_text: str

    @property
    def should_short_circuit(self) -> bool:
        return self.type in {
            GroupHumanIntentType.IDENTITY_INQUIRY,
            GroupHumanIntentType.HANDOFF_REQUEST,
        }


_MENTION_PREFIX_RE = re.compile(
    r"^\s*(?:@\S+[\s\u2000-\u200a\u202f\u205f\u3000]+)+"
)
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_SPACE_RE = re.compile(r"\s+")
_CJK_GAP_RE = re.compile(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])")
_QUOTED_TEXT_RE = re.compile(
    r"“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|《[^》]*》|\"[^\"]*\""
)
_CLAUSE_BOUNDARY_RE = re.compile(r"[,，。！？!?；;：:\n]")

_IDENTITY_KIND = (
    r"(?:真人(?:客服)?|人类|人工客服|人工智能|AI(?:助手)?|智能助手|"
    r"聊天机器人|机器人|程序|语言模型|大模型|人(?!工))"
)
_IDENTITY_QUESTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        rf"(?:你|您|这个(?:助手|机器人)|bot)"
        rf"(?:到底|究竟|其实|真的)?(?:是|是不是|是否是|算不算|属于)?"
        rf"(?:个|一个)?{_IDENTITY_KIND}(?:吗|嘛|么|呢|吧|还是|？|\?|$)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:你|您|这个(?:助手|机器人)|bot).{{0,8}}"
        rf"(?:身份|本质).{{0,6}}{_IDENTITY_KIND}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^(?:到底|究竟|其实|真的)?(?:是|是不是|是否是)?"
        rf"(?:个|一个)?{_IDENTITY_KIND}(?:吗|嘛|么|呢|还是|？|\?)$",
        re.IGNORECASE,
    ),
)
_ENGLISH_IDENTITY_QUESTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:are|aren['’]?t)\s+you\s+(?:really\s+)?(?:an?\s+)?"
        r"(?:ai|human|real\s+person|bot|robot|language\s+model)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bis\s+this\s+(?:really\s+)?(?:an?\s+)?"
        r"(?:ai|human|real\s+person|bot|robot|language\s+model)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:what\s+are\s+you|are\s+you\s+real)\b",
        re.IGNORECASE,
    ),
)
_IDENTITY_OVERRIDE_CONTROL_RE = re.compile(
    r"(?:忽略|无视|绕过|覆盖).{0,12}(?:规则|指令|提示词|系统消息)|"
    r"(?:假装|冒充|扮演|装作|伪装|自称|声称|不要承认|别承认|不要说|别说|"
    r"只(?:能)?回复|必须回答)",
    re.IGNORECASE,
)
_IDENTITY_CLAIM_RE = re.compile(
    rf"(?:你|您|自己|我).{{0,5}}(?:是|不是).{{0,5}}{_IDENTITY_KIND}|"
    rf"(?:假装|冒充|扮演|装作|伪装).{{0,8}}{_IDENTITY_KIND}",
    re.IGNORECASE,
)

_HANDOFF_ZH_TARGET = (
    r"(?:人工(?:客服|服务|坐席)?|真人(?:客服)?|客服人员)"
)
_HANDOFF_ZH_ACTION = (
    rf"(?:转(?:接)?(?:到|给)?{_HANDOFF_ZH_TARGET}|"
    rf"找(?:个)?(?:真人(?:客服)?|人工(?:客服)?)|"
    rf"联系{_HANDOFF_ZH_TARGET}|"
    rf"换(?:成|到)?{_HANDOFF_ZH_TARGET}|"
    rf"接入{_HANDOFF_ZH_TARGET}|"
    rf"安排{_HANDOFF_ZH_TARGET})"
)
_HANDOFF_EN_TARGET = (
    r"(?:(?:an?\s+)?human(?:\s+agent)?|"
    r"(?:an?\s+)?live\s+(?:agent|person)|"
    r"(?:a\s+)?real\s+person|"
    r"(?:a\s+)?support\s+agent|"
    r"(?:a\s+)?customer\s+(?:service|support)(?:\s+(?:agent|representative))?|"
    r"(?:a\s+)?representative)"
)
_HANDOFF_EN_ACTION = (
    rf"(?:(?:connect|transfer|route|switch)\s+me\s+(?:to|with)\s+{_HANDOFF_EN_TARGET}|"
    rf"put\s+me\s+through\s+to\s+{_HANDOFF_EN_TARGET}|"
    rf"(?:speak|talk|chat)\s+(?:to|with)\s+{_HANDOFF_EN_TARGET})"
)
_HANDOFF_RELATED_RE = re.compile(
    r"(?:转(?:接)?人工|人工客服|真人(?:客服)?|找(?:个)?真人|找人工|联系人工|"
    r"人工服务|人工坐席|客服人员)|"
    rf"\b(?:{_HANDOFF_EN_ACTION}|{_HANDOFF_EN_TARGET}|human\s+support)\b",
    re.IGNORECASE,
)
_HANDOFF_NEGATED_RE = re.compile(
    r"(?:不用|不要|别|无需|不必|不需要|不想)"
    r"(?:再|给我|帮我|替我|进行|安排|直接|马上|现在|了|啦|\s){0,6}"
    rf"(?:{_HANDOFF_ZH_ACTION}|{_HANDOFF_ZH_TARGET})|"
    rf"(?:取消|停止)(?:给我|帮我|\s){{0,4}}{_HANDOFF_ZH_ACTION}",
    re.IGNORECASE,
)
_HANDOFF_CANCELLED_AFTER_RE = re.compile(
    rf"(?:{_HANDOFF_ZH_ACTION}|人工客服|真人客服|人工服务|人工坐席|客服人员)"
    r"(?:吧|一下|下)?[\s，,。！？!?；;：:]{0,4}"
    r"(?:算了(?:吧)?(?:不用了?)?|不用了?|不要了|取消(?:吧|了)?|先不用了?|没事了?)"
    r"(?=$|[\s，,。！？!?；;：:])",
    re.IGNORECASE,
)
_HANDOFF_EN_NEGATED_RE = re.compile(
    rf"\b(?:do\s+not|don['’]?t|dont|no\s+need\s+to|"
    rf"i\s+(?:do\s+not|don['’]?t|dont)\s+(?:want|need)\s+to|"
    rf"i\s+(?:do\s+not|don['’]?t|dont)\s+(?:want|need))"
    rf".{{0,40}}?(?:{_HANDOFF_EN_ACTION}|{_HANDOFF_EN_TARGET})\b|"
    rf"\b(?:cancel|stop)\s+(?:that\s+|the\s+)?"
    rf"(?:transfer|handoff|request\s+for\s+{_HANDOFF_EN_TARGET})\b",
    re.IGNORECASE,
)
_HANDOFF_EN_CANCELLED_AFTER_RE = re.compile(
    rf"(?:{_HANDOFF_EN_ACTION}|{_HANDOFF_EN_TARGET})"
    r"[\s,.;:!?-]{0,8}(?:never\s+mind|cancel\s+(?:that|it)|forget\s+it|"
    r"not\s+anymore|i\s+changed\s+my\s+mind)"
    r"(?=$|[\s,.;:!?-])",
    re.IGNORECASE,
)
_HANDOFF_BARE_CANCEL_RE = re.compile(
    r"(?:^|[，,。！？!?；;：:])\s*"
    r"(?:(?:还是|那(?:还是)?|我(?:还是)?)\s*)?"
    r"(?:算了(?:吧)?(?:不用了?)?|不用了?|不要了|取消(?:吧|了)?|先不用了?|没事了?|"
    r"(?:(?:不过|但是|但|后来|现在|我|还是)\s*){0,3}"
    r"(?:决定)?\s*(?:别|不)转(?:人工)?了?|"
    r"never\s+mind|cancel\s+(?:that|it)|forget\s+it|i\s+changed\s+my\s+mind)"
    r"\s*(?=$|[，,。！？!?；;：:])",
    re.IGNORECASE,
)
_HANDOFF_STRONG_REQUEST_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:我要|我想(?:要)?|我需要|帮我|给我|替我|麻烦|请|你能|您能|能|能不能|能否|"
        r"可以(?:不可以)?|是否可以|赶紧|马上|现在|直接).{0,10}"
        rf"{_HANDOFF_ZH_ACTION}",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:^|[，,。！？!?；;])\s*"
        r"(?:还是|那就|改成)?\s*(?:请|麻烦|赶紧|马上|现在|直接|帮我|给我)?\s*"
        rf"{_HANDOFF_ZH_ACTION}"
        r"(?:吧|一下|下|可以吗|行吗|谢谢|谢了|[。！!？?]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:^|[，,。！？!?；;])\s*"
        r"(?:还是|那就|改成)?\s*"
        r"(?:我要|我想要|我需要|给我|来个|安排|请|麻烦)?\s*"
        r"(?:人工客服|真人客服|人工服务|人工坐席|客服人员)"
        r"(?:吧|一下|下|谢谢|[。！!？?]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:有|能联系到|可以联系到|能找到|可以找到)\s*"
        r"(?:人工客服|真人客服|人工坐席|客服人员)(?:吗|么|嘛|？|\?)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:please\s+)?{_HANDOFF_EN_ACTION}\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:can|could|would|will)\s+you\s+(?:please\s+)?"
        rf"(?:{_HANDOFF_EN_ACTION}|(?:get|find)\s+me\s+{_HANDOFF_EN_TARGET})\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\bi\s+(?:want|need|would\s+like)\s+(?:to\s+)?"
        rf"(?:{_HANDOFF_EN_ACTION}|{_HANDOFF_EN_TARGET})\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:^|[,.;:!?])\s*(?:please\s+)?{_HANDOFF_EN_TARGET}"
        r"\s*(?:,\s*)?(?:please)?\s*(?:[.!?]|$)",
        re.IGNORECASE,
    ),
)
_HANDOFF_REFERENCE_RE = re.compile(
    r"(?:这(?:句|个)?话|这个词|这些字|什么意思|什么含义|"
    r"怎么说|如何说|怎么写|如何写|怎么读|如何读|指的是|意味着|"
    r"讨论|解释|举例|例子|提到|原话|他说|她说|别人说|客服说)|"
    r"\b(?:what\s+does|what\s+is\s+the\s+meaning|meaning\s+of|"
    r"how\s+(?:do|can|would)\s+(?:i|you)\s+(?:say|write|spell|pronounce)|"
    r"phrase|word|term|quote|example|discuss|mentioned|said|wrote)\b",
    re.IGNORECASE,
)


def normalize_identity_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = _INVISIBLE_RE.sub("", value)
    value = _MENTION_PREFIX_RE.sub("", value)
    return _SPACE_RE.sub(" ", value).strip()


def _identity_inquiry(text: str) -> bool:
    compact = _SPACE_RE.sub("", text)
    if any(pattern.search(compact) for pattern in _IDENTITY_QUESTION_PATTERNS):
        return True
    if any(pattern.search(text) for pattern in _ENGLISH_IDENTITY_QUESTION_PATTERNS):
        return True
    return bool(
        _IDENTITY_OVERRIDE_CONTROL_RE.search(compact)
        and _IDENTITY_CLAIM_RE.search(compact)
    )


def _mask_quoted_text(text: str) -> str:
    """Hide quoted examples while preserving positions and quote delimiters."""

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


def _handoff_reference_match(text: str, match: re.Match[str]) -> bool:
    return bool(_HANDOFF_REFERENCE_RE.search(_match_clause(text, match)))


def _last_match(
    patterns: tuple[re.Pattern[str], ...],
    text: str,
    *,
    exclude_references: bool = False,
) -> re.Match[str] | None:
    matches = [
        match
        for pattern in patterns
        for match in pattern.finditer(text)
        if not exclude_references or not _handoff_reference_match(text, match)
    ]
    return max(matches, key=lambda match: (match.end(), match.start()), default=None)


def classify_group_human_intent(text: str) -> GroupHumanIntent:
    """Classify group-chat identity/handoff text without calling an LLM.

    When a message contains both a cancelled and a later renewed handoff
    instruction, the last explicit instruction wins. A negation wins ties so
    phrases such as ``不用转人工`` cannot be mistaken for a request.
    A valid explicit handoff request also takes precedence when the same
    message contains an identity inquiry.
    """

    normalized = normalize_identity_text(text)
    if not normalized:
        return GroupHumanIntent(
            GroupHumanIntentType.NONE,
            "group_human_intent_none",
            normalized,
        )

    identity_inquiry = _identity_inquiry(normalized)
    scan_text = _CJK_GAP_RE.sub("", normalized)
    request_scan_text = _mask_quoted_text(scan_text)
    request_match = _last_match(
        _HANDOFF_STRONG_REQUEST_PATTERNS,
        request_scan_text,
        exclude_references=True,
    )
    negated_match = _last_match(
        (
            _HANDOFF_NEGATED_RE,
            _HANDOFF_CANCELLED_AFTER_RE,
            _HANDOFF_EN_NEGATED_RE,
            _HANDOFF_EN_CANCELLED_AFTER_RE,
        ),
        request_scan_text,
    )
    if request_match is not None:
        bare_cancel_match = _last_match((_HANDOFF_BARE_CANCEL_RE,), request_scan_text)
        if (
            bare_cancel_match is not None
            and bare_cancel_match.start() >= request_match.end()
            and (
                negated_match is None
                or bare_cancel_match.end() > negated_match.end()
            )
        ):
            negated_match = bare_cancel_match
    if request_match is not None and (
        negated_match is None or request_match.end() > negated_match.end()
    ):
        return GroupHumanIntent(
            GroupHumanIntentType.HANDOFF_REQUEST,
            "group_handoff_unavailable",
            normalized,
        )
    if identity_inquiry:
        return GroupHumanIntent(
            GroupHumanIntentType.IDENTITY_INQUIRY,
            "group_identity_disclosure",
            normalized,
        )
    if negated_match is not None or _HANDOFF_RELATED_RE.search(scan_text):
        return GroupHumanIntent(
            GroupHumanIntentType.HANDOFF_NON_REQUEST,
            "group_handoff_non_request",
            normalized,
        )
    return GroupHumanIntent(
        GroupHumanIntentType.NONE,
        "group_human_intent_none",
        normalized,
    )
