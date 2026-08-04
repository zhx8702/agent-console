from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.agent.engine import AgentCapabilityEngine
from app.agent.registry import AgentToolDefinition, AgentToolRegistry
from app.agent.scopes import FILE_ANALYSIS_SCOPE
from app.common.config import Settings
from app.common.types import (
    Channel,
    ChatResponse,
    Citation,
    PreprocessedMessage,
    Role,
    Session,
    ToolCall,
    Turn,
)


class _TextOnlyLLM:
    def __init__(
        self,
        content: str,
        *,
        citations: list[Citation] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.content = content
        self.citations = list(citations or [])
        self.error = error
        self.requests = []

    async def chat(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return ChatResponse(
            content=self.content,
            citations=list(self.citations),
            model="fake-agent",
        )


class _ToolCallingLLM:
    def __init__(self) -> None:
        self.requests = []

    async def chat(self, request):
        self.requests.append(request)
        return ChatResponse(
            model="fake-agent",
            tool_calls=[
                ToolCall(
                    id=f"file-call-{len(self.requests)}",
                    name="generate_text_file",
                    arguments={"content": "今日热点正文", "format": "txt"},
                )
            ],
        )


class _SlowLLM:
    def __init__(self) -> None:
        self.requests = []

    async def chat(self, request):
        self.requests.append(request)
        await asyncio.sleep(1)
        return ChatResponse(content="late", model="fake-agent")


class _SlowCancellationLLM:
    def __init__(self) -> None:
        self.requests = []
        self.cleanup_finished = asyncio.Event()

    async def chat(self, request):
        self.requests.append(request)
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            await asyncio.sleep(0.2)
            self.cleanup_finished.set()
            raise
        return ChatResponse(content="late", model="fake-agent")


class _GenerateFileHandler:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, _session: Session, arguments: dict[str, Any]):
        self.calls.append(dict(arguments))
        if self.fail:
            raise RuntimeError("file queue unavailable")
        return {
            "ok": True,
            "sent_to_current_session": True,
            "delivery_status": "queued",
            "self_enqueued_reply": True,
            "suppress_final_reply": True,
            "channel_reply_effects": [],
        }


def _private_session() -> Session:
    session = Session(
        session_id="wxid_private",
        tenant_id="demo",
        user_id="wxid_private",
        channel=Channel.WECHAT,
        metadata={"session_kind": "private"},
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content=(
                "联网搜索今天热点新闻，按标题、摘要、来源链接整理成 TXT 文件发给我"
            ),
            trace_id="trace-private-file",
        )
    ]
    return session


def _preprocessed() -> PreprocessedMessage:
    text = "联网搜索今天热点新闻，按标题、摘要、来源链接整理成 TXT 文件发给我"
    return PreprocessedMessage(original_text=text, cleaned_text=text)


def _required_effect(*, web_search_required: bool = False) -> dict[str, Any]:
    effect: dict[str, Any] = {
        "type": "outbound_file",
        "scope": FILE_ANALYSIS_SCOPE,
        "tool": "generate_text_file",
        "operation": "generate",
        "format": "txt",
    }
    if web_search_required:
        effect["web_search_required"] = True
    return effect


def _engine(
    llm: Any,
    handler: _GenerateFileHandler,
    *,
    web_search_enabled: bool = False,
) -> AgentCapabilityEngine:
    registry = AgentToolRegistry()
    registry.register(
        AgentToolDefinition(
            scope=FILE_ANALYSIS_SCOPE,
            name="generate_text_file",
            description="generate current answer as a file",
            parameters={
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "format": {"type": "string"},
                },
                "required": ["content"],
            },
            handler=handler,
            metadata={"channels": ["wechat"], "session_kinds": ["private"]},
        )
    )
    return AgentCapabilityEngine(
        llm,
        settings=Settings(
            customer_service_prompt_enabled=False,
            agent_tools_require_explicit_policy=False,
            llm_provider="openai" if web_search_enabled else "fake",
            openai_api_key="sk-test" if web_search_enabled else None,
            openai_api_mode="responses",
            openai_web_search_enabled=web_search_enabled,
            openai_web_search_live_enabled=web_search_enabled,
        ),
        agent_tool_registry=registry,
    )


@pytest.mark.asyncio
async def test_required_private_file_is_generated_when_model_skips_tool_call() -> None:
    llm = _TextOnlyLLM("今日热点：第一条新闻及来源链接。")
    handler = _GenerateFileHandler()
    engine = _engine(llm, handler)

    result = await engine.answer(
        _preprocessed(),
        _private_session(),
        {
            "agent_tool_scope": FILE_ANALYSIS_SCOPE,
            "agent_required_effect": _required_effect(),
        },
    )

    assert handler.calls == [
        {"content": "今日热点：第一条新闻及来源链接。", "format": "txt"}
    ]
    assert [item.name for item in result.tool_calls] == ["generate_text_file"]
    assert result.reply_text == ""
    assert result.metadata["required_effect_satisfied"] is True
    assert result.metadata["required_effect_auto_fulfilled"] is True
    assert result.metadata["required_effect_failure"] == ""
    assert result.metadata["suppress_final_reply"] is True


