from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.channel import ChannelRegistry
from app.common.types import (
    Channel,
    InboundEvent,
    Message,
    OutboundReply,
    PreprocessedMessage,
    ReplySegment,
    Role,
    Session,
    SessionState,
    Turn,
)
from app.orchestrator.effect_handlers import (
    EFFECT_HANDLER_STATUS_HANDLER_ERROR,
    EFFECT_HANDLER_STATUS_NO_HANDLER,
    EFFECT_HANDLER_STATUS_OWNER_SKIPPED,
    ChannelReplyEffectHandler,
    CorePublishOutboundEffectHandler,
    EffectDispatcher,
    EffectHandlerRegistry,
    effect_handler_opt_in_enabled,
    effect_handler_registry_payload,
    register_core_publish_outbound_handler,
    register_core_session_effect_handlers,
    register_memory_save_handler,
)
from app.orchestrator.effects import (
    EFFECT_STATUS_DRY_RUN,
    EFFECT_STATUS_DUPLICATE,
    EFFECT_STATUS_RECORDED,
    EffectCommitRecord,
    InMemoryEffectCommitter,
)
from app.orchestrator.flow import MessageEffect
from app.orchestrator.owner_gate import OwnerExecutionDecision
from app.orchestrator.pipeline import PipelineContext
from app.social.contracts import (
    GroupParticipationPolicyDocument,
    KillSwitches,
    ParticipationPolicyValues,
)
from plugins.wxbot.channel import WxbotChannelOutbound


class _RecordingHandler:
    def __init__(self) -> None:
        self.calls: list[tuple[MessageEffect, EffectCommitRecord]] = []

    async def __call__(
        self,
        effect: MessageEffect,
        ctx: PipelineContext,
        record: EffectCommitRecord,
    ) -> None:
        _ = ctx
        self.calls.append((effect, record))


class _BoomHandler:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(
        self,
        effect: MessageEffect,
        ctx: PipelineContext,
        record: EffectCommitRecord,
    ) -> None:
        _ = effect, ctx, record
        self.calls += 1
        raise RuntimeError("handler boom")


@pytest.mark.asyncio
async def test_effect_dispatcher_gates_framework_stamped_producer_before_handler_owner() -> None:
    handler = _RecordingHandler()
    registry = EffectHandlerRegistry()
    registry.register("run", "wxbot", handler)
    seen: list[str] = []

    async def gate(owner: str, ctx: PipelineContext) -> OwnerExecutionDecision:
        _ = ctx
        seen.append(owner)
        return OwnerExecutionDecision(owner != "draw", "plugin_disabled")

    dispatcher = EffectDispatcher(
        registry,
        InMemoryEffectCommitter(),
        owner_gate=gate,
    )
    result = await dispatcher.dispatch(
        MessageEffect(
            type="run",
            owner="wxbot",
            producer_owner="draw",
            idempotency_key="draw:send:1",
        ),
        _ctx(),
    )

    assert result.status == EFFECT_HANDLER_STATUS_OWNER_SKIPPED
    assert result.error == "plugin_disabled"
    assert seen == ["draw"]
    assert handler.calls == []


@pytest.mark.asyncio
async def test_effect_dispatcher_keeps_transient_owner_gate_failure_retryable() -> None:
    handler = _RecordingHandler()
    registry = EffectHandlerRegistry()
    registry.register("run", "wxbot", handler)

    async def unavailable(owner: str, ctx: PipelineContext) -> bool:
        _ = owner, ctx
        raise RuntimeError("state store unavailable")

    dispatcher = EffectDispatcher(
        registry,
        InMemoryEffectCommitter(),
        owner_gate=unavailable,
    )
    result = await dispatcher.dispatch(
        MessageEffect(
            type="run",
            owner="wxbot",
            producer_owner="draw",
            idempotency_key="draw:send:gate-error",
        ),
        _ctx(),
    )

    assert result.status == EFFECT_HANDLER_STATUS_HANDLER_ERROR
    assert result.error == "owner_gate_error"
    assert handler.calls == []


