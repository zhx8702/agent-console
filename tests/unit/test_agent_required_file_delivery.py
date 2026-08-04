from __future__ import annotations

from typing import Any

import pytest

from app.agent.engine import AgentCapabilityEngine
from app.agent.registry import AgentToolDefinition, AgentToolRegistry
from app.agent.scopes import FILE_ANALYSIS_SCOPE
from app.common.config import Settings
from app.common.types import (
    Channel,
    ChatResponse,
    PreprocessedMessage,
    Role,
    Session,
    Turn,
)


class _TextOnlyLLM:
    def __init__(self, content: str) -> None:
        self.content = content
        self.requests = []

    async def chat(self, request):
        self.requests.append(request)
        return ChatResponse(content=self.content, model="fake-agent")


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
            content="搜一下今天热点新闻，整理成 TXT 文件发给我",
            trace_id="trace-private-file",
        )
    ]
    return session


def _preprocessed() -> PreprocessedMessage:
    text = "搜一下今天热点新闻，整理成 TXT 文件发给我"
    return PreprocessedMessage(original_text=text, cleaned_text=text)


def _required_effect() -> dict[str, str]:
    return {
        "type": "outbound_file",
        "scope": FILE_ANALYSIS_SCOPE,
        "tool": "generate_text_file",
        "operation": "generate",
        "format": "txt",
    }


def _engine(llm: _TextOnlyLLM, handler: _GenerateFileHandler) -> AgentCapabilityEngine:
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
