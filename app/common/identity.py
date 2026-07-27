
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

_HANDOFF_RELATED_RE = re.compile(
    r"(?:转(?:接)?人工|人工客服|真人(?:客服)?|找(?:个)?真人|找人工|联系人工|"
    r"人工服务|人工坐席|客服人员)",
    re.IGNORECASE,
)
_HANDOFF_NEGATED_RE = re.compile(
    r"(?:不用|不要|别|无需|不必|不需要|不想)"
    r"(?:再|给我|帮我|替我|进行|安排|直接|马上|现在|了|啦|\s){0,6}"
    r"(?:转(?:接)?|找|联系|换|接入|安排)?(?:到|给)?"
    r"(?:人工(?:客服|服务|坐席)?|真人(?:客服)?|客服人员)|"
    r"(?:取消|停止)(?:给我|帮我|\s){0,4}(?:转(?:接)?人工|人工客服|找真人)|"
    r"(?:转(?:接)?人工|人工客服|真人客服|找真人)(?:就)?(?:不用|不要|算了|取消)",
    re.IGNORECASE,
)
_HANDOFF_STRONG_REQUEST_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:我要|我想(?:要)?|我需要|帮我|给我|替我|麻烦|请|你能|您能|能|能不能|能否|"
        r"可以(?:不可以)?|是否可以|赶紧|马上|现在|直接).{0,10}"
        r"(?:转(?:接)?人工|人工客服|真人客服|找(?:个)?真人|找人工|联系人工|"
        r"人工服务|人工坐席|客服人员)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:^|[，,。！？!?；;])\s*"
        r"(?:还是|那就|改成)?\s*(?:请|麻烦|赶紧|马上|现在|直接|帮我|给我)?\s*"
        r"(?:转(?:接)?人工|找(?:个)?真人|找人工|联系人工|换(?:成|到)?"
        r"(?:人工(?:客服)?|真人(?:客服)?)|安排(?:人工(?:客服)?|真人(?:客服)?))"
        r"(?:吧|一下|下|可以吗|行吗|谢谢|谢了|[。！!？?]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:^|[，,。！？!?；;])\s*"
        r"(?:我要|我想要|我需要|给我|来个|安排)\s*"
        r"(?:人工客服|真人客服|人工服务|人工坐席|客服人员)"
        r"(?:吧|一下|下|谢谢|[。！!？?]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:有|能联系到|可以联系到|能找到|可以找到)\s*"
        r"(?:人工客服|真人客服|人工坐席|客服人员)(?:吗|么|嘛|？|\?)",
        re.IGNORECASE,
    ),
)


def normalize_identity_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = _INVISIBLE_RE.sub("", value)
    value = _MENTION_PREFIX_RE.sub("", value)
    return _SPACE_RE.sub(" ", value).strip()


def _identity_inquiry(text: str) -> bool:
    if any(pattern.search(text) for pattern in _IDENTITY_QUESTION_PATTERNS):
        return True
    return bool(
        _IDENTITY_OVERRIDE_CONTROL_RE.search(text)
        and _IDENTITY_CLAIM_RE.search(text)
    )


def _last_match(patterns: tuple[re.Pattern[str], ...], text: str) -> re.Match[str] | None:
    matches = [
        match
        for pattern in patterns
        for match in pattern.finditer(text)
    ]
    return max(matches, key=lambda match: (match.end(), match.start()), default=None)


def classify_group_human_intent(text: str) -> GroupHumanIntent:
    """Classify group-chat identity/handoff text without calling an LLM.

    When a message contains both a cancelled and a later renewed handoff
    instruction, the last explicit instruction wins. A negation wins ties so
    phrases such as ``不用转人工`` cannot be mistaken for a request.
    """

    normalized = normalize_identity_text(text)
    if not normalized:
        return GroupHumanIntent(
            GroupHumanIntentType.NONE,
            "group_human_intent_none",
            normalized,
        )
    scan_text = _SPACE_RE.sub("", normalized)
    if _identity_inquiry(scan_text):
        return GroupHumanIntent(
            GroupHumanIntentType.IDENTITY_INQUIRY,
            "group_identity_disclosure",
            normalized,
        )

    negated_match = _last_match((_HANDOFF_NEGATED_RE,), scan_text)
    request_match = _last_match(_HANDOFF_STRONG_REQUEST_PATTERNS, scan_text)
    if request_match is not None and (
        negated_match is None or request_match.end() > negated_match.end()
    ):
        return GroupHumanIntent(
            GroupHumanIntentType.HANDOFF_REQUEST,
            "group_handoff_unavailable",
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
