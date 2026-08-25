from __future__ import annotations

import asyncio

import pytest

from app.common.config import Settings, get_settings
from app.common.exceptions import QuotaExceeded
from app.common.types import ChatMessage, ChatRequest, Role
from app.infra.metrics import LLM_COST_USD, LLM_REQUESTS, LLM_TOKENS
from app.llm.providers.fake_provider import FakeProvider
from app.llm.quota import QuotaTracker
from app.llm.service import LLMService, build_llm_service, validate_llm_settings


class _InMemoryRedis:
    """Minimal async Redis stub covering what QuotaTracker needs."""

    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    async def incrby(self, key: str, amount: int) -> int:
        self.store[key] = int(self.store.get(key, 0)) + int(amount)
        return self.store[key]

    async def decrby(self, key: str, amount: int) -> int:
        self.store[key] = int(self.store.get(key, 0)) - int(amount)
        return self.store[key]

    async def expire(self, key: str, seconds: int) -> bool:
        self.ttls[key] = int(seconds)
        return True

    async def ttl(self, key: str) -> int:
        if key not in self.store:
            return -2
        return self.ttls.get(key, -1)

    async def get(self, key: str) -> str | None:
        if key not in self.store:
            return None
        return str(self.store[key])


class _StubProvider:
    name = "stub"

    async def chat(self, request):  # pragma: no cover - not used in these tests
        raise AssertionError("chat should not be called")

    def stream_chat(self, request):  # pragma: no cover - not used in these tests
        raise AssertionError("stream_chat should not be called")

    async def embed(self, request):  # pragma: no cover - not used in these tests
        raise AssertionError("embed should not be called")


def _make_request(text: str = "hi", tenant: str = "demo") -> ChatRequest:
    return ChatRequest(
        tenant_id=tenant,
        trace_id="trace-x",
        messages=[ChatMessage(role=Role.USER, content=text)],
    )


def _metric_total(metric, **labels) -> float:
    # Prometheus sample retrieval helper
    m = metric.labels(**labels)
    return float(m._value.get())  # type: ignore[attr-defined]


@pytest.fixture
def service() -> LLMService:
    settings = get_settings()
    redis = _InMemoryRedis()
    quota = QuotaTracker(redis, default_daily_tokens=10_000_000)
    return LLMService(
        chat_provider=FakeProvider(),
        embed_provider=FakeProvider(),
        quota=quota,
        settings=settings,
    )


@pytest.mark.asyncio
async def test_chat_records_metrics_and_usage(service: LLMService) -> None:
    settings = get_settings()
    expected_model = settings.llm_model_tier2
    req = _make_request("你好")

    before = _metric_total(LLM_REQUESTS, provider="fake", model=expected_model, result="ok")
    resp = await service.chat(req)
    after = _metric_total(LLM_REQUESTS, provider="fake", model=expected_model, result="ok")

    assert resp.content.startswith("[fake] 你说了: ")
    assert resp.model == expected_model
    assert resp.usage.input_tokens >= 1
    assert resp.usage.output_tokens >= 1
    assert after - before == pytest.approx(1.0)

    # Tokens metric got incremented for input + output
    in_tokens = _metric_total(LLM_TOKENS, provider="fake", model=expected_model, kind="input")
    out_tokens = _metric_total(LLM_TOKENS, provider="fake", model=expected_model, kind="output")
    assert in_tokens >= resp.usage.input_tokens
    assert out_tokens >= resp.usage.output_tokens


@pytest.mark.asyncio
async def test_chat_cost_computed_for_priced_model(service: LLMService) -> None:
    req = _make_request("hello world priced")
    req.model = "claude-sonnet-4-6"  # priced at (3, 15) per MTok
    before = _metric_total(LLM_COST_USD, tenant="demo", model="claude-sonnet-4-6")
    resp = await service.chat(req)
    after = _metric_total(LLM_COST_USD, tenant="demo", model="claude-sonnet-4-6")

    assert resp.usage.cost_usd > 0.0
    assert after - before == pytest.approx(resp.usage.cost_usd, rel=1e-9, abs=1e-12)


