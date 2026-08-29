from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.channel import get_reply_policy_override
from app.common.types import (
    Channel,
    InboundEvent,
    Message,
    PreprocessedMessage,
    Role,
    Session,
    Turn,
)
from app.orchestrator.effect_handlers import EffectDispatcher, EffectHandlerRegistry
from app.orchestrator.effects import (
    EFFECT_STATUS_DUPLICATE,
    EFFECT_STATUS_RECORDED,
    InMemoryEffectCommitter,
)
from app.orchestrator.pipeline import PipelineContext
from app.plugin.hooks import RESULT_PRODUCER_OWNER_KEY, HookAbort, HookPoint, HookRunner
from plugins.repeater.hooks import RepeaterDetectStep, RepeaterHook, RepeaterTriggerEffectHandler
from plugins.wxbot.hooks import WxbotReplyPolicyHook


class _FakeRepeaterStore:
    def __init__(self, *, enabled: bool = True, should_trigger: bool = True) -> None:
        self.enabled = enabled
        self._should_trigger = should_trigger
        self.recorded: list[dict[str, str]] = []
        self.config_session_ids: list[str] = []
        self.trigger_session_ids: list[str] = []

    async def get_config(self, tenant_id: str, session_id: str) -> dict:
        self.config_session_ids.append(session_id)
        return {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "enabled": self.enabled,
            "cooldown_seconds": 300,
        }

    async def should_trigger(
        self,
        tenant_id: str,
        session_id: str,
        content_text: str,
        cooldown_seconds: int,
    ) -> bool:
        self.trigger_session_ids.append(session_id)
        return self._should_trigger

    async def record_trigger(
        self,
        tenant_id: str,
        session_id: str,
        content_text: str,
        *,
        trace_id: str = "",
    ) -> int:
        self.recorded.append(
            {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "content_text": content_text,
                "trace_id": trace_id,
            }
        )
        return 1


def _ctx(current: str, previous_user: str) -> PipelineContext:
    now = datetime.now(UTC)
    event = InboundEvent(
        message_id="m-1",
        tenant_id="demo",
        channel=Channel.WECHAT,
        user_id="u1",
        session_id="group-1@chatroom",
        message=Message(content=current),
        trace_id="trace-1",
        received_at=now,
        metadata={"sender_wxid": "wxid-current"},
    )
    session = Session(
        session_id="group-1@chatroom",
        tenant_id="demo",
        user_id="u1",
        channel=Channel.WECHAT,
        turns=[
            Turn(
                session_id="group-1@chatroom",
                role=Role.USER,
                content=previous_user,
                trace_id="trace-prev",
                created_at=now - timedelta(seconds=10),
                metadata={"sender_wxid": "wxid-previous"},
            ),
            Turn(
                session_id="group-1@chatroom",
                role=Role.USER,
                content=current,
                trace_id="trace-1",
                created_at=now,
                metadata={"sender_wxid": "wxid-current"},
            ),
        ],
    )
    pre = PreprocessedMessage(original_text=current, cleaned_text=current)
    return PipelineContext(event=event, trace_id="trace-1", session=session, pre=pre)


def _use_managed_identity(ctx: PipelineContext) -> tuple[str, str]:
    canonical_session_id = "cx1:c:managed-group@chatroom"
    external_session_id = "00000000000@chatroom"
    ctx.event.session_id = canonical_session_id
    ctx.event.canonical_conversation_id = canonical_session_id
    ctx.event.external_conversation_id = external_session_id
    ctx.event.metadata.update(
        {
            "connection_id": "managed-connection",
            "canonical_conversation_id": canonical_session_id,
            "external_conversation_id": external_session_id,
        }
    )
    assert ctx.session is not None
    ctx.session.session_id = canonical_session_id
    ctx.session.canonical_conversation_id = canonical_session_id
    ctx.session.external_conversation_id = external_session_id
    for turn in ctx.session.turns:
        turn.session_id = canonical_session_id
    return canonical_session_id, external_session_id


