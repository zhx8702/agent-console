from __future__ import annotations

import pytest

from app.common.types import (
    CapabilityResult,
    Channel,
    Citation,
    ReplyType,
    RouteType,
    Session,
    SessionState,
)
from app.postprocessing.processor import build_postprocessor


def _session(pii: dict[str, str] | None = None) -> Session:
    return Session(
        session_id="se_test00000000000001",
        tenant_id="demo",
        user_id="u1",
        channel=Channel.WEB,
        state=SessionState.CHATTING,
        pii_map=pii or {},
    )


@pytest.mark.asyncio
async def test_postprocessor_restores_pii():
    post = build_postprocessor()
    session = _session({"<PII:phone:1>": "13800138000"})
    result = CapabilityResult(
        route=RouteType.LLM,
        reply_text="我们已联系 <PII:phone:1>",
    )
    reply = await post.run(result, session)
    assert reply.primary_text == "我们已联系 13800138000"
    assert reply.type == ReplyType.TEXT
    assert reply.tenant_id == "demo"
    assert reply.session_id == session.session_id


@pytest.mark.asyncio
async def test_postprocessor_restores_placeholders_longest_first():
    post = build_postprocessor()
    session = _session(
        {
            "<PII:phone:1>": "111",
            "<PII:phone:10>": "2222",
        }
    )
    result = CapabilityResult(
        route=RouteType.LLM,
        reply_text="a <PII:phone:10> b <PII:phone:1> c",
    )
    reply = await post.run(result, session)
    assert reply.primary_text == "a 2222 b 111 c"


@pytest.mark.asyncio
async def test_postprocessor_formats_citations_and_marks_markdown():
    post = build_postprocessor()
    session = _session()
    cites = [
        Citation(id="c1", title="退款政策", url="https://example.com/refund"),
        Citation(id="c2", title="FAQ", source="kb"),
    ]
    result = CapabilityResult(
        route=RouteType.RAG,
        reply_text="您可以在 7 日内申请退款。",
        citations=cites,
    )
    reply = await post.run(result, session)
    assert reply.type == ReplyType.MARKDOWN
    assert len(reply.citations) == 2
    assert "参考资料" in reply.primary_text
    assert "[1] 退款政策" in reply.primary_text
    assert "https://example.com/refund" in reply.primary_text
    assert "[2] FAQ" in reply.primary_text


@pytest.mark.asyncio
async def test_postprocessor_synthesizes_web_search_output_without_raw_sources():
    post = build_postprocessor()
    session = _session()
    citation = Citation(
        id="grok_web:1",
        source="grok_web_search",
        title="xAI docs",
        url="https://docs.x.ai/developers/tools/web-search",
    )
    result = CapabilityResult(
        route=RouteType.LLM,
        reply_text=(
            "Grok's tool is `web_search`. [[1]](https://docs.x.ai/developers/tools/web-search)\n\n"
            "Source: https://docs.x.ai/developers/tools/web-search\n"
            "参考资料：\n"
            "[1] xAI docs - https://docs.x.ai/developers/tools/web-search"
        ),
        citations=[citation],
    )

    reply = await post.run(result, session)

    assert reply.type == ReplyType.TEXT
    assert reply.primary_text == "Grok's tool is `web_search`."
    assert "[[1]]" not in reply.primary_text
    assert "参考资料" not in reply.primary_text
    assert "https://" not in reply.primary_text
    assert reply.citations == [citation]


@pytest.mark.asyncio
async def test_postprocessor_hides_openai_web_search_sources_too():
    post = build_postprocessor()
    session = _session()
    result = CapabilityResult(
        route=RouteType.LLM,
        reply_text="结论：可以。\n\nSources:\n[1] https://example.com",
        citations=[
            Citation(
                id="openai_web:1",
                source="openai_web_search",
                url="https://example.com",
            )
        ],
    )

    reply = await post.run(result, session)

    assert reply.primary_text == "结论：可以。"
    assert reply.type == ReplyType.TEXT


@pytest.mark.asyncio
async def test_postprocessor_truncates_long_text():
    post = build_postprocessor()
    session = _session()
    long_text = "a" * 5000
    result = CapabilityResult(route=RouteType.LLM, reply_text=long_text)
    reply = await post.run(result, session)
    assert len(reply.primary_text) == 4000
    assert reply.primary_text.endswith("\u2026")


@pytest.mark.asyncio
async def test_postprocessor_strips_leaked_placeholders():
    post = build_postprocessor()
    session = _session({"<PII:phone:1>": "13800138000"})
    # placeholder 2 is not in the session's pii_map -> must be stripped
    result = CapabilityResult(
        route=RouteType.LLM,
        reply_text="hi <PII:phone:1> and <PII:phone:2>",
    )
    reply = await post.run(result, session)
    assert "<PII:" not in reply.primary_text
    assert "13800138000" in reply.primary_text
    assert "[敏感信息]" in reply.primary_text


@pytest.mark.asyncio
async def test_postprocessor_metadata_includes_route_and_tool_calls():
    from app.common.types import ToolCall

    post = build_postprocessor()
    session = _session()
    result = CapabilityResult(
        route=RouteType.AGENT,
        reply_text="done",
        tool_calls=[ToolCall(id="t1", name="lookup_order")],
    )
    reply = await post.run(result, session)
    assert reply.metadata["route"] == "agent"
    assert reply.metadata["tool_calls"] == [
        {"name": "lookup_order", "id": "t1", "error": None}
    ]


@pytest.mark.asyncio
async def test_postprocessor_supports_structured_reply_segments() -> None:
    post = build_postprocessor()
    session = _session({"<PII:name:1>": "张三"})
    result = CapabilityResult(
        route=RouteType.AGENT,
        reply_text="fallback",
        metadata={
            "reply_segments": [
                {"type": "text", "content": "你好，<PII:name:1>"},
                {
                    "type": "text",
                    "content": "",
                    "metadata": {
                        "wxbot_msg_type": "image",
                        "image_path": "images/demo.png",
                    },
                },
            ]
        },
    )

    reply = await post.run(result, session)

    assert reply.type == ReplyType.MULTI
    assert len(reply.segments) == 2
    assert reply.segments[0].content == "你好，张三"
    assert reply.segments[1].metadata["wxbot_msg_type"] == "image"
    assert reply.segments[1].metadata["image_path"] == "images/demo.png"


@pytest.mark.asyncio
async def test_postprocessor_appends_citations_to_first_text_segment_for_structured_reply() -> None:
    post = build_postprocessor()
    session = _session()
    result = CapabilityResult(
        route=RouteType.RAG,
        reply_text="fallback",
        citations=[Citation(id="c1", title="帮助中心", url="https://example.com/help")],
        metadata={
            "reply_segments": [
                {
                    "type": "text",
                    "content": "先给结论",
                },
                {
                    "type": "text",
                    "content": "",
                    "metadata": {
                        "wxbot_msg_type": "image",
                        "image_path": "images/demo.png",
                    },
                },
            ]
        },
    )

    reply = await post.run(result, session)

    assert reply.type == ReplyType.MULTI
    assert reply.segments[0].type == ReplyType.MARKDOWN
    assert "参考资料" in reply.segments[0].content
    assert "https://example.com/help" in reply.segments[0].content
    assert reply.segments[1].metadata["wxbot_msg_type"] == "image"