class _MemoryStore:
    def __init__(self) -> None:
        self.remember_calls: list[dict[str, str]] = []

    async def remember_interaction(self, **kwargs):
        self.remember_calls.append(kwargs)
        return {
            "tenant_id": kwargs["tenant_id"],
            "channel": kwargs["channel"],
            "source_key": kwargs["source_key"],
            "user_id": kwargs["user_id"],
            "session_id": kwargs["session_id"],
            "short_term_memory": "用户最近说: 我要看物流",
            "long_term_memory": "已知用户事实与偏好:\n- 偏好微信联系",
            "manual_notes": "全局记忆备注:\nVIP 客户",
            "identity_manual_notes": "VIP 客户",
            "session_manual_notes": "",
            "message_count": 4,
            "identity_message_count": 4,
            "session_message_count": 2,
            "imported_message_count": 2,
            "last_session_id": kwargs["session_id"],
            "identity_profile": {
                "user_id": kwargs["user_id"],
                "updated_at": datetime(2026, 4, 21, 12, 2, tzinfo=UTC),
            },
            "session_profile": {
                "session_id": kwargs["session_id"],
                "updated_at": datetime(2026, 4, 21, 12, 3, tzinfo=UTC),
            },
            "memory_items": {
                "identity": [
                    {
                        "source_type": "manual",
                        "status": "active",
                        "content": "人工标记为 VIP",
                        "created_at": datetime(2026, 4, 21, 12, 2, tzinfo=UTC),
                    }
                ],
                "session": [
                    {
                        "source_type": "auto",
                        "status": "active",
                        "content": "用户刚刚要求看物流",
                    }
                ],
            },
        }


class _ChannelOutbound:
    def __init__(self) -> None:
        self.text_calls: list[dict[str, object]] = []
        self.image_calls: list[dict[str, object]] = []

    async def get_session_policy(self, target):
        _ = target
        return {}

    async def send_text(self, target, text, options=None):
        self.text_calls.append(
            {
                "target": target,
                "text": text,
                "options": options,
            }
        )
        return type(
            "Result",
            (),
            {
                "message_id": "queued-1",
                "provider": "fake",
                "metadata": {"reply_queue_id": 1},
            },
        )()

    async def send_image(self, target, media, options=None):
        self.image_calls.append(
            {
                "target": target,
                "media": media,
                "options": options,
            }
        )
        return type(
            "Result",
            (),
            {
                "message_id": "queued-image-1",
                "provider": "fake",
                "metadata": {"reply_queue_id": 2},
            },
        )()


class _Bus:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def publish(
        self,
        stream: str,
        payload: dict[str, object],
        *,
        partition_key: str | None = None,
    ) -> str:
        self.messages.append(
            {
                "stream": stream,
                "payload": payload,
                "partition_key": partition_key,
            }
        )
        return f"bus-message-{len(self.messages)}"


class _SessionManager:
    def __init__(self) -> None:
        self.appended: list[Turn] = []
        self.states: list[SessionState] = []

    async def append_turn(self, session: Session, turn: Turn) -> None:
        session.turns.append(turn)
        self.appended.append(turn)

    async def set_state(self, session: Session, new_state: SessionState) -> None:
        session.state = new_state
        self.states.append(new_state)


class _WxbotReplyStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def get_session_policy(self, tenant_id: str, session_id: str):
        _ = tenant_id, session_id
        return {"effective_mention_sender": True}

    async def enqueue_reply(
        self,
        tenant_id: str,
        session_id: str,
        session_name: str,
        sender_name: str,
        reply_text: str,
        trace_id: str = "",
        *,
        mention_sender: bool = False,
        msg_type: str = "text",
        image_path: str = "",
        image_url: str = "",
        sender_wxid: str = "",
        reply_to_msg_svr_id: str = "",
        session_kind: str = "",
        source_message: dict[str, object] | None = None,
        delivery: dict[str, object] | None = None,
        command_id: str = "",
    ) -> int:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "session_name": session_name,
                "sender_name": sender_name,
                "reply_text": reply_text,
                "trace_id": trace_id,
                "mention_sender": mention_sender,
                "msg_type": msg_type,
                "image_path": image_path,
                "image_url": image_url,
                "sender_wxid": sender_wxid,
                "reply_to_msg_svr_id": reply_to_msg_svr_id,
                "session_kind": session_kind,
                "source_message": source_message or {},
                "delivery": delivery or {},
                "command_id": command_id,
            }
        )
        return len(self.calls)


class _WxbotPolicyStore:
    async def get_group_policy(
        self,
        tenant_id: str,
        session_id: str,
    ) -> GroupParticipationPolicyDocument:
        assert tenant_id == "demo"
        assert session_id == "wx-session-1@chatroom"
        return GroupParticipationPolicyDocument(
            tenant_id=tenant_id,
            session_id=session_id,
            version=7,
            kill_switches=KillSwitches(),
            effective_enabled=True,
            policy=ParticipationPolicyValues(rollout_stage="contextual"),
        )


