"""Bounded OpenAI-compatible embedding transport, independent of chat secrets."""
from __future__ import annotations

import asyncio
import hashlib
import math
import time
from collections import OrderedDict

import httpx

from app.common.config import Settings
from app.common.exceptions import UpstreamUnavailable
from app.llm.base import EmbedRequest, EmbedResponse


class EmbeddingProvider:
    name = "openai"

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        if not settings.llm_embed_base_url or not settings.llm_embed_api_key:
            raise ValueError("Separate embedding URL and API key are required")
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.llm_embed_base_url.rstrip("/") + "/",
            headers={"Authorization": "Bearer " + settings.llm_embed_api_key},
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
        self._cache: OrderedDict[str, tuple[float, list[list[float]]]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def embed(self, request: EmbedRequest) -> EmbedResponse:
        if not request.texts:
            return EmbedResponse(vectors=[], model=request.model, input_tokens=0)
        model = request.model or self._settings.llm_embed_model
        key = hashlib.sha256(repr((request.tenant_id, model, request.texts)).encode()).hexdigest()
        # Concurrent item/fact/episode queries can reuse one query embedding.
        # Bound waiting as well as network I/O; background calls use a separate lane.
        timeout = (
            self._settings.llm_embed_background_timeout_seconds
            if request.priority == "background" else self._settings.llm_embed_timeout_seconds
        )
        try:
            async with asyncio.timeout(timeout):
                if request.priority == "background":
                    return await self._fetch(request, model)
                async with self._lock:
                    cached = self._cache.get(key)
                    if cached and cached[0] > time.monotonic():
                        self._cache.move_to_end(key)
                        return EmbedResponse([v[:] for v in cached[1]], model, 0)
                    response = await self._fetch(request, model)
                    if self._settings.llm_embed_cache_entries:
                        self._cache[key] = (
                            time.monotonic() + self._settings.llm_embed_cache_ttl_seconds,
                            [v[:] for v in response.vectors],
                        )
                        self._cache.move_to_end(key)
                        while len(self._cache) > self._settings.llm_embed_cache_entries:
                            self._cache.popitem(last=False)
                    return response
        except (TimeoutError, httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            # Do not include URLs, response bodies, request text or credentials.
            raise UpstreamUnavailable(f"embedding request failed: {type(exc).__name__}") from exc

    async def _fetch(self, request: EmbedRequest, model: str) -> EmbedResponse:
        response = await self._client.post(
            "embeddings",
            json={"model": model, "input": request.texts, "encoding_format": "float"},
            headers={"X-Embedding-Priority": request.priority},
            timeout=None,  # The outer deadline covers pool, transport and validation.
        )
        response.raise_for_status()
        body = response.json()
        rows = sorted(body["data"], key=lambda row: row["index"])
        if [row["index"] for row in rows] != list(range(len(request.texts))):
            raise ValueError("embedding response indexes mismatch")
        vectors = [row["embedding"] for row in rows]
        if not vectors or not all(
            isinstance(v, list) and v and all(
                isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
                for x in v
            ) and sum(x * x for x in v) > 0
            for v in vectors
        ) or len({len(v) for v in vectors}) != 1:
            raise ValueError("invalid embedding vectors")
        return EmbedResponse(vectors, str(body.get("model") or model),
                             int((body.get("usage") or {}).get("prompt_tokens", 0)))

    async def close(self) -> None:
        await self._client.aclose()
