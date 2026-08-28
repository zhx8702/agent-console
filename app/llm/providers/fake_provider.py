"""
Deterministic offline LLM provider used for tests and local development.

- ``chat`` echoes back a canned reply derived from the last user message.
- ``stream_chat`` yields the same content in fixed-size chunks.
- ``embed`` produces reproducible 64-dim unit vectors via SHA-256 hashing.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from collections.abc import AsyncIterator

from app.common.types import ChatRequest, ChatResponse, ChatUsage, Role, ToolCall
from app.llm.base import EmbedRequest, EmbedResponse

_EMBED_DIM = 64
_CHUNK_SIZE = 20

_ORDER_RE = re.compile(r"ORD[-_A-Za-z0-9]+")


def _last_user_content(req: ChatRequest) -> str:
    for msg in reversed(req.messages):
        if msg.role == Role.USER:
            return msg.content or ""
    return ""


def _canned_reply(user_text: str) -> str:
    return f"[fake] 你说了: {user_text[:120]}"


def _classify_query(user_text: str) -> str:
    text = str(user_text or "").strip()
    if "\n\nContext:\n" in text:
        text = text.split("\n\nContext:\n", 1)[0].strip()
    return text


def _classify_reply(user_text: str) -> str:
    """Return a parseable IntentDecision for hermetic classify calls."""

    query = _classify_query(user_text)
    if "转人工" in query:
        decision = {
            "operation": "handoff",
            "source": "none",
            "artifact": "text",
            "domain": "handoff",
            "action": "request",
            "query": query,
            "confidence": 0.95,
            "needs_tool": False,
            "tool_name": None,
            "slots": {},
        }
    else:
        decision = {
            "operation": "converse",
            "source": "none",
            "artifact": "text",
            "domain": "none",
            "action": "",
            "query": query,
            "confidence": 0.0,
            "needs_tool": False,
            "tool_name": None,
            "slots": {},
        }
    return json.dumps(decision, ensure_ascii=False)


def _maybe_tool_call(req: ChatRequest, user_text: str) -> list[ToolCall]:
    if not req.tools:
        return []
    if "查订单" not in user_text or "ORD" not in user_text:
        return []
    m = _ORDER_RE.search(user_text)
    if not m:
        return []
    return [
        ToolCall(
            id="fake_tool_1",
            name="query_order",
            arguments={"order_id": m.group(0)},
        )
    ]


def _estimate_tokens(s: str) -> int:
    return max(1, len(s) // 4)


def _embed_vector(text: str) -> list[float]:
    """Deterministic 64-dim unit vector derived from the SHA-256 of ``text``."""
    out: list[float] = []
    seed = text.encode("utf-8")
    counter = 0
    while len(out) < _EMBED_DIM:
        h = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        # SHA-256 gives 32 bytes -> 8 uint32s.
        for i in range(0, 32, 4):
            if len(out) >= _EMBED_DIM:
                break
            val = struct.unpack(">I", h[i : i + 4])[0]
            # Map uint32 into [-1.0, 1.0] deterministically.
            out.append((val / 0xFFFFFFFF) * 2.0 - 1.0)
        counter += 1
    # Normalise to unit length.
    norm = math.sqrt(sum(x * x for x in out))
    if norm == 0.0:
        # Degenerate: return a canonical unit vector.
        unit = [0.0] * _EMBED_DIM
        unit[0] = 1.0
        return unit
    return [x / norm for x in out]


class FakeProvider:
    """Deterministic offline provider conforming to :class:`LLMProvider`."""

    name = "fake"

    def __init__(self, *, chat_model: str = "fake-chat", embed_model: str = "fake-embed") -> None:
        self._chat_model = chat_model
        self._embed_model = embed_model

    async def chat(self, request: ChatRequest) -> ChatResponse:
        user_text = _last_user_content(request)
        metadata = request.metadata or {}
        if str(metadata.get("route") or "") == "intent_classify":
            content = _classify_reply(user_text)
        else:
            content = _canned_reply(user_text)
        tool_calls = _maybe_tool_call(request, user_text)
        model = request.model or self._chat_model
        usage = ChatUsage(
            input_tokens=_estimate_tokens(user_text),
            output_tokens=_estimate_tokens(content),
        )
        return ChatResponse(
            content=content,
            tool_calls=tool_calls,
            model=model,
            finish_reason="tool_use" if tool_calls else "stop",
            usage=usage,
            latency_ms=5,
        )

    def stream_chat(self, request: ChatRequest) -> AsyncIterator[str]:
        # Returns an async generator; callers use ``async for chunk in provider.stream_chat(req)``.
        return _stream(self, request)

    async def embed(self, request: EmbedRequest) -> EmbedResponse:
        vectors = [_embed_vector(t) for t in request.texts]
        tokens = sum(_estimate_tokens(t) for t in request.texts)
        return EmbedResponse(
            vectors=vectors,
            model=request.model or self._embed_model,
            input_tokens=tokens,
        )

    async def close(self) -> None:
        return None


async def _stream(provider: FakeProvider, request: ChatRequest) -> AsyncIterator[str]:
    resp = await provider.chat(request)
    for i in range(0, len(resp.content), _CHUNK_SIZE):
        yield resp.content[i : i + _CHUNK_SIZE]