@pytest.mark.asyncio
async def test_chat_quota_exceeded_raises() -> None:
    settings = get_settings()
    redis = _InMemoryRedis()
    # Tiny cap so a single request blows through.
    quota = QuotaTracker(redis, default_daily_tokens=1)
    svc = LLMService(
        chat_provider=FakeProvider(),
        embed_provider=FakeProvider(),
        quota=quota,
        settings=settings,
    )
    with pytest.raises(QuotaExceeded):
        await svc.chat(_make_request("some fairly long input to exceed 1 token"))


@pytest.mark.asyncio
async def test_chat_quota_disabled_allows_request_over_limit() -> None:
    settings = get_settings()
    redis = _InMemoryRedis()
    quota = QuotaTracker(redis, default_daily_tokens=0)
    svc = LLMService(
        chat_provider=FakeProvider(),
        embed_provider=FakeProvider(),
        quota=quota,
        settings=settings,
    )

    resp = await svc.chat(_make_request("some fairly long input that would exceed 1 token"))

    assert resp.content.startswith("[fake]")
    assert quota.limit_for("demo") == 0


@pytest.mark.asyncio
async def test_embed_flow_increments_tokens(service: LLMService) -> None:
    from app.llm.base import EmbedRequest

    req = EmbedRequest(
        tenant_id="demo", trace_id="t", model="fake-embed", texts=["a", "b"]
    )
    resp = await service.embed(req)
    assert len(resp.vectors) == 2
    assert resp.input_tokens >= 1


def test_build_llm_service_uses_fake_when_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Override the redis builder so no real connection is attempted.
    redis = _InMemoryRedis()
    svc = build_llm_service(redis_client=redis)
    assert isinstance(svc, LLMService)
    # With LLM_PROVIDER=fake (default in conftest), chat provider should be FakeProvider.
    assert svc._chat.__class__.__name__ == "FakeProvider"  # type: ignore[attr-defined]
    assert svc._embed.__class__.__name__ == "FakeProvider"  # type: ignore[attr-defined]


def test_build_llm_service_uses_settings_quota_limit() -> None:
    settings = Settings(_env_file=None, tenant_default_daily_tokens=4321)

    svc = build_llm_service(settings=settings, redis_client=_InMemoryRedis())

    assert svc._quota.limit_for("demo") == 4321  # type: ignore[attr-defined]


def test_validate_llm_settings_rejects_fake_in_prod() -> None:
    settings = get_settings().model_copy(
        update={
            "app_env": "prod",
            "llm_provider": "fake",
            "llm_embed_provider": "fake",
            "outbound_hmac_secret": "prod_secret",
            "admin_bearer_token": "prod_admin_token",
            "tenant_demo_secret": "prod_tenant_secret",
        }
    )

    errors = validate_llm_settings(settings)

    assert "LLM_PROVIDER=fake is not allowed in prod" in errors
    assert "LLM_EMBED_PROVIDER=fake is not allowed in prod" in errors


def test_validate_llm_settings_requires_openai_key() -> None:
    settings = get_settings().model_copy(
        update={
            "llm_provider": "openai",
            "llm_embed_provider": "openai",
            "openai_api_key": None,
        }
    )

    errors = validate_llm_settings(settings)

    assert "OPENAI_API_KEY is required when LLM_PROVIDER=openai" in errors
    assert "OPENAI_API_KEY is required when LLM_EMBED_PROVIDER=openai" in errors


def test_validate_llm_settings_rejects_invalid_web_search_configuration() -> None:
    settings = get_settings().model_copy(
        update={
            "llm_provider": "fake",
            "openai_web_search_enabled": True,
            "openai_api_mode": "chat",
            "openai_web_search_tool": "unknown_search",
        }
    )

    errors = validate_llm_settings(settings)

    assert "OPENAI_WEB_SEARCH_ENABLED requires LLM_PROVIDER=openai" in errors
    assert "OPENAI_WEB_SEARCH_ENABLED requires OPENAI_API_MODE=responses" in errors
    assert (
        "OPENAI_WEB_SEARCH_TOOL must be one of: web_search, web_search_preview"
        in errors
    )