class _ManagedWxbotConnectionStore:
    async def get(self, tenant_id: str, connection_id: str) -> SimpleNamespace:
        assert (tenant_id, connection_id) == ("demo", "wechat-main")
        return SimpleNamespace(adapter_id="wechat-sdk", desired_state="enabled")


def _ctx() -> PipelineContext:
    event = InboundEvent(
        message_id="m-1",
        tenant_id="demo",
        channel=Channel.WEB,
        user_id="u1",
        session_id="s1",
        message=Message(content="hello"),
    )
    return PipelineContext(event=event, trace_id=event.trace_id)


def _dispatcher(
    handler: _RecordingHandler | _BoomHandler,
) -> tuple[EffectDispatcher, InMemoryEffectCommitter]:
    registry = EffectHandlerRegistry()
    registry.register("publish_outbound", "wxbot", handler)
    committer = InMemoryEffectCommitter()
    return EffectDispatcher(registry, committer), committer


def test_effect_handler_registry_lists_registered_handlers() -> None:
    registry = EffectHandlerRegistry()
    channel_handler = _RecordingHandler()
    memory_handler = _RecordingHandler()
    registry.register("enqueue_channel_reply", "channel", channel_handler)
    registry.register("save_memory", "memory", memory_handler)

    assert registry.list_handlers() == [
        {
            "type": "enqueue_channel_reply",
            "owner": "channel",
            "handler": "_RecordingHandler",
        },
        {
            "type": "save_memory",
            "owner": "memory",
            "handler": "_RecordingHandler",
        },
    ]
    assert effect_handler_registry_payload(registry) == {
        "count": 2,
        "owners": ["channel", "memory"],
        "types": ["enqueue_channel_reply", "save_memory"],
        "fallbacks": [
            {
                "type": "enqueue_channel_reply",
                "owner": "channel",
                "fallback_for": "missing exact channel owner",
            }
        ],
        "items": registry.list_handlers(),
    }


def test_effect_handler_opt_in_accepts_specific_selector() -> None:
    ctx = _ctx()
    ctx.signals["effects"] = {"handler_opt_in": ["memory:save_memory"]}

    assert effect_handler_opt_in_enabled(ctx, owner="memory", effect_type="save_memory")
    assert not effect_handler_opt_in_enabled(
        ctx,
        owner="wxbot",
        effect_type="enqueue_reply",
    )


@pytest.mark.asyncio
async def test_effect_dispatcher_commits_then_runs_registered_handler() -> None:
    handler = _RecordingHandler()
    dispatcher, committer = _dispatcher(handler)
    effect = MessageEffect(
        type="publish_outbound",
        owner="wxbot",
        payload={"text": "ok"},
        idempotency_key="send:1",
    )

    result = await dispatcher.dispatch(effect, _ctx())

    assert result.status == EFFECT_STATUS_RECORDED
    assert result.commit_status == EFFECT_STATUS_RECORDED
    assert result.payload == {"text": "ok"}
    assert len(committer.records) == 1
    assert len(handler.calls) == 1
    handled_effect, commit_record = handler.calls[0]
    assert handled_effect.idempotency_key == "send:1"
    assert handled_effect.payload == {"text": "ok"}
    assert commit_record.status == EFFECT_STATUS_RECORDED


@pytest.mark.asyncio
async def test_effect_dispatcher_dry_run_records_without_running_handler() -> None:
    handler = _RecordingHandler()
    dispatcher, committer = _dispatcher(handler)
    effect = MessageEffect(
        type="publish_outbound",
        owner="wxbot",
        idempotency_key="send:dry-run",
    )

    result = await dispatcher.dispatch(effect, _ctx(), dry_run=True)

    assert result.status == EFFECT_STATUS_DRY_RUN
    assert result.commit_status == EFFECT_STATUS_DRY_RUN
    assert result.dry_run is True
    assert len(committer.records) == 1
    assert handler.calls == []


@pytest.mark.asyncio
async def test_effect_dispatcher_dry_run_does_not_consume_real_gate_key() -> None:
    handler = _RecordingHandler()
    dispatcher, committer = _dispatcher(handler)
    effect = MessageEffect(
        type="publish_outbound",
        owner="wxbot",
        idempotency_key="send:dry-run-then-real",
    )

    dry_run = await dispatcher.dispatch(effect, _ctx(), dry_run=True)
    real = await dispatcher.dispatch(effect, _ctx())

    assert dry_run.status == EFFECT_STATUS_DRY_RUN
    assert real.status == EFFECT_STATUS_RECORDED
    assert real.commit_status == EFFECT_STATUS_RECORDED
    assert len(committer.records) == 2
    assert len(handler.calls) == 1


