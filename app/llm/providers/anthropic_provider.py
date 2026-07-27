"""
Anthropic Claude provider using the official async SDK.

Notes:
- ``anthropic`` is imported lazily in ``__init__`` so the fake path keeps
  working even if the SDK is not importable.
- Chat requests support ephemeral prompt caching on the system prompt
  (``cache_system=True``). The response usage is projected onto the
  :class:`ChatUsage` contract, including cache read/creation tokens.
- Tool calls from the model are surfaced as :class:`ToolCall` entries.
  Tool *results* (role=tool in the request) are collapsed into user
  messages that contain ``tool_result`` content blocks, which is the
  format the Messages API expects.
- Embeddings are *not* implemented here; callers should wire a dedicated
  embeddings backend and fall back to :class:`FakeProvider` in tests.
"""
from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.common.config import Settings
from app.common.exceptions import UpstreamUnavailable
from app.common.logging import get_logger
from app.common.types import ChatMessage, ChatRequest, ChatResponse, ChatUsage, Role, ToolCall
from app.llm.base import EmbedRequest, EmbedResponse

logger = get_logger(__name__)


_TIER_ATTR = {
    "tier-1": "llm_model_tier1",
    "tier-2": "llm_model_tier2",
    "tier-3": "llm_model_tier3",
}


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


def _convert_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    """Translate contract messages into Anthropic's Messages API format."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == Role.SYSTEM:
            # System messages are handled via the top-level ``system`` parameter.
            continue
        if msg.role == Role.TOOL:
            # Tool results are expressed as a user message with a tool_result block.
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg.tool_call_id or "",
                            "content": msg.content or "",
                        }
                    ],
                }
            )
            continue
        if msg.role == Role.ASSISTANT:
            blocks: list[dict[str, Any]] = []
            if msg.content:
                blocks.append({"type": "text", "text": msg.content})
            for tc in msg.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": _serialise_args(tc.arguments),
                    }
                )
            if not blocks:
                # Anthropic rejects empty assistant messages; inject a placeholder.
                blocks.append({"type": "text", "text": ""})
            out.append({"role": "assistant", "content": blocks})
            continue
        # user / agent_human -> user
        out.append({"role": "user", "content": msg.content or ""})
    return out


def _convert_tools(tools: list[Any]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for t in tools:
        converted.append(
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters,
            }
        )
    return converted


class AnthropicProvider:
    """Claude chat provider. Embeddings are intentionally not supported here."""

    name = "anthropic"

    def __init__(self, api_key: str, settings: Settings) -> None:
        # Lazy import so that environments without the SDK (or without an API
        # key) can still import :mod:`app.llm.service`.
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._settings = settings
        self._retry_exceptions: tuple[type[BaseException], ...] = (
            anthropic.APIConnectionError,
            anthropic.RateLimitError,
            anthropic.InternalServerError,
        )

    # ------------------------------------------------------------------ helpers
    def _resolve_model(self, req: ChatRequest) -> str:
        if req.model:
            return req.model
        attr = _TIER_ATTR.get(req.model_tier, "llm_model_tier2")
        return getattr(self._settings, attr)

    def _build_system(self, req: ChatRequest) -> Any:
        if not req.system:
            return None
        if req.cache_system:
            return [
                {
                    "type": "text",
                    "text": req.system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        return req.system

    def _build_kwargs(self, req: ChatRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self._resolve_model(req),
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "messages": _convert_messages(req.messages),
        }
        system = self._build_system(req)
        if system is not None:
            kwargs["system"] = system
        if req.tools:
            kwargs["tools"] = _convert_tools(req.tools)
        return kwargs

    @staticmethod
    def _usage_from_sdk(raw: Any) -> ChatUsage:
        if raw is None:
            return ChatUsage()
        # SDK exposes pydantic-style attributes.
        return ChatUsage(
            input_tokens=int(getattr(raw, "input_tokens", 0) or 0),
            output_tokens=int(getattr(raw, "output_tokens", 0) or 0),
            cache_read_tokens=int(getattr(raw, "cache_read_input_tokens", 0) or 0),
            cache_write_tokens=int(getattr(raw, "cache_creation_input_tokens", 0) or 0),
        )

    # ---------------------------------------------------------------- public API
    async def chat(self, request: ChatRequest) -> ChatResponse:
        kwargs = self._build_kwargs(request)
        model = kwargs["model"]
        started = time.monotonic()
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(4),
                wait=wait_exponential(multiplier=0.5, min=0.5, max=8.0),
                retry=retry_if_exception_type(self._retry_exceptions),
                reraise=True,
            ):
                with attempt:
                    raw = await self._client.messages.create(**kwargs)
        except self._retry_exceptions as exc:
            logger.warning(
                "llm.anthropic.upstream_unavailable",
                error=exc.__class__.__name__,
                model=model,
            )
            raise UpstreamUnavailable(f"anthropic unavailable: {exc}") from exc
        latency_ms = int((time.monotonic() - started) * 1000)

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in getattr(raw, "content", []) or []:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_parts.append(getattr(block, "text", "") or "")
            elif block_type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=getattr(block, "id", ""),
                        name=getattr(block, "name", ""),
                        arguments=_serialise_args(getattr(block, "input", {}) or {}),
                    )
                )

        return ChatResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            model=getattr(raw, "model", model) or model,
            finish_reason=getattr(raw, "stop_reason", "stop") or "stop",
            usage=self._usage_from_sdk(getattr(raw, "usage", None)),
            latency_ms=latency_ms,
        )

    def stream_chat(self, request: ChatRequest) -> AsyncIterator[str]:
        return _anthropic_stream(self, request)

    async def embed(self, request: EmbedRequest) -> EmbedResponse:
        raise NotImplementedError(
            "AnthropicProvider does not implement embeddings; configure a "
            "dedicated embeddings provider (e.g. voyage/cohere) or use FakeProvider."
        )

    async def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is None:
            return None
        try:
            result = close()
            if hasattr(result, "__await__"):
                await result
        except Exception:
            logger.debug("llm.anthropic.close_failed", exc_info=True)


async def _anthropic_stream(provider: AnthropicProvider, request: ChatRequest) -> AsyncIterator[str]:
    kwargs = provider._build_kwargs(request)
    model = kwargs["model"]
    # Streaming responses cannot be replayed mid-flight, so we do not retry
    # once any data has been yielded. We only wrap connection-level failures.
    try:
        async with provider._client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                if text:
                    yield text
    except provider._retry_exceptions as exc:
        logger.warning(
            "llm.anthropic.stream_upstream_unavailable",
            error=exc.__class__.__name__,
            model=model,
        )
        raise UpstreamUnavailable(f"anthropic stream unavailable: {exc}") from exc
