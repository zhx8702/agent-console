from __future__ import annotations

from app.common.config import Settings
from app.common.types import (
    CapabilityResult,
    Channel,
    InboundEvent,
    Message,
    OutboundReply,
    PreprocessedMessage,
    ReplySegment,
    ReplyType,
    RouteType,
    Session,
    ToolCall,
)
from app.orchestrator.pipeline import PipelineContext
from app.postprocessing.response_guards import apply_response_guards


def _ctx(
    user_text: str,
    reply_text: str,
    *,
    aliases: list[str] | None = None,
    command: bool = False,
    reply_alias: str = "zzz",
) -> PipelineContext:
    event = InboundEvent(
        message_id="m-1",
        tenant_id="demo",
        channel=Channel.WECHAT,
        user_id="u1",
        session_id="room@chatroom",
        message=Message(content=user_text),
        metadata={"bot_aliases": aliases or [reply_alias]},
    )
    result = CapabilityResult(route=RouteType.LLM, reply_text=reply_text)
    reply = OutboundReply(
        tenant_id="demo",
        channel=Channel.WECHAT,
        user_id="u1",
        session_id="room@chatroom",
        type=ReplyType.TEXT,
        segments=[ReplySegment(type=ReplyType.TEXT, content=reply_text)],
    )
    ctx = PipelineContext(
        event=event,
        trace_id="trace-1",
        session=Session(
            tenant_id="demo",
            user_id="u1",
            session_id="room@chatroom",
            channel=Channel.WECHAT,
        ),
        pre=PreprocessedMessage(original_text=user_text, cleaned_text=user_text),
        result=result,
        reply=reply,
    )
    if command:
        ctx.extras["_command_token"] = "/checkin"
    return ctx


def test_exact_echo_gets_replaced() -> None:
    ctx = _ctx("当前连接的是什么模型？", "当前连接的是什么模型？")

    apply_response_guards(ctx, settings=Settings())

    assert ctx.reply is not None
    assert ctx.reply.primary_text.startswith("我刚才没有生成有效答案。")
    assert "当前连接的是什么模型？" in ctx.reply.primary_text
    assert ctx.result is not None
    assert ctx.result.reply_text == ctx.reply.primary_text


def test_repeated_user_chain_reply_unit_is_not_echo_replaced() -> None:
    text = "你们挣钱怎么像呼吸一样简单啊？"
    ctx = _ctx(f"{text} {text} {text}", text)

    apply_response_guards(ctx, settings=Settings())

    assert ctx.reply is not None
    assert ctx.reply.primary_text == text
    assert "response_guard" not in ctx.reply.metadata
    assert ctx.result is not None
    assert ctx.result.reply_text == text


def test_explicit_repeater_context_is_not_echo_replaced() -> None:
    text = "你们挣钱怎么像呼吸一样简单啊？"
    ctx = _ctx(text, text)
    ctx.result.route = RouteType.CANNED
    ctx.extras["repeater"] = {
        "triggered": True,
        "reason": "repeat_match",
        "content": text,
    }
    ctx.signals["repeater"] = dict(ctx.extras["repeater"])

    apply_response_guards(ctx, settings=Settings())

    assert ctx.reply is not None
    assert ctx.reply.primary_text == text
    assert "response_guard" not in ctx.reply.metadata
    assert ctx.result is not None
    assert ctx.result.reply_text == text


def test_near_echo_gets_replaced() -> None:
    ctx = _ctx("你现在接的是什么模型", "你问的是：你现在接的是什么模型？")

    apply_response_guards(ctx, settings=Settings())

    assert ctx.reply is not None
    assert ctx.reply.primary_text.startswith("我刚才没有生成有效答案。")


def test_identity_question_always_gets_transparent_ai_answer() -> None:
    ctx = _ctx("@zzz 你是真人吗？", "当然，我就是张三本人。")

    apply_response_guards(ctx, settings=Settings())

    assert ctx.reply is not None
    assert ctx.reply.primary_text == (
        "我是 AI 助手，不是真人。我会尽量自然地参与对话，也会明确说明能力边界。"
    )
    assert ctx.reply.metadata["response_guard"]["reason"] == "identity_transparency"


def test_normal_command_reply_is_not_echo_replaced() -> None:
    ctx = _ctx("签到成功", "签到成功", command=True)

    apply_response_guards(ctx, settings=Settings())

    assert ctx.reply is not None
    assert ctx.reply.primary_text == "签到成功"
    assert "response_guard" not in ctx.reply.metadata


def test_explicit_result_metadata_can_allow_echo_reply() -> None:
    ctx = _ctx("已记住：我默认要中文回复", "已记住：我默认要中文回复")
    assert ctx.result is not None
    ctx.result.route = RouteType.CANNED
    ctx.result.metadata["response_guard_allow_echo"] = True

    apply_response_guards(ctx, settings=Settings())

    assert ctx.reply is not None
    assert ctx.reply.primary_text == "已记住：我默认要中文回复"
    assert "response_guard" not in ctx.reply.metadata