@pytest.mark.asyncio
async def test_effect_dispatcher_duplicate_commit_skips_handler() -> None:
    handler = _RecordingHandler()
    dispatcher, committer = _dispatcher(handler)
    effect = MessageEffect(
        type="publish_outbound",
        owner="wxbot",
        idempotency_key="send:duplicate",
    )

    first = await dispatcher.dispatch(effect, _ctx())
    second = await dispatcher.dispatch(effect, _ctx())

    assert first.status == EFFECT_STATUS_RECORDED
    assert second.status == EFFECT_STATUS_DUPLICATE
    assert second.commit_status == EFFECT_STATUS_DUPLICATE
    assert len(committer.records) == 1
    assert len(handler.calls) == 1


@pytest.mark.asyncio
async def test_effect_dispatcher_reports_handler_error_after_commit() -> None:
    handler = _BoomHandler()
    dispatcher, committer = _dispatcher(handler)
    effect = MessageEffect(
        type="publish_outbound",
        owner="wxbot",
        idempotency_key="send:boom",
    )

    result = await dispatcher.dispatch(effect, _ctx())

    assert result.status == EFFECT_HANDLER_STATUS_HANDLER_ERROR
    assert result.commit_status == EFFECT_STATUS_RECORDED
    assert result.error == "handler boom"
    assert len(committer.records) == 1
    assert handler.calls == 1


@pytest.mark.asyncio
async def test_effect_dispatcher_reports_missing_handler_after_commit() -> None:
    registry = EffectHandlerRegistry()
    committer = InMemoryEffectCommitter()
    dispatcher = EffectDispatcher(registry, committer)
    effect = MessageEffect(
        type="publish_outbound",
        owner="wxbot",
        idempotency_key="send:missing",
    )

    result = await dispatcher.dispatch(effect, _ctx())

    assert result.status == EFFECT_HANDLER_STATUS_NO_HANDLER
    assert result.commit_status == EFFECT_STATUS_RECORDED
    assert result.error == ""
    assert len(committer.records) == 1


@pytest.mark.asyncio
async def test_core_publish_outbound_handler_publishes_after_commit() -> None:
    bus = _Bus()
    registry = EffectHandlerRegistry()
    register_core_publish_outbound_handler(
        registry,
        bus,
        default_stream="cs:outbound",
    )
    dispatcher = EffectDispatcher(registry, InMemoryEffectCommitter())
    ctx = _ctx()
    effect = MessageEffect(
        type="publish_outbound",
        owner="core",
        payload={
            "stream": "cs:outbound",
            "partition_key": "demo:s1",
            "session_id": "s1",
            "payload": {
                "tenant_id": "demo",
                "channel": "web",
                "session_id": "s1",
                "segments": [{"content": "hello"}],
            },
        },
        idempotency_key="core:publish_outbound:demo:s1:trace-1",
    )

    result = await dispatcher.dispatch(effect, ctx)

    assert result.status == EFFECT_STATUS_RECORDED
    assert bus.messages == [
        {
            "stream": "cs:outbound",
            "payload": {
                "tenant_id": "demo",
                "channel": "web",
                "session_id": "s1",
                "segments": [{"content": "hello"}],
            },
            "partition_key": "demo:s1",
        }
    ]
    assert ctx.signals["effects"]["published_outbound"] == [
        {
            "type": "publish_outbound",
            "owner": "core",
            "stream": "cs:outbound",
            "partition_key": "demo:s1",
            "message_id": "bus-message-1",
        }
    ]


@pytest.mark.asyncio
async def test_core_publish_outbound_handler_uses_default_stream_and_session_key() -> None:
    bus = _Bus()
    handler = CorePublishOutboundEffectHandler(bus, default_stream="cs:outbound")
    ctx = _ctx()
    effect = MessageEffect(
        type="publish_outbound",
        owner="core",
        payload={"payload": {"text": "ok"}},
        idempotency_key="core:publish_outbound:default-stream",
    )
    record = EffectCommitRecord(
        type=effect.type,
        owner=effect.owner,
        idempotency_key=effect.idempotency_key,
        payload=dict(effect.payload),
        status=EFFECT_STATUS_RECORDED,
    )

    await handler(effect, ctx, record)

    assert bus.messages == [
        {
            "stream": "cs:outbound",
            "payload": {"text": "ok"},
            "partition_key": "demo:s1",
        }
    ]


