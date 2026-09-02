"""
OpenAI-compatible chat + embeddings provider using the official async SDK.

Supports:
- OpenAI native endpoints
- OpenAI-compatible base URLs configured via ``OPENAI_BASE_URL``
- old wx-bot style root URL + ``responses`` compatibility
- automatic fallback from ``responses`` to ``chat.completions``
- embeddings via the standard embeddings endpoint
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.common.config import Settings
from app.common.exceptions import CSError, UpstreamRejected, UpstreamUnavailable
from app.common.intent import (
    IntentArtifact,
    IntentDecision,
    IntentDomain,
    IntentOperation,
    IntentSource,
)
from app.common.logging import get_logger
from app.common.types import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatUsage,
    Citation,
    MessageType,
    Role,
    ToolCall,
)
from app.infra.metrics import LLM_API_ATTEMPTS
from app.llm.activity import record_llm_stream_event
from app.llm.base import EmbedRequest, EmbedResponse

logger = get_logger(__name__)

_TIER_ATTR = {
    "tier-1": "llm_model_tier1",
    "tier-2": "llm_model_tier2",
    "tier-3": "llm_model_tier3",
}

_SUPPORTED_API_MODES = {"chat", "responses"}
_MAX_WEB_SEARCH_SOURCE_CITATIONS = 20
_SUPPORTED_WEB_SEARCH_TOOLS = {"web_search", "web_search_preview", "x_search"}
_RESPONSES_FIRST_EVENT_TIMEOUT_SECONDS = 45.0
_RESPONSES_IDLE_TIMEOUT_SECONDS = 120.0
_RESPONSES_TOTAL_TIMEOUT_SECONDS = 3600.0


class _ResponsesStreamInterrupted(UpstreamUnavailable):
    """A Responses stream failed after emitting data and must not be replayed."""


def _fallback_label(fallback_from: str | None) -> str:
    return f"from_{fallback_from}" if fallback_from else "none"


def _error_class(exc: BaseException | None) -> str:
    if exc is None:
        return "none"
    cause = exc.__cause__ if exc.__cause__ is not None else exc
    return cause.__class__.__name__


def _record_api_attempt(
    *,
    api_mode: str,
    result: str,
    fallback_from: str | None = None,
    exc: BaseException | None = None,
) -> None:
    LLM_API_ATTEMPTS.labels(
        provider="openai",
        api_mode=api_mode,
        fallback=_fallback_label(fallback_from),
        result=result,
        error_class=_error_class(exc),
    ).inc()


def _serialise_args(args: Any) -> dict[str, Any]:
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            val = json.loads(args)
            return val if isinstance(val, dict) else {"value": val}
        except Exception:
            return {"value": args}
    return {"value": args}


def _image_attachment_urls(msg: ChatMessage) -> list[str]:
    urls: list[str] = []
    for attachment in msg.attachments:
        if attachment.type != MessageType.IMAGE:
            continue
        url = str(attachment.url or attachment.content or "").strip()
        if url:
            urls.append(url)
    return urls


def _chat_content(msg: ChatMessage) -> str | list[dict[str, Any]]:
    image_urls = _image_attachment_urls(msg)
    text = msg.content or ""
    if not image_urls:
        return text
    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append({"type": "text", "text": text})
    for url in image_urls:
        blocks.append({"type": "image_url", "image_url": {"url": url}})
    return blocks


def _responses_content(msg: ChatMessage) -> str | list[dict[str, Any]]:
    image_urls = _image_attachment_urls(msg)
    text = msg.content or ""
    if not image_urls:
        return text
    blocks: list[dict[str, Any]] = []
    if text:
        blocks.append({"type": "input_text", "text": text})
    for url in image_urls:
        blocks.append({"type": "input_image", "image_url": url})
    return blocks


def _convert_messages(
    messages: list[ChatMessage],
    system: str | None = None,
    *,
    include_compat_call_id: bool = True,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for msg in messages:
        if msg.role == Role.SYSTEM:
            out.append({"role": "system", "content": msg.content or ""})
            continue
        if msg.role == Role.TOOL:
            payload: dict[str, Any] = {
                "role": "tool",
                "tool_call_id": msg.tool_call_id or "",
                "content": msg.content or "",
            }
            # Some OpenAI-compatible proxies internally bridge tool
            # continuation through the Responses format and require
            # `call_id` even on chat-style HTTP requests. xAI follows the
            # standard Chat Completions shape and rejects this extra field.
            if include_compat_call_id:
                payload["call_id"] = msg.tool_call_id or ""
            out.append(payload)
            continue
        if msg.role == Role.ASSISTANT:
            payload: dict[str, Any] = {
                "role": "assistant",
                "content": msg.content or "",
            }
            if msg.tool_calls:
                payload["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments or {}, ensure_ascii=False),
                        },
                    }
                    for tc in msg.tool_calls
                ]
            out.append(payload)
            continue
        out.append({"role": "user", "content": _chat_content(msg)})
    return out


def _convert_messages_for_responses(
    messages: list[ChatMessage],
    system: str | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for msg in messages:
        if msg.role == Role.SYSTEM:
            out.append({"role": "system", "content": msg.content or ""})
            continue
        if msg.role == Role.TOOL:
            out.append(
                {
                    "type": "function_call_output",
                    "call_id": msg.tool_call_id or "",
                    "output": msg.content or "",
                }
            )
            continue
        if msg.role == Role.ASSISTANT:
            if msg.content:
                out.append({"role": "assistant", "content": msg.content or ""})
            for tc in msg.tool_calls:
                out.append(
                    {
                        "type": "function_call",
                        "call_id": tc.id,
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments or {}, ensure_ascii=False),
                    }
                )
            continue
        out.append({"role": "user", "content": _responses_content(msg)})
    return out


def _convert_chat_tools(tools: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in tools
    ]


def _convert_responses_tools(
    tools: list[Any],
    *,
    xai_compatible: bool = False,
) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for tool in tools:
        item = {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        }
        # xAI treats function schemas as strict by default and does not need
        # the OpenAI compatibility shim used by the existing gateway.
        if not xai_compatible:
            item["strict"] = False
        converted.append(item)
    return converted


def _preserve_base_url(base_url: str | None, default: str = "https://api.openai.com/v1") -> str:
    raw = (base_url or default or "").strip()
    if not raw:
        return default
    return raw.rstrip("/")


def _normalize_v1_base_url(base_url: str | None, default: str = "https://api.openai.com/v1") -> str:
    raw = _preserve_base_url(base_url, default=default)
    parsed = urlparse(raw)
    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1"
    normalized = parsed._replace(path=path)
    return urlunparse(normalized).rstrip("/")


def is_xai_base_url(base_url: str | None) -> bool:
    """Return whether a configured endpoint is the native xAI API."""
    host = (urlparse(str(base_url or "")).hostname or "").lower().rstrip(".")
    return host == "api.x.ai" or host == "mtls.api.x.ai" or host.endswith(".api.x.ai")


def is_grok_compatible_settings(settings: Settings, *, base_url: str | None = None) -> bool:
    """Return whether the OpenAI-compatible adapter is carrying a Grok request.

    Grok is intentionally served through the existing OpenAI-compatible
    adapter.  The explicit Grok aliases are the reliable signal for private
    gateways such as sub2api; the native xAI hostname covers direct API use.
    """

    endpoint = base_url or settings.openai_base_url
    return bool(
        settings.grok_models_base_url
        or settings.xai_api_key
        or is_xai_base_url(endpoint)
    )


def _extract_text_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_extract_text_content(item) for item in value]
        return "".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("text", "content", "value", "output_text"):
            text = _extract_text_content(value.get(key))
            if text:
                return text
        return ""

    text_attr = getattr(value, "text", None)
    if text_attr:
        return _extract_text_content(text_attr)
    content_attr = getattr(value, "content", None)
    if content_attr is not None:
        return _extract_text_content(content_attr)
    output_text_attr = getattr(value, "output_text", None)
    if output_text_attr:
        return _extract_text_content(output_text_attr)
    return ""


def _extract_chat_completion_payload(
    raw: Any,
) -> tuple[str, list[ToolCall], list[Citation], str, str, ChatUsage]:
    choice = (getattr(raw, "choices", None) or [None])[0]
    message = getattr(choice, "message", None)
    tool_calls: list[ToolCall] = []
    for item in getattr(message, "tool_calls", None) or []:
        fn = getattr(item, "function", None)
        tool_calls.append(
            ToolCall(
                id=getattr(item, "id", "") or "",
                name=getattr(fn, "name", "") or "",
                arguments=_serialise_args(getattr(fn, "arguments", "") or ""),
            )
        )
    usage = getattr(raw, "usage", None)
    chat_usage = ChatUsage(
        input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
    )
    return (
        _extract_text_content(getattr(message, "content", None)),
        tool_calls,
        [],
        str(getattr(raw, "model", "") or ""),
        str(getattr(choice, "finish_reason", "stop") or "stop"),
        chat_usage,
    )


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _extract_citations_from_annotations(
    content: Any,
    *,
    source: str = "openai_web_search",
    id_prefix: str = "openai_web",
) -> list[Citation]:
    if not isinstance(content, list):
        return []

    citations: list[Citation] = []
    seen_urls: set[str] = set()
    for block in content:
        annotations = _field(block, "annotations", []) or []
        if not isinstance(annotations, list):
            continue
        for annotation in annotations:
            annotation_type = str(_field(annotation, "type", "") or "")
            nested = _field(annotation, "url_citation", None) or {}
            url = str(_field(annotation, "url", "") or _field(nested, "url", "") or "").strip()
            if not url or url in seen_urls:
                continue
            title = str(
                _field(annotation, "title", "") or _field(nested, "title", "") or ""
            ).strip()
            snippet = str(
                _field(annotation, "snippet", "") or _field(nested, "snippet", "") or ""
            ).strip()
            if (
                annotation_type
                and annotation_type not in {"url_citation", "citation"}
                and not nested
            ):
                continue
            seen_urls.add(url)
            citations.append(
                Citation(
                    id=f"{id_prefix}:{len(citations) + 1}",
                    source=source,
                    title=title or None,
                    url=url,
                    snippet=snippet or None,
                )
            )
    return citations


def _extract_web_search_source_urls(
    item: Any,
    *,
    limit: int = _MAX_WEB_SEARCH_SOURCE_CITATIONS,
    exclude_urls: set[str] | None = None,
) -> list[str]:
    action = _field(item, "action", None)
    if action is None:
        return []

    urls: list[str] = []
    seen: set[str] = set(exclude_urls or ())
    sources = _field(action, "sources", []) or []
    if isinstance(sources, list):
        for source in sources:
            url = str(_field(source, "url", "") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            urls.append(url)
            if len(urls) >= limit:
                return urls

    action_url = str(_field(action, "url", "") or "").strip()
    if action_url and action_url not in seen and len(urls) < limit:
        urls.append(action_url)
    return urls


def _request_query(request: ChatRequest) -> str:
    """Return the latest user text for compact intent audit metadata."""

    for message in reversed(request.messages or []):
        if message.role == Role.USER and str(message.content or "").strip():
            return str(message.content or "").strip()[:4000]
    return ""


def _intent_from_tool_name(
    tool_name: str,
    *,
    query: str = "",
    search_call: bool = False,
) -> dict[str, Any]:
    """Project a provider tool selection into the shared intent contract."""

    normalized = str(tool_name or "").strip().lower()
    if normalized in {"web_search", "web_search_preview", "web_search_call"}:
        return IntentDecision(
            operation=IntentOperation.RETRIEVE,
            source=IntentSource.WEB,
            artifact=IntentArtifact.TEXT,
            domain=IntentDomain.WEB_SEARCH,
            query=query,
            confidence=1.0 if search_call else 0.9,
            needs_tool=True,
            tool_name="web_search" if normalized != "web_search_preview" else normalized,
        ).to_minimal_dict()
    if normalized in {"x_search", "x_search_call"}:
        return IntentDecision(
            operation=IntentOperation.RETRIEVE,
            source=IntentSource.X,
            artifact=IntentArtifact.TEXT,
            domain=IntentDomain.WEB_SEARCH,
            query=query,
            confidence=1.0 if search_call else 0.9,
            needs_tool=True,
            tool_name="x_search",
        ).to_minimal_dict()
    return IntentDecision(
        operation=IntentOperation.EXECUTE,
        source=IntentSource.NONE,
        artifact=IntentArtifact.STRUCTURED_DATA,
        query=query,
        confidence=0.9 if search_call else 0.8,
        needs_tool=True,
        tool_name=normalized or None,
    ).to_minimal_dict()


def _extract_responses_semantic_intent(
    raw: Any,
    *,
    query: str = "",
) -> dict[str, Any]:
    """Read the model's actual native tool choice without another LLM call."""

    for item in _field(raw, "output", None) or []:
        item_type = str(_field(item, "type", "") or "").strip().lower()
        if item_type in {"web_search_call", "x_search_call"}:
            return _intent_from_tool_name(item_type, query=query, search_call=True)
        if item_type == "function_call":
            return _intent_from_tool_name(
                str(_field(item, "name", "") or ""),
                query=query,
                search_call=False,
            )
    return {}


