"""M12 Safety Service: input / output content checks."""
from __future__ import annotations

import re

from app.common.config import Settings, get_settings
from app.common.logging import get_logger
from app.common.types import PreprocessedMessage
from app.preprocessing.pii import detect_and_mask
from app.safety.keywords import load_keywords

log = get_logger(__name__)

_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ignore\s+(?:all|previous|above|prior)\s+instructions", re.I),
    re.compile(r"disregard\s+the\s+(?:above|prior|previous)", re.I),
    re.compile(r"you\s+are\s+now\s+.{0,40}?jailbroken", re.I),
)


class SafetyService:
    def __init__(self, keywords: list[str], *, block_pii_output: bool = True):
        # store lowercased for case-insensitive matching
        self._keywords_lower = tuple(kw.lower() for kw in keywords if kw)
        self._block_pii_output = block_pii_output

    @property
    def keywords(self) -> tuple[str, ...]:
        return self._keywords_lower

    def _match_keyword(self, text: str) -> str | None:
        low = text.lower()
        for kw in self._keywords_lower:
            if kw in low:
                return kw
        return None

    def _match_injection(self, text: str) -> str | None:
        for pat in _INJECTION_PATTERNS:
            if pat.search(text):
                return pat.pattern
        return None

    def check_input(self, pre: PreprocessedMessage) -> tuple[bool, str | None]:
        text = pre.cleaned_text or ""

        kw = self._match_keyword(text)
        if kw:
            log.info("safety.input.blocked", reason="keyword", match=kw)
            return True, f"keyword:{kw}"

        inj = self._match_injection(text)
        if inj:
            log.info("safety.input.blocked", reason="prompt_injection")
            return True, "prompt_injection"

        # Respect an upstream pre-check flag set by the preprocessor.
        if pre.sensitive:
            return True, pre.block_reason or "pre_flagged"

        return False, None

    def check_output(self, text: str) -> tuple[bool, str | None]:
        if not text:
            return False, None

        kw = self._match_keyword(text)
        if kw:
            log.info("safety.output.blocked", reason="keyword", match=kw)
            return True, f"keyword:{kw}"

        if self._block_pii_output:
            _masked, pii_map = detect_and_mask(text)
            if pii_map:
                pii_types = sorted(
                    {
                        placeholder.split(":", 2)[1]
                        for placeholder in pii_map
                        if placeholder.startswith("<PII:")
                    }
                )
                reason = f"pii_output:{','.join(pii_types)}"
                log.info("safety.output.blocked", reason="pii", pii_types=pii_types)
                return True, reason

        return False, None


def build_safety(settings: Settings | None = None) -> SafetyService:
    s = settings or get_settings()
    keywords = load_keywords(s.safety_keywords_path)
    return SafetyService(keywords, block_pii_output=s.safety_block_pii_output)
