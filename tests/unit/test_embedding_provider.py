from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.common.config import Settings
from app.common.exceptions import UpstreamUnavailable
from app.llm.base import EmbedRequest, EmbedResponse
from app.llm.providers.embedding_provider import EmbeddingProvider
from app.llm.service import LLMService, _build_embed_provider, validate_llm_settings


def settings(**changes):
    return Settings(_env_file=None, llm_embed_provider="openai",
                    llm_embed_base_url="http://embedding.internal/v1",
                    llm_embed_api_key="embedding-only", openai_api_key="chat-only", **changes)


@pytest.mark.asyncio
async def test_separate_credentials_order_validation_and_tenant_cache():
    calls = []

    async def respond(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer embedding-only"
        assert request.url.host == "embedding.internal"
        assert request.url.path == "/v1/embeddings"
        assert json.loads(request.content)["encoding_format"] == "float"
        return httpx.Response(200, json={"data": [
            {"index": 1, "embedding": [0., 1.]}, {"index": 0, "embedding": [1., 0.]}],
            "usage": {"prompt_tokens": 9}})

    provider = EmbeddingProvider(settings(), transport=httpx.MockTransport(respond))
    request = EmbedRequest("a", "trace", "model", ["first", "second"])
    try:
        first = await provider.embed(request)
        assert first.vectors == [[1., 0.], [0., 1.]]
        first.vectors[0][0] = 99
        assert (await provider.embed(request)).vectors[0][0] == 1.
        await provider.embed(EmbedRequest("b", "trace", "model", request.texts))
        assert len(calls) == 2
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [[], [{"index": 1, "embedding": [1]}],
                                  [{"index": 0, "embedding": [0, 0]}],
                                  [{"index": 0, "embedding": [True, 1]}],
                                  [{"index": 0, "embedding": ["bad"]}]])
async def test_malformed_vectors_fail_closed(data):
    provider = EmbeddingProvider(settings(), transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"data": data})))
    try:
        with pytest.raises(UpstreamUnavailable):
            await provider.embed(EmbedRequest("a", "t", "model", ["text"]))
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_network_deadline_no_retries_or_secret_in_error():
    attempts = 0

    async def slow(_request):
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(1)

    provider = EmbeddingProvider(settings(llm_embed_timeout_seconds=.02),
                                 transport=httpx.MockTransport(slow))
    try:
        with pytest.raises(UpstreamUnavailable) as error:
            await provider.embed(EmbedRequest("a", "t", "model", ["private-text"]))
        assert attempts == 1
        assert "private-text" not in str(error.value)
        assert "embedding-only" not in str(error.value)
    finally:
        await provider.close()


def test_separate_endpoint_never_falls_back_to_chat_key():
    s = settings().model_copy(update={"llm_embed_api_key": None})
    assert any("LLM_EMBED_API_KEY" in error for error in validate_llm_settings(s))
    with pytest.raises(ValueError):
        _build_embed_provider(s)


@pytest.mark.asyncio
async def test_model_resolution_preserves_background_priority():
    captured = []

    async def embed(request):
        captured.append(request)
        return EmbedResponse([[1., 0.]], request.model, 1)

    provider = SimpleNamespace(name="test", embed=embed)
    service = LLMService(chat_provider=provider, embed_provider=provider,
                         quota=SimpleNamespace(reserve_tokens=AsyncMock(), commit=AsyncMock()),
                         settings=settings())
    await service.embed(EmbedRequest("tenant", "trace", "", ["text"], priority="background"))
    assert captured[0].priority == "background"
    assert captured[0].model == settings().llm_embed_model