def _extract_responses_payload(
    raw: Any,
    *,
    citation_source: str = "openai_web_search",
    citation_id_prefix: str = "openai_web",
) -> tuple[str, list[ToolCall], list[Citation], str, str, ChatUsage]:
    output = _field(raw, "output", None) or []
    tool_calls: list[ToolCall] = []
    text_parts: list[str] = []
    citations: list[Citation] = []
    seen_citation_urls: set[str] = set()
    web_search_source_urls: list[str] = []
    seen_web_search_source_urls: set[str] = set()
    web_search_completed = False
    for item in output:
        item_type = str(_field(item, "type", "") or "")
        if item_type in {"web_search_call", "x_search_call"}:
            completed = str(_field(item, "status", "") or "").strip().lower() == "completed"
            web_search_completed = completed or web_search_completed
            remaining_source_slots = _MAX_WEB_SEARCH_SOURCE_CITATIONS - len(web_search_source_urls)
            if completed and remaining_source_slots > 0:
                for url in _extract_web_search_source_urls(
                    item,
                    limit=remaining_source_slots,
                    exclude_urls=seen_web_search_source_urls,
                ):
                    if url in seen_web_search_source_urls:
                        continue
                    seen_web_search_source_urls.add(url)
                    web_search_source_urls.append(url)
            continue
        if item_type == "function_call":
            tool_calls.append(
                ToolCall(
                    id=str(_field(item, "call_id", "") or _field(item, "id", "") or ""),
                    name=str(_field(item, "name", "") or ""),
                    arguments=_serialise_args(_field(item, "arguments", "") or ""),
                )
            )
            continue
        content = _field(item, "content", None)
        text = _extract_text_content(content)
        if text:
            text_parts.append(text)
        for citation in _extract_citations_from_annotations(
            content,
            source=citation_source,
            id_prefix=citation_id_prefix,
        ):
            if citation.url and citation.url in seen_citation_urls:
                continue
            if citation.url:
                seen_citation_urls.add(citation.url)
            citations.append(citation)
    if not web_search_completed:
        # URL-shaped annotations without a completed hosted search call are
        # not sufficient evidence for a fresh web-grounded answer.
        citations = []
    else:
        # Inline annotations remain the preferred citations because they carry
        # titles and are tied to the generated text.  The explicitly included
        # hosted-search sources are a fail-closed fallback for otherwise valid
        # searches whose answer omitted inline annotations.
        for url in web_search_source_urls:
            if url in seen_citation_urls:
                continue
            seen_citation_urls.add(url)
            citations.append(
                Citation(
                    id=f"{citation_id_prefix}:{len(citations) + 1}",
                    source=citation_source,
                    url=url,
                )
            )
    if not text_parts:
        text_parts.append(_extract_text_content(_field(raw, "output_text", None)))

    usage = _field(raw, "usage", None)
    input_tokens = int(_field(usage, "input_tokens", 0) or 0)
    output_tokens = int(_field(usage, "output_tokens", 0) or 0)
    if input_tokens == 0 and output_tokens == 0:
        usage_dict = getattr(usage, "model_dump", None)
        if callable(usage_dict):
            dumped = usage.model_dump()
            input_tokens = int(dumped.get("input_tokens", 0) or 0)
            output_tokens = int(dumped.get("output_tokens", 0) or 0)

    return (
        "".join(part for part in text_parts if part),
        tool_calls,
        citations,
        str(_field(raw, "model", "") or ""),
        str(_field(raw, "status", "completed") or "completed"),
        ChatUsage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _wrap_upstream_error(exc: Exception, operation: str) -> CSError:
    """Map a provider exception to a breaker-relevant error type.

    HTTP 4xx responses (except 429, which the retry policy already treats
    as transient) mean the request itself was rejected — retrying cannot
    succeed, and such failures must not open the circuit breaker that
    guards against real provider outages.
    """

    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int) and 400 <= status_code < 500 and status_code != 429:
        return UpstreamRejected(f"{operation} rejected ({status_code}): {exc}")
    return UpstreamUnavailable(f"{operation} unavailable: {exc}")


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        api_key: str,
        settings: Settings,
        *,
        base_url: str | None = None,
        api_mode: str | None = None,
    ) -> None:
        import openai

        self._openai = openai
        self._settings = settings
        self._api_mode = str(api_mode or settings.openai_api_mode or "responses").strip().lower()
        if self._api_mode not in _SUPPORTED_API_MODES:
            raise ValueError(f"unsupported OPENAI_API_MODE={self._api_mode}")
        self._responses_base_url = _preserve_base_url(base_url or settings.openai_base_url)
        self._chat_base_url = _normalize_v1_base_url(base_url or settings.openai_base_url)
        self._xai_compatible = is_xai_base_url(self._responses_base_url)
        self._grok_compatible = is_grok_compatible_settings(
            settings,
            base_url=self._responses_base_url,
        )
        self._web_search_citation_source = (
            "grok_web_search" if self._grok_compatible else "openai_web_search"
        )
        self._web_search_citation_id_prefix = (
            "grok_web" if self._grok_compatible else "openai_web"
        )
        self._responses_client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=self._responses_base_url,
            # Tenacity below owns retry policy. Disabling the SDK's nested
            # retries prevents one transient 5xx from expanding into as many
            # as twelve HTTP attempts before the chat fallback runs.
            max_retries=0,
        )
        self._chat_client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=self._chat_base_url,
            max_retries=0,
        )
        self._retry_exceptions: tuple[type[BaseException], ...] = (
            openai.APIConnectionError,
            openai.APITimeoutError,
            openai.RateLimitError,
            openai.InternalServerError,
        )

    def _resolve_model(self, req: ChatRequest) -> str:
        if req.model:
            return req.model
        attr = _TIER_ATTR.get(req.model_tier, "llm_model_tier2")
        return str(getattr(self._settings, attr))

    def _build_chat_kwargs(self, req: ChatRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self._resolve_model(req),
            "messages": _convert_messages(
                req.messages,
                req.system,
                include_compat_call_id=not self._xai_compatible,
            ),
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
        }
        if req.tools:
            kwargs["tools"] = _convert_chat_tools(req.tools)
        return kwargs

    def _should_use_web_search(self, req: ChatRequest) -> bool:
        metadata = req.metadata or {}
        if metadata.get("openai_web_search_required") is True:
            return True
        if "openai_web_search" in metadata:
            return bool(metadata.get("openai_web_search"))
        if "web_search" in metadata:
            return bool(metadata.get("web_search"))
        return bool(self._settings.openai_web_search_enabled)

    def _search_tool_types(self, req: ChatRequest) -> list[str]:
        """Resolve the hosted search choices exposed to the model.

        Ordinary LLM turns use native tool choice instead of a local keyword
        classifier.  Grok can therefore choose between the public web and X
        without us guessing from the wording of a multilingual request.
        Required-search callers still pass a single configured tool and keep
        their deterministic contract.
        """

        metadata = req.metadata or {}
        raw_types = metadata.get("openai_web_search_tools")
        if raw_types is None:
            if (
                metadata.get("semantic_intent_mode") == "native_tool_choice"
                and self._grok_compatible
            ):
                raw_types = ["web_search", "x_search"]
            else:
                raw_types = [
                    metadata.get("openai_web_search_tool")
                    or self._settings.openai_web_search_tool
                    or "web_search"
                ]
        elif isinstance(raw_types, str):
            raw_types = [raw_types]
        elif not isinstance(raw_types, (list, tuple, set)):
            raw_types = []

        resolved: list[str] = []
        for raw_type in raw_types:
            tool_type = str(raw_type or "").strip().lower()
            if tool_type not in _SUPPORTED_WEB_SEARCH_TOOLS:
                continue
            if self._grok_compatible and tool_type == "web_search_preview":
                tool_type = "web_search"
            if not self._grok_compatible and tool_type == "x_search":
                continue
            if tool_type not in resolved:
                resolved.append(tool_type)

        if resolved:
            return resolved
        # A malformed optional override should not silently disable a search
        # capability that was explicitly enabled.
        fallback = str(self._settings.openai_web_search_tool or "web_search").strip().lower()
        if fallback == "web_search_preview" and self._grok_compatible:
            fallback = "web_search"
        if fallback == "x_search" and not self._grok_compatible:
            fallback = "web_search"
        return [fallback if fallback in _SUPPORTED_WEB_SEARCH_TOOLS else "web_search"]

    def _build_web_search_tool(
        self,
        req: ChatRequest,
        *,
        tool_type: str | None = None,
    ) -> dict[str, Any]:
        metadata = req.metadata or {}
        tool_type = str(tool_type or self._search_tool_types(req)[0]).strip().lower()
        if self._grok_compatible and tool_type == "web_search_preview":
            # Grok exposes live web search as `web_search`; the OpenAI preview
            # alias is not part of the Grok tool vocabulary.
            tool_type = "web_search"
        tool: dict[str, Any] = {"type": tool_type}

        allowed_domains = metadata.get("openai_web_search_allowed_domains") or []
        if isinstance(allowed_domains, str):
            allowed_domains = [allowed_domains]
        if tool_type == "web_search" and isinstance(allowed_domains, list):
            cleaned_domains = [
                str(domain).strip() for domain in allowed_domains if str(domain).strip()
            ]
            if cleaned_domains:
                tool["filters"] = {"allowed_domains": cleaned_domains}

        if tool_type == "web_search" and not self._grok_compatible:
            tool["external_web_access"] = bool(self._settings.openai_web_search_live_enabled)
        return tool

    def _build_web_search_tools(self, req: ChatRequest) -> list[dict[str, Any]]:
        return [
            self._build_web_search_tool(req, tool_type=tool_type)
            for tool_type in self._search_tool_types(req)
        ]

    def _build_responses_kwargs(self, req: ChatRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self._resolve_model(req),
            "input": _convert_messages_for_responses(req.messages, req.system),
            "temperature": req.temperature,
            "max_output_tokens": req.max_tokens,
        }
        web_search_required = (req.metadata or {}).get("openai_web_search_required") is True
        tools: list[dict[str, Any]]
        if web_search_required:
            # Required search is a separate hosted-tool phase. Ignore custom
            # functions defensively so generic tool_choice="required" cannot
            # be satisfied by a local function instead of web search.
            tools = [self._build_web_search_tools(req)[0]]
        else:
            tools = []
            if req.tools:
                tools.extend(
                    _convert_responses_tools(
                        req.tools,
                        xai_compatible=self._xai_compatible,
                    )
                )
            if self._should_use_web_search(req):
                tools.extend(self._build_web_search_tools(req))
        if tools:
            kwargs["tools"] = tools
        if web_search_required:
            # Required live-search requests are issued without custom
            # function tools, so "required" deterministically selects the
            # only available hosted tool instead of leaving fresh evidence to
            # the model's optional tool choice.
            kwargs["tool_choice"] = "required"
            include_sources = (req.metadata or {}).get("openai_web_search_include_sources")
            if include_sources is None:
                include_sources = (
                    self._grok_compatible
                    or (urlparse(self._responses_base_url).hostname or "").lower()
                    == "api.openai.com"
                )
            if bool(include_sources):
                search_tool = tools[0].get("type")
                call_type = "x_search_call" if search_tool == "x_search" else "web_search_call"
                kwargs["include"] = [f"{call_type}.action.sources"]
        return kwargs

    async def chat(self, request: ChatRequest) -> ChatResponse:
        disable_fallback = bool(
            self._settings.openai_disable_fallback
            or bool((request.metadata or {}).get("disable_openai_fallback"))
        )
        if self._api_mode == "responses":
            if disable_fallback:
                return await self._chat_via_responses(request)
            try:
                return await self._chat_via_responses(request)
            except _ResponsesStreamInterrupted:
                # Replaying after any Responses event could duplicate hosted
                # tool work or produce a second, inconsistent answer.
                raise
            except (UpstreamUnavailable, UpstreamRejected) as exc:
                # 4xx also falls back: gateways such as sub2api may reject
                # Responses-API-only parameters while supporting plain chat.
                logger.warning(
                    "llm.openai.responses_fallback_to_chat",
                    api_mode="responses",
                    fallback="to_chat",
                    error_class=_error_class(exc),
                )
                return await self._chat_via_completions(request, fallback_from="responses")
        return await self._chat_via_completions(request)

    async def _chat_via_completions(
        self,
        request: ChatRequest,
        *,
        fallback_from: str | None = None,
    ) -> ChatResponse:
        kwargs = self._build_chat_kwargs(request)
        model = str(kwargs["model"])
        started = time.monotonic()
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(4),
                wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
                retry=retry_if_exception_type(self._retry_exceptions),
                reraise=True,
            ):
                with attempt:
                    raw = await self._chat_client.chat.completions.create(**kwargs)
        except self._retry_exceptions as exc:
            _record_api_attempt(
                api_mode="chat",
                result="error",
                fallback_from=fallback_from,
                exc=exc,
            )
            logger.warning(
                "llm.openai.upstream_unavailable",
                api_mode="chat",
                fallback=_fallback_label(fallback_from),
                error_class=exc.__class__.__name__,
            )
            raise UpstreamUnavailable(f"openai chat unavailable: {exc}") from exc
        except Exception as exc:
            _record_api_attempt(
                api_mode="chat",
                result="error",
                fallback_from=fallback_from,
                exc=exc,
            )
            logger.warning(
                "llm.openai.chat_failed",
                api_mode="chat",
                fallback=_fallback_label(fallback_from),
                error_class=exc.__class__.__name__,
            )
            raise _wrap_upstream_error(exc, "openai chat") from exc
        latency_ms = int((time.monotonic() - started) * 1000)

        content, tool_calls, citations, resolved_model, finish_reason, usage = (
            _extract_chat_completion_payload(raw)
        )
        _record_api_attempt(
            api_mode="chat",
            result="success",
            fallback_from=fallback_from,
        )
        return ChatResponse(
            content=content,
            tool_calls=tool_calls,
            citations=citations,
            model=resolved_model or model,
            finish_reason=finish_reason,
            usage=usage,
            latency_ms=latency_ms,
            metadata=(
                {
                    "semantic_intent": _intent_from_tool_name(
                        tool_calls[0].name,
                        query=_request_query(request),
                        search_call=False,
                    ),
                    "semantic_intent_method": "native_tool_call",
                }
                if tool_calls
                else {}
            ),
        )

    async def _chat_via_responses(self, request: ChatRequest) -> ChatResponse:
        kwargs = self._build_responses_kwargs(request)
        model = str(kwargs["model"])
        max_attempts = (
            2 if (request.metadata or {}).get("openai_web_search_required") is True else 4
        )
        started = time.monotonic()
        first_event_timeout = float(
            getattr(
                self._settings,
                "openai_responses_stream_first_event_timeout_seconds",
                _RESPONSES_FIRST_EVENT_TIMEOUT_SECONDS,
            )
            or _RESPONSES_FIRST_EVENT_TIMEOUT_SECONDS
        )
        idle_timeout = float(
            getattr(
                self._settings,
                "openai_responses_stream_idle_timeout_seconds",
                _RESPONSES_IDLE_TIMEOUT_SECONDS,
            )
            or _RESPONSES_IDLE_TIMEOUT_SECONDS
        )
        total_timeout = float(
            getattr(
                self._settings,
                "openai_responses_stream_max_duration_seconds",
                _RESPONSES_TOTAL_TIMEOUT_SECONDS,
            )
            or _RESPONSES_TOTAL_TIMEOUT_SECONDS
        )
        deadline = started + total_timeout
        stream_retry_exceptions = (
            *self._retry_exceptions,
            TimeoutError,
            httpx.TransportError,
        )
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(max_attempts),
                wait=wait_exponential(multiplier=0.25, min=0.25, max=1.0),
                retry=retry_if_exception_type(stream_retry_exceptions),
                reraise=True,
            ):
                with attempt:
                    raw = await _aggregate_responses_stream(
                        self,
                        kwargs,
                        deadline=deadline,
                        first_event_timeout=first_event_timeout,
                        idle_timeout=idle_timeout,
                    )
        except _ResponsesStreamInterrupted as exc:
            _record_api_attempt(api_mode="responses", result="error", exc=exc)
            logger.warning(
                "llm.openai.responses_stream_interrupted",
                api_mode="responses",
                fallback="none",
                error_class=_error_class(exc),
            )
            raise
        except stream_retry_exceptions as exc:
            _record_api_attempt(api_mode="responses", result="error", exc=exc)
            logger.warning(
                "llm.openai.upstream_unavailable",
                api_mode="responses",
                fallback="none",
                error_class=exc.__class__.__name__,
            )
            raise UpstreamUnavailable(f"openai responses unavailable: {exc}") from exc
        except Exception as exc:
            # Some OpenAI-compatible gateways implement Responses but not SSE.
            # A synchronous compatibility attempt is safe only before the
            # stream has emitted an event; post-event failures are converted to
            # _ResponsesStreamInterrupted above and never reach this branch.
            logger.info(
                "llm.openai.responses_stream_fallback_to_sync",
                api_mode="responses",
                fallback="to_sync",
                error_class=exc.__class__.__name__,
            )
            try:
                raw = await _create_sync_response_with_deadline(
                    self,
                    kwargs,
                    deadline=deadline,
                )
            except Exception as sync_exc:
                _record_api_attempt(api_mode="responses", result="error", exc=sync_exc)
                logger.warning(
                    "llm.openai.responses_failed",
                    api_mode="responses",
                    fallback="none",
                    error_class=sync_exc.__class__.__name__,
                )
                raise _wrap_upstream_error(sync_exc, "openai responses") from sync_exc

        latency_ms = int((time.monotonic() - started) * 1000)
        content, tool_calls, citations, resolved_model, finish_reason, usage = (
            _extract_responses_payload(
                raw,
                citation_source=self._web_search_citation_source,
                citation_id_prefix=self._web_search_citation_id_prefix,
            )
        )
        _record_api_attempt(api_mode="responses", result="success")
        return ChatResponse(
            content=content,
            tool_calls=tool_calls,
            citations=citations,
            model=resolved_model or model,
            finish_reason=finish_reason,
            usage=usage,
            latency_ms=latency_ms,
            metadata=(
                {
                    "semantic_intent": semantic_intent,
                    "semantic_intent_method": "native_tool_call",
                }
                if (semantic_intent := _extract_responses_semantic_intent(
                    raw,
                    query=_request_query(request),
                ))
                else {}
            ),
        )

    def stream_chat(self, request: ChatRequest) -> AsyncIterator[str]:
        if self._api_mode == "responses":
            return _openai_responses_stream_with_fallback(self, request)
        return _openai_stream(self, request)

    async def embed(self, request: EmbedRequest) -> EmbedResponse:
        model = request.model or self._settings.llm_embed_model
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(4),
                wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
                retry=retry_if_exception_type(self._retry_exceptions),
                reraise=True,
            ):
                with attempt:
                    raw = await self._chat_client.embeddings.create(
                        model=model,
                        input=request.texts,
                    )
        except self._retry_exceptions as exc:
            logger.warning(
                "llm.openai.embed_upstream_unavailable",
                error=exc.__class__.__name__,
                model=model,
            )
            raise UpstreamUnavailable(f"openai embeddings unavailable: {exc}") from exc
        except Exception as exc:
            logger.warning(
                "llm.openai.embed_failed",
                error=exc.__class__.__name__,
                model=model,
            )
            raise _wrap_upstream_error(exc, "openai embeddings") from exc

        data = getattr(raw, "data", None) or []
        vectors = [list(item.embedding) for item in data]
        usage = getattr(raw, "usage", None)
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        return EmbedResponse(
            vectors=vectors,
            model=str(getattr(raw, "model", model) or model),
            input_tokens=input_tokens,
        )

    async def close(self) -> None:
        for client in {self._responses_client, self._chat_client}:
            close = getattr(client, "close", None)
            if close is None:
                continue
            try:
                result = close()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                logger.debug("llm.openai.close_failed", exc_info=True)


