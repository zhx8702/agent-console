"""Canned reply texts used for safety blocks and degradation fallbacks."""
from __future__ import annotations

SAFETY_BLOCK = "抱歉，您的消息包含不合适的内容，我无法回应。"
DEGRADATION_BUSY = "系统正繁忙，请稍后再试，或输入「转人工」联系客服。"
SYSTEM_BUSY = "系统服务暂时不可用，请稍后再试。"
MODEL_BUSY = "模型服务暂时不可用，请稍后再试。"
COMMAND_BUSY = "命令服务暂时不可用，请稍后再试。"
ACCOUNT_BUSY = "账户或积分服务暂时不可用，请稍后再试。"
DRAW_BUSY = "画图服务暂时不可用，请稍后再试。"
HANDOFF_PENDING = "已为您转接人工客服，请稍候。"
NO_ANSWER = "抱歉，我暂时没有相关答案。您可以换一种问法，或输入「转人工」联系客服。"


def degradation_text(reason: str = "") -> str:
    """Return a concise user-facing degradation reply for a known failure class."""
    normalized = str(reason or "").strip().lower()
    if any(token in normalized for token in ("safety", "moderation")):
        return SAFETY_BLOCK
    if any(token in normalized for token in ("draw", "redraw", "image")):
        return DRAW_BUSY
    if any(token in normalized for token in ("credit", "billing", "account", "balance")):
        return ACCOUNT_BUSY
    if any(token in normalized for token in ("command", "plugin.commands")):
        return COMMAND_BUSY
    if any(
        token in normalized
        for token in ("llm", "model", "capability_failed:llm", "no_engine:llm")
    ):
        return MODEL_BUSY
    if normalized:
        return SYSTEM_BUSY
    return DEGRADATION_BUSY
