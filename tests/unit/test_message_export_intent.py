from __future__ import annotations

import pytest

from app.agent.engine import AgentCapabilityEngine
from app.agent.registry import AgentToolDefinition, AgentToolRegistry
from app.agent.scopes import MESSAGE_EXPORT_SCOPE, agent_scope_system_hint
from app.common.config import Settings
from app.common.types import (
    Channel,
    ChatResponse,
    InboundEvent,
    Message,
    Session,
)
from app.orchestrator.pipeline import PipelineContext
from app.social.contracts import (
    GroupParticipationPolicyDocument,
    KillSwitches,
    ParticipationPolicyValues,
)
from plugins.wxbot.agent_intent_hook import WxbotAgentIntentHook
from plugins.wxbot.hook_context import _message_export_requested


class _EnabledGroupFilePolicyStore:
    async def get_group_policy(
        self,
        tenant_id: str,
        session_id: str,
    ) -> GroupParticipationPolicyDocument:
        return GroupParticipationPolicyDocument(
            tenant_id=tenant_id,
            session_id=session_id,
            version=1,
            kill_switches=KillSwitches(),
            effective_enabled=True,
            policy=ParticipationPolicyValues(file_send_enabled=True),
        )


def _pipeline_context(
    text: str,
    *,
    session_id: str,
    mentioned_me: bool = False,
) -> PipelineContext:
    session = Session(
        session_id=session_id,
        tenant_id="demo",
        user_id="wxid_user",
        channel=Channel.WECHAT,
        metadata={"session_kind": ("group" if session_id.endswith("@chatroom") else "private")},
    )
    event = InboundEvent(
        message_id=f"message-{session_id}",
        tenant_id="demo",
        channel=Channel.WECHAT,
        user_id="wxid_user",
        session_id=session_id,
        message=Message(content=text),
        trace_id=f"trace-{session_id}",
        metadata={
            "mentioned_me": mentioned_me,
            "wxbot_normalized_content": text,
        },
    )
    return PipelineContext(event=event, trace_id=event.trace_id, session=session)


@pytest.mark.parametrize(
    "text",
    [
        "把今天的群消息汇总成文件发给我",
        "总结一下刚才的聊天记录，导出成 TXT 文件给我",
        "请把最近私聊消息整理一下，生成一份文档发我",
        "做个消息记录摘要，输出 CSV 文件给大家",
        "把今天的消息记录总结一下，发我一个文件",
        "把聊天记录生成文件给我",
        "把聊天记录分析成文件发我",
    ],
)
def test_message_export_intent_requires_explicit_combined_request(text: str) -> None:
    assert _message_export_requested(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "帮我汇总今天的聊天记录",
        "发个文件给我",
        "把这份文件总结一下发给我",
        "汇总一下今天的群消息，直接回复文字就行",
        "导出最近消息记录给我",
        "/汇总 今天的消息并发文件",
    ],
)
def test_message_export_intent_rejects_partial_or_unrelated_requests(text: str) -> None:
    assert _message_export_requested(text) is False


@pytest.mark.asyncio
async def test_group_export_requires_mention_and_private_export_does_not() -> None:
    hook = WxbotAgentIntentHook(
        social_policy_store=_EnabledGroupFilePolicyStore(),
    )
    mentioned_group = _pipeline_context(
        "把今天的群消息汇总成文件发给我",
        session_id="room@chatroom",
        mentioned_me=True,
    )
    unmentioned_group = _pipeline_context(
        "把今天的群消息汇总成文件发给我",
        session_id="room@chatroom",
    )
    private = _pipeline_context(
        "把今天的聊天记录汇总成文件发给我",
        session_id="wxid_private",
    )

    await hook.run(mentioned_group)
    await hook.run(unmentioned_group)
    await hook.run(private)

    assert mentioned_group.extras["agent_tool_scope"] == MESSAGE_EXPORT_SCOPE
    assert "agent_tool_scope" not in unmentioned_group.extras
    assert private.extras["agent_tool_scope"] == MESSAGE_EXPORT_SCOPE
    assert private.extras["router_signals"]["tool_intent_matched"] is True
    assert private.extras["router_signals"]["tools_available"] is True
    assert "wxbot_async_delivery_contract" not in private.extras


