"""
Deterministic in-memory LLM fakes for unit tests.

FakeEmbeddingsProvider produces reproducible embeddings from a bag-of-words hash
so tests can assert similarity ordering without touching a real model.
"""
from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from typing import Any

from app.common.types import ChatRequest, ChatResponse, ChatUsage
from app.llm.base import EmbedRequest, EmbedResponse

DEFAULT_DIM = 64


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    buf: list[str] = []
    for ch in text.lower():
        if ch.isspace() or not (ch.isalnum() or ord(ch) > 0x2E80):
            if buf:
                tokens.append("".join(buf))
                buf = []
            continue
        if ord(ch) > 0x2E80:  # CJK-ish
            if buf:
                tokens.append("".join(buf))
                buf = []
            tokens.append(ch)
            continue
        buf.append(ch)
    if buf:
        tokens.append("".join(buf))
    return tokens


def hash_embed(text: str, dim: int = DEFAULT_DIM) -> list[float]:
    """Deterministic bag-of-words -> fixed-dim vector."""
    vec = [0.0] * dim
    for tok in _tokenize(text):
        h = hashlib.md5(tok.encode("utf-8")).digest()
        idx = int.from_bytes(h[:4], "big") % dim
        sign = 1.0 if (h[4] & 1) else -1.0
        vec[idx] += sign
    # L2 normalize to stabilize cosine.
    norm = sum(v * v for v in vec) ** 0.5
    if norm == 0:
        vec[0] = 1.0
        return vec
    return [v / norm for v in vec]


class FakeEmbeddingsProvider:
    """Implements LLMProvider.embed with deterministic hash vectors."""

    name = "fake-embed"

    def __init__(self, dim: int = DEFAULT_DIM) -> None:
        self.dim = dim

    async def embed(self, request: EmbedRequest) -> EmbedResponse:
        vectors = [hash_embed(t, self.dim) for t in request.texts]
        return EmbedResponse(
            vectors=vectors,
            model=request.model,
            input_tokens=sum(len(t) for t in request.texts),
        )

    async def chat(self, request: ChatRequest) -> ChatResponse:
        # Echoes back the user message prefix — deterministic enough for tests.
        user_text = request.messages[-1].content if request.messages else ""
        return ChatResponse(
            content=f"FAKE_REPLY::{user_text[:120]}",
            model=request.model or "fake-chat",
            finish_reason="stop",
            usage=ChatUsage(input_tokens=len(user_text), output_tokens=16),
            latency_ms=1,
        )

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[str]:  # pragma: no cover
        resp = await self.chat(request)
        async def _iter() -> AsyncIterator[str]:
            yield resp.content
        return _iter()

    async def close(self) -> None:
        return None


class CannedChatProvider(FakeEmbeddingsProvider):
    """A FakeEmbeddingsProvider with a fixed chat reply."""

    def __init__(self, reply: str = "OK [1]", dim: int = DEFAULT_DIM) -> None:
        super().__init__(dim=dim)
        self._reply = reply

    async def chat(self, request: ChatRequest) -> ChatResponse:  # type: ignore[override]
        _ = request
        return ChatResponse(
            content=self._reply,
            model="canned",
            finish_reason="stop",
            usage=ChatUsage(input_tokens=10, output_tokens=len(self._reply)),
            latency_ms=1,
        )


def make_preprocessed(text: str, **extra: Any) -> Any:
    from app.common.types import PreprocessedMessage

    return PreprocessedMessage(original_text=text, cleaned_text=text, **extra)


def make_session(tenant_id: str = "demo", session_id: str = "se_test00000000000001") -> Any:
    from app.common.types import Channel, Session

    return Session(
        session_id=session_id, tenant_id=tenant_id, user_id="u1", channel=Channel.WEB
    )