@pytest.mark.asyncio
async def test_core_publish_outbound_handler_rejects_cross_scope_partition_key() -> None:
    bus = _Bus()
    handler = CorePublishOutboundEffectHandler(bus, default_stream="cs:outbound")
    ctx = _ctx()
    effect = MessageEffect(
        type="publish_outbound",
        owner="core",
        payload={
            "partition_key": "other:s1",
            "payload": {
                "tenant_id": "demo",
                "session_id": "s1",
                "text": "ok",
            },
        },
        idempotency_key="core:publish_outbound:wrong-scope",
    )
    record = EffectCommitRecord(
        type=effect.type,
        owner=effect.owner,
        idempotency_key=effect.idempotency_key,
        payload=dict(effect.payload),
        status=EFFECT_STATUS_RECORDED,
    )

    with pytest.raises(ValueError, match="partition scope mismatch"):
        await handler(effect, ctx, record)

    assert bus.messages == []


@pytest.mark.asyncio
async def test_core_publish_outbound_handler_reports_invalid_payload() -> None:
    bus = _Bus()
    registry = EffectHandlerRegistry()
    register_core_publish_outbound_handler(registry, bus, default_stream="cs:outbound")
    dispatcher = EffectDispatcher(registry, InMemoryEffectCommitter())
    effect = MessageEffect(
        type="publish_outbound",
        owner="core",
        payload={"payload": "not-a-dict"},
        idempotency_key="core:publish_outbound:invalid-payload",
    )

    result = await dispatcher.dispatch(effect, _ctx())

    assert result.status == EFFECT_HANDLER_STATUS_HANDLER_ERROR
    assert result.commit_status == EFFECT_STATUS_RECORDED
    assert result.error == "publish_outbound effect missing payload"
    assert bus.messages == []


@pytest.mark.asyncio
async def test_core_publish_outbound_handler_dry_run_skips_bus_publish() -> None:
    bus = _Bus()
    registry = EffectHandlerRegistry()
    register_core_publish_outbound_handler(registry, bus, default_stream="cs:outbound")
    dispatcher = EffectDispatcher(registry, InMemoryEffectCommitter())
    effect = MessageEffect(
        type="publish_outbound",
        owner="core",
        payload={"payload": {"text": "ok"}},
        idempotency_key="core:publish_outbound:dry-run",
    )

    result = await dispatcher.dispatch(effect, _ctx(), dry_run=True)

    assert result.status == EFFECT_STATUS_DRY_RUN
    assert bus.messages == []


@pytest.mark.asyncio
async def test_core_session_append_turn_handler_persists_after_commit() -> None:
    sessions = _SessionManager()
    registry = EffectHandlerRegistry()
    register_core_session_effect_handlers(registry, sessions)
    dispatcher = EffectDispatcher(registry, InMemoryEffectCommitter())
    ctx = _ctx()
    ctx.session = Session(
        session_id="s1",
        tenant_id="demo",
        user_id="u1",
        channel=Channel.WEB,
    )
    turn = Turn(
        session_id="s1",
        role=Role.USER,
        content="hello",
        trace_id="trace-1",
        metadata={"cleaned_content": "hello"},
    )
    effect = MessageEffect(
        type="append_user_turn",
        owner="core",
        payload={"turn": turn.model_dump(mode="json")},
        idempotency_key="core:append_user_turn:demo:s1:trace-1",
    )

    result = await dispatcher.dispatch(effect, ctx)

    assert result.status == EFFECT_STATUS_RECORDED
    assert sessions.appended == [turn]
    assert ctx.session.turns == [turn]
    assert ctx.signals["effects"]["session_turns"] == [
        {
            "type": "append_user_turn",
            "owner": "core",
            "session_id": "s1",
            "role": "user",
            "turn_id": turn.turn_id,
        }
    ]


@pytest.mark.asyncio
async def test_core_session_append_turn_duplicate_skips_session_write() -> None:
    sessions = _SessionManager()
    registry = EffectHandlerRegistry()
    register_core_session_effect_handlers(registry, sessions)
    dispatcher = EffectDispatcher(registry, InMemoryEffectCommitter())
    ctx = _ctx()
    ctx.session = Session(
        session_id="s1",
        tenant_id="demo",
        user_id="u1",
        channel=Channel.WEB,
    )
    turn = Turn(session_id="s1", role=Role.ASSISTANT, content="answer")
    effect = MessageEffect(
        type="append_assistant_turn",
        owner="core",
        payload={"turn": turn.model_dump(mode="json")},
        idempotency_key="core:append_assistant_turn:demo:s1:trace-1",
    )

    first = await dispatcher.dispatch(effect, ctx)
    second = await dispatcher.dispatch(effect, ctx)

    assert first.status == EFFECT_STATUS_RECORDED
    assert second.status == EFFECT_STATUS_DUPLICATE
    assert sessions.appended == [turn]
    assert ctx.session.turns == [turn]


