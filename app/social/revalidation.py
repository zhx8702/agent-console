"""Deterministic send-time checks for delayed group replies.

The classifier is deliberately conservative: it only treats a human message as
an answer when it can tie that message back to the queued source question.  It
never sends message text to another service and returns reason codes rather than
persisting a second copy of group content.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

_QUESTION_RE = re.compile(
    r"[?？]|(?:谁|什么|怎么|怎样|哪里|哪儿|哪个|多少|几时|几点|为何|为什么|"
    r"有没有|是否|能否|可以吗|知道吗|请问|求助|帮忙|帮我|推荐)"
)
_ANSWER_PREFIX_RE = re.compile(
    r"^(?:可以|不可以|能|不能|是|不是|有|没有|在|不在|会|不会|"
    r"建议|答案|因为|应该|不用|需要|大概|我觉得|我试过|我知道)"
)
_UPDATE_RE = re.compile(r"(?:补充|更正|改成|不是.+是|刚才说错|重新|算了|不用了|取消)")
_LATIN_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_+.-]{1,31}", re.IGNORECASE)
_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff]{2,24}")
_MEANINGFUL_RE = re.compile(r"[a-z0-9\u3400-\u9fff]", re.IGNORECASE)

_GENERIC_TERMS = {
    "什么",
    "怎么",
    "怎样",
    "哪里",
    "哪个",
    "多少",
    "为什么",
    "有没有",
    "是否",
    "可以",
    "知道",
    "请问",
    "帮忙",
    "帮我",
    "一下",
    "这个",
    "那个",
    "有人",
    "大家",
}


@dataclass(frozen=True, slots=True)
class GroupReplyRevalidation:
    context_available: bool
    valid_member_answer_exists: bool = False
    topic_changed: bool = False
    superseded_by_newer_message: bool = False
    source_is_self_sent: bool = False
    newer_human_messages: int = 0
    reason_codes: tuple[str, ...] = ()


def evaluate_group_reply_revalidation(
    *,
    source: dict[str, Any] | None,
    newer_observations: Iterable[dict[str, Any]],
    participation_status: str,
) -> GroupReplyRevalidation:
    """Classify volatile changes between generation and SDK dispatch.

    ``must_reply`` is fenced only by a tied human answer or a newer addressed
    message from the same member.  Unrelated chatter never suppresses a direct
    call.  ``may_reply`` is additionally cancelled after a durable topic shift.
    """

    if not source:
        return GroupReplyRevalidation(
            context_available=False,
            reason_codes=("source_observation_missing",),
        )

    source_message_id = str(source.get("message_id") or "").strip()
    source_text = str(source.get("content") or "").strip()
    source_sender = _sender_id(source)
    source_terms = _topic_terms(source_text)
    source_is_question = bool(_QUESTION_RE.search(source_text))
    status = str(participation_status or "").strip().lower()

    human_messages: list[dict[str, Any]] = []
    relevant_flags: list[bool] = []
    valid_answer = False
    superseded = False
    reasons: list[str] = []

    observations = sorted(
        (dict(item) for item in newer_observations if isinstance(item, dict)),
        key=lambda item: (
            int(item.get("id") or 0),
            int(item.get("occurred_ts") or 0),
        ),
    )
    for item in observations:
        if bool(item.get("is_self_sent")):
            continue
        text = str(item.get("content") or "").strip()
        if not text or not _MEANINGFUL_RE.search(text):
            continue
        human_messages.append(item)
        same_sender = bool(source_sender and _sender_id(item) == source_sender)
        quotes_source = _quotes_message(item, source_message_id)
        mentions_source_sender = _mentions_sender(item, source_sender)
        related = bool(
            quotes_source
            or mentions_source_sender
            or _topics_overlap(source_terms, _topic_terms(text))
        )
        relevant_flags.append(related)

        bot_addressed = bool(item.get("bot_addressed") or item.get("mentioned_me"))
        if (
            source_is_question
            and not same_sender
            and not bot_addressed
            and related
            and _looks_like_answer(text)
        ):
            valid_answer = True

        if same_sender:
            if status == "must_reply":
                superseded = superseded or bool(
                    bot_addressed
                    or quotes_source
                    or (related and _UPDATE_RE.search(text))
                )
            else:
                superseded = superseded or bool(
                    bot_addressed
                    or quotes_source
                    or (related and _UPDATE_RE.search(text))
                )

    topic_changed = False
    if status != "must_reply" and len(human_messages) >= 2:
        tail_messages = human_messages[-3:]
        tail_relevance = relevant_flags[-3:]
        unrelated_tail = sum(not flag for flag in tail_relevance)
        distinct_senders = {_sender_id(item) for item in tail_messages if _sender_id(item)}
        topic_changed = bool(
            unrelated_tail >= 2
            and (len(distinct_senders) >= 2 or len(tail_messages) >= 3)
        )

    if valid_answer:
        reasons.append("valid_member_answer_after_source")
    if superseded:
        reasons.append("newer_member_update_superseded_source")
    if topic_changed:
        reasons.append("durable_topic_shift_after_source")
    if not reasons:
        reasons.append("source_still_current")
    return GroupReplyRevalidation(
        context_available=True,
        valid_member_answer_exists=valid_answer,
        topic_changed=topic_changed,
        superseded_by_newer_message=superseded,
        source_is_self_sent=bool(source.get("is_self_sent")),
        newer_human_messages=len(human_messages),
        reason_codes=tuple(reasons),
    )


def _sender_id(item: dict[str, Any]) -> str:
    return str(item.get("sender_wxid") or item.get("sender_name") or "").strip().lower()


def _topic_terms(text: str) -> set[str]:
    lowered = str(text or "").lower()
    terms = {token for token in _LATIN_TOKEN_RE.findall(lowered)}
    for run in _CJK_RUN_RE.findall(lowered):
        if run not in _GENERIC_TERMS and len(run) <= 8:
            terms.add(run)
        for width in (2, 3):
            for index in range(max(0, len(run) - width + 1)):
                token = run[index : index + width]
                if token not in _GENERIC_TERMS:
                    terms.add(token)
    return terms


def _topics_overlap(left: set[str], right: set[str]) -> bool:
    if not left or not right:
        return False
    shared = left & right
    if not shared:
        return False
    if any(len(term) >= 3 for term in shared):
        return True
    return len(shared) >= 2 or min(len(left), len(right)) <= 3


def _looks_like_answer(text: str) -> bool:
    cleaned = re.sub(r"@[\w\-\u3400-\u9fff]+", "", str(text or "")).strip()
    if len(cleaned) < 2 or not _MEANINGFUL_RE.search(cleaned):
        return False
    if _ANSWER_PREFIX_RE.search(cleaned):
        return True
    # A short follow-up question is not accepted as somebody else's answer.
    return not bool(re.search(r"[?？]\s*$", cleaned)) and len(cleaned) >= 4


def _quotes_message(item: dict[str, Any], source_message_id: str) -> bool:
    if not source_message_id:
        return False
    metadata = item.get("metadata")
    candidates: list[dict[str, Any]] = []
    if isinstance(metadata, dict):
        candidates.append(metadata)
        quote = metadata.get("quote")
        if isinstance(quote, dict):
            candidates.append(quote)
    for key in ("quote", "metadata"):
        nested = item.get(key)
        if isinstance(nested, dict) and nested not in candidates:
            candidates.append(nested)
    reference_keys = {
        "refer_msg_svr_id",
        "refer_message_id",
        "refer_id",
        "quoted_message_id",
        "reply_to_msg_svr_id",
        "message_id",
        "msg_svr_id",
        "id",
    }
    for candidate in candidates:
        for key in reference_keys:
            if str(candidate.get(key) or "").strip() == source_message_id:
                return True
    return False


def _mentions_sender(item: dict[str, Any], source_sender: str) -> bool:
    if not source_sender:
        return False
    metadata = item.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    raw = metadata.get("at_wxids") or item.get("at_wxids") or []
    if isinstance(raw, str):
        mentioned = {part.strip().lower() for part in re.split(r"[,;\s]+", raw)}
    elif isinstance(raw, list | tuple | set):
        mentioned = {str(part).strip().lower() for part in raw}
    else:
        mentioned = set()
    return source_sender in mentioned
