"""Simple heuristic language detector.

Counts Han-range characters vs ASCII letters to infer language.
"""
from __future__ import annotations

_HAN_RANGES: tuple[tuple[int, int], ...] = (
    (0x4E00, 0x9FFF),   # CJK Unified Ideographs
    (0x3400, 0x4DBF),   # CJK Ext A
    (0x20000, 0x2A6DF), # CJK Ext B
)


def _is_han(ch: str) -> bool:
    cp = ord(ch)
    for lo, hi in _HAN_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def detect_language(text: str) -> str:
    """Return one of 'zh', 'en', 'mixed' based on simple char ratios.

    Rules:
      - If >30% of alpha-ish chars are Han -> 'zh'
      - Else if there are ASCII letters and no Han -> 'en'
      - Else 'mixed' (fallback when text is empty or ambiguous)
    """
    if not text:
        return "zh"

    han = 0
    ascii_letters = 0
    for ch in text:
        if _is_han(ch):
            han += 1
        elif ("a" <= ch <= "z") or ("A" <= ch <= "Z"):
            ascii_letters += 1

    total = han + ascii_letters
    if total == 0:
        return "zh"

    han_ratio = han / total
    if han_ratio > 0.30:
        return "zh"
    if ascii_letters > 0 and han == 0:
        return "en"
    if ascii_letters > 0 and han > 0:
        return "mixed"
    return "en"
