"""Deterministic AI-identity and human-support intent handling.

Classification comes from the semantic intent contract.  Distilled COS
replies as that person, including identity questions; handoff still uses the
canned group reply.  Untrusted chat text cannot invent a human handoff.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from app.common.intent import IntentDecision, IntentDomain, IntentOperation
from app.common.intent_runtime import is_confident

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


def normalize_identity_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = _INVISIBLE_RE.sub("", value)
    value = _MENTION_PREFIX_RE.sub("", value)
    return _SPACE_RE.sub(" ", value).strip()


# Guard, not recognition: a bare "介绍下/介绍一下/介绍" almost always refers
# to whatever was just discussed (a product, a file, ...), so it vetoes the
# semantic identity verdict, which classifiers tend to over-trigger on such
# ultra-short follow-ups.
_FOLLOWUP_INTRODUCE_RE = re.compile(r"^(?:介绍下|介绍一下|介绍)$")


def classify_group_human_intent(
    text: str,
    *,
    decision: IntentDecision | None = None,
) -> GroupHumanIntent:
    """Map a semantic decision onto the group identity/handoff contract."""

    normalized = normalize_identity_text(text)
    if decision is None or not is_confident(decision):
        return GroupHumanIntent(
            GroupHumanIntentType.NONE,
            "group_human_intent_none",
            normalized,
        )

    if decision.domain is IntentDomain.HANDOFF or decision.operation is IntentOperation.HANDOFF:
        if decision.action in {"cancel", "non_request"}:
            return GroupHumanIntent(
                GroupHumanIntentType.HANDOFF_NON_REQUEST,
                "group_handoff_non_request",
                normalized,
            )
        return GroupHumanIntent(
            GroupHumanIntentType.HANDOFF_REQUEST,
            "group_handoff_unavailable",
            normalized,
        )
    if decision.domain is IntentDomain.IDENTITY:
        if _FOLLOWUP_INTRODUCE_RE.fullmatch(normalized):
            return GroupHumanIntent(
                GroupHumanIntentType.NONE,
                "group_human_intent_none",
                normalized,
            )
        # The identity domain has a single action in the classify contract
        # (identity/inquiry), so the domain verdict alone is the signal;
        # matching the action string would only add brittleness.
        return GroupHumanIntent(
            GroupHumanIntentType.IDENTITY_INQUIRY,
            "group_identity_disclosure",
            normalized,
        )
    return GroupHumanIntent(
        GroupHumanIntentType.NONE,
        "group_human_intent_none",
        normalized,
    )