@pytest.mark.asyncio
async def test_group_export_detects_real_trailing_bot_mention_payload() -> None:
    hook = WxbotAgentIntentHook(
        social_policy_store=_EnabledGroupFilePolicyStore(),
    )
    ctx = _pipeline_context(
        "把今天的群消息汇总成 TXT 文件发给我 \u2005@zzz",
        session_id="room@chatroom",
        mentioned_me=True,
    )
    ctx.event.metadata.update(
        {
            "bot_addressed": True,
            "bot_normalized_content": "",
            "wxbot_normalized_content": "",
        }
    )

    await hook.run(ctx)

    assert ctx.extras["agent_tool_scope"] == MESSAGE_EXPORT_SCOPE
    assert ctx.extras["wxbot_file_intent"]["operation"] == "export_history"
    assert ctx.extras["wxbot_file_intent"]["requested_format"] == "txt"
    assert ctx.extras["wxbot_file_intent"]["delivery_required"] is True


@pytest.mark.asyncio
async def test_private_session_enables_only_combined_message_export_intent() -> None:
    hook = WxbotAgentIntentHook()
    ordinary_summary = _pipeline_context(
        "帮我汇总今天的聊天记录",
        session_id="wxid_private",
    )
    group_query = _pipeline_context(
        "群里有哪些人",
        session_id="wxid_private",
    )

    await hook.run(ordinary_summary)
    await hook.run(group_query)

    assert "agent_tool_scope" not in ordinary_summary.extras
    assert "agent_tool_scope" not in group_query.extras


async def _noop_handler(
    _session: Session,
    _arguments: dict[str, object],
) -> dict[str, object]:
    return {"ok": True}


class _CapturingLLM:
    def __init__(self) -> None:
        self.requests = []

    async def chat(self, request):
        self.requests.append(request)
        return ChatResponse(content="ok")


@pytest.mark.asyncio
async def test_private_tool_visibility_is_explicit_and_keeps_group_tools_isolated() -> None:
    registry = AgentToolRegistry()
    registry.register(
        AgentToolDefinition(
            scope=MESSAGE_EXPORT_SCOPE,
            name="export_current_messages_file",
            description="export",
            parameters={"type": "object", "properties": {}},
            handler=_noop_handler,
            metadata={
                "channels": ["wechat"],
                "session_kinds": ["group", "private"],
                "required_group_role": "admin",
            },
        )
    )
    registry.register(
        AgentToolDefinition(
            scope=MESSAGE_EXPORT_SCOPE,
            name="legacy_group_only_tool",
            description="group only",
            parameters={"type": "object", "properties": {}},
            handler=_noop_handler,
            metadata={"channels": ["wechat"], "session_kinds": ["group"]},
        )
    )
    registry.register(
        AgentToolDefinition(
            scope=MESSAGE_EXPORT_SCOPE,
            name="legacy_unspecified_session_tool",
            description="legacy unspecified session kind",
            parameters={"type": "object", "properties": {}},
            handler=_noop_handler,
            metadata={"channels": ["wechat"]},
        )
    )
    llm = _CapturingLLM()
    engine = AgentCapabilityEngine(
        llm,
        settings=Settings(
            customer_service_prompt_enabled=False,
            agent_tools_require_explicit_policy=False,
        ),
        agent_tool_registry=registry,
    )
    private = Session(
        session_id="wxid_private",
        tenant_id="demo",
        user_id="wxid_private",
        channel=Channel.WECHAT,
        metadata={"session_kind": "private"},
    )
    group = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_user",
        channel=Channel.WECHAT,
        metadata={"session_kind": "group", "sender_is_group_admin": True},
    )

    private_tools, _ = await engine._available_tools(
        private,
        {"agent_tool_scope": MESSAGE_EXPORT_SCOPE},
    )
    group_tools, _ = await engine._available_tools(
        group,
        {"agent_tool_scope": MESSAGE_EXPORT_SCOPE},
    )

    assert set(private_tools) == {"export_current_messages_file"}
    assert set(group_tools) == {
        "export_current_messages_file",
        "legacy_group_only_tool",
        "legacy_unspecified_session_tool",
    }


def test_message_export_scope_prompt_forbids_implicit_file_delivery() -> None:
    prompt = agent_scope_system_hint(MESSAGE_EXPORT_SCOPE)

    assert "同时明确要求" in prompt
    assert "普通的消息汇总请求不得调用导出工具" in prompt
    assert "只能导出当前群聊或当前私聊" in prompt
