"""
Small CapabilityEngine implementations for routes that don't need a full
dedicated module: plain LLM chat (RouteType.LLM) and handoff (RouteType.HANDOFF).

These are kept here (inside the orchestrator package) because they are
glue — they wire the shared LLMService / SessionManager into the Capability
Protocol and have no business logic of their own.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote, urlparse, urlsplit, urlunsplit

import httpx

from app.common.canned import HANDOFF_PENDING
from app.common.config import Settings, get_settings
from app.common.context import get_trace_id
from app.common.context_budget import select_recent_turns
from app.common.conversation import (
    is_group_session,
)
from app.common.conversation import (
    render_turn as render_conversation_turn,
)
from app.common.ids import new_trace_id
from app.common.image_preview import (
    fetch_image_once,
    is_http_url,
    preview_url_from_thumbnail,
    wait_for_image,
)
from app.common.intent import IntentArtifact, IntentDecision, IntentDomain, IntentOperation
from app.common.intent_runtime import decision_from_pre, decision_from_session, is_confident, persist_decision
from app.common.logging import get_logger
from app.common.prompting import (
    augment_prompt_with_persona_and_memory,
    chat_system_prompt,
)
from app.common.quote_images import quote_image_source_from_metadata
from app.common.safe_url import configure_http_client
from app.common.types import (
    Attachment,
    CapabilityResult,
    Channel,
    ChatMessage,
    ChatRequest,
    MessageType,
    PreprocessedMessage,
    Role,
    RouteType,
    Session,
    SessionState,
    Turn,
)
from app.common.wxbot_auth import wxbot_sdk_headers
from app.infra.metrics import LLM_IMAGE_ATTACHMENT_EVENTS
from app.llm.service import LLMService
from app.session.manager import SessionManager
from plugins.draw.avatar import extract_avatar_query, resolve_group_avatar_reference

log = get_logger(__name__)
_GROUP_MENTION_PREFIX_RE = re.compile(r"^\s*(?:@\S+[\s\u2005\u00a0]+)+")
_MENTION_NAME_RE = re.compile(r"@([^\s\u2005\u00a0]+)")

_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_WXBOT_CONTAINER_LOCAL_HOSTS = {"127.0.0.1", "localhost", "host.docker.internal"}


@dataclass(frozen=True)
class _ImageAttachmentResult:
    url: str = ""
    reason: str = "no_image"
    data_url: bool = False


@dataclass(frozen=True)
class _MentionedAvatarCandidate:
    wxid: str = ""
    query: str = ""
    found: bool = False
    target_resolved: bool = False
    reason: str = "not_applicable"


def _metadata_record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _image_variant_url_from_records(records: list[dict[str, Any]], variant: str) -> str:
    for record in records:
        image_variants = _metadata_record(record.get("image_variants"))
        variants = _metadata_record(record.get("variants"))
        for payload in (
            image_variants.get(variant),
            variants.get(variant),
            record.get(variant),
        ):
            item = _metadata_record(payload)
            url = str(
                item.get("image_url")
                or item.get("url")
                or item.get("media_url")
                or ""
            ).strip()
            if url:
                return url
    return ""


def _image_variant_url_from_metadata(metadata: dict[str, Any], variant: str) -> str:
    return _image_variant_url_from_records(
        [
            metadata,
            _metadata_record(metadata.get("media")),
            _metadata_record(metadata.get("raw")),
        ],
        variant,
    )


def _image_fetch_failure_reason(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return "http_status"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    message = str(exc).lower()
    if "too large" in message:
        return "too_large"
    if "empty image" in message:
        return "empty"
    return "fetch_failed"


def _wxbot_image_relative_path(image_path: str) -> str:
    normalized = str(image_path or "").strip().replace("\\", "/")
    lower = normalized.lower()
    marker = "/images/"
    marker_index = lower.rfind(marker)
    if marker_index >= 0:
        normalized = normalized[marker_index + len(marker) :]
    elif lower.startswith("images/"):
        normalized = normalized[len("images/") :]
    return normalized.lstrip("/")


def _append_url_query_fragment(url: str, query: str, fragment: str) -> str:
    if query:
        url = f"{url}?{query}"
    if fragment:
        url = f"{url}#{fragment}"
    return url


def _wxbot_image_url_from_locator(
    sdk_url: str,
    locator: str,
    *,
    rewrite_http_to_sdk: bool = False,
) -> str:
    locator = str(locator or "").strip()
    if not locator:
        return ""
    if locator.startswith(("http://", "https://")):
        parsed = urlsplit(locator)
        url_path = unquote(parsed.path).replace("\\", "/")
        marker = "/images/"
        marker_index = url_path.lower().rfind(marker)
        if marker_index < 0:
            sdk_base = str(sdk_url or "").rstrip("/")
            if (
                rewrite_http_to_sdk
                and sdk_base
                and str(parsed.hostname or "").lower() in _WXBOT_CONTAINER_LOCAL_HOSTS
            ):
                return _append_url_query_fragment(
                    f"{sdk_base}/{url_path.lstrip('/')}",
                    parsed.query,
                    parsed.fragment,
                )
            return locator
        relative = _wxbot_image_relative_path(url_path[marker_index + len(marker) :])
        sdk_base = str(sdk_url or "").rstrip("/")
        if rewrite_http_to_sdk and sdk_base:
            return _append_url_query_fragment(
                f"{sdk_base}/images/{quote(relative, safe='/')}",
                parsed.query,
                parsed.fragment,
            )
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                f"/images/{quote(relative, safe='/')}",
                parsed.query,
                parsed.fragment,
            )
        )
    relative = _wxbot_image_relative_path(locator)
    if not relative:
        return ""
    return f"{str(sdk_url or '').rstrip('/')}/images/{quote(relative, safe='/')}"


def _has_unsupported_image_locator_scheme(locator: str) -> bool:
    parsed = urlparse(str(locator or "").strip())
    return bool(parsed.scheme and parsed.scheme not in {"http", "https"} and len(parsed.scheme) > 1)


def _dedupe_nonempty(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            deduped.append(item)
            seen.add(item)
    return deduped


def parse_at_wxids(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                return _dedupe_nonempty([str(item or "").strip() for item in parsed])
        separators = (",", "，", ";", "；")
        items = [text]
        for separator in separators:
            if separator in text:
                items = text.split(separator)
                break
        return _dedupe_nonempty([item.strip().strip('"').strip("'") for item in items])
    if isinstance(value, (list, tuple, set)):
        return _dedupe_nonempty([str(item or "").strip() for item in value])
    return []


def _metadata_text(metadata: dict[str, Any], content: str) -> str:
    return str(
        metadata.get("wxbot_normalized_content")
        or metadata.get("cleaned_content")
        or metadata.get("wxbot_original_content")
        or metadata.get("original_content")
        or content
        or ""
    ).strip()


def _metadata_text_variants(metadata: dict[str, Any], content: str) -> list[str]:
    return _dedupe_nonempty(
        [
            str(metadata.get("wxbot_original_content") or "").strip(),
            str(metadata.get("original_content") or "").strip(),
            str(metadata.get("wxbot_normalized_content") or "").strip(),
            str(metadata.get("cleaned_content") or "").strip(),
            str(content or "").strip(),
        ]
    )


def _avatar_intent_detected(
    text: str,
    *,
    decision: IntentDecision | None = None,
) -> bool:
    _ = text
    return bool(
        decision is not None
        and is_confident(decision)
        and decision.domain is IntentDomain.AVATAR
    )


def _bot_wxid_candidates(metadata: dict[str, Any], session: Session | None) -> set[str]:
    values: list[str] = []
    for key in (
        "bot_wxid",
        "self_wxid",
        "robot_wxid",
        "account_wxid",
        "login_wxid",
        "current_wxid",
    ):
        values.append(str(metadata.get(key) or "").strip())
    raw_values = [
        _metadata_record(metadata.get("raw")),
        _metadata_record(metadata.get("meta")),
        _metadata_record(_metadata_record(metadata.get("raw")).get("meta")),
    ]
    for record in raw_values:
        for key in (
            "self_wxid",
            "bot_wxid",
            "robot_wxid",
            "account_wxid",
            "login_wxid",
        ):
            values.append(str(record.get(key) or "").strip())
    if session is not None:
        values.append(str(session.metadata.get("bot_wxid") or "").strip())
        values.append(str(session.metadata.get("self_wxid") or "").strip())
        values.append(str(session.variables.get("bot_wxid") or "").strip())
        values.append(str(session.variables.get("self_wxid") or "").strip())
    return {item for item in values if item}


def _mentioned_names(text: str) -> list[str]:
    return _dedupe_nonempty([item.strip() for item in _MENTION_NAME_RE.findall(str(text or "")) if item.strip()])


def _best_mentioned_names(texts: list[str]) -> list[str]:
    all_names: list[str] = []
    for text in texts:
        names = _mentioned_names(text)
        if len(names) >= 2:
            return names
        all_names.extend(names)
    return _dedupe_nonempty(all_names)


def _non_sender_wxids(
    at_wxids: list[str],
    metadata: dict[str, Any],
    session: Session,
) -> list[str]:
    excluded = _dedupe_nonempty(
        [
            str(metadata.get("sender_wxid") or "").strip(),
            str(metadata.get("from_wxid") or "").strip(),
            str(metadata.get("user_id") or "").strip(),
            str(session.user_id or "").strip(),
            str(session.session_id or "").strip(),
        ]
    )
    return [wxid for wxid in at_wxids if wxid not in excluded]


def _mentioned_avatar_candidate(
    turn: Turn,
    *,
    session: Session | None,
) -> _MentionedAvatarCandidate:
    if session is None:
        return _MentionedAvatarCandidate(reason="missing_session")
    if session.channel != Channel.WECHAT or not str(session.session_id or "").endswith("@chatroom"):
        return _MentionedAvatarCandidate(reason="not_wechat_group")
    metadata = dict(turn.metadata or {})
    if metadata.get("session_kind") and str(metadata.get("session_kind")) != "group":
        return _MentionedAvatarCandidate(reason="not_group")
    text = _metadata_text(metadata, turn.content)
    decision = decision_from_session(session)
    if not _avatar_intent_detected(text, decision=decision):
        return _MentionedAvatarCandidate(reason="no_avatar_intent")

    text_variants = _metadata_text_variants(metadata, turn.content)
    at_wxids = parse_at_wxids(metadata.get("at_wxids"))
    bot_wxids = _bot_wxid_candidates(metadata, session)
    query = extract_avatar_query(text, decision=decision)
    query_names = _mentioned_names(text)
    ordered_names = _best_mentioned_names(text_variants)
    if query_names:
        query = query_names[-1]
    non_bot_wxids = [wxid for wxid in at_wxids if wxid not in bot_wxids]
    if bot_wxids and len(non_bot_wxids) == 1:
        return _MentionedAvatarCandidate(
            wxid=non_bot_wxids[0],
            query=query,
            found=True,
            target_resolved=True,
            reason="at_wxids",
        )
    if bot_wxids and len(non_bot_wxids) > 1:
        non_sender_wxids = _non_sender_wxids(non_bot_wxids, metadata, session)
        if len(non_sender_wxids) == 1:
            return _MentionedAvatarCandidate(
                wxid=non_sender_wxids[0],
                query=query,
                found=True,
                target_resolved=True,
                reason="non_sender_mention",
            )
        return _MentionedAvatarCandidate(found=True, reason="ambiguous_mentions")
    if not bot_wxids:
        if len(at_wxids) == 1 and not bool(metadata.get("mentioned_me")):
            return _MentionedAvatarCandidate(
                wxid=at_wxids[0],
                query=query,
                found=True,
                target_resolved=True,
                reason="unique_mention",
            )
        if len(at_wxids) > 2:
            return _MentionedAvatarCandidate(found=True, reason="ambiguous_mentions")
        if len(at_wxids) == 2:
            if bool(metadata.get("mentioned_me")) and len(ordered_names) >= 2:
                return _MentionedAvatarCandidate(
                    wxid=at_wxids[1],
                    query=ordered_names[1],
                    found=True,
                    target_resolved=True,
                    reason="ordered_two_mentions",
                )
            non_sender_wxids = _non_sender_wxids(at_wxids, metadata, session)
            if len(non_sender_wxids) == 1:
                return _MentionedAvatarCandidate(
                    wxid=non_sender_wxids[0],
                    query=query,
                    found=True,
                    target_resolved=True,
                    reason="non_sender_mention",
                )
            return _MentionedAvatarCandidate(found=True, reason="ambiguous_mentions")

    if query:
        return _MentionedAvatarCandidate(
            query=query,
            found=True,
            target_resolved=False,
            reason="query_fallback",
        )
    return _MentionedAvatarCandidate(found=True, reason="missing_target")


class LLMCapabilityEngine:
    """Plain LLM chat with provider-native semantic tool selection.

    The route still receives the cheap coarse intent from preprocessing for
    compatibility, but actionable search intent is no longer guessed from
    message keywords.  When search is enabled, the Responses provider exposes
    hosted tools and the model's actual tool choice becomes the structured
    intent evidence.
    """

    name = "llm"

    def __init__(
        self,
        llm_service: LLMService,
        *,
        history_turns: int = 8,
        max_tokens: int = 600,
        temperature: float = 0.4,
        tier: str = "tier-2",
        settings: Settings | None = None,
    ) -> None:
        self._llm = llm_service
        self._history_turns = history_turns
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._tier = tier
        self._settings = settings or get_settings()

    async def answer(
        self,
        pre: PreprocessedMessage,
        session: Session,
        hints: dict[str, Any] | None = None,
    ) -> CapabilityResult:
        request_metadata = dict((hints or {}).get("request_metadata") or {})
        persist_decision(decision_from_pre(pre), pre=pre, session=session)
        query = str(pre.cleaned_text or pre.original_text or "").strip()
        search_required = request_metadata.get("openai_web_search_required") is True
        explicit_search_request = any(
            request_metadata.get(key) is True
            for key in ("openai_web_search", "web_search", "web_search_requested")
        )
        search_override_present = any(
            key in request_metadata
            for key in ("openai_web_search", "web_search", "web_search_requested")
        )
        search_capability_enabled = (
            True
            if search_required
            else (
                bool(
                    request_metadata.get("openai_web_search")
                    or request_metadata.get("web_search")
                    or request_metadata.get("web_search_requested")
                )
                if search_override_present
                else bool(self._settings.openai_web_search_enabled)
            )
        )
        semantic_intent_mode = (
            "required_hosted_tool"
            if search_required
            else ("native_tool_choice" if search_capability_enabled else "disabled")
        )
        prompt_trace: dict[str, Any] = {}
        system_prompt = self._compose_system_prompt(
            session,
            web_search_enabled=search_capability_enabled,
            prompt_trace=prompt_trace,
        )
        messages: list[ChatMessage] = []
        current_trace_id = get_trace_id()
        history_turns = (
            max(self._history_turns, 20)
            if is_group_session(session)
            else self._history_turns
        )
        context_window = select_recent_turns(
            session.turns,
            max_turns=history_turns,
            max_chars=getattr(self._settings, "llm_context_budget_chars", 12_000),
            render=lambda turn: self._render_turn_content(session, turn, current=False),
        )
        recent = context_window.turns
        current_turn = next(
            (
                turn
                for turn in reversed(recent)
                if turn.role == Role.USER and turn.trace_id and turn.trace_id == current_trace_id
            ),
            None,
        )
        for turn in recent:
            if turn.role in (Role.USER, Role.ASSISTANT):
                if turn.role == Role.USER and turn.trace_id and turn.trace_id == current_trace_id:
                    continue
                messages.append(
                    ChatMessage(
                        role=turn.role,
                        content=self._render_turn_content(session, turn, current=False),
                    )
                )
        # The latest user message (preprocessed) is always the final input.
        current_attachments, image_observation = await self._current_turn_attachments(
            current_turn,
            session=session,
        )
        current_content = (
            self._render_turn_content(session, current_turn, current=True)
            if current_turn is not None
            else self._render_current_user_input(session, pre)
        )
        if (
            image_observation.get("source") == "mentioned_avatar"
            and int(image_observation.get("attachment_count") or 0) > 0
        ):
            current_content = f"{current_content}\n已附图：被 @ 成员的微信头像"
        messages.append(
            ChatMessage(
                role=Role.USER,
                content=current_content,
                attachments=current_attachments,
            )
        )

        request_metadata.update(
            {
                "route": "llm",
                "openai_web_search": search_capability_enabled,
                "openai_web_search_required": search_required,
                # Compatibility field: this now means the caller explicitly
                # requested search, while the provider may still choose a
                # search tool semantically when the capability is enabled.
                "web_search_requested": explicit_search_request or search_required,
                "semantic_intent_mode": semantic_intent_mode,
                "prompt_sections": prompt_trace.get("section_names", []),
                "prompt_section_chars": prompt_trace.get("section_chars", {}),
                "context_window": {
                    "source_turns": context_window.source_turns,
                    "selected_turns": len(context_window.turns),
                    "dropped_turns": context_window.dropped_turns,
                    "source_chars": context_window.source_chars,
                    "selected_chars": context_window.selected_chars,
                },
                **(
                    {"image_attachment_observation": image_observation}
                    if image_observation
                    else {}
                ),
            }
        )
        request = ChatRequest(
            tenant_id=session.tenant_id,
            trace_id=get_trace_id() or new_trace_id(),
            model_tier=self._tier,
            messages=messages,
            system=system_prompt,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            cache_system=True,
            metadata=request_metadata,
        )
        response = await self._llm.chat(request)
        response_metadata = getattr(response, "metadata", {}) or {}
        if not isinstance(response_metadata, dict):
            response_metadata = {}
        raw_intent = response_metadata.get("semantic_intent")
        intent = (
            IntentDecision.from_dict(raw_intent)
            if raw_intent is not None
            else IntentDecision(
                operation=IntentOperation.CONVERSE,
                artifact=IntentArtifact.TEXT,
                query=query,
                confidence=0.0,
                needs_tool=False,
            )
        )
        if not intent.query:
            intent = intent.model_copy(update={"query": query})
        semantic_intent = intent.to_minimal_dict()
        web_search_used = intent.source.value in {"web", "x"} and intent.needs_tool
        return CapabilityResult(
            route=RouteType.LLM,
            reply_text=response.content,
            citations=list(response.citations),
            tool_calls=list(response.tool_calls),
            usage=response.usage,
            metadata={
                "model": response.model,
                "latency_ms": response.latency_ms,
                "web_search_requested": explicit_search_request or search_required,
                "web_search_capability_enabled": search_capability_enabled,
                "web_search_used": web_search_used,
                "semantic_intent": semantic_intent,
                "semantic_intent_method": response_metadata.get(
                    "semantic_intent_method", "default_conversation"
                ),
                "prompt_sections": prompt_trace.get("section_names", []),
                "context_window": request_metadata.get("context_window", {}),
                "persona_profile": session.variables.get("persona_profile"),
            },
        )

    def _compose_system_prompt(
        self,
        session: Session,
        *,
        web_search_enabled: bool = False,
        prompt_trace: dict[str, Any] | None = None,
    ) -> str:
        base_system = chat_system_prompt(self._settings.customer_service_prompt_enabled)
        return augment_prompt_with_persona_and_memory(
            base_system,
            session,
            memory_intro=(
                "以下是当前用户的历史记忆，请把它当作个性化上下文使用。"
                "涉及商品规则、售后政策、价格和知识库事实时，以系统规则和知识库为准："
            ),
            web_search_enabled=web_search_enabled,
            prompt_trace=prompt_trace,
        )

    def _render_turn_content(self, session: Session, turn: Turn, *, current: bool) -> str:
        return render_conversation_turn(session, turn, current=current)

    def _render_current_user_input(self, session: Session, pre: PreprocessedMessage) -> str:
        content = str(pre.cleaned_text or pre.original_text or "").strip()
        if not content:
            return ""
        if session.channel != Channel.WECHAT or not str(session.session_id or "").endswith("@chatroom"):
            return content
        speaker = str(session.user_id or "当前发言人").strip()
        return f"当前发言人[{speaker}]：{content}"

    async def _current_turn_attachments(
        self,
        turn: Turn | None,
        *,
        session: Session | None = None,
    ) -> tuple[list[Attachment], dict[str, Any]]:
        if turn is None:
            return [], {}
        metadata = dict(turn.metadata or {})
        preview_url = str(
            _image_variant_url_from_metadata(metadata, "preview")
            or metadata.get("image_preview_url")
            or ""
        ).strip()
        fallback_url = str(
            metadata.get("image_thumbnail_url")
            or _metadata_record(metadata.get("media")).get("image_thumbnail_url")
            or metadata.get("image_url")
            or ""
        ).strip()
        image_url = preview_url or preview_url_from_thumbnail(fallback_url) or fallback_url
        image_path = str(metadata.get("image_path") or "").strip()
        current_image_found = bool(image_url or image_path)

        quote_image = quote_image_source_from_metadata(
            metadata,
            session=session,
        )
        quote_image_url = quote_image.image_url
        quote_image_path = quote_image.image_path
        quote_image_found = bool(quote_image_url or quote_image_path)

        mentioned_avatar = _MentionedAvatarCandidate(reason="not_checked")
        mentioned_avatar_url = ""
        mentioned_avatar_path = ""
        if not current_image_found and not quote_image_found and session is not None:
            mentioned_avatar = _mentioned_avatar_candidate(turn, session=session)
            if mentioned_avatar.found and (mentioned_avatar.wxid or mentioned_avatar.query):
                avatar_ref = await resolve_group_avatar_reference(
                    self._settings,
                    session_id=session.session_id,
                    wxid=mentioned_avatar.wxid,
                    query=mentioned_avatar.query,
                    trace_id=turn.trace_id or get_trace_id() or "",
                )
                if avatar_ref is None and mentioned_avatar.wxid and mentioned_avatar.query:
                    avatar_ref = await resolve_group_avatar_reference(
                        self._settings,
                        session_id=session.session_id,
                        query=mentioned_avatar.query,
                        trace_id=turn.trace_id or get_trace_id() or "",
                    )
                if avatar_ref is not None:
                    mentioned_avatar_url = avatar_ref.avatar_url
                    mentioned_avatar_path = avatar_ref.image_path
                    mentioned_avatar = _MentionedAvatarCandidate(
                        wxid=avatar_ref.wxid or mentioned_avatar.wxid,
                        query=avatar_ref.query or mentioned_avatar.query,
                        found=True,
                        target_resolved=True,
                        reason=mentioned_avatar.reason,
                    )
                elif mentioned_avatar.reason not in {"ambiguous_mentions", "missing_target"}:
                    mentioned_avatar = _MentionedAvatarCandidate(
                        wxid=mentioned_avatar.wxid,
                        query=mentioned_avatar.query,
                        found=True,
                        target_resolved=mentioned_avatar.target_resolved,
                        reason="avatar_not_found",
                    )
        mentioned_avatar_found = bool(mentioned_avatar_url or mentioned_avatar_path)

        source = (
            "current"
            if current_image_found
            else "quote"
            if quote_image_found
            else "mentioned_avatar"
            if mentioned_avatar_found or mentioned_avatar.found
            else "none"
        )
        selected_candidates = (
            _dedupe_nonempty([image_url, fallback_url, image_path])
            if current_image_found
            else _dedupe_nonempty([quote_image_url, quote_image_path])
            if quote_image_found
            else _dedupe_nonempty([mentioned_avatar_url, mentioned_avatar_path])
        )

        result = _ImageAttachmentResult(
            reason=(
                mentioned_avatar.reason
                if mentioned_avatar.found and not mentioned_avatar_found
                else "no_image"
            )
        )
        if source != "none" and selected_candidates:
            result = await self._resolve_image_candidates_for_llm(
                selected_candidates,
                rewrite_http_to_sdk=source == "quote",
            )
        attachments = (
            [Attachment(type=MessageType.IMAGE, url=result.url)]
            if result.url
            else []
        )
        attachment_count = len(attachments)
        data_url_count = sum(1 for attachment in attachments if str(attachment.url or "").startswith("data:image/"))
        observation = {
            "source": source,
            "current_image_found": current_image_found,
            "quote_image_found": quote_image_found,
            "mentioned_avatar_found": mentioned_avatar_found,
            "target_resolved": mentioned_avatar.target_resolved,
            "attachment_count": attachment_count,
            "data_url_count": data_url_count,
            "result": "attached" if attachments else "skipped",
            "reason": result.reason,
        }
        self._record_image_attachment_observation(observation)
        return attachments, observation

    async def _image_url_for_llm(self, image_url: str, *, fallback_url: str = "") -> str:
        return (await self._resolve_image_candidates_for_llm([image_url, fallback_url])).url

    async def _resolve_image_candidates_for_llm(
        self,
        candidates: list[str],
        *,
        rewrite_http_to_sdk: bool = False,
    ) -> _ImageAttachmentResult:
        locators = _dedupe_nonempty(candidates)
        if not locators:
            return _ImageAttachmentResult(reason="missing_url")
        unsupported_scheme_seen = any(_has_unsupported_image_locator_scheme(locator) for locator in locators)
        last_result = _ImageAttachmentResult(
            reason="unsupported_scheme" if unsupported_scheme_seen else "missing_url"
        )
        for index, locator in enumerate(locators):
            result = await self._resolve_image_url_for_llm(
                locator,
                fallback_url=locators[index + 1] if index + 1 < len(locators) else "",
                rewrite_http_to_sdk=rewrite_http_to_sdk,
            )
            if result.url:
                if last_result.reason not in {"missing_url", "unsupported_scheme"}:
                    return _ImageAttachmentResult(
                        url=result.url,
                        reason=f"fallback_{result.reason}",
                        data_url=result.data_url,
                    )
                return result
            if result.reason != "missing_url":
                last_result = result
        return last_result

    async def _resolve_image_url_for_llm(
        self,
        image_url: str,
        *,
        fallback_url: str = "",
        rewrite_http_to_sdk: bool = False,
    ) -> _ImageAttachmentResult:
        image_url = str(image_url or "").strip()
        fallback_url = str(fallback_url or "").strip()
        if not image_url:
            return _ImageAttachmentResult(reason="missing_url")
        if image_url.startswith("data:image/"):
            return _ImageAttachmentResult(
                url=image_url,
                reason="data_url",
                data_url=True,
            )
        parsed = urlparse(image_url)
        if parsed.scheme not in {"http", "https"}:
            if _has_unsupported_image_locator_scheme(image_url):
                return _ImageAttachmentResult(reason="unsupported_scheme")
            resolved_url = _wxbot_image_url_from_locator(
                self._settings.wxbot_sdk_url,
                image_url,
                rewrite_http_to_sdk=rewrite_http_to_sdk,
            )
            if not resolved_url or not is_http_url(resolved_url):
                return _ImageAttachmentResult(reason="non_http_unresolvable")
            resolved_fallback_url = (
                _wxbot_image_url_from_locator(
                    self._settings.wxbot_sdk_url,
                    fallback_url,
                    rewrite_http_to_sdk=rewrite_http_to_sdk,
                )
                if fallback_url and not is_http_url(fallback_url)
                else fallback_url
            )
            if resolved_fallback_url == resolved_url:
                resolved_fallback_url = ""
            return await self._resolve_image_url_for_llm(
                resolved_url,
                fallback_url=resolved_fallback_url,
                rewrite_http_to_sdk=rewrite_http_to_sdk,
            )

        resolved_url = _wxbot_image_url_from_locator(
            self._settings.wxbot_sdk_url,
            image_url,
            rewrite_http_to_sdk=rewrite_http_to_sdk,
        )
        if resolved_url and resolved_url != image_url and is_http_url(resolved_url):
            resolved_fallback_url = (
                _wxbot_image_url_from_locator(
                    self._settings.wxbot_sdk_url,
                    fallback_url,
                    rewrite_http_to_sdk=rewrite_http_to_sdk,
                )
                if fallback_url
                else ""
            )
            if resolved_fallback_url == resolved_url:
                resolved_fallback_url = ""
            return await self._resolve_image_url_for_llm(
                resolved_url,
                fallback_url=resolved_fallback_url,
                rewrite_http_to_sdk=rewrite_http_to_sdk,
            )

        wait_seconds = float(getattr(self._settings, "wxbot_preview_wait_seconds", 8.0) or 0.0)
        poll_interval = float(
            getattr(self._settings, "wxbot_preview_poll_interval_seconds", 0.7) or 0.7
        )
        try:
            async with httpx.AsyncClient(
                timeout=10,
                # Redirects are owned by safe_get so every hop is re-pinned.
                follow_redirects=False,
                trust_env=False,
            ) as client:
                configure_http_client(
                    client,
                    allowed_private_origins=[self._settings.wxbot_sdk_url],
                    origin_headers={
                        self._settings.wxbot_sdk_url: wxbot_sdk_headers(
                            self._settings
                        )
                    },
                )
                if fallback_url and image_url != fallback_url and is_http_url(image_url):
                    fetched = await wait_for_image(
                        client,
                        image_url,
                        wait_seconds=wait_seconds,
                        poll_interval_seconds=poll_interval,
                        max_bytes=_MAX_IMAGE_BYTES,
                    )
                else:
                    fetched = await fetch_image_once(
                        client,
                        image_url,
                        max_bytes=_MAX_IMAGE_BYTES,
                    )
        except Exception as exc:
            reason = _image_fetch_failure_reason(exc)
            log.warning(
                "llm.image_attachment_fetch_failed",
                error_class=exc.__class__.__name__,
                reason=reason,
                fallback_attempted=bool(fallback_url and fallback_url != image_url),
            )
            if fallback_url and fallback_url != image_url:
                fallback_result = await self._resolve_image_url_for_llm(
                    fallback_url,
                    rewrite_http_to_sdk=rewrite_http_to_sdk,
                )
                if fallback_result.url:
                    return _ImageAttachmentResult(
                        url=fallback_result.url,
                        reason=f"fallback_{fallback_result.reason}",
                        data_url=fallback_result.data_url,
                    )
            return _ImageAttachmentResult(
                url="" if is_http_url(image_url) else image_url,
                reason=reason,
            )

        if not fetched.content or len(fetched.content) > _MAX_IMAGE_BYTES:
            log.warning(
                "llm.image_attachment_skipped",
                reason="too_large" if fetched.content else "empty",
            )
            return _ImageAttachmentResult(
                reason="too_large" if fetched.content else "empty",
            )
        encoded = base64.b64encode(fetched.content).decode("ascii")
        return _ImageAttachmentResult(
            url=f"data:{fetched.media_type};base64,{encoded}",
            reason="converted_data_url",
            data_url=True,
        )

    def _record_image_attachment_observation(self, observation: dict[str, Any]) -> None:
        source = str(observation.get("source") or "none")
        result = str(observation.get("result") or "skipped")
        reason = str(observation.get("reason") or "unknown")
        LLM_IMAGE_ATTACHMENT_EVENTS.labels(
            source=source,
            image_kind="wechat",
            result=result,
            reason=reason,
        ).inc()
        log.info(
            "llm.image_attachment_observed",
            source=source,
            current_image_found=bool(observation.get("current_image_found")),
            quote_image_found=bool(observation.get("quote_image_found")),
            mentioned_avatar_found=bool(observation.get("mentioned_avatar_found")),
            target_resolved=bool(observation.get("target_resolved")),
            attachment_count=int(observation.get("attachment_count") or 0),
            data_url_count=int(observation.get("data_url_count") or 0),
            result=result,
            reason=reason,
        )


class HandoffCapabilityEngine:
    """Escalate the session to a human. Marks state=ESCALATED and emits a canned acknowledgement."""

    name = "handoff"

    def __init__(self, session_manager: SessionManager) -> None:
        self._sm = session_manager

    async def answer(
        self,
        pre: PreprocessedMessage,
        session: Session,
        hints: dict[str, Any] | None = None,
    ) -> CapabilityResult:
        if session.state != SessionState.ESCALATED:
            try:
                await self._sm.set_state(session, SessionState.ESCALATED)
            except Exception as exc:
                log.warning("handoff.state_transition_failed", error=str(exc))
        return CapabilityResult(
            route=RouteType.HANDOFF,
            reply_text=HANDOFF_PENDING,
            metadata={"handoff": True},
        )