def _remaining_responses_time(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("openai responses exceeded the safety time limit")
    return remaining


async def _close_responses_stream(stream: Any) -> None:
    close = getattr(stream, "close", None) or getattr(stream, "aclose", None)
    if close is None:
        return
    try:
        result = close()
        if hasattr(result, "__await__"):
            await result
    except Exception:
        logger.debug("llm.openai.responses_stream_close_failed", exc_info=True)


async def _aggregate_responses_stream(
    provider: OpenAIProvider,
    kwargs: dict[str, Any],
    *,
    deadline: float,
    first_event_timeout: float = _RESPONSES_FIRST_EVENT_TIMEOUT_SECONDS,
    idle_timeout: float = _RESPONSES_IDLE_TIMEOUT_SECONDS,
) -> Any:
    """Consume a Responses SSE stream and return its complete terminal response."""

    stream: Any = None
    received_event = False
    first_event_deadline = time.monotonic() + max(0.001, first_event_timeout)
    try:
        create_timeout = min(
            _remaining_responses_time(first_event_deadline),
            _remaining_responses_time(deadline),
        )
        stream = await asyncio.wait_for(
            provider._responses_client.responses.create(**kwargs, stream=True),
            timeout=create_timeout,
        )
        iterator = aiter(stream)
        while True:
            event_timeout = min(
                (
                    max(0.001, idle_timeout)
                    if received_event
                    else _remaining_responses_time(first_event_deadline)
                ),
                _remaining_responses_time(deadline),
            )
            try:
                event = await asyncio.wait_for(anext(iterator), timeout=event_timeout)
            except StopAsyncIteration:
                break
            received_event = True
            record_llm_stream_event(idle_timeout_seconds=idle_timeout)
            event_type = str(_field(event, "type", "") or "")
            if event_type == "response.completed":
                response = _field(event, "response", None)
                if response is None:
                    raise RuntimeError("response.completed did not include a response")
                return response
            if event_type in {"response.failed", "response.incomplete", "error"}:
                raise RuntimeError(f"responses stream ended with {event_type}")
        raise RuntimeError("responses stream ended without response.completed")
    except _ResponsesStreamInterrupted:
        raise
    except Exception as exc:
        if received_event:
            raise _ResponsesStreamInterrupted(
                f"openai responses stream interrupted after first event: {exc}"
            ) from exc
        raise
    finally:
        if stream is not None:
            await _close_responses_stream(stream)


async def _create_sync_response_with_deadline(
    provider: OpenAIProvider,
    kwargs: dict[str, Any],
    *,
    deadline: float,
) -> Any:
    return await asyncio.wait_for(
        provider._responses_client.responses.create(**kwargs),
        timeout=_remaining_responses_time(deadline),
    )


async def _openai_stream(
    provider: OpenAIProvider,
    request: ChatRequest,
    *,
    fallback_from: str | None = None,
) -> AsyncIterator[str]:
    kwargs = provider._build_chat_kwargs(request)
    kwargs["stream"] = True
    try:
        stream = await provider._chat_client.chat.completions.create(**kwargs)
        async for chunk in stream:
            for choice in getattr(chunk, "choices", None) or []:
                delta = getattr(choice, "delta", None)
                text = getattr(delta, "content", None) if delta is not None else None
                if text:
                    yield str(text)
    except provider._retry_exceptions as exc:
        _record_api_attempt(
            api_mode="chat",
            result="error",
            fallback_from=fallback_from,
            exc=exc,
        )
        logger.warning(
            "llm.openai.stream_upstream_unavailable",
            api_mode="chat",
            fallback=_fallback_label(fallback_from),
            error_class=exc.__class__.__name__,
        )
        raise UpstreamUnavailable(f"openai stream unavailable: {exc}") from exc
    except Exception as exc:
        _record_api_attempt(
            api_mode="chat",
            result="error",
            fallback_from=fallback_from,
            exc=exc,
        )
        logger.warning(
            "llm.openai.stream_failed",
            api_mode="chat",
            fallback=_fallback_label(fallback_from),
            error_class=exc.__class__.__name__,
        )
        raise _wrap_upstream_error(exc, "openai stream") from exc
    _record_api_attempt(
        api_mode="chat",
        result="success",
        fallback_from=fallback_from,
    )


async def _openai_responses_stream(
    provider: OpenAIProvider, request: ChatRequest
) -> AsyncIterator[str]:
    kwargs = provider._build_responses_kwargs(request)
    kwargs["stream"] = True
    try:
        stream = await provider._responses_client.responses.create(**kwargs)
        async for event in stream:
            text = _extract_text_content(getattr(event, "delta", None))
            if not text:
                text = _extract_text_content(getattr(event, "output_text", None))
            if text:
                yield text
    except provider._retry_exceptions as exc:
        _record_api_attempt(api_mode="responses", result="error", exc=exc)
        logger.warning(
            "llm.openai.responses_stream_upstream_unavailable",
            api_mode="responses",
            fallback="none",
            error_class=exc.__class__.__name__,
        )
        raise UpstreamUnavailable(f"openai responses stream unavailable: {exc}") from exc
    except Exception as exc:
        _record_api_attempt(api_mode="responses", result="error", exc=exc)
        logger.warning(
            "llm.openai.responses_stream_failed",
            api_mode="responses",
            fallback="none",
            error_class=exc.__class__.__name__,
        )
        raise _wrap_upstream_error(exc, "openai responses stream") from exc
    _record_api_attempt(api_mode="responses", result="success")


async def _openai_responses_stream_with_fallback(
    provider: OpenAIProvider,
    request: ChatRequest,
) -> AsyncIterator[str]:
    yielded = False
    try:
        async for chunk in _openai_responses_stream(provider, request):
            yielded = True
            yield chunk
    except UpstreamUnavailable as exc:
        if yielded:
            raise
        logger.warning(
            "llm.openai.responses_stream_fallback_to_chat",
            api_mode="responses",
            fallback="to_chat",
            error_class=_error_class(exc),
        )
        async for chunk in _openai_stream(provider, request, fallback_from="responses"):
            yield chunk
    except Exception as exc:
        if yielded:
            raise
        logger.warning(
            "llm.openai.responses_stream_failed",
            api_mode="responses",
            fallback="to_chat",
            error_class=exc.__class__.__name__,
        )
        async for chunk in _openai_stream(provider, request, fallback_from="responses"):
            yield chunk