@pytest.mark.asyncio
async def test_plain_text_without_required_effect_does_not_send_file() -> None:
    llm = _TextOnlyLLM("普通文字回复")
    handler = _GenerateFileHandler()
    engine = _engine(llm, handler)

    result = await engine.answer(
        _preprocessed(),
        _private_session(),
        {"agent_tool_scope": FILE_ANALYSIS_SCOPE},
    )

    assert handler.calls == []
    assert result.tool_calls == []
    assert result.reply_text == "普通文字回复"
    assert "required_effect" not in result.metadata


@pytest.mark.asyncio
async def test_required_private_file_failure_never_degrades_to_success_text() -> None:
    llm = _TextOnlyLLM("我已经把热点新闻文件发给你了。")
    handler = _GenerateFileHandler(fail=True)
    engine = _engine(llm, handler)

    result = await engine.answer(
        _preprocessed(),
        _private_session(),
        {
            "agent_tool_scope": FILE_ANALYSIS_SCOPE,
            "agent_required_effect": _required_effect(),
        },
    )

    assert len(handler.calls) == 1
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].error == "file queue unavailable"
    assert result.reply_text == "这次文件没有生成或发送成功，请稍后重试。"
    assert result.metadata["required_effect_satisfied"] is False
    assert result.metadata["required_effect_auto_fulfilled"] is False
    assert result.metadata["required_effect_failure"] == "required_tool_failed"
    assert result.metadata["suppress_final_reply"] is False


@pytest.mark.asyncio
async def test_terminal_file_delivery_stops_agent_tool_rounds_immediately() -> None:
    llm = _ToolCallingLLM()
    handler = _GenerateFileHandler()
    engine = _engine(llm, handler)

    result = await engine.answer(
        _preprocessed(),
        _private_session(),
        {
            "agent_tool_scope": FILE_ANALYSIS_SCOPE,
            "agent_required_effect": _required_effect(),
        },
    )

    assert len(llm.requests) == 1
    assert len(handler.calls) == 1
    assert result.reply_text == ""
    assert result.metadata["required_effect_satisfied"] is True


@pytest.mark.asyncio
async def test_required_live_search_is_verified_before_file_generation() -> None:
    citation = Citation(
        id="news-1",
        source="openai_web_search",
        title="新闻来源",
        url="https://news.example/item-1",
    )
    llm = _TextOnlyLLM("标题：热点新闻\n摘要：今日发生的重要事件。", citations=[citation])
    handler = _GenerateFileHandler()
    engine = _engine(llm, handler, web_search_enabled=True)

    result = await engine.answer(
        _preprocessed(),
        _private_session(),
        {
            "agent_tool_scope": FILE_ANALYSIS_SCOPE,
            "agent_required_effect": _required_effect(web_search_required=True),
        },
    )

    assert len(llm.requests) == 1
    request = llm.requests[0]
    assert request.tools == []
    assert request.metadata["openai_web_search"] is True
    assert request.metadata["openai_web_search_required"] is True
    assert request.metadata["disable_openai_fallback"] is True
    assert handler.calls == [
        {
            "content": (
                "标题：热点新闻\n摘要：今日发生的重要事件。\n\n"
                "来源链接：\n1. 新闻来源\n   https://news.example/item-1"
            ),
            "format": "txt",
        }
    ]
    assert result.reply_text == ""
    assert result.citations == [citation]
    assert result.metadata["required_web_search_satisfied"] is True
    assert result.metadata["required_effect_satisfied"] is True


@pytest.mark.asyncio
async def test_required_live_search_without_citations_does_not_generate_file() -> None:
    llm = _TextOnlyLLM("看起来像今天的热点，但没有来源。")
    handler = _GenerateFileHandler()
    engine = _engine(llm, handler, web_search_enabled=True)

    result = await engine.answer(
        _preprocessed(),
        _private_session(),
        {
            "agent_tool_scope": FILE_ANALYSIS_SCOPE,
            "agent_required_effect": _required_effect(web_search_required=True),
        },
    )

    assert handler.calls == []
    assert result.reply_text == (
        "这次没有取得可验证的实时来源链接，因此没有生成文件。请稍后重试。"
    )
    assert result.metadata["required_web_search_satisfied"] is False
    assert result.metadata["required_effect_satisfied"] is False


@pytest.mark.asyncio
async def test_malformed_citation_url_does_not_satisfy_required_search() -> None:
    citation = Citation(
        id="news-malformed",
        source="openai_web_search",
        title="无主机来源",
        url="https:///missing-host",
    )
    llm = _TextOnlyLLM("不应生成文件", citations=[citation])
    handler = _GenerateFileHandler()
    engine = _engine(llm, handler, web_search_enabled=True)

    result = await engine.answer(
        _preprocessed(),
        _private_session(),
        {
            "agent_tool_scope": FILE_ANALYSIS_SCOPE,
            "agent_required_effect": _required_effect(web_search_required=True),
        },
    )

    assert handler.calls == []
    assert result.metadata["required_web_search_satisfied"] is False
    assert result.metadata["required_effect_satisfied"] is False


