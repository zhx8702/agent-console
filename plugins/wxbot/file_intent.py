"""Fail-closed file intent detection for wxbot conversations.

The channel can now receive and send files, but a file must not become an
implicit side effect of an ordinary answer.  This module keeps the decision
small and inspectable: it classifies the requested operation, the requested
format and whether the user explicitly asked for delivery.  It is deliberately
deterministic; an LLM may refine the content of a file, but it does not get to
invent the fact that a file should be sent.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

FileOperation = Literal[
    "none",
    "inspect_incoming",
    "generate",
    "send_existing",
    "convert",
    "export_history",
]
FileSource = Literal["none", "incoming_attachment", "conversation", "user_path"]

MAX_RECENT_MESSAGE_EXPORT_MINUTES = 24 * 60

_FORMAT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pdf", re.compile(r"(?:pdf|PDF|便携文档)")),
    ("docx", re.compile(r"(?:docx|word|Word)")),
    ("xlsx", re.compile(r"(?:xlsx|excel|Excel)")),
    ("csv", re.compile(r"(?:csv|CSV)")),
    ("json", re.compile(r"(?:json|JSON)")),
    ("md", re.compile(r"(?:markdown|Markdown|md)")),
    ("txt", re.compile(r"(?:txt|TXT|纯文本|文本文件)")),
)

_FILE_REFERENCE_RE = re.compile(
    r"(?:文件|文档|附件|报告|表格|压缩包|资料|下载|file|document|attachment)",
    re.IGNORECASE,
)
_DELIVERY_RE = re.compile(
    r"(?:发(?:给我|我|一下|一份|个)?|发送|给我|给大家|给群里|转发|附上|作为附件|"
    r"下载|导出|输出|生成|做成|整理成|保存成|另存为|send|attach|download|export)",
    re.IGNORECASE,
)
_INBOUND_FILE_SEND_RE = re.compile(
    r"(?:(?:我|咱们?|本人)\s*)?(?:发送?给你|传给你|上传给你|给你\s*(?:发|传|上传)|"
    r"发来|传来|"
    r"发(?:个|一个|一下)?|传(?:个|一个|一下)?|上传(?:个|一个|一下)?)"
    r"(?:\s*(?:一个|一份|个))?\s*(?:文件|附件|文档|资料)",
    re.IGNORECASE,
)
_OUTBOUND_FILE_DELIVERY_RE = re.compile(
    r"(?:发给我|发我|发送给我|给我|发给大家|给大家|发到群里|给群里|"
    r"转发|下载|导出|输出|保存(?:成|为)?|另存为|作为附件|send|attach|"
    r"download|export)",
    re.IGNORECASE,
)
_EXPLICIT_FILE_OUTPUT_RE = re.compile(
    r"(?:文件|附件|文档|file|document|attachment|pdf|docx|xlsx|csv|json|"
    r"markdown|md|txt|下载|导出|输出|保存|另存|作为附件|发给我|发我|"
    r"发送给我|给我|转发|send|attach|download|export)",
    re.IGNORECASE,
)
_ANALYZE_TO_FILE_RE = re.compile(
    r"(?:总结|汇总|整理|归纳|摘要|分析|解析|提取).{0,16}"
    r"(?:成|为|做成|生成|输出|导出|保存)",
    re.IGNORECASE,
)
_CONVERT_RE = re.compile(
    r"(?:转换|转成|改成|换成|另存为|导出为|convert|transform)",
    re.IGNORECASE,
)
_INSPECT_RE = re.compile(
    r"(?:总结|汇总|摘要|解析|分析|读取|读一下|看看|看一下|看下|查看|打开|提取|识别|查一下|"
    r"里面|内容|上面写了什么|总结一下|summari[sz]e|inspect|read)",
    re.IGNORECASE,
)
_MESSAGE_RE = re.compile(
    r"(?:消息|聊天记录|群聊记录|私聊记录|消息记录|聊天内容|群消息|"
    r"群(?:里|内)(?:的)?话题|群(?:里|内).{1,12}话题|群聊话题)"
)
_SUMMARY_RE = re.compile(r"(?:汇总|总结|整理|归纳|摘要)")
_PATH_RE = re.compile(r"(?:^|\s)(?:[A-Za-z]:[\\/]|/|\\\\)[^\s]+")
_NEGATED_RE = re.compile(
    r"(?:不要|别|无需|不用|不必|不需要|不想|禁止|切勿|避免|"
    r"直接回复文字|只要文字|不用文件|无需文件|不发文件|不发送文件|"
    r"do\s+not|don['’]?t|no\s+need)",
    re.IGNORECASE,
)
_DELIVERY_NEGATION_RE = re.compile(
    r"(?:不要|别|无需|不用|不必|不需要|不想|禁止|切勿|避免|"
    r"直接回复文字|只要文字|不用文件|无需文件|不发文件|不发送文件)"
    r".{0,16}(?:发|发送|导出|输出|生成|文件|附件|download|export|send|attach)"
    r"|(?:发|发送|导出|输出|生成|文件|附件|download|export|send|attach)"
    r".{0,16}(?:不要|别|无需|不用|不必|不需要|不发|不发送|只要文字)",
    re.IGNORECASE,
)
_RECENT_MINUTES_RE = re.compile(
    r"(?P<amount>\d{1,5}|[零〇一二两三四五六七八九十百千]+)\s*(?:个)?分钟"
)
_RECENT_PREFIX_RE = re.compile(r"(?:最近|近|过去|前|刚才|此前)\s*$")
_RECENT_SUFFIX_RE = re.compile(r"^\s*(?:内|以内|之内|以来)")
_FUTURE_DURATION_RE = re.compile(r"^\s*(?:后|以后|之后|再)")
_PAST_DURATION_RE = re.compile(r"^\s*(?:前|以前|之前)")
_APPROXIMATE_DURATION_PREFIX_RE = re.compile(
    r"(?:大约|大概|大概要|约|差不多|超过|多于|大于|不到|不满|至少|最多|不超过)\s*$"
)
_APPROXIMATE_DURATION_SUFFIX_RE = re.compile(r"^\s*(?:左右|以上|以下|多|余)")
_EFFORT_DURATION_RE = re.compile(r"(?:用|花|耗时|等待|在)\s*$")
_CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CHINESE_UNITS = {"十": 10, "百": 100, "千": 1000}


@dataclass(frozen=True, slots=True)
class FileIntent:
    """The bounded, auditable result of file intent detection."""

    operation: FileOperation = "none"
    delivery_required: bool = False
    requested_format: str = ""
    source: FileSource = "none"
    confidence: float = 0.0
    has_attachment: bool = False
    needs_confirmation: bool = False
    recent_minutes: int | None = None
    recent_minutes_invalid: bool = False
    cues: tuple[str, ...] = ()

    @property
    def file_requested(self) -> bool:
        return self.operation != "none"

    def as_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "delivery_required": self.delivery_required,
            "requested_format": self.requested_format,
            "source": self.source,
            "confidence": self.confidence,
            "has_attachment": self.has_attachment,
            "needs_confirmation": self.needs_confirmation,
            "recent_minutes": self.recent_minutes,
            "recent_minutes_invalid": self.recent_minutes_invalid,
            "cues": list(self.cues),
        }


def _parse_chinese_integer(value: str) -> int | None:
    if not value or any(
        char not in _CHINESE_DIGITS and char not in _CHINESE_UNITS for char in value
    ):
        return None
    if not any(char in _CHINESE_UNITS for char in value):
        return _CHINESE_DIGITS[value] if len(value) == 1 else None

    total = 0
    digit = 0
    previous_unit = 10_000
    for char in value:
        if char in _CHINESE_DIGITS:
            digit = _CHINESE_DIGITS[char]
            continue
        unit = _CHINESE_UNITS[char]
        if unit >= previous_unit:
            return None
        total += (digit or 1) * unit
        digit = 0
        previous_unit = unit
    return total + digit


def _recent_message_minutes_state(text: str) -> tuple[bool, int | None]:
    value = unicodedata.normalize("NFKC", str(text or "")).strip()
    if not value:
        return False, None
    requested = False
    candidates: list[int] = []
    for match in _RECENT_MINUTES_RE.finditer(value):
        before = value[max(0, match.start() - 24) : match.start()]
        after = value[match.end() : match.end() + 24]
        if _EFFORT_DURATION_RE.search(before):
            continue
        has_recent_cue = bool(_RECENT_PREFIX_RE.search(before) or _RECENT_SUFFIX_RE.match(after))
        has_message_summary_context = bool(
            (_SUMMARY_RE.search(before) or _SUMMARY_RE.search(after))
            and (_MESSAGE_RE.search(before) or _MESSAGE_RE.search(after))
        )
        if not has_recent_cue and not has_message_summary_context:
            continue
        requested = True
        if (
            _FUTURE_DURATION_RE.match(after)
            or _PAST_DURATION_RE.match(after)
            or re.search(r"(?:未来|接下来|随后|往后)\s*$", before)
            or _APPROXIMATE_DURATION_PREFIX_RE.search(before)
            or _APPROXIMATE_DURATION_SUFFIX_RE.match(after)
            or _NEGATED_RE.search(before[-12:])
        ):
            continue
        amount = match.group("amount")
        minutes = int(amount) if amount.isdigit() else _parse_chinese_integer(amount)
        if minutes is not None:
            candidates.append(minutes)
    if requested and len(candidates) == 1:
        return True, candidates[0]
    return requested, None


def parse_recent_message_minutes(text: str) -> int | None:
    """Return one unambiguous recent-message window, otherwise ``None``."""

    _requested, minutes = _recent_message_minutes_state(text)
    return minutes


def _normalized(text: str) -> str:
    value = str(text or "").strip()
    if value.startswith("/"):
        return ""
    return value


def _affirmative_matches(pattern: re.Pattern[str], text: str) -> list[str]:
    """Return matches which are not covered by a nearby negation."""

    found: list[str] = []
    for match in pattern.finditer(text):
        before = text[max(0, match.start() - 12) : match.start()]
        if _NEGATED_RE.search(before):
            continue
        found.append(match.group(0))
    return found


def _requested_format(text: str) -> str:
    for name, pattern in _FORMAT_PATTERNS:
        if _affirmative_matches(pattern, text):
            return name
    return ""


def _delivery_is_affirmative(text: str, delivery_cues: list[str]) -> bool:
    if not delivery_cues:
        return False
    # A negated delivery clause must not be rescued by a positive cue in an
    # earlier clause (for example, a request to create a file followed by a
    # request not to send it).
    clauses = re.split(
        r"[,，。！？!?；;\n]|(?:但|不过|然而|而是|改成|改为|然后)",
        text,
        flags=re.IGNORECASE,
    )
    affirmative = False
    for clause in clauses:
        if not _DELIVERY_RE.search(clause):
            continue
        if _DELIVERY_NEGATION_RE.search(clause) or _NEGATED_RE.search(clause):
            affirmative = False
        else:
            affirmative = True
    return affirmative


def _inbound_only_file_send(text: str) -> bool:
    """Detect a caption saying that the user is sending a file to us.

    ``发个文件给你`` is an inbound attachment announcement, not a request
    for the bot to generate or send a file.  A later outbound clause such as
    ``转换后发我`` keeps the whole request actionable and is handled by the
    normal clause-scoped delivery check.
    """

    match = _INBOUND_FILE_SEND_RE.search(text)
    if match is None:
        return False
    return _OUTBOUND_FILE_DELIVERY_RE.search(text, match.end()) is None


def classify_file_intent(text: str, *, has_attachment: bool = False) -> FileIntent:
    """Classify a file operation without turning file words into side effects.

    ``delivery_required`` is true only for an affirmative delivery/generation
    cue.  A bare request such as ``总结一下这个文件`` therefore becomes an
    inspect operation, while ``总结成 PDF 发我`` becomes a generated-file
    request (and the tool can reject unsupported formats explicitly).
    """

    value = _normalized(text)
    attachment = bool(has_attachment)
    if not value:
        return FileIntent(has_attachment=attachment)

    file_cues = _affirmative_matches(_FILE_REFERENCE_RE, value)
    format_name = _requested_format(value)
    # Keep negated delivery words as structural cues (they still establish
    # that the user is talking about file delivery).  The affirmative/negative
    # decision is made clause-by-clause in ``_delivery_is_affirmative`` so a
    # later explicit "发我" is not masked by an earlier "不要发送".
    delivery_cues = [match.group(0) for match in _DELIVERY_RE.finditer(value)]
    if _inbound_only_file_send(value):
        delivery_cues = []
    convert_cues = _affirmative_matches(_CONVERT_RE, value)
    inspect_cues = _affirmative_matches(_INSPECT_RE, value)
    message_cues = _affirmative_matches(_MESSAGE_RE, value)
    summary_cues = _affirmative_matches(_SUMMARY_RE, value)
    path_cues = _affirmative_matches(_PATH_RE, value)

    has_file_reference = bool(file_cues or format_name or attachment or path_cues)
    if not has_file_reference:
        return FileIntent(has_attachment=attachment)

    # A history export may be phrased as analysis/inspection rather than
    # "summary" (for example, "把聊天记录分析成文件发我").  Keep the
    # message cue mandatory so a normal attachment analysis is not mistaken
    # for a conversation export.
    history_action = bool(
        summary_cues or format_name or (inspect_cues and file_cues) or (file_cues and delivery_cues)
    )
    history_summary = bool(message_cues and history_action)
    delivery_required = _delivery_is_affirmative(value, delivery_cues)
    explicit_file_output = bool(_EXPLICIT_FILE_OUTPUT_RE.search(value))
    if history_summary and explicit_file_output:
        recent_minutes_requested, recent_minutes = _recent_message_minutes_state(value)
        return FileIntent(
            operation="export_history",
            delivery_required=delivery_required,
            requested_format=format_name or "txt",
            source="conversation",
            confidence=0.98 if delivery_required else 0.72,
            has_attachment=attachment,
            needs_confirmation=not delivery_required,
            recent_minutes=recent_minutes,
            recent_minutes_invalid=(recent_minutes_requested and recent_minutes is None),
            cues=tuple(dict.fromkeys((*message_cues, *summary_cues, *delivery_cues))),
        )

    # ``分析成文件发我`` means: inspect the inbound file, then package the
    # resulting answer as a new outbound file.  It is different from a plain
    # inspection and must remain in the file scope so the agent can call
    # inspect_current_file followed by generate_text_file.
    if (
        attachment
        and inspect_cues
        and delivery_required
        and explicit_file_output
        and _ANALYZE_TO_FILE_RE.search(value)
    ):
        return FileIntent(
            operation="generate",
            delivery_required=True,
            requested_format=format_name,
            source="incoming_attachment",
            confidence=0.94,
            has_attachment=True,
            cues=tuple(dict.fromkeys((*inspect_cues, *delivery_cues, *file_cues))),
        )

    if convert_cues and (format_name or file_cues or attachment):
        return FileIntent(
            operation="convert",
            # ``explicit_file_output`` only records that delivery words were
            # present.  It can still be a negated clause ("生成文件但不要
            # 发"), so never let it turn a negative request into a send.
            delivery_required=delivery_required,
            requested_format=format_name,
            source="incoming_attachment"
            if attachment
            else "user_path"
            if path_cues
            else "conversation",
            confidence=0.94 if delivery_required else 0.78,
            has_attachment=attachment,
            needs_confirmation=not delivery_required,
            cues=tuple(dict.fromkeys((*convert_cues, *delivery_cues, *file_cues))),
        )

    # Receiving a file alone is not a request.  Require an affirmative read/
    # analyse cue before exposing the file tool; otherwise a file upload to a
    # mentioned bot would unexpectedly trigger an agent turn.
    if attachment and inspect_cues:
        return FileIntent(
            operation="inspect_incoming",
            delivery_required=False,
            requested_format=format_name,
            source="incoming_attachment",
            confidence=0.92 if inspect_cues else 0.72,
            has_attachment=True,
            cues=tuple(dict.fromkeys((*inspect_cues, *file_cues))),
        )

    if delivery_cues and (format_name or path_cues or (file_cues and explicit_file_output)):
        return FileIntent(
            operation="send_existing" if path_cues and not convert_cues else "generate",
            delivery_required=delivery_required,
            requested_format=format_name,
            source="user_path" if path_cues else "conversation",
            confidence=0.9 if delivery_required else 0.65,
            has_attachment=attachment,
            needs_confirmation=not delivery_required,
            cues=tuple(dict.fromkeys((*delivery_cues, *file_cues))),
        )

    if inspect_cues and (file_cues or format_name):
        return FileIntent(
            operation="inspect_incoming" if attachment else "none",
            delivery_required=False,
            requested_format=format_name,
            source="incoming_attachment" if attachment else "none",
            confidence=0.8 if attachment else 0.0,
            has_attachment=attachment,
            cues=tuple(dict.fromkeys((*inspect_cues, *file_cues))),
        )

    return FileIntent(
        operation="none",
        requested_format=format_name,
        has_attachment=attachment,
    )


__all__ = [
    "MAX_RECENT_MESSAGE_EXPORT_MINUTES",
    "FileIntent",
    "classify_file_intent",
    "parse_recent_message_minutes",
]
