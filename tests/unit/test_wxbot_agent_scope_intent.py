from __future__ import annotations

import pytest

from app.agent.scopes import (
    DEFAULT_AGENT_SCOPE,
    FILE_ANALYSIS_SCOPE,
    GROUP_DRAW_GENERATION_SCOPE,
    GROUP_PERSONAL_MAP_SCOPE,
    MESSAGE_EXPORT_SCOPE,
)
from app.common.types import Channel, InboundEvent, Message, Role, Session, Turn
from app.orchestrator.pipeline import PipelineContext
from app.social.contracts import (
    GroupParticipationPolicyDocument,
    KillSwitches,
    ParticipationPolicyValues,
)
from plugins.wxbot.agent_intent_hook import WxbotAgentIntentHook
from plugins.wxbot.hook_context import _resolve_group_agent_scope
from plugins.wxbot.hooks import WxbotAgentScopeEnrichStep


class _FilePolicyStore:
    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled

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
            policy=ParticipationPolicyValues(file_send_enabled=self.enabled),
        )


def _group_context(text: str) -> PipelineContext:
    session = Session(
        session_id="scope-test@chatroom",
        tenant_id="demo",
        user_id="wxid_user",
        channel=Channel.WECHAT,
    )
    event = InboundEvent(
        message_id=f"message-{abs(hash(text))}",
        tenant_id="demo",
        channel=Channel.WECHAT,
        user_id="wxid_user",
        session_id=session.session_id,
        message=Message(content=text),
        trace_id=f"trace-{abs(hash(text))}",
        metadata={
            "mentioned_me": True,
            "wxbot_normalized_content": text,
        },
    )
    return PipelineContext(event=event, trace_id=event.trace_id, session=session)


@pytest.mark.parametrize(
    "text",
    [
        "你在哪里学的",
        "你在哪里",
        "小明在哪里",
        "小明家在哪里",
        "张三的位置在哪里",
        "你的位置是什么",
        "不要查询家庭地址",
        "哪里不舒服",
        "咖啡真好喝",
        "我今天喝了两杯咖啡",
        "旅游真的很开心",
        "这个导航软件真难用",
    ],
)
def test_map_scope_rejects_hard_negative_phrases(text: str) -> None:
    assert _resolve_group_agent_scope(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "不要生成上海地图",
        "不生成上海地图",
        "别查询故宫地址",
        "请不要画一张猫",
        "无需把今天群消息总结成文件发我",
        "do not 生成上海地图",
        "don't 把今天群消息总结成文件发我",
    ],
)
def test_agent_tool_scopes_reject_negated_actions(text: str) -> None:
    assert _resolve_group_agent_scope(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "帮我查一下公司的详细地址",
        "从人民广场到外滩怎么走",
        "三里屯附近咖啡店",
        "上海有哪些景点",
        "生成一张上海景点地图",
        "群硕软件开发（武汉）有限公司 武汉的具体位置 精确到楼栋",
        "故宫在哪里？",
    ],
)
def test_map_scope_keeps_explicit_location_queries(text: str) -> None:
    assert _resolve_group_agent_scope(text) == GROUP_PERSONAL_MAP_SCOPE


@pytest.mark.parametrize(
    "text",
    [
        "不要生成地图，只查一下故宫地址",
        "不用画图，帮我查一下公司地址",
    ],
)
def test_map_scope_keeps_later_affirmative_query_after_negated_tool_action(
    text: str,
) -> None:
    assert _resolve_group_agent_scope(text) == GROUP_PERSONAL_MAP_SCOPE


@pytest.mark.parametrize(
    ("text", "expected_scope"),
    [
        (
            "把今天群里关于上海地图的消息总结成文件发我，再生成地图",
            MESSAGE_EXPORT_SCOPE,
        ),
        ("帮我画一张上海旅游地图", GROUP_DRAW_GENERATION_SCOPE),
        ("查一下最近聊天记录里谁说过上海地图", DEFAULT_AGENT_SCOPE),
    ],
)
def test_group_scope_conflicts_use_specific_primary_intent(
    text: str,
    expected_scope: str,
) -> None:
    assert _resolve_group_agent_scope(text) == expected_scope


