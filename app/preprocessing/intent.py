"""Rule-based coarse intent classifier for the MVP."""
from __future__ import annotations

from app.common.types import IntentCoarse

_HANDOFF_WORDS = ("转人工", "人工客服", "真人")
_COMPLAINT_WORDS = ("投诉", "差评", "举报")
_FAQ_PREFIXES = ("怎么", "如何", "为什么", "什么是")
_BUSINESS_WORDS = ("订单", "发货", "退款", "物流", "账单")
_CHITCHAT_WORDS = ("你好", "谢谢", "再见", "哈喽", "拜拜")


def classify_intent(text: str) -> IntentCoarse:
    if not text:
        return IntentCoarse.UNKNOWN

    t = text.strip()
    lowered = t.lower()

    if any(w in t for w in _HANDOFF_WORDS):
        return IntentCoarse.HANDOFF_REQUEST
    if any(w in t for w in _COMPLAINT_WORDS):
        return IntentCoarse.COMPLAINT
    # FAQ prefix check happens before business/chitchat so "怎么退款" lands as FAQ.
    if any(t.startswith(p) for p in _FAQ_PREFIXES) or any(
        lowered.startswith(p.lower()) for p in ("how ", "why ", "what ")
    ):
        return IntentCoarse.FAQ
    if any(w in t for w in _BUSINESS_WORDS):
        return IntentCoarse.BUSINESS
    if any(w in t for w in _CHITCHAT_WORDS):
        return IntentCoarse.CHITCHAT
    return IntentCoarse.UNKNOWN
