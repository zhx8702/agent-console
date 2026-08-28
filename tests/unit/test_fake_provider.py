from __future__ import annotations

import math

import pytest

from app.common.types import ChatMessage, ChatRequest, Role, ToolSchema
from app.llm.base import EmbedRequest
from app.llm.providers.fake_provider import FakeProvider


def _req(text: str, *, tools: list[ToolSchema] | None = None) -> ChatRequest:
    return ChatRequest(
        tenant_id="demo",
        trace_id="trace-1",
        messages=[ChatMessage(role=Role.USER, content=text)],
        tools=tools or [],
    )


@pytest.mark.asyncio
async def test_fake_chat_classifies_handoff_for_intent_route() -> None:
    provider = FakeProvider()
    resp = await provider.chat(
        ChatRequest(
            tenant_id="demo",
            trace_id="trace-classify",
            messages=[ChatMessage(role=Role.USER, content="转人工")],
            metadata={"route": "intent_classify"},
        )
    )
    assert '"domain": "handoff"' in resp.content
    assert '"action": "request"' in resp.content


@pytest.mark.asyncio
async def test_fake_chat_canned_reply() -> None:
    provider = FakeProvider()
    resp = await provider.chat(_req("你好"))
    assert resp.content == "[fake] 你说了: 你好"
    assert resp.tool_calls == []
    assert resp.latency_ms == 5
    assert resp.usage.input_tokens >= 1
    assert resp.usage.output_tokens >= 1
    assert resp.finish_reason == "stop"


@pytest.mark.asyncio
async def test_fake_chat_tool_call_when_order_present() -> None:
    tools = [ToolSchema(name="query_order", description="q", parameters={"type": "object"})]
    provider = FakeProvider()
    resp = await provider.chat(_req("帮我查订单 ORD-12345 的状态", tools=tools))
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.name == "query_order"
    assert tc.arguments == {"order_id": "ORD-12345"}
    assert resp.finish_reason == "tool_use"


@pytest.mark.asyncio
async def test_fake_chat_no_tool_call_without_match() -> None:
    tools = [ToolSchema(name="query_order", description="q", parameters={"type": "object"})]
    provider = FakeProvider()
    resp = await provider.chat(_req("你好", tools=tools))
    assert resp.tool_calls == []
    # and without tools at all
    resp2 = await provider.chat(_req("查订单 ORD-1"))
    assert resp2.tool_calls == []


@pytest.mark.asyncio
async def test_fake_stream_chat_yields_chunks() -> None:
    provider = FakeProvider()
    chunks: list[str] = []
    async for chunk in provider.stream_chat(_req("abc")):
        chunks.append(chunk)
    assert len(chunks) >= 1
    assert all(chunks)  # non-empty chunks
    assert "".join(chunks) == "[fake] 你说了: abc"


@pytest.mark.asyncio
async def test_fake_embed_unit_length_and_deterministic() -> None:
    provider = FakeProvider()
    req = EmbedRequest(
        tenant_id="demo",
        trace_id="trace-2",
        model="fake-embed",
        texts=["hello", "world", "hello"],
    )
    resp1 = await provider.embed(req)
    resp2 = await provider.embed(req)

    assert len(resp1.vectors) == 3
    assert all(len(v) == 64 for v in resp1.vectors)
    for vec in resp1.vectors:
        norm = math.sqrt(sum(x * x for x in vec))
        assert norm == pytest.approx(1.0, abs=1e-6)

    # Deterministic: identical input -> identical vectors across calls AND within.
    assert resp1.vectors == resp2.vectors
    assert resp1.vectors[0] == resp1.vectors[2]  # same text
    assert resp1.vectors[0] != resp1.vectors[1]  # different text

    assert resp1.model == "fake-embed"
    assert resp1.input_tokens >= 1


@pytest.mark.asyncio
async def test_fake_close_is_noop() -> None:
    await FakeProvider().close()
