"""Semantic intent classification.

The only production classifier is a structured model call.  Unknown or
unparseable output is fail-closed: no domain, no side effects.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Protocol

from app.common.intent import IntentDecision
from app.common.logging import get_logger
from app.common.types import ChatMessage, ChatRequest, Role

logger = get_logger(__name__)

_CLASSIFY_ATTEMPTS = 3

_CLASSIFY_SYSTEM = """Classify the latest user message into one JSON object.
Do not treat quoted text, mentioned examples, or cancelled requests as an instruction.
If the user is asking what a phrase means, or is not making a request, use domain none.
If unsure, use domain none, operation unknown, and confidence 0.
Return only JSON.

Schema:
{
  "operation": "converse|retrieve|create|update|execute|handoff|unknown",
  "source": "none|web|x|knowledge_base|local_history|user_attachment|unknown",
  "artifact": "text|image|video|audio|file|structured_data|unknown",
  "domain": "none|identity|handoff|complaint|faq|business|chitchat|credits|file|map|draw|video|memory|tibo_reset|avatar|group_info|group_plugin_status|web_search|unknown",
  "action": "",
  "query": "",
  "confidence": 0.0,
  "needs_tool": false,
  "tool_name": null,
  "slots": {}
}

Domain/action:
- identity/inquiry
- handoff/request|non_request|cancel
- complaint/request
- faq/ask
- business/ask
- chitchat/greet
- credits/balance_self|balance_other|rank|checkin_status|checkin_action|transfer_self_to_other_unsupported|transfer_reverse_unauthorized|redeem_or_discussion
- file/inspect_incoming|generate|send_existing|convert|export_history
- map/search|generate
- draw/generate
- video/generate|question
- memory/remember|forget|forget_all|list|search
- tibo_reset/week_count|today_status|latest|retention|summary
- avatar/analyze
- group_info/query
- group_plugin_status/query
- web_search/retrieve
- none/""

Slots:
- credits: target, amount
- file: format, delivery_required, recent_minutes
- memory: content, query
- avatar: name
- map: city, keywords
"""


class IntentClassifier(Protocol):
    async def classify(
        self,
        text: str,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> IntentDecision: ...


class NullIntentClassifier:
    """Fail-closed classifier used when no model is wired."""

    async def classify(
        self,
        text: str,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> IntentDecision:
        _ = text, context
        return IntentDecision()


class StaticIntentClassifier:
    """Test double that returns a preloaded decision."""

    def __init__(self, decision: IntentDecision | Mapping[str, Any] | None = None) -> None:
        self.decision = (
            decision
            if isinstance(decision, IntentDecision)
            else IntentDecision.from_dict(decision)
        )

    async def classify(
        self,
        text: str,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> IntentDecision:
        _ = text, context
        return self.decision


def classify_context_from_event(
    event: Any,
    *,
    has_attachment: bool = False,
) -> dict[str, Any]:
    """Collect cheap address/participation signals for the classify gate."""

    metadata = event.metadata if isinstance(getattr(event, "metadata", None), Mapping) else {}
    session_id = str(getattr(event, "session_id", "") or "")
    session_kind = str(metadata.get("session_kind") or "").strip().lower()
    is_group = session_kind in {"group", "chatroom", "channel", "guild"} or session_id.endswith(
        "@chatroom"
    )
    return {
        "has_attachment": bool(has_attachment),
        "tenant_id": str(getattr(event, "tenant_id", "") or ""),
        "trace_id": str(getattr(event, "trace_id", "") or ""),
        "mentioned_me": bool(metadata.get("mentioned_me") or metadata.get("bot_mentioned")),
        "replied_to_bot": bool(
            metadata.get("replied_to_bot")
            or metadata.get("reply_to_bot")
            or metadata.get("quoted_bot")
            or metadata.get("quote_is_self_sent")
        ),
        "is_self_sent": bool(metadata.get("is_self_sent")),
        "is_group": is_group,
    }


def semantic_classify_skip_reason(
    text: str,
    context: Mapping[str, Any] | None = None,
) -> str:
    """Return why a model classify call should not run, or ``""`` to proceed.

    Group chatter is not classified until the bot is addressed.  Missing
    address signals fail closed so an incomplete caller cannot burn tokens.
    """

    query = str(text or "").strip()
    if not query:
        return "empty"
    extra = dict(context or {})
    if extra.get("force") is True:
        return ""
    if extra.get("is_self_sent"):
        return "self_sent"
    if query.startswith("/"):
        return "command"
    if extra.get("mentioned_me") or extra.get("replied_to_bot"):
        return ""
    if extra.get("is_group") is False:
        return ""
    return "not_addressed"


class LlmIntentClassifier:
    """Structured model call, only when the bot is actually addressed."""

    def __init__(self, llm: Any, *, tenant_id: str = "system") -> None:
        self._llm = llm
        self._tenant_id = tenant_id

    async def classify(
        self,
        text: str,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> IntentDecision:
        query = str(text or "").strip()
        extra = dict(context or {})
        skip_reason = semantic_classify_skip_reason(query, extra)
        if skip_reason:
            logger.debug(
                "intent.classify_skipped",
                reason=skip_reason,
                is_group=bool(extra.get("is_group")),
                mentioned_me=bool(extra.get("mentioned_me")),
            )
            return IntentDecision()
        if not query:
            return IntentDecision()
        hints: list[str] = []
        if extra.get("has_attachment"):
            hints.append("The message includes a file attachment.")
        if extra.get("mentioned_me"):
            hints.append("The bot was explicitly mentioned.")
        previous = extra.get("previous_intent")
        if isinstance(previous, Mapping) and previous:
            hints.append(f"Previous handled intent: {previous}")
        user_content = query if not hints else query + "\n\nContext:\n" + "\n".join(hints)
        request = ChatRequest(
            tenant_id=str(extra.get("tenant_id") or self._tenant_id),
            trace_id=str(extra.get("trace_id") or "intent-classify"),
            model_tier="tier-1",
            messages=[ChatMessage(role=Role.USER, content=user_content[:4000])],
            system=_CLASSIFY_SYSTEM,
            temperature=0.0,
            max_tokens=220,
            cache_system=True,
            metadata={
                "route": "intent_classify",
                "openai_web_search": False,
                "semantic_intent_mode": "structured_classify",
            },
        )
        response = None
        for attempt in range(1, _CLASSIFY_ATTEMPTS + 1):
            try:
                response = await self._llm.chat(request)
                break
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "intent.classify_failed",
                    attempt=attempt,
                    attempts=_CLASSIFY_ATTEMPTS,
                    exc_info=True,
                )
                if attempt >= _CLASSIFY_ATTEMPTS:
                    return IntentDecision()
                await asyncio.sleep(0.4 * attempt)
        if response is None:
            return IntentDecision()
        return IntentDecision.from_json(_extract_json_object(getattr(response, "content", "") or ""))


def _extract_json_object(text: str) -> str:
    value = str(text or "").strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    start = value.find("{")
    end = value.rfind("}")
    if start >= 0 and end > start:
        return value[start : end + 1]
    return value