def _ctx_with_preprocessed(
    *,
    event_content: str,
    original_text: str,
    cleaned_text: str,
    previous_user: str,
    metadata: dict | None = None,
    turn_original_content: str | None = None,
) -> PipelineContext:
    now = datetime.now(UTC)
    event_metadata = {"sender_wxid": "wxid-current", **dict(metadata or {})}
    event = InboundEvent(
        message_id="m-1",
        tenant_id="demo",
        channel=Channel.WECHAT,
        user_id="u1",
        session_id="group-1@chatroom",
        message=Message(content=event_content),
        trace_id="trace-1",
        received_at=now,
        metadata=event_metadata,
    )
    session = Session(
        session_id="group-1@chatroom",
        tenant_id="demo",
        user_id="u1",
        channel=Channel.WECHAT,
        turns=[
            Turn(
                session_id="group-1@chatroom",
                role=Role.USER,
                content=previous_user,
                trace_id="trace-prev",
                created_at=now - timedelta(seconds=10),
                metadata={"sender_wxid": "wxid-previous"},
            ),
            Turn(
                session_id="group-1@chatroom",
                role=Role.USER,
                content=cleaned_text,
                trace_id="trace-1",
                created_at=now,
                metadata=(
                    {
                        "original_content": turn_original_content,
                        "sender_wxid": "wxid-current",
                    }
                    if turn_original_content is not None
                    else {"sender_wxid": "wxid-current"}
                ),
            ),
        ],
    )
    pre = PreprocessedMessage(original_text=original_text, cleaned_text=cleaned_text)
    return PipelineContext(event=event, trace_id="trace-1", session=session, pre=pre)


def _ctx_with_turns(current: str, turns: list[Turn]) -> PipelineContext:
    event = InboundEvent(
        message_id="m-1",
        tenant_id="demo",
        channel=Channel.WECHAT,
        user_id="u1",
        session_id="group-1@chatroom",
        message=Message(content=current),
        trace_id="trace-1",
    )
    session = Session(
        session_id="group-1@chatroom",
        tenant_id="demo",
        user_id="u1",
        channel=Channel.WECHAT,
        turns=turns,
    )
    pre = PreprocessedMessage(original_text=current, cleaned_text=current)
    return PipelineContext(event=event, trace_id="trace-1", session=session, pre=pre)


@pytest.mark.asyncio
async def test_repeater_hook_repeats_on_two_identical_user_messages() -> None:
    store = _FakeRepeaterStore(enabled=True, should_trigger=True)
    hook = RepeaterHook(store)
    ctx = _ctx("复读测试", "复读测试")

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reply_text == "复读测试"
    assert store.recorded == [
        {
            "tenant_id": "demo",
            "session_id": "group-1@chatroom",
            "content_text": "复读测试",
            "trace_id": "trace-1",
        }
    ]
    override = get_reply_policy_override(ctx.extras)
    assert override["force_send"] is True
    assert override["mention_sender"] is False
    assert override["reason"] == "repeater_triggered"
    assert "wxbot_force_send" not in ctx.extras
    assert "wxbot_force_no_mention_sender" not in ctx.extras


@pytest.mark.asyncio
async def test_repeater_hook_skips_when_group_scope_is_disabled() -> None:
    store = _FakeRepeaterStore(enabled=True, should_trigger=True)

    async def deny_scope(_tenant_id: str, _session_id: str) -> bool:
        return False

    hook = RepeaterHook(store, scope_execution_allowed=deny_scope)
    ctx = _ctx("复读测试", "复读测试")

    await hook.run(ctx)

    assert store.recorded == []
    assert store.config_session_ids == []
    assert ctx.extras["repeater"]["reason"] == "scope_disabled"


@pytest.mark.asyncio
async def test_repeater_uses_external_group_id_after_managed_identity_migration() -> None:
    store = _FakeRepeaterStore(enabled=True, should_trigger=True)
    ctx = _ctx("复读测试", "复读测试")
    _, external_session_id = _use_managed_identity(ctx)

    with pytest.raises(HookAbort) as excinfo:
        await RepeaterHook(store).run(ctx)

    assert excinfo.value.reply_text == "复读测试"
    assert store.config_session_ids == [external_session_id]
    assert store.trigger_session_ids == [external_session_id]
    assert store.recorded[0]["session_id"] == external_session_id