@pytest.mark.asyncio
async def test_core_session_state_handler_persists_after_commit() -> None:
    sessions = _SessionManager()
    registry = EffectHandlerRegistry()
    register_core_session_effect_handlers(registry, sessions)
    dispatcher = EffectDispatcher(registry, InMemoryEffectCommitter())
    ctx = _ctx()
    ctx.session = Session(
        session_id="s1",
        tenant_id="demo",
        user_id="u1",
        channel=Channel.WEB,
    )
    effect = MessageEffect(
        type="set_session_state",
        owner="core",
        payload={"state": "chatting"},
        idempotency_key="core:set_session_state:demo:s1:trace-1:chatting",
    )

    result = await dispatcher.dispatch(effect, ctx)

    assert result.status == EFFECT_STATUS_RECORDED
    assert sessions.states == [SessionState.CHATTING]
    assert ctx.session.state == SessionState.CHATTING
    assert ctx.signals["effects"]["session_states"] == [
        {
            "type": "set_session_state",
            "owner": "core",
            "session_id": "s1",
            "state": "chatting",
        }
    ]


@pytest.mark.asyncio
async def test_channel_reply_effect_handler_enqueues_text_reply() -> None:
    outbound = _ChannelOutbound()
    channel_registry = ChannelRegistry()
    channel_registry.register_outbound("wechat", outbound)
    registry = EffectHandlerRegistry()
    registry.register(
        "enqueue_channel_reply",
        "wxbot",
        ChannelReplyEffectHandler(channel_registry),
    )
    dispatcher = EffectDispatcher(registry, InMemoryEffectCommitter())
    ctx = _ctx()
    ctx.event.metadata.update(
        {
            "session_name": "测试群",
            "sender_wxid": "wxid_sender",
            "msg_svr_id": "msg-1",
        }
    )
    effect = MessageEffect(
        type="enqueue_channel_reply",
        owner="wxbot",
        payload={
            "channel": "wechat",
            "tenant_id": "demo",
            "session_id": "wx-session-1",
            "session_name": "测试群",
            "sender_wxid": "wxid_sender",
            "body": {"type": "text", "text": "第一条文本"},
            "trace_id": "trace-1",
            "delivery": {
                "command_id": "wxbot-reply:demo:m-1:0",
                "idempotency_key": "wxbot-reply:demo:m-1:0",
                "mention_sender": True,
                "reply_to_msg_svr_id": "msg-1",
            },
            "command_id": "wxbot-reply:demo:m-1:0",
        },
        idempotency_key="wxbot-reply:demo:m-1:0",
    )

    result = await dispatcher.dispatch(effect, ctx)

    assert result.status == EFFECT_STATUS_RECORDED
    assert len(outbound.text_calls) == 1
    call = outbound.text_calls[0]
    assert call["text"] == "第一条文本"
    assert call["target"].channel == "wechat"
    assert call["target"].session_id == "wx-session-1"
    assert call["target"].sender_id == "wxid_sender"
    assert call["target"].reply_to_message_id == "msg-1"
    assert call["options"].mention_sender is True
    assert call["options"].idempotency_key == "wxbot-reply:demo:m-1:0"
    assert ctx.signals["effects"]["channel_replies"][0]["message_id"] == "queued-1"


@pytest.mark.asyncio
async def test_channel_reply_effect_handler_uses_wxbot_channel_outbound_store() -> None:
    store = _WxbotReplyStore()
    channel_registry = ChannelRegistry()
    channel_registry.register_adapter_outbound(
        "wechat-sdk",
        WxbotChannelOutbound(
            store,
            social_policy_store=_WxbotPolicyStore(),
            connection_store=_ManagedWxbotConnectionStore(),
        ),
        channel="wechat",
    )
    registry = EffectHandlerRegistry()
    registry.register(
        "enqueue_channel_reply",
        "wxbot",
        ChannelReplyEffectHandler(channel_registry),
    )
    dispatcher = EffectDispatcher(registry, InMemoryEffectCommitter())
    effect = MessageEffect(
        type="enqueue_channel_reply",
        owner="wxbot",
        payload={
            "channel": "wechat",
            "adapter_id": "wechat-sdk",
            "connection_id": "wechat-main",
            "tenant_id": "demo",
            "session_id": "wx-session-1@chatroom",
            "session_name": "测试群",
            "session_kind": "group",
            "sender_name": "客服",
            "sender_wxid": "wxid_sender",
            "body": "群回复",
            "trace_id": "trace-1",
            "reply_to_msg_svr_id": "msg-1",
            "delivery": {
                "command_id": "wxbot-reply:demo:m-1:0",
                "idempotency_key": "wxbot-reply:demo:m-1:0",
                "participation_status": "must_reply",
                "source_message_id": "msg-1",
                "participation_policy_version": 7,
                "send_revalidation_enabled": True,
                "response_kind": "tool_result",
                "speech_class": "obligation",
            },
        },
        idempotency_key="wxbot-reply:demo:m-1:0",
    )

    result = await dispatcher.dispatch(effect, _ctx())

    assert result.status == EFFECT_STATUS_RECORDED
    assert len(store.calls) == 1
    call = store.calls[0]
    assert call["reply_text"] == "群回复"
    assert call["session_id"] == "wx-session-1@chatroom"
    assert call["sender_wxid"] == "wxid_sender"
    assert call["mention_sender"] is False
    assert call["reply_to_msg_svr_id"] == "msg-1"
    assert call["command_id"] == "wxbot-reply:demo:m-1:0"
    assert call["delivery"]["command_id"] == "wxbot-reply:demo:m-1:0"
    assert call["delivery"]["adapter_id"] == "wechat-sdk"
    assert call["delivery"]["connection_id"] == "wechat-main"


