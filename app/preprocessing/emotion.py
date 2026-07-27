"""Lexicon-based emotion heuristic (positive / neutral / negative)."""
from __future__ import annotations

from app.common.types import EmotionLabel

_POSITIVE: tuple[str, ...] = ("谢谢", "很好", "满意", "喜欢", "不错", "棒")
_NEGATIVE: tuple[str, ...] = (
    "差",
    "烂",
    "投诉",
    "气死",
    "骗",
    "坑",
    "失望",
    "太慢",
    "垃圾",
)


def score_emotion(text: str) -> EmotionLabel:
    if not text:
        return EmotionLabel.NEUTRAL

    score = 0
    for w in _POSITIVE:
        if w in text:
            score += text.count(w)
    for w in _NEGATIVE:
        if w in text:
            score -= text.count(w)

    if score > 0:
        return EmotionLabel.POSITIVE
    if score < 0:
        return EmotionLabel.NEGATIVE
    return EmotionLabel.NEUTRAL