@pytest.mark.asyncio
async def test_repeater_effect_records_external_group_id_after_identity_migration() -> None:
    store = _FakeRepeaterStore(enabled=True, should_trigger=True)
    ctx = _ctx("复读测试", "复读测试")
    _, external_session_id = _use_managed_identity(ctx)
    step = RepeaterDetectStep(store, effect_handler_enabled=True)

    result = await step.run(ctx)

    assert result.action == "stop"
    assert result.effects[0].payload["session_id"] == external_session_id
    assert result.effects[0].idempotency_key == (
        f"repeater:trigger:demo:{external_session_id}:trace-1"
    )
    registry = EffectHandlerRegistry()
    registry.register(
        "record_repeater_trigger",
        "repeater",
        RepeaterTriggerEffectHandler(store),
    )
    dispatcher = EffectDispatcher(registry, InMemoryEffectCommitter())
    await dispatcher.dispatch(result.effects[0], ctx)
    assert store.recorded[0]["session_id"] == external_session_id


@pytest.mark.asyncio
async def test_repeater_hook_matches_cleaned_text_but_preserves_original_spacing() -> None:
    store = _FakeRepeaterStore(enabled=True, should_trigger=True)
    hook = RepeaterHook(store)
    ctx = _ctx_with_preprocessed(
        event_content="复读  测试",
        original_text="复读  测试",
        cleaned_text="复读 测试",
        previous_user="复读 测试",
    )

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reply_text == "复读  测试"
    assert ctx.extras["repeater"]["content"] == "复读 测试"
    assert store.recorded[0]["content_text"] == "复读 测试"


@pytest.mark.asyncio
async def test_repeater_hook_replies_with_triggering_original_when_spacing_differs() -> None:
    store = _FakeRepeaterStore(enabled=True, should_trigger=True)
    hook = RepeaterHook(store)
    ctx = _ctx_with_preprocessed(
        event_content="复读 测试",
        original_text="复读 测试",
        cleaned_text="复读 测试",
        previous_user="复读   测试",
        turn_original_content="复读  测试",
    )

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reply_text == "复读  测试"
    assert ctx.extras["repeater"]["content"] == "复读 测试"
    assert store.recorded[0]["content_text"] == "复读 测试"


@pytest.mark.asyncio
async def test_repeater_hook_falls_back_to_cleaned_reply_without_original() -> None:
    store = _FakeRepeaterStore(enabled=True, should_trigger=True)
    hook = RepeaterHook(store)
    ctx = _ctx_with_preprocessed(
        event_content="",
        original_text="",
        cleaned_text="复读 测试",
        previous_user="复读 测试",
    )

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reply_text == "复读 测试"
    assert store.recorded[0]["content_text"] == "复读 测试"


@pytest.mark.asyncio
async def test_repeater_detect_step_repeats_on_two_identical_user_messages() -> None:
    store = _FakeRepeaterStore(enabled=True, should_trigger=True)
    step = RepeaterDetectStep(store)
    ctx = _ctx("复读测试", "复读测试")

    result = await step.run(ctx)

    assert result.action == "stop"
    assert result.finalize is True
    assert result.skip_output_safety is True
    assert result.reason == "repeater_triggered"
    assert result.result is not None
    assert result.result.reply_text == "复读测试"
    assert len(result.effects) == 1
    assert result.effects[0].type == "record_repeater_trigger"
    assert result.effects[0].owner == "repeater"
    assert result.effects[0].idempotency_key == (
        "repeater:trigger:demo:group-1@chatroom:trace-1"
    )
    assert result.effects[0].payload["content"] == "复读测试"
    assert ctx.signals["repeater"]["triggered"] is True
    assert ctx.extras[RESULT_PRODUCER_OWNER_KEY] == "repeater"
    assert get_reply_policy_override(ctx.extras)["force_send"] is True
    assert store.recorded == [
        {
            "tenant_id": "demo",
            "session_id": "group-1@chatroom",
            "content_text": "复读测试",
            "trace_id": "trace-1",
        }
    ]


@pytest.mark.asyncio
async def test_repeater_detect_step_can_defer_trigger_record_to_effect_handler() -> None:
    store = _FakeRepeaterStore(enabled=True, should_trigger=True)
    step = RepeaterDetectStep(store, effect_handler_enabled=True)
    ctx = _ctx("复读测试", "复读测试")

    result = await step.run(ctx)

    assert result.action == "stop"
    assert len(result.effects) == 1
    effect = result.effects[0]
    assert effect.type == "record_repeater_trigger"
    assert effect.payload["commit_semantics"] == "gate_before_side_effect"
    assert effect.payload["content"] == "复读测试"
    assert store.recorded == []
    assert ctx.signals["repeater"]["triggered"] is True
    assert get_reply_policy_override(ctx.extras)["force_send"] is True