@pytest.mark.asyncio
async def test_incomplete_required_live_search_does_not_generate_truncated_file() -> None:
    citation = Citation(
        id="news-incomplete",
        source="openai_web_search",
        title="新闻来源",
        url="https://news.example/incomplete",
    )
    llm = _TextOnlyLLM("只有一半的热点正文", citations=[citation])
    handler = _GenerateFileHandler()
    engine = _engine(llm, handler, web_search_enabled=True)
    original_chat = llm.chat

    async def _incomplete_chat(request):
        response = await original_chat(request)
        response.finish_reason = "incomplete"
        return response

    llm.chat = _incomplete_chat

    result = await engine.answer(
        _preprocessed(),
        _private_session(),
        {
            "agent_tool_scope": FILE_ANALYSIS_SCOPE,
            "agent_required_effect": _required_effect(web_search_required=True),
        },
    )

    assert handler.calls == []
    assert result.metadata["required_web_search_satisfied"] is False
    assert result.metadata["required_effect_satisfied"] is False


@pytest.mark.asyncio
async def test_required_live_search_never_executes_unexposed_function_call() -> None:
    llm = _ToolCallingLLM()
    handler = _GenerateFileHandler()
    engine = _engine(llm, handler, web_search_enabled=True)

    result = await engine.answer(
        _preprocessed(),
        _private_session(),
        {
            "agent_tool_scope": FILE_ANALYSIS_SCOPE,
            "agent_required_effect": _required_effect(web_search_required=True),
        },
    )

    assert len(llm.requests) == 1
    assert llm.requests[0].tools == []
    assert handler.calls == []
    assert result.tool_calls == []
    assert result.reply_text == (
        "实时联网搜索暂时不可用，这次没有生成文件，请稍后重试。"
    )
    assert result.metadata["required_effect_satisfied"] is False
    assert result.metadata["required_web_search_satisfied"] is False
    assert result.metadata["required_effect_failure"] == (
        "required_web_search_invalid_response"
    )


@pytest.mark.asyncio
async def test_required_live_search_configuration_failure_is_actionable() -> None:
    llm = _TextOnlyLLM("不应调用")
    handler = _GenerateFileHandler()
    engine = _engine(llm, handler)

    result = await engine.answer(
        _preprocessed(),
        _private_session(),
        {
            "agent_tool_scope": FILE_ANALYSIS_SCOPE,
            "agent_required_effect": _required_effect(web_search_required=True),
        },
    )

    assert llm.requests == []
    assert handler.calls == []
    assert "模型配置" in result.reply_text
    assert "这次没有生成文件" in result.reply_text
    assert result.metadata["required_effect_failure"] == (
        "required_web_search_not_configured"
    )


@pytest.mark.asyncio
async def test_required_live_search_upstream_failure_is_not_generic_system_busy() -> None:
    llm = _TextOnlyLLM("", error=RuntimeError("responses unavailable"))
    handler = _GenerateFileHandler()
    engine = _engine(llm, handler, web_search_enabled=True)

    result = await engine.answer(
        _preprocessed(),
        _private_session(),
        {
            "agent_tool_scope": FILE_ANALYSIS_SCOPE,
            "agent_required_effect": _required_effect(web_search_required=True),
        },
    )

    assert handler.calls == []
    assert result.reply_text == (
        "实时联网搜索暂时不可用，这次没有生成文件，请稍后重试。"
    )
    assert result.metadata["required_effect_failure"] == "required_web_search_failed"


@pytest.mark.asyncio
async def test_required_live_search_timeout_is_not_generic_system_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.agent.engine.REQUIRED_WEB_SEARCH_TIMEOUT_SECONDS",
        0.01,
    )
    llm = _SlowLLM()
    handler = _GenerateFileHandler()
    engine = _engine(llm, handler, web_search_enabled=True)

    result = await engine.answer(
        _preprocessed(),
        _private_session(),
        {
            "agent_tool_scope": FILE_ANALYSIS_SCOPE,
            "agent_required_effect": _required_effect(web_search_required=True),
        },
    )

    assert handler.calls == []
    assert result.reply_text == (
        "实时联网搜索超时了，这次没有生成文件，请稍后重试。"
    )
    assert result.metadata["required_effect_failure"] == "required_web_search_timeout"


@pytest.mark.asyncio
async def test_required_live_search_timeout_does_not_wait_for_cancellation_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.agent.engine.REQUIRED_WEB_SEARCH_TIMEOUT_SECONDS",
        0.01,
    )
    llm = _SlowCancellationLLM()
    handler = _GenerateFileHandler()
    engine = _engine(llm, handler, web_search_enabled=True)

    result = await engine.answer(
        _preprocessed(),
        _private_session(),
        {
            "agent_tool_scope": FILE_ANALYSIS_SCOPE,
            "agent_required_effect": _required_effect(web_search_required=True),
        },
    )

    assert result.metadata["required_effect_failure"] == "required_web_search_timeout"
    assert llm.cleanup_finished.is_set() is False
    await asyncio.wait_for(llm.cleanup_finished.wait(), timeout=0.5)