@pytest.mark.asyncio
async def test_channel_reply_effect_handler_duplicate_commit_skips_provider() -> None:
    outbound = _ChannelOutbound()
    channel_registry = ChannelRegistry()
    channel_registry.register_outbound("wechat", outbound)
    registry = EffectHandlerRegistry()
    registry.register(
        "enqueue_channel_reply",
        "wxbot",
        ChannelReplyEffectHandler(channel_registry),
    )
    dispatcher = EffectDispatcher(registry, InMemoryEffectCommitter())
    effect = MessageEffect(
        type="enqueue_channel_reply",
        owner="wxbot",
        payload={"channel": "wechat", "body": "hello"},
        idempotency_key="channel:duplicate",
    )

    first = await dispatcher.dispatch(effect, _ctx())
    second = await dispatcher.dispatch(effect, _ctx())

    assert first.status == EFFECT_STATUS_RECORDED
    assert second.status == EFFECT_STATUS_DUPLICATE
    assert len(outbound.text_calls) == 1


@pytest.mark.asyncio
async def test_channel_reply_effect_handler_dry_run_skips_provider() -> None:
    outbound = _ChannelOutbound()
    channel_registry = ChannelRegistry()
    channel_registry.register_outbound("wechat", outbound)
    registry = EffectHandlerRegistry()
    registry.register(
        "enqueue_channel_reply",
        "wxbot",
        ChannelReplyEffectHandler(channel_registry),
    )
    dispatcher = EffectDispatcher(registry, InMemoryEffectCommitter())
    effect = MessageEffect(
        type="enqueue_channel_reply",
        owner="wxbot",
        payload={"channel": "wechat", "body": "hello"},
        idempotency_key="channel:dry-run",
    )

    result = await dispatcher.dispatch(effect, _ctx(), dry_run=True)

    assert result.status == EFFECT_STATUS_DRY_RUN
    assert outbound.text_calls == []


@pytest.mark.asyncio
async def test_channel_reply_effect_handler_reports_missing_provider() -> None:
    registry = EffectHandlerRegistry()
    registry.register(
        "enqueue_channel_reply",
        "wxbot",
        ChannelReplyEffectHandler(ChannelRegistry()),
    )
    dispatcher = EffectDispatcher(registry, InMemoryEffectCommitter())
    effect = MessageEffect(
        type="enqueue_channel_reply",
        owner="wxbot",
        payload={"channel": "wechat", "body": "hello"},
        idempotency_key="channel:no-provider",
    )

    result = await dispatcher.dispatch(effect, _ctx())

    assert result.status == EFFECT_HANDLER_STATUS_HANDLER_ERROR
    assert "channel outbound provider not registered: wechat" in result.error