@pytest.mark.asyncio
async def test_repeater_trigger_effect_handler_records_once_after_commit() -> None:
    store = _FakeRepeaterStore(enabled=True, should_trigger=True)
    step = RepeaterDetectStep(store, effect_handler_enabled=True)
    ctx = _ctx("复读测试", "复读测试")
    registry = EffectHandlerRegistry()
    registry.register(
        "record_repeater_trigger",
        "repeater",
        RepeaterTriggerEffectHandler(store),
    )
    dispatcher = EffectDispatcher(registry, InMemoryEffectCommitter())

    step_result = await step.run(ctx)
    first = await dispatcher.dispatch(step_result.effects[0], ctx)
    second = await dispatcher.dispatch(step_result.effects[0], ctx)

    assert first.status == EFFECT_STATUS_RECORDED
    assert second.status == EFFECT_STATUS_DUPLICATE
    assert store.recorded == [
        {
            "tenant_id": "demo",
            "session_id": "group-1@chatroom",
            "content_text": "复读测试",
            "trace_id": "trace-1",
        }
    ]
    assert ctx.signals["effects"]["repeater"] == [
        {
            "type": "record_repeater_trigger",
            "owner": "repeater",
            "idempotency_key": "repeater:trigger:demo:group-1@chatroom:trace-1",
            "event_id": 1,
            "status": "recorded",
        }
    ]


@pytest.mark.asyncio
async def test_repeater_hook_skips_when_in_cooldown() -> None:
    store = _FakeRepeaterStore(enabled=True, should_trigger=False)
    hook = RepeaterHook(store)
    ctx = _ctx("复读测试", "复读测试")

    await hook.run(ctx)

    assert store.recorded == []
    assert ctx.extras["repeater"]["reason"] == "cooldown"


@pytest.mark.asyncio
async def test_repeater_detect_step_continues_in_cooldown() -> None:
    store = _FakeRepeaterStore(enabled=True, should_trigger=False)
    step = RepeaterDetectStep(store)
    ctx = _ctx("复读测试", "复读测试")

    result = await step.run(ctx)

    assert result.action == "continue"
    assert result.reason == "cooldown"
    assert store.recorded == []
    assert ctx.signals["repeater"]["triggered"] is False


class _AbortingHook:
    name = "test.abort_after_repeater"
    point = HookPoint.BEFORE_ROUTE
    priority = 20

    async def run(self, ctx: PipelineContext) -> None:
        raise HookAbort("", reason="blocked_after_repeater")


@pytest.mark.asyncio
async def test_repeater_hook_priority_runs_before_wxbot_reply_policy() -> None:
    repeater = RepeaterHook(_FakeRepeaterStore(enabled=True, should_trigger=True))

    assert repeater.priority < WxbotReplyPolicyHook.priority

    runner = HookRunner()
    runner.register(_AbortingHook())
    runner.register(repeater)

    ctx = _ctx("[旺柴]", "[旺柴]")

    with pytest.raises(HookAbort) as excinfo:
        await runner.run(HookPoint.BEFORE_ROUTE, ctx)

    assert excinfo.value.reason == "repeater_triggered"
    assert ctx.extras["repeater"]["triggered"] is True


@pytest.mark.asyncio
async def test_repeater_hook_supports_non_wechat_group_context() -> None:
    store = _FakeRepeaterStore(enabled=True, should_trigger=True)
    hook = RepeaterHook(store)
    ctx = _ctx("复读测试", "复读测试")
    ctx.event.channel = Channel.DISCORD
    ctx.event.session_id = "discord-channel-1"
    ctx.event.metadata["session_kind"] = "group"
    assert ctx.session is not None
    ctx.session.channel = Channel.DISCORD
    ctx.session.session_id = "discord-channel-1"
    for turn in ctx.session.turns:
        turn.session_id = "discord-channel-1"

    with pytest.raises(HookAbort) as excinfo:
        await hook.run(ctx)

    assert excinfo.value.reply_text == "复读测试"
    assert store.recorded[0]["session_id"] == "discord-channel-1"
    assert get_reply_policy_override(ctx.extras)["force_send"] is True


