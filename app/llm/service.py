"""
High-level LLM orchestration.

Responsibilities:
- Resolve the model from the request tier (tier-1/2/3 -> settings).
- Enforce per-tenant daily token quota (Redis) before hitting the provider.
- Record Prometheus metrics and estimated cost per request.
- Provide a simple circuit breaker so we fail fast after repeated upstream errors.
- Offer a factory :func:`build_llm_service` that wires the correct providers
  based on :class:`Settings`.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from app.common.config import Settings, get_settings
from app.common.exceptions import UpstreamUnavailable
from app.common.logging import get_logger
from app.common.types import ChatRequest, ChatResponse
from app.infra.metrics import LLM_COST_USD, LLM_LATENCY, LLM_REQUESTS, LLM_TOKENS
from app.llm.base import EmbedRequest, EmbedResponse, LLMProvider
from app.llm.pricing import compute_cost
from app.llm.providers.fake_provider import FakeProvider
from app.llm.quota import QuotaTracker

logger = get_logger(__name__)

_SUPPORTED_CHAT_PROVIDERS = {"fake", "openai"}
_SUPPORTED_EMBED_PROVIDERS = {"fake", "openai"}
_SUPPORTED_OPENAI_API_MODES = {"chat", "responses"}


_TIER_ATTR = {
    "tier-1": "llm_model_tier1",
    "tier-2": "llm_model_tier2",
    "tier-3": "llm_model_tier3",
}


def validate_llm_settings(settings: Settings) -> list[str]:
    errors: list[str] = []

    if settings.llm_provider not in _SUPPORTED_CHAT_PROVIDERS:
        errors.append(f"unsupported LLM_PROVIDER={settings.llm_provider}")
    elif settings.llm_provider == "openai" and not settings.openai_api_key:
        errors.append("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
    elif settings.is_prod and settings.llm_provider == "fake":
        errors.append("LLM_PROVIDER=fake is not allowed in prod")

    if settings.openai_api_mode not in _SUPPORTED_OPENAI_API_MODES:
        errors.append(
            "OPENAI_API_MODE must be one of: "
            + ", ".join(sorted(_SUPPORTED_OPENAI_API_MODES))
        )
    if settings.openai_web_search_enabled and settings.openai_api_mode != "responses":
        errors.append("OPENAI_WEB_SEARCH_ENABLED requires OPENAI_API_MODE=responses")

    if settings.llm_embed_provider not in _SUPPORTED_EMBED_PROVIDERS:
        errors.append(f"unsupported LLM_EMBED_PROVIDER={settings.llm_embed_provider}")
    elif settings.llm_embed_provider == "openai" and not settings.openai_api_key:
        errors.append("OPENAI_API_KEY is required when LLM_EMBED_PROVIDER=openai")
    elif (
        settings.is_prod
        and settings.knowledge_features_enabled
        and settings.llm_embed_provider == "fake"
    ):
        errors.append("LLM_EMBED_PROVIDER=fake is not allowed in prod")

    return errors


def _resolve_model(req: ChatRequest, settings: Settings) -> str:
    if req.model:
        return req.model
    attr = _TIER_ATTR.get(req.model_tier, "llm_model_tier2")
    return getattr(settings, attr)


def _estimate_input_tokens(req: ChatRequest) -> int:
    """Cheap up-front token estimator used for quota reservation."""
    total = 0
    for m in req.messages:
        total += max(1, len(m.content or "") // 4)
        total += 1000 * len(m.attachments or [])
    if req.system:
        total += max(1, len(req.system) // 4)
    return max(total, 1)


def _estimate_embed_tokens(req: EmbedRequest) -> int:
    return max(1, sum(max(1, len(t) // 4) for t in req.texts))


@dataclass
class _CircuitBreaker:
    """Tiny circuit breaker.

    Opens after ``threshold`` consecutive upstream failures and stays open for
    ``cooldown`` seconds. While open, calls raise :class:`UpstreamUnavailable`
    without hitting the provider.
    """

    threshold: int = 5
    cooldown: float = 30.0
    failures: int = 0
    opened_at: float = 0.0

    def allow(self) -> bool:
        if self.opened_at == 0.0:
            return True
        if time.monotonic() - self.opened_at >= self.cooldown:
            # Half-open: clear state and let the next call through.
            self.opened_at = 0.0
            self.failures = 0
            return True
        return False

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = 0.0

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()


class LLMService:
    """Orchestrator in front of chat + embedding providers."""

    def __init__(
        self,
        chat_provider: LLMProvider,
        embed_provider: LLMProvider,
        quota: QuotaTracker,
        settings: Settings,
    ) -> None:
        self._chat = chat_provider
        self._embed = embed_provider
        self._quota = quota
        self._settings = settings
        self._chat_breaker = _CircuitBreaker()
        self._embed_breaker = _CircuitBreaker()

    # ----------------------------------------------------------------- chat
    async def chat(self, request: ChatRequest) -> ChatResponse:
        model = _resolve_model(request, self._settings)
        provider_name = getattr(self._chat, "name", "unknown")

        if not self._chat_breaker.allow():
            LLM_REQUESTS.labels(provider=provider_name, model=model, result="breaker_open").inc()
            raise UpstreamUnavailable("llm chat circuit breaker is open")

        estimate = _estimate_input_tokens(request)
        await self._quota.reserve_tokens(request.tenant_id, estimate)

        effective_req = (
            request.model_copy(update={"model": model}) if request.model is None else request
        )
        started = time.monotonic()
        try:
            resp = await self._chat.chat(effective_req)
        except asyncio.CancelledError:
            LLM_REQUESTS.labels(provider=provider_name, model=model, result="cancelled").inc()
            await asyncio.shield(
                self._quota.commit(request.tenant_id, actual=0, estimate=estimate)
            )
            raise
        except UpstreamUnavailable:
            self._chat_breaker.record_failure()
            LLM_REQUESTS.labels(provider=provider_name, model=model, result="error").inc()
            # Refund the reservation since the call did not land.
            await self._quota.commit(request.tenant_id, actual=0, estimate=estimate)
            raise
        except Exception:
            self._chat_breaker.record_failure()
            LLM_REQUESTS.labels(provider=provider_name, model=model, result="error").inc()
            await self._quota.commit(request.tenant_id, actual=0, estimate=estimate)
            raise
        finally:
            LLM_LATENCY.labels(provider=provider_name, model=model).observe(
                time.monotonic() - started
            )

        self._chat_breaker.record_success()

        # Reconcile quota using actual total tokens.
        actual = int(resp.usage.input_tokens or 0) + int(resp.usage.output_tokens or 0)
        await self._quota.commit(request.tenant_id, actual=actual, estimate=estimate)

        # Cost accounting.
        cost = compute_cost(resp.model or model, resp.usage.input_tokens, resp.usage.output_tokens)
        resp.usage.cost_usd = cost
        if cost > 0:
            LLM_COST_USD.labels(tenant=request.tenant_id, model=resp.model or model).inc(cost)

        # Metrics.
        LLM_REQUESTS.labels(provider=provider_name, model=resp.model or model, result="ok").inc()
        if resp.usage.input_tokens:
            LLM_TOKENS.labels(
                provider=provider_name, model=resp.model or model, kind="input"
            ).inc(resp.usage.input_tokens)
        if resp.usage.output_tokens:
            LLM_TOKENS.labels(
                provider=provider_name, model=resp.model or model, kind="output"
            ).inc(resp.usage.output_tokens)
        if resp.usage.cache_read_tokens:
            LLM_TOKENS.labels(
                provider=provider_name, model=resp.model or model, kind="cache_read"
            ).inc(resp.usage.cache_read_tokens)
        if resp.usage.cache_write_tokens:
            LLM_TOKENS.labels(
                provider=provider_name, model=resp.model or model, kind="cache_write"
            ).inc(resp.usage.cache_write_tokens)

        return resp

    # -------------------------------------------------------------- streaming
    def stream_chat(self, request: ChatRequest) -> AsyncIterator[str]:
        return _service_stream(self, request)

    # ---------------------------------------------------------------- embed
    async def embed(self, request: EmbedRequest) -> EmbedResponse:
        provider_name = getattr(self._embed, "name", "unknown")
        model = request.model or self._settings.llm_embed_model

        if not self._embed_breaker.allow():
            LLM_REQUESTS.labels(
                provider=provider_name, model=model, result="breaker_open"
            ).inc()
            raise UpstreamUnavailable("llm embed circuit breaker is open")

        estimate = _estimate_embed_tokens(request)
        await self._quota.reserve_tokens(request.tenant_id, estimate)

        started = time.monotonic()
        try:
            effective_req = (
                request if request.model else EmbedRequest(
                    tenant_id=request.tenant_id,
                    trace_id=request.trace_id,
                    model=model,
                    texts=list(request.texts),
                )
            )
            resp = await self._embed.embed(effective_req)
        except asyncio.CancelledError:
            LLM_REQUESTS.labels(provider=provider_name, model=model, result="cancelled").inc()
            await asyncio.shield(
                self._quota.commit(request.tenant_id, actual=0, estimate=estimate)
            )
            raise
        except Exception:
            self._embed_breaker.record_failure()
            LLM_REQUESTS.labels(provider=provider_name, model=model, result="error").inc()
            await self._quota.commit(request.tenant_id, actual=0, estimate=estimate)
            raise
        finally:
            LLM_LATENCY.labels(provider=provider_name, model=model).observe(
                time.monotonic() - started
            )

        self._embed_breaker.record_success()

        actual = int(resp.input_tokens or 0)
        await self._quota.commit(request.tenant_id, actual=actual, estimate=estimate)

        cost = compute_cost(resp.model or model, resp.input_tokens, 0)
        if cost > 0:
            LLM_COST_USD.labels(tenant=request.tenant_id, model=resp.model or model).inc(cost)

        LLM_REQUESTS.labels(provider=provider_name, model=resp.model or model, result="ok").inc()
        if resp.input_tokens:
            LLM_TOKENS.labels(
                provider=provider_name, model=resp.model or model, kind="input"
            ).inc(resp.input_tokens)

        return resp

    async def close(self) -> None:
        for provider in {self._chat, self._embed}:
            close = getattr(provider, "close", None)
            if close is None:
                continue
            try:
                result = close()
                if hasattr(result, "__await__"):
                    await result
            except Exception:
                logger.debug("llm.service.close_failed", exc_info=True)


async def _service_stream(service: LLMService, request: ChatRequest) -> AsyncIterator[str]:
    provider = service._chat
    stream = provider.stream_chat(request)
    async for chunk in stream:
        yield chunk


# ----------------------------------------------------------------------- factory
def _build_chat_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
        from app.llm.providers.openai_provider import OpenAIProvider

        return OpenAIProvider(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            api_mode=settings.openai_api_mode,
            settings=settings,
        )
    if settings.llm_provider == "fake":
        return FakeProvider()
    raise ValueError(f"unsupported chat provider: {settings.llm_provider}")


def _build_embed_provider(settings: Settings) -> LLMProvider:
    if settings.llm_embed_provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required when LLM_EMBED_PROVIDER=openai")
        from app.llm.providers.openai_provider import OpenAIProvider

        return OpenAIProvider(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            settings=settings,
        )
    if settings.llm_embed_provider == "fake":
        return FakeProvider()
    raise ValueError(f"unsupported embed provider: {settings.llm_embed_provider}")


def _build_quota(settings: Settings, redis_client: Any | None) -> QuotaTracker:
    if redis_client is None:
        from app.infra.redis_client import get_redis

        redis_client = get_redis()
    return QuotaTracker(
        redis_client,
        default_daily_tokens=settings.tenant_default_daily_tokens,
    )


def build_llm_service(
    settings: Settings | None = None,
    *,
    redis_client: Any | None = None,
    chat_provider: LLMProvider | None = None,
    embed_provider: LLMProvider | None = None,
    quota: QuotaTracker | None = None,
) -> LLMService:
    """Factory that wires the correct providers from :class:`Settings`.

    Optional keyword arguments allow dependency injection in tests.
    """
    settings = settings or get_settings()
    config_errors = validate_llm_settings(settings)
    if config_errors:
        raise RuntimeError(f"invalid llm configuration: {'; '.join(config_errors)}")
    chat = chat_provider or _build_chat_provider(settings)
    embed = embed_provider or _build_embed_provider(settings)
    if settings.is_prod:
        if isinstance(chat, FakeProvider):
            raise RuntimeError("chat provider FakeProvider is not allowed in prod")
        if settings.knowledge_features_enabled and isinstance(embed, FakeProvider):
            raise RuntimeError("embed provider FakeProvider is not allowed in prod")
    quota_tracker = quota or _build_quota(settings, redis_client)
    return LLMService(
        chat_provider=chat,
        embed_provider=embed,
        quota=quota_tracker,
        settings=settings,
    )
