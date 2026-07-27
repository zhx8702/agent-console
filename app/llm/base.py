from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from app.common.types import ChatRequest, ChatResponse


@dataclass
class EmbedRequest:
    tenant_id: str
    trace_id: str
    model: str
    texts: list[str]


@dataclass
class EmbedResponse:
    vectors: list[list[float]]
    model: str
    input_tokens: int = 0


class LLMProvider(Protocol):
    name: str

    async def chat(self, request: ChatRequest) -> ChatResponse: ...

    async def stream_chat(self, request: ChatRequest) -> AsyncIterator[str]: ...

    async def embed(self, request: EmbedRequest) -> EmbedResponse: ...

    async def close(self) -> None: ...
