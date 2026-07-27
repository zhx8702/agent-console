from __future__ import annotations

import os

import pytest

from app.common.config import get_settings
from app.common.types import ChatMessage, ChatRequest, Role

pytestmark = pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY"),
    reason="no api key; live anthropic test skipped",
)


@pytest.mark.asyncio
async def test_anthropic_live_small_call() -> None:
    from app.llm.providers.anthropic_provider import AnthropicProvider

    settings = get_settings()
    provider = AnthropicProvider(
        api_key=os.environ["ANTHROPIC_API_KEY"], settings=settings
    )
    try:
        resp = await provider.chat(
            ChatRequest(
                tenant_id="demo",
                trace_id="live-1",
                model_tier="tier-3",
                messages=[ChatMessage(role=Role.USER, content="Say 'hi' and nothing else.")],
                max_tokens=4,
                temperature=0.0,
                cache_system=False,
            )
        )
        assert resp.model
        assert resp.usage.input_tokens >= 0
    finally:
        await provider.close()