@pytest.mark.asyncio
async def test_agent_intent_hook_emits_new_and_legacy_router_signals() -> None:
    ctx = _group_context("帮我查一下公司的详细地址")

    await WxbotAgentIntentHook().run(ctx)

    assert ctx.extras["agent_tool_scope"] == GROUP_PERSONAL_MAP_SCOPE
    assert ctx.extras["router_signals"] == {
        "tool_intent_matched": True,
        "tools_available": True,
    }


@pytest.mark.asyncio
async def test_agent_scope_step_syncs_new_and_legacy_router_signals() -> None:
    ctx = _group_context("帮我查一下公司的详细地址")

    result = await WxbotAgentScopeEnrichStep().run(ctx)

    assert result.reason == "enriched"
    assert ctx.signals["router"] == {
        "tool_intent_matched": True,
        "tools_available": True,
    }
    assert ctx.signals["channel"]["wechat"]["agent_scope"][
        "tool_intent_matched"
    ] is True
    assert ctx.signals["channel"]["wechat"]["agent_scope"]["tools_available"] is True


@pytest.mark.asyncio
async def test_generate_file_scope_requires_explicit_delivery() -> None:
    requested = _group_context("把上面的内容整理成文件发我")
    denied = _group_context("把上面的内容整理成文件但不要发")

    await WxbotAgentIntentHook(
        social_policy_store=_FilePolicyStore(enabled=True),
    ).run(requested)
    await WxbotAgentIntentHook().run(denied)

    assert requested.extras["agent_tool_scope"] == FILE_ANALYSIS_SCOPE
    assert "agent_tool_scope" not in denied.extras


@pytest.mark.asyncio
async def test_group_file_send_scope_fails_closed_when_master_switch_is_off() -> None:
    ctx = _group_context("把上面的内容整理成文件发我")

    await WxbotAgentIntentHook(
        social_policy_store=_FilePolicyStore(enabled=False),
    ).run(ctx)

    assert "agent_tool_scope" not in ctx.extras
    assert ctx.session.metadata["group_file_send_enabled"] is False
    assert ctx.extras["wxbot_file_send_denial_reason"] == "group_file_send_disabled"
    assert ctx.extras["router_signals"]["tool_intent_matched"] is True
    assert ctx.extras["router_signals"]["tools_available"] is False


@pytest.mark.asyncio
async def test_group_file_followup_does_not_cross_sender_boundary() -> None:
    ctx = _group_context("分析刚才这个文件")
    ctx.session.turns.append(
        Turn(
            session_id=ctx.session.session_id,
            role=Role.USER,
            content="[文件] private.txt",
            metadata={
                "msg_type": "file",
                "file_name": "private.txt",
                "sender_wxid": "wxid_other",
            },
        )
    )

    await WxbotAgentIntentHook().run(ctx)

    assert "agent_tool_scope" not in ctx.extras


@pytest.mark.asyncio
async def test_managed_group_identity_still_requires_group_mention_for_file_scope() -> None:
    ctx = _group_context("分析刚才这个文件")
    ctx.event.session_id = "cx1:managed-room"
    ctx.event.external_conversation_id = "room@chatroom"
    ctx.event.metadata.update(
        {
            "session_kind": "group",
            "external_conversation_id": "room@chatroom",
            "sender_wxid": "wxid_user",
        }
    )
    ctx.session.turns.append(
        Turn(
            session_id=ctx.session.session_id,
            role=Role.USER,
            content="[文件] report.txt",
            metadata={
                "msg_type": "file",
                "file_name": "report.txt",
                "sender_wxid": "wxid_user",
            },
        )
    )

    await WxbotAgentIntentHook().run(ctx)

    assert ctx.extras["agent_tool_scope"] == FILE_ANALYSIS_SCOPE
