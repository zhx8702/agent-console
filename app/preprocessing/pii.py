"""PII detection and placeholder mapping.

Each regex is applied in a fixed order and matches are replaced with
``<PII:type:N>`` placeholders. The returned pii_map maps placeholder -> original
so the postprocessor can restore the original text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Order matters: run more specific patterns (e.g. ID card) before more generic
# ones (phone number, bank card) to avoid a longer ID being chopped up by the
# shorter phone/bank regex.
_PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "secret",
        re.compile(
            r"(?i)\b(?:sk-[a-z0-9_-]{12,}|"
            r"[a-z0-9_-]{24,}\.[a-z0-9_-]{12,}\.[a-z0-9_-]{12,}|"
            r"(?:api[_-]?key|token|secret)\s*[:=]\s*[a-z0-9_./+=-]{8,})\b"
        ),
    ),
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    (
        "id_card",
        re.compile(
            r"[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]"
        ),
    ),
    ("phone", re.compile(r"1[3-9]\d{9}")),
    ("bank_card", re.compile(r"\b\d{13,19}\b")),
    ("ip", re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")),
]


@dataclass
class _Match:
    start: int
    end: int
    pii_type: str
    original: str


def detect_and_mask(text: str) -> tuple[str, dict[str, str]]:
    """Return (masked_text, pii_map).

    pii_map maps ``<PII:type:N>`` -> original.
    """
    if not text:
        return text, {}

    matches: list[_Match] = []
    # Track byte-positions already claimed, so later regexes don't overlap.
    claimed: list[tuple[int, int]] = []

    def _overlaps(s: int, e: int) -> bool:
        for cs, ce in claimed:
            if s < ce and e > cs:
                return True
        return False

    for pii_type, pattern in _PII_PATTERNS:
        for m in pattern.finditer(text):
            s, e = m.start(), m.end()
            if _overlaps(s, e):
                continue
            matches.append(_Match(s, e, pii_type, m.group(0)))
            claimed.append((s, e))

    if not matches:
        return text, {}

    # Sort matches by start for deterministic numbering.
    matches.sort(key=lambda x: x.start)

    # Assign per-type counters in order of appearance.
    counters: dict[str, int] = {}
    placeholders: list[tuple[int, int, str, str]] = []  # start, end, placeholder, original
    pii_map: dict[str, str] = {}
    for m in matches:
        counters[m.pii_type] = counters.get(m.pii_type, 0) + 1
        placeholder = f"<PII:{m.pii_type}:{counters[m.pii_type]}>"
        pii_map[placeholder] = m.original
        placeholders.append((m.start, m.end, placeholder, m.original))

    # Rebuild text by splicing replacements from the end backwards.
    out = text
    for s, e, placeholder, _orig in sorted(placeholders, key=lambda x: x[0], reverse=True):
        out = out[:s] + placeholder + out[e:]
    return out, pii_map