@pytest.mark.asyncio
async def test_repeater_hook_skips_bot_mention_messages() -> None:
    store = _FakeRepeaterStore(enabled=True, should_trigger=True)
    hook = RepeaterHook(store)
    ctx = _ctx("@zzz [旺柴]", "@zzz [旺柴]")

    await hook.run(ctx)

    assert store.recorded == []
    assert ctx.extras["repeater"]["reason"] == "mention_content"


@pytest.mark.asyncio
async def test_repeater_hook_skips_placeholder_non_text_messages() -> None:
    store = _FakeRepeaterStore(enabled=True, should_trigger=True)
    hook = RepeaterHook(store)
    ctx = _ctx("[图片]", "[图片]")

    await hook.run(ctx)

    assert store.recorded == []
    assert ctx.extras["repeater"]["reason"] == "non_text_placeholder"


@pytest.mark.asyncio
async def test_repeater_hook_requires_immediately_previous_turn_to_be_user() -> None:
    store = _FakeRepeaterStore(enabled=True, should_trigger=True)
    hook = RepeaterHook(store)
    ctx = _ctx_with_turns(
        "复读测试",
        [
            Turn(session_id="group-1@chatroom", role=Role.USER, content="复读测试", trace_id="trace-prev"),
            Turn(session_id="group-1@chatroom", role=Role.ASSISTANT, content="机器人上一条回复", trace_id="trace-bot"),
            Turn(session_id="group-1@chatroom", role=Role.USER, content="复读测试", trace_id="trace-1"),
        ],
    )

    await hook.run(ctx)

    assert store.recorded == []
    assert ctx.extras["repeater"]["reason"] == "previous_turn_not_user"


@pytest.mark.asyncio
async def test_repeater_requires_two_distinct_real_members() -> None:
    store = _FakeRepeaterStore(enabled=True, should_trigger=True)
    ctx = _ctx("复读测试", "复读测试")
    assert ctx.session is not None
    ctx.session.turns[0].metadata["sender_wxid"] = "wxid-current"

    await RepeaterHook(store).run(ctx)

    assert store.recorded == []
    assert ctx.extras["repeater"]["reason"] == "same_sender"


@pytest.mark.asyncio
async def test_repeater_allows_adjacent_matching_messages_after_long_gap() -> None:
    store = _FakeRepeaterStore(enabled=True, should_trigger=True)
    ctx = _ctx("复读测试", "复读测试")
    assert ctx.session is not None
    ctx.session.turns[0].created_at = ctx.event.received_at - timedelta(days=1)

    with pytest.raises(HookAbort):
        await RepeaterHook(store).run(ctx)

    assert ctx.extras["repeater"]["reason"] == "repeat_match"
    assert len(store.recorded) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("手机号 13800138000", "pii_or_secret_content"),
        ("看看 https://example.com/x", "link_content"),
        ("/ban wxid_a", "command_content"),
        ("a" * 121, "content_too_long"),
    ],
)
async def test_repeater_blocks_sensitive_or_high_risk_content(
    content: str,
    reason: str,
) -> None:
    store = _FakeRepeaterStore(enabled=True, should_trigger=True)
    ctx = _ctx(content, content)

    await RepeaterHook(store).run(ctx)

    assert store.recorded == []
    assert ctx.extras["repeater"]["reason"] == reason
    assert ctx.extras["repeater"]["content"] == ""


@pytest.mark.asyncio
async def test_repeater_blocks_identity_content() -> None:
    from app.common.intent import IntentDecision, IntentDomain
    from app.common.intent_runtime import persist_decision

    store = _FakeRepeaterStore(enabled=True, should_trigger=True)
    ctx = _ctx("你是真人吗?", "你是真人吗?")
    persist_decision(
        IntentDecision(domain=IntentDomain.IDENTITY, action="inquiry", confidence=0.95),
        pre=ctx.pre,
    )

    await RepeaterHook(store).run(ctx)

    assert store.recorded == []
    assert ctx.extras["repeater"]["reason"] == "identity_or_handoff_content"
    assert ctx.extras["repeater"]["content"] == ""


@pytest.mark.asyncio
async def test_repeater_never_reacts_to_its_own_message() -> None:
    store = _FakeRepeaterStore(enabled=True, should_trigger=True)
    ctx = _ctx("复读测试", "复读测试")
    ctx.event.metadata["is_self_sent"] = True

    await RepeaterHook(store).run(ctx)

    assert store.recorded == []
    assert ctx.extras["repeater"]["reason"] == "self_message"