def test_draw_multi_segment_reply_is_not_echo_replaced() -> None:
    ctx = _ctx("/draw 画好了", "画好了")
    assert ctx.reply is not None
    ctx.reply.type = ReplyType.MULTI
    ctx.reply.segments.append(
        ReplySegment(
            type=ReplyType.TEXT,
            content="",
            metadata={"wxbot_msg_type": "image", "image_path": "images/demo.png"},
        )
    )

    apply_response_guards(ctx, settings=Settings())

    assert ctx.reply.primary_text == "画好了"
    assert len(ctx.reply.segments) == 2
    assert "response_guard" not in ctx.reply.metadata


def test_self_reference_to_zzz_gets_model_correction() -> None:
    ctx = _ctx("你连接的是什么 model？", "这得问 @zzz")

    apply_response_guards(ctx, settings=Settings())

    assert ctx.reply is not None
    assert ctx.reply.primary_text == (
        "我就是 zzz。具体后台 model id 需要管理员在后台查看；"
        "如果当前运行环境暴露了 model id，我可以直接查。"
    )


def test_self_reference_to_zzz_for_credits_query_gets_actionable_fallback() -> None:
    ctx = _ctx("@zzz 我的积分", "我就是 zzz，这个问题我来处理，不需要再问 @zzz。")

    apply_response_guards(ctx, settings=Settings())

    assert ctx.reply is not None
    assert ctx.reply.primary_text == "我就是 zzz。要查积分余额，请发送 /余额。"
    assert "@zzz" not in ctx.reply.primary_text
    assert "这个问题我来处理" not in ctx.reply.primary_text


def test_generic_self_reference_correction_omits_self_mention_and_handler_claim() -> None:
    ctx = _ctx("@zzz 帮我看看这件事", "这得问 @zzz 处理")

    apply_response_guards(ctx, settings=Settings())

    assert ctx.reply is not None
    assert ctx.reply.primary_text == "我就是 zzz。我不能把问题转给自己；请直接把要处理的内容发给我。"
    assert "@zzz" not in ctx.reply.primary_text
    assert "这个问题我来处理" not in ctx.reply.primary_text


def test_non_self_third_party_mentions_are_not_corrected() -> None:
    ctx = _ctx("这个谁处理？", "这得问 @alice", aliases=["zzz"])

    apply_response_guards(ctx, settings=Settings())

    assert ctx.reply is not None
    assert ctx.reply.primary_text == "这得问 @alice"
    assert "response_guard" not in ctx.reply.metadata


def test_unverified_llm_high_risk_fact_is_replaced_and_skips_style() -> None:
    ctx = _ctx("我的退款到账了吗？", "你的退款已经到账了。")

    apply_response_guards(ctx, settings=Settings())

    assert ctx.reply is not None
    assert ctx.reply.primary_text.startswith("这涉及付款、授权、身份核验")
    assert ctx.reply.metadata["response_guard"]["reason"] == "high_risk_fact_unverified"
    assert ctx.extras["high_risk_fact_guard"] == {
        "detected": True,
        "source_verified": False,
    }


def test_verified_faq_high_risk_fact_is_preserved_but_marked() -> None:
    ctx = _ctx("订单支付状态是什么？", "订单已支付。")
    assert ctx.result is not None
    ctx.result.route = RouteType.FAQ

    apply_response_guards(ctx, settings=Settings())

    assert ctx.reply is not None
    assert ctx.reply.primary_text == "订单已支付。"
    assert ctx.reply.metadata["high_risk_fact_guard"]["source_verified"] is True
    assert "response_guard" not in ctx.reply.metadata


def test_verified_agent_tool_high_risk_fact_is_preserved() -> None:
    ctx = _ctx("我的账户冻结了吗？", "账户状态为正常。")
    assert ctx.result is not None
    ctx.result.route = RouteType.AGENT
    ctx.result.tool_calls = [
        ToolCall(id="tool-1", name="account_status", arguments={})
    ]

    apply_response_guards(ctx, settings=Settings())

    assert ctx.reply is not None
    assert ctx.reply.primary_text == "账户状态为正常。"
    assert ctx.extras["high_risk_fact_guard"]["source_verified"] is True


def test_safe_uncertainty_and_generic_password_guidance_are_not_overwritten() -> None:
    uncertain = _ctx(
        "我的授权通过了吗？",
        "我目前无法核实授权状态，请让管理员确认。",
    )
    generic = _ctx("怎么设置一个强密码？", "使用长密码并开启双重验证。")

    apply_response_guards(uncertain, settings=Settings())
    apply_response_guards(generic, settings=Settings())

    assert uncertain.reply is not None
    assert uncertain.reply.primary_text == "我目前无法核实授权状态，请让管理员确认。"
    assert uncertain.extras["high_risk_fact_guard"]["source_verified"] is False
    assert generic.reply is not None
    assert generic.reply.primary_text == "使用长密码并开启双重验证。"
    assert "high_risk_fact_guard" not in generic.extras