def test_validate_llm_settings_accepts_xai_search_tools() -> None:
    settings = get_settings().model_copy(
        update={
            "llm_provider": "openai",
            "openai_api_key": "xai-test",
            "openai_base_url": "https://api.x.ai/v1",
            "openai_api_mode": "responses",
            "openai_web_search_enabled": True,
            "openai_web_search_tool": "x_search",
        }
    )

    errors = validate_llm_settings(settings)

    assert errors == []


def test_build_llm_service_rejects_fake_providers_in_prod() -> None:
    redis = _InMemoryRedis()
    settings = get_settings().model_copy(
        update={
            "app_env": "prod",
            "llm_provider": "openai",
            "openai_api_key": "sk-test",
            "llm_embed_provider": "fake",
            "outbound_hmac_secret": "prod_secret",
            "admin_bearer_token": "prod_admin_token",
            "tenant_demo_secret": "prod_tenant_secret",
        }
    )

    with pytest.raises(RuntimeError, match="LLM_EMBED_PROVIDER=fake is not allowed in prod"):
        build_llm_service(
            settings=settings,
            redis_client=redis,
            chat_provider=FakeProvider(),
            embed_provider=FakeProvider(),
        )


def test_build_llm_service_allows_fake_embed_in_prod_when_knowledge_disabled() -> None:
    redis = _InMemoryRedis()
    settings = get_settings().model_copy(
        update={
            "app_env": "prod",
            "llm_provider": "openai",
            "openai_api_key": "sk-test",
            "llm_embed_provider": "fake",
            "knowledge_features_enabled": False,
            "outbound_hmac_secret": "prod_secret",
            "admin_bearer_token": "prod_admin_token",
            "tenant_demo_secret": "prod_tenant_secret",
        }
    )

    svc = build_llm_service(
        settings=settings,
        redis_client=redis,
        chat_provider=_StubProvider(),
        embed_provider=FakeProvider(),
    )

    assert isinstance(svc, LLMService)


def test_build_llm_service_accepts_openai_with_injected_providers() -> None:
    redis = _InMemoryRedis()
    settings = get_settings().model_copy(
        update={
            "llm_provider": "openai",
            "llm_embed_provider": "openai",
            "openai_api_key": "sk-test",
            "openai_base_url": "https://api.openai.com/v1",
        }
    )

    svc = build_llm_service(
        settings=settings,
        redis_client=redis,
        chat_provider=_StubProvider(),
        embed_provider=_StubProvider(),
    )

    assert isinstance(svc, LLMService)


@pytest.mark.asyncio
async def test_quota_commit_reconciles_actual_usage() -> None:
    settings = get_settings()
    redis = _InMemoryRedis()
    quota = QuotaTracker(redis, default_daily_tokens=1_000_000)
    svc = LLMService(
        chat_provider=FakeProvider(),
        embed_provider=FakeProvider(),
        quota=quota,
        settings=settings,
    )
    await svc.chat(_make_request("你好"))
    # After request, usage counter should equal actual input + output tokens.
    total = await quota.usage("demo")
    assert total >= 1


@pytest.mark.asyncio
async def test_cancelled_chat_refunds_reserved_quota_without_opening_breaker() -> None:
    class BlockingProvider(_StubProvider):
        name = "blocking"

        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def chat(self, request):
            _ = request
            self.started.set()
            await asyncio.Event().wait()

    settings = get_settings()
    redis = _InMemoryRedis()
    quota = QuotaTracker(redis, default_daily_tokens=1_000_000)
    provider = BlockingProvider()
    svc = LLMService(
        chat_provider=provider,
        embed_provider=FakeProvider(),
        quota=quota,
        settings=settings,
    )

    task = asyncio.create_task(svc.chat(_make_request("a message with reserved tokens")))
    await asyncio.wait_for(provider.started.wait(), timeout=1)
    assert await quota.usage("demo") > 0

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await quota.usage("demo") == 0
    assert svc._chat_breaker.failures == 0  # type: ignore[attr-defined]
