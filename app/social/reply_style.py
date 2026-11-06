"""Deterministic, conservative style shaping for soft group conversation."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

_LIST_PREFIX_RE = re.compile(r"(?m)^\s*(?:[-*•·]|\d{1,2}[.)、]|[一二三四五六七八九十]+[、.])\s*")
_EMOJI_RE = re.compile(
    "["
    "\U0001f1e6-\U0001f1ff"
    "\U0001f300-\U0001faff"
    "\u2600-\u27bf"
    "](?:\ufe0f|\u200d[\U0001f300-\U0001faff])?"
)
_DETAIL_REQUEST_RE = re.compile(
    r"(?:详细|展开|具体说|说清楚|一步步|逐条|列出|清单|步骤|完整说明|多说点)"
)
_LIST_REQUEST_RE = re.compile(r"(?:列出|清单|逐条|分点|步骤|一二三|列表)")
_IDENTITY_DISCLOSURE_RE = re.compile(
    r"^\s*((?:我是|这里是)(?:一名|一个)?\s*(?:AI|人工智能)(?:助手|机器人)?[，,。.!！?？：:]*)\s*",
    re.IGNORECASE,
)
_IDENTITY_DISCLOSURE_PREFIX = "我是 AI 助手。"
_MAX_EMOJI_FREQUENCY = 0.15
_SHORT_REPLY_INLINE_CHARS = 80
_TERMINAL_PUNCT = "，,。.!！?？；;：:…~～"
_TRAILING_TIC_RE = re.compile(
    r"(?P<pad>[\s，,。.!！?？~～]*)(?P<tic>hhh|干就完了)(?P<tail>[。.!！?？~～]*)\s*$",
    re.IGNORECASE,
)
_TRAILING_TIC_WINDOW = 3
_AGREE_OPENER_RE = re.compile(r"^(是啊|对啊|对的|对，|可以啊|有啊|行啊)[，,。.!！?？\s]*")
_NEW_INFO_RE = re.compile(
    r"(不过|但是|可是|问题是|下次|要不|其实|另外|建议|试试|先|然后|因为|所以|区别|为啥|怎么|但)"
)
_ECHO_TOKEN_MIN = 2
_ECHO_TOKEN_RATIO = 0.28
_ECHO_SEQUENCE = 0.42
_CJK_STOP = set("的了是在就都也还真这那把被和与他她你我")
_CATCHPHRASES = (
    "哈哈",
    "确实",
    "没错",
    "可以的",
    "懂了",
    "收到",
    "好家伙",
    "说真的",
    "简单说",
    "总之",
    "其实",
    "讲真",
    "有一说一",
)


@dataclass(frozen=True, slots=True)
class ReplyStyleHistory:
    emojis_last_20: tuple[str, ...] = ()
    catchphrases_last_30: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReplyStyleResult:
    text: str
    mode: str
    transformed: bool
    emoji: str = ""
    catchphrase: str = ""
    identity_disclosed: bool = False
    reason_codes: tuple[str, ...] = ()


class NaturalReplyStyleGuard:
    """Shape only explicitly nominated soft replies.

    The caller decides eligibility.  Tool results, factual/high-risk answers,
    safety responses, commands, reports, and repeater text must pass with
    ``eligible=False`` and are returned byte-for-byte unchanged.
    """

    def apply(
        self,
        text: str,
        *,
        deterministic_key: str,
        eligible: bool,
        source_text: str = "",
        explicitly_detailed: bool | None = None,
        history: ReplyStyleHistory | None = None,
        voice_profile: Mapping[str, Any] | None = None,
        preserve_persona_style: bool = False,
    ) -> ReplyStyleResult:
        original = str(text or "")
        if not eligible or not original.strip():
            return ReplyStyleResult(
                text=original,
                mode="preserved",
                transformed=False,
                emoji=_first_emoji(original),
                catchphrase=_catchphrase(original),
            )

        active_history = history or ReplyStyleHistory()
        list_requested = bool(_LIST_REQUEST_RE.search(str(source_text or "")))
        profile = dict(voice_profile or {})
        profile_phrases = _profile_phrase_preferences(profile)
        # Persona and generic chat keep the model text. Length clipping used
        # to make no-persona replies look refused or truncated.
        mode = "persona" if preserve_persona_style else "natural"

        value = original.strip()
        reasons: list[str] = []
        list_policy = str(profile.get("list_format_policy") or "avoid_by_default").strip()
        if list_policy != "allow" and not list_requested and _LIST_PREFIX_RE.search(value):
            value = _flatten_list(value)
            reasons.append("list_flattened")

        candidate_emoji = _first_emoji(value)
        value = _EMOJI_RE.sub("", value)
        allowed_emoji = ""
        emoji_frequency = _bounded_float(
            profile.get("emoji_frequency"),
            default=_MAX_EMOJI_FREQUENCY,
            maximum=_MAX_EMOJI_FREQUENCY,
        )
        if (
            candidate_emoji
            and _bucket(deterministic_key, "emoji", 10_000) < round(emoji_frequency * 10_000)
            and candidate_emoji not in set(active_history.emojis_last_20[:20])
        ):
            allowed_emoji = candidate_emoji
        elif candidate_emoji:
            reasons.append("emoji_suppressed")

        identity_prefix = ""
        identity_disclosed = False
        if str(profile.get("identity_disclosure") or "contextual") == "always":
            identity_prefix, value, inserted = _identity_disclosure_parts(value)
            identity_disclosed = True
            reasons.append("identity_disclosed")
            if inserted:
                reasons.append("identity_prefix_added")

        candidate_catchphrase = _catchphrase(value, profile_phrases)
        recent_catchphrases = {
            _phrase_key(phrase) for phrase in active_history.catchphrases_last_30[:30]
        }
        if candidate_catchphrase and _phrase_key(candidate_catchphrase) in recent_catchphrases:
            value = _remove_leading_catchphrase(value, candidate_catchphrase)
            reasons.append("catchphrase_suppressed")
            candidate_catchphrase = ""

        value, trailing_tic, trailing_stripped = soften_repeated_trailing_tics(
            value,
            recent_catchphrases=active_history.catchphrases_last_30,
        )
        if trailing_stripped:
            reasons.append("trailing_tic_suppressed")
        elif trailing_tic and not candidate_catchphrase:
            candidate_catchphrase = trailing_tic

        inlined = _inline_short_newlines(value)
        if inlined != value:
            reasons.append("short_lines_inlined")
        value = _normalize_spacing(inlined)
        if identity_prefix:
            value = f"{identity_prefix}{value}"
        if allowed_emoji:
            value = f"{value}{allowed_emoji}" if value else allowed_emoji

        return ReplyStyleResult(
            text=value,
            mode=mode,
            transformed=value != original,
            emoji=allowed_emoji,
            catchphrase=candidate_catchphrase,
            identity_disclosed=identity_disclosed,
            reason_codes=tuple(reasons),
        )


def soften_repeated_trailing_tics(
    text: str,
    *,
    recent_catchphrases: tuple[str, ...] = (),
    window: int = _TRAILING_TIC_WINDOW,
) -> tuple[str, str, bool]:
    """Keep a trailing tic only when the last few bot lines did not already use it."""

    value = str(text or "")
    match = _TRAILING_TIC_RE.search(value)
    if match is None:
        return value, "", False
    tic = match.group("tic")
    recent = [
        phrase
        for phrase in recent_catchphrases[: max(1, window)]
        if _phrase_key(phrase) == _phrase_key(tic)
    ]
    if not recent:
        return value, tic, False
    prefix = value[: match.start()].rstrip("，,。.!！?？~～ ")
    if not prefix:
        return value, tic, False
    return prefix, tic, True


def is_agree_echo(reply: str, source: str) -> bool:
    """True when a soft reply is just agreeing and restating the last line."""

    reply_raw = str(reply or "").strip()
    source_raw = str(source or "").strip()
    if not reply_raw or not source_raw:
        return False
    compact_reply = _compact_chat_text(reply_raw)
    if _AGREE_OPENER_RE.match(reply_raw) and len(compact_reply) < 4:
        return True
    compact_source = _compact_chat_text(source_raw)
    if not compact_reply or not compact_source:
        return False
    if compact_reply == compact_source:
        return True
    shorter, longer = (
        (compact_reply, compact_source)
        if len(compact_reply) <= len(compact_source)
        else (compact_source, compact_reply)
    )
    if len(shorter) >= 6 and shorter in longer and len(shorter) / len(longer) >= 0.7:
        return True
    if not _AGREE_OPENER_RE.match(reply_raw):
        return False
    if _NEW_INFO_RE.search(_AGREE_OPENER_RE.sub("", reply_raw, count=1)):
        return False
    source_tokens = _distinctive_tokens(compact_source)
    shared = source_tokens & _distinctive_tokens(compact_reply)
    if len(shared) >= _ECHO_TOKEN_MIN and (
        len(shared) / max(1, len(source_tokens)) >= _ECHO_TOKEN_RATIO
    ):
        return True
    return SequenceMatcher(None, compact_reply, compact_source).ratio() >= _ECHO_SEQUENCE


def _compact_chat_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).lower()
    value = _AGREE_OPENER_RE.sub("", value)
    value = re.sub(r"\bhhh+\b", "", value, flags=re.IGNORECASE)
    value = value.replace("干就完了", "")
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE)


def _distinctive_tokens(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9]{3,}", text))
    for index, char in enumerate(text):
        if index + 1 >= len(text):
            break
        if not ("\u4e00" <= char <= "\u9fff"):
            continue
        gram = text[index : index + 2]
        if any(item in _CJK_STOP for item in gram):
            continue
        tokens.add(gram)
    return tokens


def requests_detailed_answer(source_text: str) -> bool:
    return bool(_DETAIL_REQUEST_RE.search(str(source_text or "")))


def text_fingerprint(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or "")).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _bucket(key: str, namespace: str, modulo: int) -> int:
    digest = hashlib.sha256(f"{namespace}\0{key or ''!s}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % modulo


def _first_emoji(text: str) -> str:
    match = _EMOJI_RE.search(str(text or ""))
    return match.group(0) if match else ""


def _catchphrase(text: str, phrase_preferences: tuple[str, ...] = ()) -> str:
    value = str(text or "").lstrip("，,。.!！?？~～ ")
    phrases = tuple(
        sorted(
            (*phrase_preferences, *_CATCHPHRASES),
            key=lambda phrase: (-len(phrase), phrase),
        )
    )
    for phrase in phrases:
        if value.startswith(phrase):
            return phrase
    return ""


def _profile_phrase_preferences(profile: Mapping[str, Any]) -> tuple[str, ...]:
    raw = profile.get("phrase_preferences")
    if not isinstance(raw, (list, tuple)):
        return ()
    values: list[str] = []
    seen: set[str] = set()
    for item in raw:
        phrase = " ".join(str(item or "").split())[:80]
        key = _phrase_key(phrase)
        if not phrase or key in seen:
            continue
        seen.add(key)
        values.append(phrase)
        if len(values) >= 30:
            break
    return tuple(values)


def _phrase_key(value: str) -> str:
    return unicodedata.normalize(
        "NFKC",
        " ".join(str(value or "").split()),
    ).casefold()


def _identity_disclosure_parts(text: str) -> tuple[str, str, bool]:
    value = str(text or "").strip()
    match = _IDENTITY_DISCLOSURE_RE.match(value)
    if match:
        prefix = match.group(1).strip()
        if prefix[-1:] not in "，,。.!！?？：:":
            prefix = f"{prefix}。"
        return prefix, value[match.end() :].lstrip(), False
    return _IDENTITY_DISCLOSURE_PREFIX, value, True


def _remove_leading_catchphrase(text: str, phrase: str) -> str:
    value = str(text or "").lstrip()
    if value.startswith(phrase):
        value = value[len(phrase) :].lstrip("，,。.!！?？~～ ")
    return value


def _flatten_list(text: str) -> str:
    parts = [
        _LIST_PREFIX_RE.sub("", line).strip().rstrip("；;。")
        for line in str(text or "").splitlines()
    ]
    parts = [part for part in parts if part]
    return "；".join(parts) + ("。" if parts else "")


def _join_inline(parts: list[str]) -> str:
    joined: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if not joined:
            joined.append(part)
            continue
        prev = joined[-1]
        if prev[-1:] not in _TERMINAL_PUNCT:
            sep = "。" if len(prev) >= 10 or len(part) >= 8 else "，"
            joined[-1] = prev + sep
        joined.append(part)
    return "".join(joined)


def _inline_short_newlines(text: str) -> str:
    """Keep WeChat short replies on one line.

    Long explanations, requested lists and already-single-line text are left
    alone. A couplet or three-word follow-up is joined with punctuation.
    """

    value = _normalize_spacing(text)
    lines = [line.strip() for line in value.split("\n") if line.strip()]
    if len(lines) <= 1 or _LIST_PREFIX_RE.search(value):
        return value
    if sum(len(line) for line in lines) > _SHORT_REPLY_INLINE_CHARS:
        return value
    return _join_inline(lines)


def _bounded_float(value: Any, *, default: float, maximum: float = 1.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.0, min(maximum, parsed))


def _normalize_spacing(text: str) -> str:
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"[ \t]*\n[ \t]*", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    value = re.sub(r"[ \t]+([，。！？；：,.!?;:])", r"\1", value)
    return value.strip()
