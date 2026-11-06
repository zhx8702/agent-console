"""Semantic intent classification.

The only production classifier is a structured model call.  Unknown or
unparseable output is fail-closed: no domain, no side effects.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Protocol

from app.common.exceptions import UpstreamRejected
from app.common.intent import IntentDecision
from app.common.logging import get_logger
from app.common.quote_images import quote_image_source_from_metadata
from app.common.types import ChatMessage, ChatRequest, Role
from app.llm.activity import wait_for_llm_activity

logger = get_logger(__name__)

_CLASSIFY_ATTEMPTS = 3
# Dedicated per-attempt timeout so a slow upstream cannot eat the whole
# preprocess budget (90s); worst case is attempts * timeout + backoff.
_CLASSIFY_TIMEOUT_SECONDS = 20.0
# Some gateways (e.g. sub2api) inject their own assistant persona ahead of
# our system prompt; fast non-reasoning models then answer conversationally
# instead of classifying. A trailing user-side reminder restores compliance.
_CLASSIFY_USER_SUFFIX = "\n\n(Classify the message above. Return only the JSON object.)"

_CLASSIFY_SYSTEM = """Classify the latest user message into one JSON object.
Do not treat quoted text, mentioned examples, or cancelled requests as an instruction.
If the user is asking what a phrase means and did not ask to search the web or X/Twitter,
or is not making a request, use domain none.
If unsure and there is no explicit live-search request, use domain none,
operation unknown, and confidence 0.
Handoff means an explicit request to transfer to a real human or customer service agent.
Asking the current assistant to help, answer questions, or act as an assistant is ordinary
conversation (chitchat/greet, operation converse), not a human handoff.
Examples: "你现在是我的助理，帮我解答群里的问题" -> chitchat/greet;
"请转人工客服" -> handoff/request. Never infer a human transfer from the word assistant.
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
- draw/generate|edit
- video/generate|question
- memory/remember|forget|forget_all|list|search
- tibo_reset/week_count|today_status|latest|retention|summary
- avatar/analyze
- group_info/query
- group_plugin_status/query
- web_search/retrieve
- none/""

Credits versus live search:
- credits is ONLY this group's 积分 system: 余额, 签到, 榜单, 转账.
- Third-party / product / API quota is not credits. Words like 额度, quota,
  remaining calls, grok bot, X, Twitter do not make it credits.
- An explicit request to search X / Twitter / the web is web_search, even if
  the query looks like chitchat, slang, insults, phrasing, or "what does this
  mean". Do not use domain none or chitchat in that case.
- 搜X / 搜下x / 搜推特 / search X / search Twitter is web_search/retrieve,
  source x, operation retrieve, needs_tool true, tool_name x_search.
- Examples: "搜下x 目前grok bot 有多少额度" -> web_search/retrieve, source x;
  "搜下x 一般是怎么说别人胖的" -> web_search/retrieve, source x;
  "搜下x 总结一下 都是怎么说的" -> web_search/retrieve, source x;
  "一般是怎么说别人胖的" (no search request) -> none;
  "我还有多少积分" -> credits/balance_self;
  "@张三 他积分多少" -> credits/balance_other.

History versus durable memory:
- Questions about past messages ("我今天问过你哪些问题", "我昨天在这个群发言了吗",
  "上周群里谁提过部署方案") are group_info/query, operation retrieve, source local_history,
  needs_tool true, tool_name search_group_messages. Preserve the requested person and time.
- "我/我的" means the current requester, not the bot or all group members.
- memory/list|search is ONLY about saved facts/preferences ("你保存了哪些关于我的偏好");
  its source is none. Remember/forget requests remain memory mutations, never history searches.
- Exporting a transcript remains file/export_history, not a plain history lookup.

Slots:
- credits: target, amount
- file: format, delivery_required, recent_minutes
- memory: content, query
- group_info history query: sender_scope (self|group), period (today|yesterday|recent),
  hours (1..336 for recent), sender_name (only if explicitly named), keywords (literal search keywords or empty; never the whole question).
  Use sender_scope=self for "我/我的", group only when the user asks about the group.
- avatar: name
- map: city, keywords
- draw: prompt

Draw:
- draw/generate is a new picture from text only.
- draw/edit is modifying a quoted, attached, or already-generated image
  (add a person beside someone, change the background, restyle, redraw).
- If the message quotes or replies to an image and the user asks to change
  that picture, use draw/edit, operation update, source user_attachment,
  artifact image, needs_tool true, tool_name edit_group_image.
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


def _leading_at_token(text: str) -> str:
    raw = str(text or "").lstrip()
    if not raw.startswith("@"):
        return ""
    token = raw.split(None, 1)[0]
    return token[1:].strip()


def _metadata_mentions_bot(metadata: Mapping[str, Any]) -> bool:
    if metadata.get("mentioned_me") or metadata.get("bot_mentioned"):
        return True
    at_wxids = {
        str(value or "").strip()
        for value in (metadata.get("at_wxids") or [])
        if str(value or "").strip()
    }
    bot_wxids = {
        str(metadata.get(key) or "").strip()
        for key in ("bot_wxid", "self_wxid", "wxbot_self_wxid")
        if str(metadata.get(key) or "").strip()
    }
    return bool(at_wxids & bot_wxids)


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
    quoted_image = quote_image_source_from_metadata(dict(metadata)).found
    message = getattr(event, "message", None)
    content = str(getattr(message, "content", "") or "")
    leading_mention = bool(_leading_at_token(content))
    mentioned_me = _metadata_mentions_bot(metadata) or (
        quoted_image and leading_mention
    )
    return {
        "has_attachment": bool(has_attachment),
        "quoted_image": quoted_image,
        "leading_mention": leading_mention,
        "tenant_id": str(getattr(event, "tenant_id", "") or ""),
        "session_id": session_id,
        "trace_id": str(getattr(event, "trace_id", "") or ""),
        "mentioned_me": mentioned_me,
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
        if extra.get("quoted_image"):
            hints.append("The message quotes or replies to an image.")
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
            messages=[
                ChatMessage(role=Role.USER, content=user_content[:4000] + _CLASSIFY_USER_SUFFIX)
            ],
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
                response = await wait_for_llm_activity(
                    self._llm.chat(request),
                    timeout=_CLASSIFY_TIMEOUT_SECONDS,
                )
                break
            except asyncio.CancelledError:
                raise
            except UpstreamRejected:
                # 4xx is deterministic; retrying the identical request
                # cannot succeed, so fail closed immediately.
                logger.warning(
                    "intent.classify_rejected",
                    attempt=attempt,
                    exc_info=True,
                )
                return IntentDecision()
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