@pytest.mark.asyncio
async def test_channel_reply_effect_handler_falls_back_to_generic_channel_owner() -> None:
    outbound = _ChannelOutbound()
    channel_registry = ChannelRegistry()
    channel_registry.register_outbound("discord", outbound)
    registry = EffectHandlerRegistry()
    registry.register(
        "enqueue_channel_reply",
        "channel",
        ChannelReplyEffectHandler(channel_registry),
    )
    dispatcher = EffectDispatcher(registry, InMemoryEffectCommitter())
    ctx = _ctx()
    effect = MessageEffect(
        type="enqueue_channel_reply",
        owner="discord",
        payload={
            "channel": "discord",
            "tenant_id": "demo",
            "session_id": "discord-thread-1",
            "body": "discord reply",
        },
        idempotency_key="discord:reply:1",
    )

    result = await dispatcher.dispatch(effect, ctx)

    assert result.status == EFFECT_STATUS_RECORDED
    assert len(outbound.text_calls) == 1
    assert outbound.text_calls[0]["target"].channel == "discord"
    assert outbound.text_calls[0]["target"].session_id == "discord-thread-1"
    assert outbound.text_calls[0]["text"] == "discord reply"
    assert ctx.signals["effects"]["channel_replies"][0]["owner"] == "discord"
    assert ctx.signals["effects"]["channel_replies"][0]["channel"] == "discord"


@pytest.mark.asyncio
async def test_channel_reply_effect_handler_prefers_exact_owner_over_generic_channel() -> None:
    specific = _RecordingHandler()
    generic = _RecordingHandler()
    registry = EffectHandlerRegistry()
    registry.register("enqueue_channel_reply", "wxbot", specific)
    registry.register("enqueue_channel_reply", "channel", generic)
    dispatcher = EffectDispatcher(registry, InMemoryEffectCommitter())
    effect = MessageEffect(
        type="enqueue_channel_reply",
        owner="wxbot",
        payload={"channel": "wechat", "body": "wechat reply"},
        idempotency_key="wxbot:reply:exact-owner",
    )

    result = await dispatcher.dispatch(effect, _ctx())

    assert result.status == EFFECT_STATUS_RECORDED
    assert len(specific.calls) == 1
    assert generic.calls == []


@pytest.mark.asyncio
async def test_memory_save_effect_handler_persists_after_commit() -> None:
    store = _MemoryStore()
    registry = EffectHandlerRegistry()
    register_memory_save_handler(registry, store)
    dispatcher = EffectDispatcher(registry, InMemoryEffectCommitter())
    event = InboundEvent(
        message_id="m-2",
        tenant_id="demo",
        channel=Channel.DISCORD,
        user_id="discord-user-b",
        session_id="discord-channel-2",
        message=Message(content="我要看物流"),
        metadata={"source": "discord"},
        trace_id="trace-2",
    )
    session = Session(
        session_id="discord-channel-2",
        tenant_id="demo",
        user_id="discord-channel-2",
        channel=Channel.DISCORD,
    )
    reply = OutboundReply(
        tenant_id="demo",
        channel=Channel.DISCORD,
        user_id="discord-channel-2",
        session_id="discord-channel-2",
        segments=[ReplySegment(content="物流今天会继续更新")],
    )
    ctx = PipelineContext(
        event=event,
        trace_id="trace-2",
        session=session,
        pre=PreprocessedMessage(original_text="我要看物流", cleaned_text="我要看物流"),
        reply=reply,
    )
    effect = MessageEffect(
        type="save_memory",
        owner="memory",
        payload={
            "tenant_id": "demo",
            "channel": "discord",
            "source_key": "discord",
            "session_id": "discord-channel-2",
            "user_id": "discord-user-b",
            "user_text": "我要看物流",
            "assistant_text": "物流今天会继续更新",
            "trace_id": "trace-2",
        },
        idempotency_key="memory:save:demo:discord:discord:discord-channel-2:discord-user-b:trace-2",
    )

    result = await dispatcher.dispatch(effect, ctx)

    assert result.status == EFFECT_STATUS_RECORDED
    assert store.remember_calls == [
        {
            "tenant_id": "demo",
            "channel": "discord",
            "source_key": "discord",
            "user_id": "discord-user-b",
            "session_id": "discord-channel-2",
            "user_text": "我要看物流",
            "assistant_text": "物流今天会继续更新",
            "trace_id": "trace-2",
            "origin_session_kind": "unknown",
            "audience_scope": "private",
            "allowed_session_ids": [],
            "sensitivity_category": "normal",
            "expires_at": None,
            "source_kind": "conversation",
        }
    ]
    assert ctx.signals["memory"]["user_profile"]["message_count"] == 4
    assert ctx.extras["user_memory_profile"]["message_count"] == 4
    assert session.variables["user_memory"]["user_id"] == "discord-user-b"
    assert session.variables["user_memory"]["session_profile"]["updated_at"] == (
        "2026-04-21T12:03:00+00:00"
    )
    assert (
        session.variables["user_memory"]["memory_items"]["identity"][0]["content"]
        == "人工标记为 VIP"
    )
    assert session.variables["user_memory"]["memory_items"]["identity"][0]["created_at"] == (
        "2026-04-21T12:02:00+00:00"
    )
