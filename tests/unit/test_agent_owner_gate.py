from __future__ import annotations

import asyncio

import pytest

from app.agent.engine import AgentCapabilityEngine
from app.agent.registry import AgentToolDefinition, AgentToolRegistry
from app.common.config import Settings
from app.common.types import (
    Channel,
    ChatResponse,
    ChatUsage,
    PreprocessedMessage,
    Session,
    ToolCall,
)


class _ToolCallingLLM:
    def __init__(self) -> None:
        self.requests = []

    async def chat(self, request):
        self.requests.append(request)
        if request.tools and len(self.requests) == 1:
            return ChatResponse(
                tool_calls=[ToolCall(id="call-1", name="plugin_tool", arguments={})],
                model="fake",
                finish_reason="tool_use",
                usage=ChatUsage(),
            )
        return ChatResponse(
            content="fallback response",
            model="fake",
            finish_reason="stop",
            usage=ChatUsage(),
        )


def _session() -> Session:
    return Session(
        session_id="room@chatroom",
        tenant_id="tenant-1",
        user_id="user-1",
        channel=Channel.WECHAT,
    )


def _pre() -> PreprocessedMessage:
    return PreprocessedMessage(original_text="run it", cleaned_text="run it")


def _registry(handler, *, owner: str = "plugin") -> AgentToolRegistry:
    registry = AgentToolRegistry()
    registry.register(
        AgentToolDefinition(
            scope="group_info",
            name="plugin_tool",
            description="plugin tool",
            parameters={"type": "object", "properties": {}},
            handler=handler,
        ),
        owner=owner,
    )
    return registry


def _settings() -> Settings:
    return Settings(
        agent_tools_require_explicit_policy=False,
        customer_service_prompt_enabled=False,
    )


@pytest.mark.asyncio
async def test_agent_filters_denied_plugin_tool_before_llm_exposure() -> None:
    handler_calls = 0
    gate_calls: list[tuple[str, str, str]] = []

    async def handler(session, arguments):
        nonlocal handler_calls
        _ = session, arguments
        handler_calls += 1
        return {"ok": True}

    async def deny(owner: str, session: Session) -> bool:
        gate_calls.append((owner, session.tenant_id, session.session_id))
        return False

    llm = _ToolCallingLLM()
    engine = AgentCapabilityEngine(
        llm,
        settings=_settings(),
        agent_tool_registry=_registry(handler),
        tool_owner_gate=deny,
    )

    result = await engine.answer(
        _pre(),
        _session(),
        {"agent_tool_scope": "group_info"},
    )

    assert result.reply_text == "fallback response"
    assert llm.requests[0].tools == []
    assert gate_calls == [("plugin", "tenant-1", "room@chatroom")]
    assert handler_calls == 0


@pytest.mark.asyncio
async def test_agent_hides_plugin_tools_when_owner_gate_is_missing() -> None:
    async def handler(session, arguments):
        _ = session, arguments
        raise AssertionError("ungated plugin tool must not execute")

    llm = _ToolCallingLLM()
    engine = AgentCapabilityEngine(
        llm,
        settings=_settings(),
        agent_tool_registry=_registry(handler),
        tool_owner_gate=None,
    )

    result = await engine.answer(
        _pre(),
        _session(),
        {"agent_tool_scope": "group_info"},
    )

    assert llm.requests[0].tools == []
    assert result.tool_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["error", "invalid", "timeout"])
async def test_agent_owner_gate_failures_hide_tool_fail_closed(mode: str) -> None:
    async def handler(session, arguments):
        _ = session, arguments
        raise AssertionError("hidden tool must not execute")

    async def broken_gate(owner: str, session: Session):
        _ = owner, session
        if mode == "error":
            raise RuntimeError("sensitive backend detail")
        if mode == "invalid":
            return "yes"
        await asyncio.sleep(0.1)
        return True

    llm = _ToolCallingLLM()
    engine = AgentCapabilityEngine(
        llm,
        settings=_settings(),
        agent_tool_registry=_registry(handler),
        tool_owner_gate=broken_gate,
        tool_owner_gate_timeout_seconds=0.01,
    )

    result = await engine.answer(
        _pre(),
        _session(),
        {"agent_tool_scope": "group_info"},
    )

    assert result.reply_text == "fallback response"
    assert llm.requests[0].tools == []


@pytest.mark.asyncio
async def test_agent_rechecks_owner_gate_immediately_before_tool_execution() -> None:
    handler_calls = 0
    gate_calls = 0

    async def handler(session, arguments):
        nonlocal handler_calls
        _ = session, arguments
        handler_calls += 1
        return {"ok": True}

    async def changed_state(owner: str, session: Session) -> bool:
        nonlocal gate_calls
        _ = owner, session
        gate_calls += 1
        return gate_calls == 1

    llm = _ToolCallingLLM()
    engine = AgentCapabilityEngine(
        llm,
        settings=_settings(),
        agent_tool_registry=_registry(handler),
        tool_owner_gate=changed_state,
    )

    result = await engine.answer(
        _pre(),
        _session(),
        {"agent_tool_scope": "group_info"},
    )

    assert [tool.name for tool in llm.requests[0].tools] == ["plugin_tool"]
    assert result.tool_calls[0].error == "tool_owner_execution_denied"
    assert gate_calls == 2
    assert handler_calls == 0


@pytest.mark.asyncio
async def test_agent_discards_tool_result_when_owner_disabled_in_flight() -> None:
    enabled = True
    gate_calls = 0

    async def handler(session, arguments):
        nonlocal enabled
        _ = session, arguments
        enabled = False
        return {
            "ok": True,
            "channel_reply_effects": [
                {
                    "type": "enqueue_channel_reply",
                    "owner": "wxbot",
                    "payload": {"body": {"type": "text", "text": "late"}},
                }
            ],
        }

    async def changed_state(owner: str, session: Session) -> bool:
        nonlocal gate_calls
        _ = owner, session
        gate_calls += 1
        return enabled

    llm = _ToolCallingLLM()
    engine = AgentCapabilityEngine(
        llm,
        settings=_settings(),
        agent_tool_registry=_registry(handler),
        tool_owner_gate=changed_state,
    )

    result = await engine.answer(
        _pre(),
        _session(),
        {"agent_tool_scope": "group_info"},
    )

    assert result.tool_calls[0].error == "tool_owner_execution_denied"
    assert result.tool_calls[0].result is None
    assert result.metadata["channel_reply_effects"] == []
    assert gate_calls == 3


@pytest.mark.asyncio
async def test_agent_core_tool_remains_compatible_and_bypasses_gate() -> None:
    handler_calls = 0
    gate_calls = 0

    async def handler(session, arguments):
        nonlocal handler_calls
        _ = session, arguments
        handler_calls += 1
        return {"ok": True}

    async def deny(owner: str, session: Session) -> bool:
        nonlocal gate_calls
        _ = owner, session
        gate_calls += 1
        return False

    llm = _ToolCallingLLM()
    engine = AgentCapabilityEngine(
        llm,
        settings=_settings(),
        agent_tool_registry=_registry(handler, owner=""),
        tool_owner_gate=deny,
    )

    result = await engine.answer(
        _pre(),
        _session(),
        {"agent_tool_scope": "group_info"},
    )

    assert result.tool_calls[0].error is None
    assert handler_calls == 1
    assert gate_calls == 0
