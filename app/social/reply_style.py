"""Deterministic, conservative style shaping for soft group conversation."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])\s*")
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
        detail_requested = (
            bool(explicitly_detailed)
            if explicitly_detailed is not None
            else bool(_DETAIL_REQUEST_RE.search(str(source_text or "")))
        )
        list_requested = bool(_LIST_REQUEST_RE.search(str(source_text or "")))
        profile = dict(voice_profile or {})
        profile_phrases = _profile_phrase_preferences(profile)
        verbosity = str(profile.get("verbosity") or "concise").strip().lower()
        bucket = _bucket(deterministic_key, "length", 100)
        if (
            verbosity == "terse"
            or (verbosity == "concise" and bucket < 65)
            or (verbosity == "balanced" and bucket < 35)
        ):
            mode = "one_sentence"
            sentence_limit = 1
            char_limit = 35
        elif (
            (verbosity == "concise" and bucket < 90)
            or (verbosity == "balanced" and bucket < 75)
            or not detail_requested
        ):
            mode = "two_sentences"
            sentence_limit = 2
            char_limit = 70
        else:
            mode = "expanded_requested"
            sentence_limit = 0
            char_limit = 0

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

        if sentence_limit:
            text_budget = max(
                1,
                char_limit - len(allowed_emoji) - len(identity_prefix),
            )
            shortened = _limit_sentences(value, sentence_limit, text_budget)
            if shortened != value:
                reasons.append("length_shaped")
            value = shortened
        value = _normalize_spacing(value)
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


def _limit_sentences(text: str, count: int, char_limit: int) -> str:
    value = _normalize_spacing(text)
    sentences = [part.strip() for part in _SENTENCE_SPLIT_RE.split(value) if part.strip()]
    selected = "".join(sentences[:count]) if sentences else value
    if len(selected) <= char_limit:
        return selected
    # Only soft, non-factual replies reach this guard. Prefer a nearby natural
    # clause boundary, then add an ellipsis inside the exact character budget.
    # Tool, safety, factual and obligation replies are preserved earlier.
    prefix = selected[: max(1, char_limit - 1)]
    minimum_boundary = max(1, int(char_limit * 0.6))
    boundary = max(
        (prefix.rfind(marker) for marker in "，、；;：:,. "),
        default=-1,
    )
    if boundary >= minimum_boundary:
        prefix = prefix[:boundary]
    return f"{prefix.rstrip('，、；;：:,. ')}…"


def _bounded_float(value: Any, *, default: float, maximum: float = 1.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(0.0, min(maximum, parsed))


def _normalize_spacing(text: str) -> str:
    value = re.sub(r"[ \t]+", " ", str(text or ""))
    value = re.sub(r"\s*\n\s*", "", value)
    value = re.sub(r"\s+([，。！？；：,.!?;:])", r"\1", value)
    return value.strip()
