"""M5 Preprocessor: clean, detect language, strip PII, quick sensitivity check,
classify coarse intent, score emotion."""
from __future__ import annotations

import html
import re
import unicodedata

from app.common.logging import get_logger
from app.common.types import Message, PreprocessedMessage
from app.preprocessing.emotion import score_emotion
from app.preprocessing.intent import classify_intent
from app.preprocessing.lang import detect_language
from app.preprocessing.pii import detect_and_mask

log = get_logger(__name__)

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

# Minimal set of prompt-injection/jailbreak patterns handled in the quick
# sensitivity pre-check. Full filter is in SafetyService.
_PROMPT_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ignore\s+(?:all\s+|the\s+)?(?:previous|prior|above)\s+instructions", re.I),
    re.compile(r"disregard\s+the\s+(?:above|prior|previous)", re.I),
    re.compile(r"you\s+are\s+now\s+.{0,40}?jailbroken", re.I),
    re.compile(r"jailbreak", re.I),
)


def _strip_control_chars(text: str) -> str:
    # Drop Cc / Cf control chars but keep newlines/tabs (they are handled by
    # whitespace collapse later).
    return "".join(
        ch for ch in text
        if ch in ("\n", "\t") or unicodedata.category(ch)[0] != "C"
    )


def _clean_text(text: str) -> str:
    # Decode HTML entities, strip tags, drop control chars, collapse whitespace.
    t = html.unescape(text or "")
    t = _HTML_TAG_RE.sub(" ", t)
    t = _strip_control_chars(t)
    t = _WHITESPACE_RE.sub(" ", t)
    return t.strip()


def _quick_sensitivity(text: str) -> tuple[bool, str | None]:
    for pat in _PROMPT_INJECTION_PATTERNS:
        if pat.search(text):
            return True, "prompt_injection_detected"
    return False, None


class Preprocessor:
    """Stateless preprocessor. Safe to instantiate once and share."""

    async def run(self, message: Message) -> PreprocessedMessage:
        original = message.content or ""
        cleaned = _clean_text(original)

        language = detect_language(cleaned)

        masked, pii_map = detect_and_mask(cleaned)

        sensitive, block_reason = _quick_sensitivity(masked)

        intent = classify_intent(masked)
        emotion = score_emotion(masked)

        log.debug(
            "preprocessor.done",
            language=language,
            intent=intent.value,
            emotion=emotion.value,
            pii_hits=len(pii_map),
            sensitive=sensitive,
        )

        return PreprocessedMessage(
            original_text=original,
            cleaned_text=masked,
            language=language,
            pii_map=pii_map,
            sensitive=sensitive,
            block_reason=block_reason,
            intent_coarse=intent,
            emotion=emotion,
            entities=[],  # hook for future NER
            attachments=list(message.attachments),
        )


def build_preprocessor() -> Preprocessor:
    return Preprocessor()
