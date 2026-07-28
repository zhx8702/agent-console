"""Unit tests for the Dialog Orchestrator (M3)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import fakeredis.aioredis
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.commands import CommandDefinition, CommandRegistryService
from app.common.config import Settings
from app.common.types import (
    CapabilityResult,
    Channel,
    InboundEvent,
    Message,
    MessageType,
    OutboundReply,
    PreprocessedMessage,
    ReplySegment,
    ReplyType,
    Role,
    RouteDecision,
    RouteType,
    Session,
    SessionState,
    Turn,
)
from app.models.reliability import (
    MessageEffectIntentRow,
    MessageOutboxRow,
    ProcessedMessageRow,
)
from app.models.session import SessionRow, TurnRow
from app.orchestrator.engine import DialogOrchestrator
from app.orchestrator.flow import FlowStepRegistry, StepResult, build_default_flow_registry
from app.orchestrator.outcome import (
    PermanentProcessingError,
    ProcessingStatus,
)
from app.orchestrator.pipeline import PipelineContext
from app.plugin.hooks import HookAbort, HookPoint, HookRunner
from app.reliability import MessageOutboxRelay, MessageReliabilityStore
from app.session.manager import SessionManager
from plugins.commands.hooks import CommandCenterHook, CommandDispatchStep
from plugins.commands.plugin import plugin as commands_plugin
from plugins.credits.hooks import (
    CreditDeductionHook,
    CreditReserveStep,
    CreditSettlementHook,
    CreditSettleStep,
    build_credit_command_definitions,
)
from plugins.credits.plugin import plugin as credits_plugin
from plugins.draw.plugin import plugin as draw_plugin
from plugins.memory.hooks import (
    MemoryContextHook,
    MemoryControlStep,
    MemoryLoadStep,
    MemoryPersistenceHook,
    MemorySaveStep,
)
from plugins.memory.plugin import plugin as memory_plugin
from plugins.moderation.hooks import (
    REMINDER_TEXT,
    ModerationAuditHook,
    ModerationEnforceInputStep,
    ModerationInspectInputStep,
    ModerationReplaceReminderHook,
)
from plugins.moderation.plugin import plugin as moderation_plugin
from plugins.persona_extract.plugin import plugin as persona_extract_plugin
from plugins.repeater.hooks import RepeaterDetectStep, RepeaterHook
from plugins.repeater.plugin import plugin as repeater_plugin
from plugins.wxbot.hooks import (
    WxbotOutboundPolicyStep,
    WxbotReplyPolicyHook,
    WxbotReplyPolicyStep,
    WxbotReplyQueueHook,
)
from plugins.wxbot.plugin import plugin as wxbot_plugin

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakePreprocessor:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, message: Message) -> PreprocessedMessage:
        self.calls += 1
        return PreprocessedMessage(
            original_text=message.content,
            cleaned_text=message.content.strip(),
            pii_map={"<PII:phone:1>": "13800000000"} if "phone" in message.content else {},
        )


class FakeRouter:
    def __init__(self, route_type: RouteType = RouteType.FAQ) -> None:
        self.route_type = route_type
        self.last_signals: dict[str, Any] | None = None

    async def decide(
        self,
        pre: PreprocessedMessage,
        session: Session,
        signals: dict[str, Any] | None = None,
    ) -> RouteDecision:
        self.last_signals = dict(signals or {})
        return RouteDecision(type=self.route_type, confidence=0.9, reason="test")


class FakeSafety:
    def __init__(self, input_safe: bool = True, output_safe: bool = True) -> None:
        self.input_safe = input_safe
        self.output_safe = output_safe

    async def check_input(self, pre: PreprocessedMessage) -> bool:
        return self.input_safe

    async def check_output(self, result: CapabilityResult) -> bool:
        return self.output_safe


class FakePostprocessor:
    async def run(self, result: CapabilityResult, session: Session) -> OutboundReply:
        return OutboundReply(
            tenant_id=session.tenant_id,
            channel=session.channel,
            user_id=session.user_id,
            session_id=session.session_id,
            type=ReplyType.TEXT,
            segments=[ReplySegment(type=ReplyType.TEXT, content=result.reply_text)],
            citations=list(result.citations),
        )


class FakeCapability:
    name = "fake_faq"

    def __init__(self, reply_text: str = "answer", raise_exc: bool = False) -> None:
        self.reply_text = reply_text
        self.raise_exc = raise_exc
        self.calls = 0
        self.last_hints: dict[str, Any] | None = None

    async def answer(
        self,
        pre: PreprocessedMessage,
        session: Session,
        hints: dict[str, Any] | None = None,
    ) -> CapabilityResult:
        self.calls += 1
        self.last_hints = hints
        if self.raise_exc:
            raise RuntimeError("capability boom")
        return CapabilityResult(route=RouteType.FAQ, reply_text=self.reply_text)


class FakeRouteCapability(FakeCapability):
    def __init__(
        self,
        route: RouteType,
        reply_text: str = "answer",
        raise_exc: bool = False,
    ) -> None:
        super().__init__(reply_text=reply_text, raise_exc=raise_exc)
        self.route = route

    async def answer(
        self,
        pre: PreprocessedMessage,
        session: Session,
        hints: dict[str, Any] | None = None,
    ) -> CapabilityResult:
        await super().answer(pre, session, hints)
        return CapabilityResult(route=self.route, reply_text=self.reply_text)


class FakeFAQCapability(FakeCapability):
    async def preview_match(
        self,
        pre: PreprocessedMessage,
        session: Session,
        hints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "matched": True,
            "score": 0.97,
            "threshold": 0.88,
            "verdict": "CLEAR",
            "scope": "global",
            "scope_session_id": None,
            "question": pre.cleaned_text,
            "answer": self.reply_text,
            "faq_id": "faq-1",
        }


class FakeBus:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, Any], str | None]] = []

    async def publish(
        self,
        stream: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        partition_key: str | None = None,
    ) -> str:
        self.messages.append((stream, payload, partition_key))
        return f"msg-{len(self.messages)}"

    async def publish_once(
        self,
        stream: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        headers: dict[str, str] | None = None,
        partition_key: str | None = None,
    ) -> str:
        _ = idempotency_key
        return await self.publish(
            stream,
            payload,
            headers=headers,
            partition_key=partition_key,
        )

    async def ensure_group(self, *a, **kw) -> None:  # pragma: no cover
        pass

    async def consume(self, *a, **kw):  # pragma: no cover
        if False:
            yield

    async def ack(self, *a, **kw) -> None:  # pragma: no cover
        pass

    async def move_to_dlq(self, *a, **kw) -> None:  # pragma: no cover
        pass

    async def close(self) -> None:  # pragma: no cover
        pass


class FailingPublishBus(FakeBus):
    async def publish(
        self,
        stream: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        partition_key: str | None = None,
    ) -> str:
        _ = stream, payload, headers, partition_key
        raise ConnectionError("redis publish unavailable")


class HeaderCapturingBus(FakeBus):
    def __init__(self) -> None:
        super().__init__()
        self.headers: list[dict[str, str]] = []

    async def publish(
        self,
        stream: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        partition_key: str | None = None,
    ) -> str:
        self.headers.append(dict(headers or {}))
        return await super().publish(
            stream,
            payload,
            headers=headers,
            partition_key=partition_key,
        )


class CountingFlowStep:
    def __init__(self, reason: str = "counted") -> None:
        self.calls = 0
        self.reason = reason

    async def run(self, ctx: PipelineContext) -> StepResult:
        self.calls += 1
        return StepResult(reason=self.reason)


class _CommandStore:
    async def get_config(self, tenant_id: str, *, catalog: list[dict[str, object]]) -> dict:
        return {
            "tenant_id": tenant_id,
            "catalog": catalog,
            "admin_user_ids": [],
            "user_commands": ["/ping", "/签到", "/checkin", "/余额", "/balance"],
            "admin_commands": [],
        }


class _MemoryControlStore:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def create_memory_item(self, **kwargs: Any) -> dict[str, Any]:
        self.created.append(kwargs)
        return {"id": len(self.created), **kwargs}


class _ModerationStore:
    def __init__(self, *, reminder_mode: str = "replace") -> None:
        self.reminder_mode = reminder_mode
        self.logged: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []

    async def get_config(self, tenant_id: str, session_id: str) -> dict:
        return {
            "enabled": True,
            "reminder_mode": self.reminder_mode,
            "webhook_enabled": False,
            "webhook_url": "",
        }

    async def match_keywords(self, tenant_id: str, session_id: str, text: str) -> list[str]:
        return ["敏感词"] if "敏感词" in text else []

    async def log_event(self, **kwargs):
        self.logged.append(kwargs)
        return len(self.logged)

    async def update_event(self, event_id: int, **kwargs) -> None:
        self.updated.append({"event_id": event_id, **kwargs})


class _RepeaterStore:
    def __init__(self) -> None:
        self.recorded: list[dict[str, str]] = []

    async def get_config(self, tenant_id: str, session_id: str) -> dict:
        return {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "enabled": True,
            "cooldown_seconds": 300,
        }

    async def should_trigger(
        self,
        tenant_id: str,
        session_id: str,
        content_text: str,
        cooldown_seconds: int,
    ) -> bool:
        _ = tenant_id, session_id, content_text, cooldown_seconds
        return True

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
        return len(self.recorded)


class _CreditStore:
    def __init__(self, *, cost: int = 4, balance: int = 20) -> None:
        self.cost = cost
        self.balance = balance
        self.reserve_calls: list[dict[str, Any]] = []
        self.capture_calls: list[dict[str, Any]] = []
        self.release_calls: list[str] = []
        self.checkin_calls: list[dict[str, Any]] = []

    async def get_config(self, tenant_id: str, session_id: str) -> dict:
        return {
            "enabled": True,
            "credit_name": "积分",
            "cost_per_chat": self.cost,
            "checkin_mode": 1,
        }

    async def peek_balance(
        self,
        tenant_id: str,
        session_id: str,
        user_id: str,
        display_name: str = "",
    ) -> int:
        _ = tenant_id, session_id, user_id, display_name
        return self.balance

    async def get_balance(
        self,
        tenant_id: str,
        session_id: str,
        user_id: str,
        display_name: str = "",
    ) -> int:
        _ = tenant_id, session_id, user_id, display_name
        return self.balance

    async def checkin(
        self,
        tenant_id: str,
        session_id: str,
        user_id: str,
        *,
        display_name: str = "",
    ) -> dict[str, object]:
        self.checkin_calls.append(
            {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "user_id": user_id,
                "display_name": display_name,
            }
        )
        reward = 10
        self.balance += reward
        return {
            "checked_in": True,
            "already_checked_in": False,
            "reward": reward,
            "balance": self.balance,
        }

    async def reserve_charge(
        self,
        tenant_id: str,
        session_id: str,
        user_id: str,
        amount: int,
        *,
        reason: str,
        reference: str = "",
        display_name: str = "",
        metadata: dict[str, object] | None = None,
        idempotency_key: str = "",
    ) -> dict[str, object]:
        self.reserve_calls.append(
            {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "user_id": user_id,
                "amount": amount,
                "reason": reason,
                "reference": reference,
                "display_name": display_name,
                "metadata": dict(metadata or {}),
                "idempotency_key": idempotency_key,
            }
        )
        if self.balance < amount:
            raise ValueError("insufficient")
        self.balance -= amount
        return {"reservation_id": f"reservation-{len(self.reserve_calls)}", "amount": amount}

    async def capture_reservation(
        self,
        reservation_id: str,
        *,
        amount: int | None = None,
        reference: str = "",
        display_name: str = "",
    ) -> dict[str, object]:
        self.capture_calls.append(
            {
                "reservation_id": reservation_id,
                "amount": amount,
                "reference": reference,
                "display_name": display_name,
            }
        )
        return {"reservation_id": reservation_id, "amount": amount or 0}

    async def release_reservation(self, reservation_id: str) -> None:
        self.release_calls.append(reservation_id)

    async def get_member_detail(
        self,
        tenant_id: str,
        session_id: str,
        user_id: str,
        *,
        ledger_limit: int = 5,
    ) -> dict[str, object]:
        _ = tenant_id, session_id, ledger_limit
        return {"user_id": user_id, "credits": self.balance, "rank": 2}


class _MemoryStore:
    def __init__(self) -> None:
        self.get_calls: list[dict[str, str]] = []
        self.remember_calls: list[dict[str, str]] = []

    async def get_runtime_profile(self, **kwargs) -> dict[str, object]:
        self.get_calls.append(dict(kwargs))
        return {
            "tenant_id": kwargs["tenant_id"],
            "channel": kwargs["channel"],
            "source_key": kwargs["source_key"],
            "user_id": kwargs["user_id"],
            "session_id": kwargs["session_id"],
            "short_term_memory": "用户最近问了物流进度",
            "long_term_memory": "偏好微信联系",
            "manual_notes": "VIP 客户",
            "identity_manual_notes": "VIP 客户",
            "session_manual_notes": "",
            "message_count": 3,
            "identity_message_count": 3,
            "session_message_count": 1,
            "imported_message_count": 0,
            "last_session_id": kwargs["session_id"],
            "identity_profile": {"user_id": kwargs["user_id"]},
            "session_profile": {"session_id": kwargs["session_id"]},
        }

    async def remember_interaction(self, **kwargs) -> dict[str, object]:
        self.remember_calls.append(dict(kwargs))
        return {
            "tenant_id": kwargs["tenant_id"],
            "channel": kwargs["channel"],
            "source_key": kwargs["source_key"],
            "user_id": kwargs["user_id"],
            "session_id": kwargs["session_id"],
            "short_term_memory": "用户最近说：我要查物流",
            "long_term_memory": "偏好微信联系",
            "manual_notes": "VIP 客户",
            "identity_manual_notes": "VIP 客户",
            "session_manual_notes": "",
            "message_count": 4,
            "identity_message_count": 4,
            "session_message_count": 2,
            "imported_message_count": 0,
            "last_session_id": kwargs["session_id"],
            "identity_profile": {"user_id": kwargs["user_id"]},
            "session_profile": {"session_id": kwargs["session_id"]},
        }


class _WxbotStore:
    def __init__(self, *, mode: str = "contains") -> None:
        self.policy: dict[str, object] = {
            "tenant_id": "demo",
            "reply_mode": mode,
            "effective_mode": mode,
            "default_mention_sender": True,
            "effective_mention_sender": True,
            "trigger_keywords": ["报价"],
        }
        self.policy_calls: list[dict[str, str]] = []
        self.calls: list[dict[str, object]] = []
        self.interactions: list[str] = []

    async def get_session_policy(self, tenant_id: str, session_id: str) -> dict[str, object]:
        self.policy_calls.append({"tenant_id": tenant_id, "session_id": session_id})
        return dict(self.policy, session_id=session_id)

    async def record_interactive_inbound(self, **kwargs: object) -> None:
        self.interactions.append(str(kwargs["message_id"]))

    async def get_participation_snapshot(self, *args, **kwargs) -> dict[str, object]:
        _ = args, kwargs
        return {
            "bot_messages_last_40": 0,
            "total_messages_last_40": 40,
            "soft_replies_last_10m": 0,
            "soft_replies_last_hour": 0,
            "consecutive_bot_messages": 0,
            "bot_replied_within_60s": False,
            "rapid_multi_party_chat": False,
        }

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
                "sender_wxid": sender_wxid,
                "reply_text": reply_text,
                "trace_id": trace_id,
                "mention_sender": mention_sender,
                "msg_type": msg_type,
                "image_path": image_path,
                "image_url": image_url,
                "reply_to_msg_svr_id": reply_to_msg_svr_id,
                "session_kind": session_kind,
                "source_message": dict(source_message or {}),
                "delivery": dict(delivery or {}),
                "command_id": command_id,
            }
        )
        return len(self.calls)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        SessionRow.__table__,
        TurnRow.__table__,
        ProcessedMessageRow.__table__,
        MessageOutboxRow.__table__,
        MessageEffectIntentRow.__table__,
    ]
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: [t.create(sync_conn) for t in tables])
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield r
    finally:
        await r.aclose()


@pytest.fixture(autouse=True)
def _isolate_flow_effect_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_FLOW_RUNTIME_ENABLED", "false")
    monkeypatch.setenv("ORCHESTRATOR_FLOW_RUNTIME_NAME", "default_compatible_flow")
    monkeypatch.setenv("ORCHESTRATOR_FLOW_RUNTIME_ALLOWED_NAMES", "default_compatible_flow")
    monkeypatch.setenv("ORCHESTRATOR_FLOW_RUNTIME_ALLOW_TARGET_FLOWS", "false")
    monkeypatch.setenv("ORCHESTRATOR_FLOW_SHADOW_ENABLED", "false")
    monkeypatch.setenv("ORCHESTRATOR_FLOW_SHADOW_NAME", "default_compatible_flow")
    monkeypatch.setenv("ORCHESTRATOR_FLOW_SHADOW_MODE", "noop")
    monkeypatch.setenv("ORCHESTRATOR_FLOW_SHADOW_CORE_PREVIEW_ENABLED", "false")
    monkeypatch.setenv("ORCHESTRATOR_FLOW_SHADOW_PLUGIN_DRY_RUN_ENABLED", "false")
    monkeypatch.setenv("ORCHESTRATOR_FLOW_SHADOW_EFFECT_DRY_RUN_ENABLED", "false")
    monkeypatch.setenv("ORCHESTRATOR_FLOW_EFFECT_HANDLERS_ENABLED", "false")
    monkeypatch.setenv("ORCHESTRATOR_FLOW_EFFECT_HANDLER_ALLOWLIST", "")
    monkeypatch.setenv("ORCHESTRATOR_FLOW_EFFECT_COMMIT_BACKEND", "none")
    monkeypatch.setenv("ORCHESTRATOR_FLOW_EFFECT_LOG_BACKEND", "none")
    monkeypatch.setenv("ORCHESTRATOR_FLOW_TRACE_SNAPSHOT_ENABLED", "false")


@pytest.fixture
def settings() -> Settings:
    return Settings(session_window_turns=10, session_lock_ttl_seconds=5)


@pytest.fixture
def session_manager(redis, settings, factory) -> SessionManager:
    return SessionManager(redis=redis, settings=settings, session_factory=factory)


def _make_event(
    content: str = "hello",
    session_id: str = "se_orc_01",
    *,
    channel: Channel = Channel.WEB,
    metadata: dict[str, Any] | None = None,
    message_type: MessageType = MessageType.TEXT,
) -> InboundEvent:
    return InboundEvent(
        message_id="msg_1",
        tenant_id="demo",
        channel=channel,
        user_id="u1",
        session_id=session_id,
        message=Message(type=message_type, content=content),
        metadata=dict(metadata or {}),
    )


def _plugin_flow_registry() -> tuple[FlowStepRegistry, dict[str, set[str]]]:
    registry = build_default_flow_registry()
    plugins = (
        commands_plugin,
        credits_plugin,
        draw_plugin,
        memory_plugin,
        moderation_plugin,
        persona_extract_plugin,
        repeater_plugin,
        wxbot_plugin,
    )
    registry.register_many([step for plugin in plugins for step in plugin.get_flow_steps()])
    return registry, {plugin.meta.name: set(plugin.get_permissions()) for plugin in plugins}


def _target_flow_settings(settings: Settings) -> Settings:
    return Settings(
        session_window_turns=settings.session_window_turns,
        session_lock_ttl_seconds=settings.session_lock_ttl_seconds,
        orchestrator_flow_runtime_enabled=True,
        orchestrator_flow_runtime_name="default_group_channel_flow",
        orchestrator_flow_runtime_allowed_names="default_group_channel_flow",
        orchestrator_flow_runtime_allow_target_flows=True,
    )


def _target_wechat_flow_settings(settings: Settings) -> Settings:
    return Settings(
        session_window_turns=settings.session_window_turns,
        session_lock_ttl_seconds=settings.session_lock_ttl_seconds,
        orchestrator_flow_runtime_enabled=True,
        orchestrator_flow_runtime_name="default_wechat_group_flow",
        orchestrator_flow_runtime_allowed_names="default_wechat_group_flow",
        orchestrator_flow_runtime_allow_target_flows=True,
    )


def _command_service() -> CommandRegistryService:
    service = CommandRegistryService()

    async def _ping(ctx: PipelineContext, args: list[str]) -> str:
        _ = ctx, args
        return "pong"

    service.register(
        [
            CommandDefinition(
                plugin_name="test",
                command="/ping",
                aliases=(),
                description="Ping",
                handler=_ping,
            )
        ]
    )
    return service


def _credit_command_service(store: _CreditStore) -> CommandRegistryService:
    service = CommandRegistryService()
    service.register(build_credit_command_definitions(store))
    return service


def _outbound_text(bus: FakeBus) -> str:
    assert bus.messages
    return str(bus.messages[0][1]["segments"][0]["content"])


def _build(
    session_manager: SessionManager,
    settings: Settings,
    *,
    capability: FakeCapability | None = None,
    safety: FakeSafety | None = None,
    router: FakeRouter | None = None,
    preprocessor: FakePreprocessor | None = None,
    postprocessor: FakePostprocessor | None = None,
    bus: FakeBus | None = None,
    extra_capabilities: dict[RouteType, Any] | None = None,
    hook_runner: HookRunner | None = None,
    flow_step_registry: FlowStepRegistry | None = None,
    flow_owner_permissions: dict[str, set[str]] | None = None,
    flow_step_executors: dict[str, Any] | None = None,
    message_store: MessageReliabilityStore | None = None,
) -> tuple[DialogOrchestrator, FakeBus, FakeCapability]:
    cap = capability or FakeCapability()
    caps = {RouteType.FAQ: cap}
    if extra_capabilities:
        caps.update(extra_capabilities)
    b = bus or FakeBus()
    orc = DialogOrchestrator(
        session_manager=session_manager,
        preprocessor=preprocessor or FakePreprocessor(),
        router=router or FakeRouter(RouteType.FAQ),
        safety=safety or FakeSafety(),
        postprocessor=postprocessor or FakePostprocessor(),
        capabilities=caps,
        bus=b,
        settings=settings,
        hook_runner=hook_runner,
        flow_step_registry=flow_step_registry,
        flow_owner_permissions=flow_owner_permissions,
        flow_step_executors=flow_step_executors,
        message_store=message_store,
    )
    return orc, b, cap


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_happy_path_publishes_reply(session_manager, settings):
    orc, bus, cap = _build(session_manager, settings)
    outcome = await orc.handle(_make_event("hello"))

    assert cap.calls == 1
    assert outcome.status == ProcessingStatus.COMPLETED
    assert orc.last_flow_shadow_result is None
    # One outbound message published to the configured stream.
    assert len(bus.messages) == 1
    stream, payload, pk = bus.messages[0]
    assert stream == settings.bus_outbound_stream
    assert pk == "demo:se_orc_01"
    assert payload["segments"][0]["content"] == "answer"


async def test_transactional_pipeline_commits_inbox_turns_and_outbox_once(
    session_manager,
    settings,
    factory,
):
    store = MessageReliabilityStore(factory)
    orc, bus, cap = _build(
        session_manager,
        settings,
        message_store=store,
    )
    event = _make_event("hello")

    first = await orc.handle(event)

    assert first.status == ProcessingStatus.COMPLETED
    assert cap.calls == 1
    # The transport is not touched until the DB outbox relay runs.
    assert bus.messages == []
    async with factory() as db:
        processed = await db.get(
            ProcessedMessageRow,
            {"tenant_id": event.tenant_id, "message_id": event.message_id},
        )
        outbox = (await db.execute(select(MessageOutboxRow))).scalars().all()
        turns = (
            (
                await db.execute(
                    select(TurnRow)
                    .where(
                        TurnRow.tenant_id == event.tenant_id,
                        TurnRow.session_id == event.session_id,
                    )
                    .order_by(TurnRow.created_at)
                )
            )
            .scalars()
            .all()
        )
    assert processed is not None
    assert processed.status == ProcessingStatus.COMPLETED.value
    assert len(outbox) == 1
    assert [turn.role for turn in turns] == ["user", "assistant"]

    duplicate = await orc.handle(event)

    assert duplicate.status == ProcessingStatus.INTENTIONALLY_SUPPRESSED
    assert duplicate.reason == "duplicate_message:completed"
    assert cap.calls == 1
    async with factory() as db:
        assert len((await db.execute(select(MessageOutboxRow))).scalars().all()) == 1
        assert len((await db.execute(select(TurnRow))).scalars().all()) == 2

    relay = MessageOutboxRelay(
        store,
        bus,
        worker_id="relay-1",
        poll_interval_seconds=0.01,
    )
    assert await relay.drain_once() == 1
    assert await relay.drain_once() == 0
    assert len(bus.messages) == 1
    async with factory() as db:
        published = (await db.execute(select(MessageOutboxRow))).scalar_one()
        assert published.status == "published"
        assert published.published_message_id == "msg-1"


async def test_transactional_flow_commits_effect_intents_with_core_state(
    session_manager,
    settings,
    factory,
):
    runtime_settings = Settings(
        session_window_turns=settings.session_window_turns,
        session_lock_ttl_seconds=settings.session_lock_ttl_seconds,
        orchestrator_flow_runtime_enabled=True,
    )
    store = MessageReliabilityStore(factory)
    orc, bus, _cap = _build(
        session_manager,
        runtime_settings,
        message_store=store,
    )
    event = _make_event("hello")

    outcome = await orc.handle(event)

    assert outcome.status == ProcessingStatus.COMPLETED
    assert bus.messages == []
    async with factory() as db:
        processed = await db.get(
            ProcessedMessageRow,
            {"tenant_id": event.tenant_id, "message_id": event.message_id},
        )
        intents = (
            (
                await db.execute(
                    select(MessageEffectIntentRow).order_by(MessageEffectIntentRow.effect_type)
                )
            )
            .scalars()
            .all()
        )
        outbox = (await db.execute(select(MessageOutboxRow))).scalars().all()
        turns = (await db.execute(select(TurnRow))).scalars().all()

    assert processed is not None and processed.status == "completed"
    assert len(outbox) == 1
    assert len(turns) == 2
    assert {intent.effect_type for intent in intents} == {
        "append_user_turn",
        "commit_turns_and_publish",
    }
    assert {intent.status for intent in intents} == {"completed"}
    assert {intent.source_message_id for intent in intents} == {event.message_id}


async def test_transactional_flow_rolls_back_effect_intents_with_inbox(
    session_manager,
    settings,
    factory,
    monkeypatch,
):
    runtime_settings = Settings(
        session_window_turns=settings.session_window_turns,
        session_lock_ttl_seconds=settings.session_lock_ttl_seconds,
        orchestrator_flow_runtime_enabled=True,
    )
    store = MessageReliabilityStore(factory)
    orc, _bus, _cap = _build(
        session_manager,
        runtime_settings,
        message_store=store,
    )

    async def fail_completion(*_args, **_kwargs) -> None:
        raise RuntimeError("inbox terminal marker unavailable")

    monkeypatch.setattr(store, "complete", fail_completion)
    outcome = await orc.handle(_make_event("hello"))

    assert outcome.status == ProcessingStatus.RETRYABLE_FAILURE
    async with factory() as db:
        assert (await db.execute(select(ProcessedMessageRow))).scalars().all() == []
        assert (await db.execute(select(MessageEffectIntentRow))).scalars().all() == []
        assert (await db.execute(select(MessageOutboxRow))).scalars().all() == []
        assert (await db.execute(select(TurnRow))).scalars().all() == []


async def test_expensive_pipeline_runs_without_an_open_core_database_transaction(
    session_manager,
    settings,
    factory,
):
    store = MessageReliabilityStore(factory)

    class TransactionProbeCapability(FakeCapability):
        async def answer(self, pre, session, hints=None):
            assert store.active_db is None
            assert session_manager.transaction_active is False
            async with factory() as db:
                claim = (await db.execute(select(ProcessedMessageRow))).scalar_one()
                assert claim.status == "processing"
                assert claim.claim_token
            return await super().answer(pre, session, hints)

    capability = TransactionProbeCapability()
    orc, bus, _cap = _build(
        session_manager,
        settings,
        capability=capability,
        message_store=store,
    )

    assert (await orc.handle(_make_event("hello"))).status == ProcessingStatus.COMPLETED
    assert capability.calls == 1
    assert bus.messages == []


async def test_stale_inbox_processing_lease_is_recoverable(factory):
    event = _make_event("hello")
    first = MessageReliabilityStore(factory)
    second = MessageReliabilityStore(factory)

    first_claim = await first.acquire(event, lease_seconds=60)
    active_duplicate = await second.acquire(event, lease_seconds=60)
    assert first_claim.claimed is True
    assert active_duplicate.claimed is False
    assert active_duplicate.status == "processing"

    async with factory() as db:
        row = await db.get(
            ProcessedMessageRow,
            {"tenant_id": event.tenant_id, "message_id": event.message_id},
        )
        assert row is not None
        row.claim_until = datetime(2000, 1, 1, tzinfo=UTC)
        await db.commit()

    recovered = await second.acquire(event, lease_seconds=60)
    assert recovered.claimed is True
    assert recovered.claim_token != first_claim.claim_token
    async with factory() as db:
        row = await db.get(
            ProcessedMessageRow,
            {"tenant_id": event.tenant_id, "message_id": event.message_id},
        )
        assert row is not None
        assert row.attempts == 2


async def test_transactional_pipeline_rolls_back_every_core_write_on_failure(
    session_manager,
    settings,
    factory,
    monkeypatch,
):
    store = MessageReliabilityStore(factory)
    orc, _bus, _cap = _build(
        session_manager,
        settings,
        message_store=store,
    )
    event = _make_event("hello")

    async def fail_completion(*_args, **_kwargs) -> None:
        raise RuntimeError("commit marker unavailable")

    monkeypatch.setattr(store, "complete", fail_completion)
    outcome = await orc.handle(event)

    assert outcome.status == ProcessingStatus.RETRYABLE_FAILURE
    assert outcome.error_type == "RuntimeError"
    async with factory() as db:
        assert (await db.execute(select(ProcessedMessageRow))).scalars().all() == []
        assert (await db.execute(select(MessageOutboxRow))).scalars().all() == []
        assert (await db.execute(select(SessionRow))).scalars().all() == []
        assert (await db.execute(select(TurnRow))).scalars().all() == []


async def test_duplicate_permanent_inbox_preserves_dlq_disposition(
    session_manager,
    settings,
    factory,
):
    store = MessageReliabilityStore(factory)
    orc, _bus, cap = _build(
        session_manager,
        settings,
        message_store=store,
    )
    event = _make_event("invalid")
    async with factory() as db:
        db.add(
            ProcessedMessageRow(
                tenant_id=event.tenant_id,
                message_id=event.message_id,
                session_id=event.session_id,
                user_id=event.user_id,
                trace_id=event.trace_id,
                status=ProcessingStatus.PERMANENT_FAILURE.value,
                route_label="schema",
                reason="unsupported_payload_version",
                error_type="SchemaError",
                received_at=event.received_at,
                completed_at=datetime.now(UTC),
            )
        )
        await db.commit()

    outcome = await orc.handle(event)

    assert outcome.status == ProcessingStatus.PERMANENT_FAILURE
    assert outcome.reason == "unsupported_payload_version"
    assert outcome.error_type == "SchemaError"
    assert cap.calls == 0


async def test_outbox_recovers_publish_failure_and_expired_lease(
    session_manager,
    settings,
    factory,
):
    store = MessageReliabilityStore(factory)
    orc, _bus, _cap = _build(
        session_manager,
        settings,
        message_store=store,
    )
    event = _make_event("hello")
    assert (await orc.handle(event)).status == ProcessingStatus.COMPLETED

    failing_relay = MessageOutboxRelay(
        store,
        FailingPublishBus(),
        worker_id="relay-failing",
    )
    assert await failing_relay.drain_once() == 0
    async with factory() as db:
        row = (await db.execute(select(MessageOutboxRow))).scalar_one()
        assert row.status == "pending"
        assert row.attempts == 1
        assert row.last_error.startswith("ConnectionError:")
        reply_id = row.reply_id
        row.status = "publishing"
        row.lease_owner = "dead-worker"
        row.lease_until = datetime(2000, 1, 1, tzinfo=UTC)
        row.available_at = datetime(2000, 1, 1, tzinfo=UTC)
        await db.commit()

    recovered_bus = HeaderCapturingBus()
    recovering_relay = MessageOutboxRelay(
        store,
        recovered_bus,
        worker_id="relay-recovery",
    )
    assert await recovering_relay.drain_once() == 1
    assert recovered_bus.messages[0][2] == f"{event.tenant_id}:{event.session_id}"
    assert recovered_bus.headers[0]["outbox_reply_id"] == reply_id
    assert recovered_bus.headers[0]["tenant_id"] == event.tenant_id
    async with factory() as db:
        row = (await db.execute(select(MessageOutboxRow))).scalar_one()
        assert row.status == "published"
        assert row.attempts == 2
        assert row.lease_owner == ""


async def test_outbox_reply_identity_is_tenant_scoped(factory):
    store = MessageReliabilityStore(factory)
    payload = {
        "reply_id": "shared-reply",
        "tenant_id": "tenant-a",
        "session_id": "session-a",
        "trace_id": "trace-a",
        "segments": [],
    }
    async with factory() as db:
        async with db.begin():
            await store.enqueue(
                db,
                stream="outbound",
                payload=payload,
                headers=None,
                partition_key=None,
            )
            await store.enqueue(
                db,
                stream="outbound",
                payload={
                    **payload,
                    "tenant_id": "tenant-b",
                    "session_id": "session-b",
                    "trace_id": "trace-b",
                },
                headers=None,
                partition_key=None,
            )

    async with factory() as db:
        rows = list((await db.execute(select(MessageOutboxRow))).scalars().all())
    assert len(rows) == 2
    assert {(row.tenant_id, row.reply_id, row.partition_key) for row in rows} == {
        ("tenant-a", "shared-reply", "tenant-a:session-a"),
        ("tenant-b", "shared-reply", "tenant-b:session-b"),
    }


async def test_outbox_expired_claim_is_fenced_even_with_same_configured_worker_id(
    factory,
):
    store = MessageReliabilityStore(factory)
    payload = {
        "reply_id": "reply-fenced",
        "tenant_id": "tenant-a",
        "session_id": "session-a",
        "trace_id": "trace-a",
        "segments": [],
    }
    async with factory() as db:
        async with db.begin():
            await store.enqueue(
                db,
                stream="outbound",
                payload=payload,
                headers=None,
                partition_key=None,
            )

    old_bus = HeaderCapturingBus()
    new_bus = HeaderCapturingBus()
    old_relay = MessageOutboxRelay(store, old_bus, worker_id="outbound-1")
    new_relay = MessageOutboxRelay(store, new_bus, worker_id="outbound-1")
    old_claim = (await old_relay._claim_batch())[0]

    async with factory() as db:
        row = await db.get(
            MessageOutboxRow,
            {"tenant_id": "tenant-a", "reply_id": "reply-fenced"},
        )
        assert row is not None
        row.lease_until = datetime(2000, 1, 1, tzinfo=UTC)
        await db.commit()

    assert await new_relay.drain_once() == 1
    assert await old_relay._renew_claim(old_claim) is False
    assert old_bus.messages == []
    assert len(new_bus.messages) == 1
    async with factory() as db:
        row = await db.get(
            MessageOutboxRow,
            {"tenant_id": "tenant-a", "reply_id": "reply-fenced"},
        )
        assert row is not None
        assert row.status == "published"
        assert row.lease_owner == ""
        assert row.lease_token == ""


async def test_outbox_poison_message_moves_to_durable_dead_letter_state(
    session_manager,
    settings,
    factory,
):
    store = MessageReliabilityStore(factory)
    orc, _bus, _cap = _build(
        session_manager,
        settings,
        message_store=store,
    )
    assert (await orc.handle(_make_event("hello"))).status == ProcessingStatus.COMPLETED

    relay = MessageOutboxRelay(
        store,
        FailingPublishBus(),
        worker_id="relay-poison",
        max_attempts=1,
    )
    assert await relay.drain_once() == 0
    assert await relay.drain_once() == 0

    async with factory() as db:
        row = (await db.execute(select(MessageOutboxRow))).scalar_one()
        assert row.status == "dead_letter"
        assert row.attempts == 1
        assert row.dead_lettered_at is not None
        assert row.last_error.startswith("ConnectionError:")


async def test_outbound_publish_failure_returns_retryable_outcome(
    session_manager,
    settings,
):
    orc, bus, cap = _build(
        session_manager,
        settings,
        bus=FailingPublishBus(),
    )

    outcome = await orc.handle(_make_event("hello"))

    assert cap.calls == 1
    assert bus.messages == []
    assert outcome.status == ProcessingStatus.RETRYABLE_FAILURE
    assert outcome.error_type == "ConnectionError"
    assert "redis publish unavailable" in outcome.reason


async def test_explicit_permanent_failure_is_returned_without_fake_success(
    session_manager,
    settings,
    monkeypatch,
):
    orc, bus, _cap = _build(session_manager, settings)

    async def fail_permanently(_event):
        raise PermanentProcessingError(
            "unsupported_event_version",
            error_type="DomainValidationError",
        )

    monkeypatch.setattr(orc, "_run", fail_permanently)
    outcome = await orc.handle(_make_event("hello"))

    assert bus.messages == []
    assert outcome.status == ProcessingStatus.PERMANENT_FAILURE
    assert outcome.reason == "unsupported_event_version"
    assert outcome.error_type == "DomainValidationError"


async def test_classic_orchestrator_replaces_echo_reply(session_manager, settings):
    question = "当前连接的是什么模型？"
    orc, bus, _cap = _build(
        session_manager,
        settings,
        capability=FakeCapability(reply_text=question),
    )

    await orc.handle(_make_event(question))

    assert _outbound_text(bus).startswith("我刚才没有生成有效答案。")


async def test_flow_shadow_run_is_observability_only(session_manager, settings):
    shadow_settings = Settings(
        session_window_turns=settings.session_window_turns,
        session_lock_ttl_seconds=settings.session_lock_ttl_seconds,
        orchestrator_flow_shadow_enabled=True,
    )
    orc, bus, cap = _build(session_manager, shadow_settings)

    await orc.handle(_make_event("hello"))

    assert cap.calls == 1
    assert len(bus.messages) == 1
    assert bus.messages[0][1]["segments"][0]["content"] == "answer"
    assert orc.last_flow_shadow_result is not None
    assert orc.last_flow_shadow_result.flow_name == "default_compatible_flow"
    assert orc.last_flow_shadow_result.status == "completed"
    assert len(orc.last_flow_shadow_result.steps) == 19
    assert {step.status for step in orc.last_flow_shadow_result.steps} == {"shadow"}


async def test_flow_runtime_default_compatible_flow_is_opt_in(
    session_manager,
    settings,
):
    runtime_settings = Settings(
        session_window_turns=settings.session_window_turns,
        session_lock_ttl_seconds=settings.session_lock_ttl_seconds,
        orchestrator_flow_runtime_enabled=True,
    )
    orc, bus, cap = _build(session_manager, runtime_settings)

    await orc.handle(_make_event("hello"))

    assert cap.calls == 1
    assert len(bus.messages) == 1
    assert bus.messages[0][1]["segments"][0]["content"] == "answer"
    assert orc.last_flow_runtime_result is not None
    assert orc.last_flow_runtime_result.flow_name == "default_compatible_flow"
    assert orc.last_flow_runtime_result.status == "completed"
    assert [step.id for step in orc.last_flow_runtime_result.steps] == [
        "load_session",
        "before_preprocess_hooks",
        "preprocess",
        "after_preprocess_hooks",
        "append_user_turn",
        "handoff_short_circuit",
        "input_safety",
        "before_route_hooks",
        "router_signal_merge",
        "route",
        "after_route_hooks",
        "before_capability_hooks",
        "capability",
        "after_capability_hooks",
        "output_safety",
        "before_postprocess_hooks",
        "postprocess",
        "after_postprocess_hooks",
        "commit",
    ]
    session = await session_manager.load("demo", "u1", "se_orc_01", Channel.WEB)
    assert [turn.role.value for turn in session.turns] == ["user", "assistant"]


async def test_flow_runtime_auto_replies_through_explicit_private_flow(
    session_manager,
    settings,
):
    runtime_settings = Settings(
        session_window_turns=settings.session_window_turns,
        session_lock_ttl_seconds=settings.session_lock_ttl_seconds,
        orchestrator_flow_runtime_enabled=True,
        orchestrator_flow_runtime_name="auto",
        orchestrator_flow_runtime_allowed_names="auto",
        orchestrator_flow_runtime_allow_target_flows=True,
        orchestrator_flow_runtime_allow_compatible_fallback=False,
    )
    orc, bus, cap = _build(session_manager, runtime_settings)

    outcome = await orc.handle(
        _make_event(
            "hello",
            session_id="wxid_private_contact",
            channel=Channel.WECHAT,
            metadata={"session_kind": "private"},
        )
    )

    assert outcome.status == ProcessingStatus.COMPLETED
    assert cap.calls == 1
    assert len(bus.messages) == 1
    assert orc.last_flow_runtime_result is not None
    assert orc.last_flow_runtime_result.flow_name == "default_private_channel_flow"


async def test_flow_runtime_rejects_requested_flow_outside_allowlist(
    session_manager,
    settings,
):
    runtime_settings = Settings(
        session_window_turns=settings.session_window_turns,
        session_lock_ttl_seconds=settings.session_lock_ttl_seconds,
        orchestrator_flow_runtime_enabled=True,
        orchestrator_flow_runtime_name="default_compatible_flow",
        orchestrator_flow_runtime_allowed_names="default_group_channel_flow",
    )
    orc, bus, cap = _build(session_manager, runtime_settings)

    outcome = await orc.handle(_make_event("hello"))

    assert cap.calls == 0
    assert orc.last_flow_runtime_result is None
    assert bus.messages == []
    assert outcome.status == ProcessingStatus.PERMANENT_FAILURE
    assert outcome.error_type == "FlowConfigurationError"


async def test_flow_runtime_effect_handlers_fail_closed_without_committer(
    session_manager,
    settings,
):
    runtime_settings = Settings(
        session_window_turns=settings.session_window_turns,
        session_lock_ttl_seconds=settings.session_lock_ttl_seconds,
        orchestrator_flow_runtime_enabled=True,
        orchestrator_flow_effect_handlers_enabled=True,
        orchestrator_flow_effect_commit_backend="none",
    )
    orc, bus, cap = _build(session_manager, runtime_settings)

    outcome = await orc.handle(_make_event("hello"))

    assert cap.calls == 0
    assert orc.last_flow_runtime_result is None
    assert bus.messages == []
    assert outcome.status == ProcessingStatus.PERMANENT_FAILURE
    assert outcome.error_type == "FlowConfigurationError"


async def test_flow_runtime_degrades_capability_exception_once(
    session_manager,
    settings,
):
    runtime_settings = Settings(
        session_window_turns=settings.session_window_turns,
        session_lock_ttl_seconds=settings.session_lock_ttl_seconds,
        orchestrator_flow_runtime_enabled=True,
    )
    cap = FakeCapability(raise_exc=True)
    orc, bus, _ = _build(session_manager, runtime_settings, capability=cap)

    await orc.handle(_make_event("boom"))

    assert cap.calls == 1
    assert len(bus.messages) == 1
    assert orc.last_flow_runtime_result is not None
    assert orc.last_flow_runtime_result.status == "stopped"
    assert orc.last_flow_runtime_result.stop_reason == "capability_degraded"
    payload = bus.messages[0][1]
    assert payload["segments"][0]["content"] != "answer"


async def test_target_flow_memory_control_preempts_command_and_faq(
    session_manager,
    settings,
):
    store = _MemoryControlStore()
    command_step = CommandDispatchStep(_CommandStore(), _command_service())
    flow_registry, owner_permissions = _plugin_flow_registry()
    flow, flow_bus, flow_cap = _build(
        session_manager,
        _target_flow_settings(settings),
        flow_step_registry=flow_registry,
        flow_owner_permissions=owner_permissions,
        flow_step_executors={
            "plugin.memory.control_intents": MemoryControlStep(store),
            "plugin.commands.dispatch": command_step,
        },
    )

    await flow.handle(
        _make_event(
            "记住 我默认要中文回复",
            session_id="flow-memory-control-room",
            channel=Channel.DISCORD,
            metadata={"session_kind": "group"},
        )
    )

    assert _outbound_text(flow_bus) == "当前群未开启成员记忆，未保存。"
    assert store.created == []
    assert flow_cap.calls == 0
    assert flow.last_flow_runtime_result is not None
    assert flow.last_flow_runtime_result.status == "stopped"
    assert flow.last_flow_runtime_result.stop_reason == "memory_control_intent"
    assert [step.id for step in flow.last_flow_runtime_result.steps] == [
        "load_session",
        "preprocess",
        "append_user_turn",
        "handoff_short_circuit",
        "input_safety",
        "memory_control_intents",
        "postprocess",
        "commit",
    ]


async def test_target_flow_matches_legacy_command_short_circuit(
    session_manager,
    settings,
):
    service = _command_service()
    legacy_hooks = HookRunner()
    legacy_hooks.register(CommandCenterHook(_CommandStore(), service))
    legacy, legacy_bus, legacy_cap = _build(
        session_manager,
        settings,
        hook_runner=legacy_hooks,
    )

    flow_registry, owner_permissions = _plugin_flow_registry()
    flow, flow_bus, flow_cap = _build(
        session_manager,
        _target_flow_settings(settings),
        flow_step_registry=flow_registry,
        flow_owner_permissions=owner_permissions,
        flow_step_executors={
            "plugin.commands.dispatch": CommandDispatchStep(_CommandStore(), service),
        },
    )

    await legacy.handle(
        _make_event(
            "/ping",
            session_id="legacy-command-room",
            channel=Channel.DISCORD,
            metadata={"session_kind": "group"},
        )
    )
    await flow.handle(
        _make_event(
            "/ping",
            session_id="flow-command-room",
            channel=Channel.DISCORD,
            metadata={"session_kind": "group"},
        )
    )

    assert _outbound_text(legacy_bus) == _outbound_text(flow_bus) == "pong"
    assert legacy_cap.calls == 0
    assert flow_cap.calls == 0
    assert flow.last_flow_runtime_result is not None
    assert flow.last_flow_runtime_result.status == "stopped"
    assert flow.last_flow_runtime_result.stop_reason == "test_command"


async def test_target_flow_silently_stops_unknown_group_slash_before_capability(
    session_manager,
    settings,
):
    flow_registry, owner_permissions = _plugin_flow_registry()
    flow, bus, capability = _build(
        session_manager,
        _target_flow_settings(settings),
        flow_step_registry=flow_registry,
        flow_owner_permissions=owner_permissions,
        flow_step_executors={
            "plugin.commands.dispatch": CommandDispatchStep(
                _CommandStore(),
                _command_service(),
            ),
        },
    )

    outcome = await flow.handle(
        _make_event(
            "@bot /unknown 帮我看看",
            session_id="group-unknown-command",
            channel=Channel.DISCORD,
            metadata={"session_kind": "group", "mentioned_me": True},
        )
    )

    assert outcome.status == ProcessingStatus.INTENTIONALLY_SUPPRESSED
    assert outcome.reason == "unknown_command"
    assert capability.calls == 0
    assert bus.messages == []
    assert flow.last_flow_runtime_result is not None
    assert flow.last_flow_runtime_result.status == "stopped"
    assert flow.last_flow_runtime_result.stop_reason == "unknown_command"
    assert flow.last_flow_runtime_result.steps[-1].id == "command_dispatch"


async def test_target_flow_matches_legacy_moderation_replace(
    session_manager,
    settings,
):
    legacy_store = _ModerationStore(reminder_mode="replace")
    legacy_hooks = HookRunner()
    legacy_hooks.register(ModerationAuditHook(legacy_store))
    legacy_hooks.register(ModerationReplaceReminderHook(legacy_store))
    legacy, legacy_bus, legacy_cap = _build(
        session_manager,
        settings,
        hook_runner=legacy_hooks,
    )

    flow_store = _ModerationStore(reminder_mode="replace")
    flow_registry, owner_permissions = _plugin_flow_registry()
    flow, flow_bus, flow_cap = _build(
        session_manager,
        _target_flow_settings(settings),
        flow_step_registry=flow_registry,
        flow_owner_permissions=owner_permissions,
        flow_step_executors={
            "plugin.memory.control_intents": CountingFlowStep("no_memory_control"),
            "plugin.commands.dispatch": CountingFlowStep("no_command"),
            "plugin.repeater.detect": CountingFlowStep("not_triggered"),
            "plugin.moderation.inspect_input": ModerationInspectInputStep(flow_store),
            "plugin.persona_extract.skill_enrich": CountingFlowStep("not_enriched"),
            "plugin.memory.load": CountingFlowStep("no_memory"),
            "plugin.credits.query_command": CountingFlowStep("not_matched"),
            "plugin.moderation.enforce_input": ModerationEnforceInputStep(flow_store),
        },
    )

    await legacy.handle(
        _make_event(
            "这条消息包含敏感词",
            session_id="legacy-moderation-room",
            channel=Channel.DISCORD,
            metadata={"session_kind": "group"},
        )
    )
    await flow.handle(
        _make_event(
            "这条消息包含敏感词",
            session_id="flow-moderation-room",
            channel=Channel.DISCORD,
            metadata={"session_kind": "group"},
        )
    )

    assert _outbound_text(legacy_bus) == _outbound_text(flow_bus) == REMINDER_TEXT
    assert legacy_cap.calls == 0
    assert flow_cap.calls == 0
    assert len(legacy_store.logged) == 1
    assert len(flow_store.logged) == 1
    assert flow.last_flow_runtime_result is not None
    assert flow.last_flow_runtime_result.status == "stopped"
    assert flow.last_flow_runtime_result.stop_reason == "moderation_reminder_replace"


async def test_target_flow_matches_legacy_repeater_short_circuit(
    session_manager,
    settings,
):
    async def seed_previous_turn(session_id: str) -> None:
        session = await session_manager.load(
            "demo",
            "u1",
            session_id,
            Channel.DISCORD,
        )
        await session_manager.append_turn(
            session,
            Turn(
                session_id=session_id,
                role=Role.USER,
                content="复读测试",
                trace_id="trace-prev",
                metadata={"sender_id": "u2"},
            ),
        )

    legacy_store = _RepeaterStore()
    legacy_hooks = HookRunner()
    legacy_hooks.register(RepeaterHook(legacy_store))
    legacy, legacy_bus, legacy_cap = _build(
        session_manager,
        settings,
        hook_runner=legacy_hooks,
    )

    flow_store = _RepeaterStore()
    flow_registry, owner_permissions = _plugin_flow_registry()
    flow, flow_bus, flow_cap = _build(
        session_manager,
        _target_flow_settings(settings),
        flow_step_registry=flow_registry,
        flow_owner_permissions=owner_permissions,
        flow_step_executors={
            "plugin.memory.control_intents": CountingFlowStep("no_memory_control"),
            "plugin.commands.dispatch": CountingFlowStep("no_command"),
            "plugin.repeater.detect": RepeaterDetectStep(flow_store),
        },
    )

    await seed_previous_turn("legacy-repeater-room")
    await seed_previous_turn("flow-repeater-room")
    await legacy.handle(
        _make_event(
            "复读测试",
            session_id="legacy-repeater-room",
            channel=Channel.DISCORD,
            metadata={"session_kind": "group"},
        )
    )
    await flow.handle(
        _make_event(
            "复读测试",
            session_id="flow-repeater-room",
            channel=Channel.DISCORD,
            metadata={"session_kind": "group"},
        )
    )

    assert flow_bus.messages, flow.last_flow_runtime_result
    assert _outbound_text(legacy_bus) == _outbound_text(flow_bus) == "复读测试"
    assert legacy_cap.calls == 0
    assert flow_cap.calls == 0
    assert legacy_store.recorded[0]["content_text"] == "复读测试"
    assert flow_store.recorded[0]["content_text"] == "复读测试"
    assert flow.last_flow_runtime_result is not None
    assert flow.last_flow_runtime_result.status == "stopped"
    assert flow.last_flow_runtime_result.stop_reason == "repeater_triggered"


async def test_target_flow_matches_legacy_credits_reserve_and_settle(
    session_manager,
    settings,
):
    legacy_store = _CreditStore(cost=4, balance=20)
    legacy_hooks = HookRunner()
    legacy_hooks.register(CreditDeductionHook(legacy_store))
    legacy_hooks.register(CreditSettlementHook(legacy_store))
    legacy, legacy_bus, legacy_cap = _build(
        session_manager,
        settings,
        hook_runner=legacy_hooks,
    )

    flow_store = _CreditStore(cost=4, balance=20)
    flow_registry, owner_permissions = _plugin_flow_registry()
    flow, flow_bus, flow_cap = _build(
        session_manager,
        _target_flow_settings(settings),
        flow_step_registry=flow_registry,
        flow_owner_permissions=owner_permissions,
        flow_step_executors={
            "plugin.memory.control_intents": CountingFlowStep("no_memory_control"),
            "plugin.commands.dispatch": CountingFlowStep("no_command"),
            "plugin.repeater.detect": CountingFlowStep("not_triggered"),
            "plugin.moderation.inspect_input": CountingFlowStep("not_matched"),
            "plugin.persona_extract.skill_enrich": CountingFlowStep("not_enriched"),
            "plugin.memory.load": CountingFlowStep("no_memory"),
            "plugin.credits.query_command": CountingFlowStep("not_matched"),
            "plugin.moderation.enforce_input": CountingFlowStep("not_replace"),
            "plugin.credits.reserve": CreditReserveStep(flow_store),
            "plugin.credits.settle": CreditSettleStep(flow_store),
            "plugin.moderation.decorate_output": CountingFlowStep("not_appended"),
            "plugin.draw.postprocess_result": CountingFlowStep("no_draw_result"),
            "plugin.memory.save": CountingFlowStep("not_saved"),
        },
    )

    await legacy.handle(
        _make_event(
            "hello",
            session_id="legacy-credits-room",
            channel=Channel.DISCORD,
            metadata={"session_kind": "group", "sender_name": "测试用户"},
        )
    )
    await flow.handle(
        _make_event(
            "hello",
            session_id="flow-credits-room",
            channel=Channel.DISCORD,
            metadata={"session_kind": "group", "sender_name": "测试用户"},
        )
    )

    assert _outbound_text(legacy_bus) == _outbound_text(flow_bus) == "answer"
    assert legacy_cap.calls == 1
    assert flow_cap.calls == 1
    assert legacy_store.reserve_calls[0]["amount"] == 4
    assert flow_store.reserve_calls[0]["amount"] == 4
    assert legacy_store.capture_calls == [
        {
            "reservation_id": "reservation-1",
            "amount": 4,
            "reference": legacy_store.reserve_calls[0]["reference"],
            "display_name": "测试用户",
        }
    ]
    assert flow_store.capture_calls == [
        {
            "reservation_id": "reservation-1",
            "amount": 4,
            "reference": flow_store.reserve_calls[0]["reference"],
            "display_name": "测试用户",
        }
    ]
    assert flow.last_flow_runtime_result is not None
    assert flow.last_flow_runtime_result.status == "completed"


async def test_target_flow_matches_legacy_memory_load_and_save(
    session_manager,
    settings,
):
    class MemoryRecordingCapability(FakeCapability):
        def __init__(self) -> None:
            super().__init__(reply_text="answer")
            self.memories: list[dict[str, Any]] = []

        async def answer(
            self,
            pre: PreprocessedMessage,
            session: Session,
            hints: dict[str, Any] | None = None,
        ) -> CapabilityResult:
            self.memories.append(dict(session.variables.get("user_memory") or {}))
            return await super().answer(pre, session, hints)

    legacy_store = _MemoryStore()
    legacy_hooks = HookRunner()
    legacy_hooks.register(MemoryContextHook(legacy_store))
    legacy_hooks.register(MemoryPersistenceHook(legacy_store))
    legacy_cap = MemoryRecordingCapability()
    legacy, legacy_bus, _ = _build(
        session_manager,
        settings,
        capability=legacy_cap,
        hook_runner=legacy_hooks,
    )

    flow_store = _MemoryStore()
    flow_cap = MemoryRecordingCapability()
    flow_registry, owner_permissions = _plugin_flow_registry()
    flow, flow_bus, _ = _build(
        session_manager,
        _target_flow_settings(settings),
        capability=flow_cap,
        flow_step_registry=flow_registry,
        flow_owner_permissions=owner_permissions,
        flow_step_executors={
            "plugin.memory.control_intents": CountingFlowStep("no_memory_control"),
            "plugin.commands.dispatch": CountingFlowStep("no_command"),
            "plugin.repeater.detect": CountingFlowStep("not_triggered"),
            "plugin.moderation.inspect_input": CountingFlowStep("not_matched"),
            "plugin.persona_extract.skill_enrich": CountingFlowStep("not_enriched"),
            "plugin.memory.load": MemoryLoadStep(flow_store),
            "plugin.credits.query_command": CountingFlowStep("not_matched"),
            "plugin.moderation.enforce_input": CountingFlowStep("not_replace"),
            "plugin.credits.reserve": CountingFlowStep("not_reserved"),
            "plugin.credits.settle": CountingFlowStep("not_settled"),
            "plugin.moderation.decorate_output": CountingFlowStep("not_appended"),
            "plugin.draw.postprocess_result": CountingFlowStep("no_draw_result"),
            "plugin.memory.save": MemorySaveStep(flow_store),
        },
    )

    legacy_event = _make_event(
        "我要查物流",
        session_id="legacy-memory-room",
        channel=Channel.DISCORD,
        metadata={"session_kind": "group", "source": "discord"},
    )
    flow_event = _make_event(
        "我要查物流",
        session_id="flow-memory-room",
        channel=Channel.DISCORD,
        metadata={"session_kind": "group", "source": "discord"},
    )

    await legacy.handle(legacy_event)
    await flow.handle(flow_event)

    assert _outbound_text(legacy_bus) == _outbound_text(flow_bus) == "answer"
    assert legacy_cap.calls == 1
    assert flow_cap.calls == 1
    assert legacy_cap.memories == [{}]
    assert flow_cap.memories == [{}]
    assert legacy_store.get_calls == []
    assert flow_store.get_calls == []
    assert legacy_store.remember_calls == []
    assert flow_store.remember_calls == []
    flow_session = await session_manager.load(
        "demo",
        "u1",
        "flow-memory-room",
        Channel.DISCORD,
    )
    assert "user_memory" not in flow_session.variables
    assert flow.last_flow_runtime_result is not None
    assert flow.last_flow_runtime_result.status == "completed"
    traces = {step.kind: step for step in flow.last_flow_runtime_result.steps}
    assert traces["plugin.memory.load"].reason == "no_session"
    assert traces["plugin.memory.save"].reason == "member_privacy_blocked"


async def test_wechat_flow_matches_legacy_reply_policy_and_outbound_queue(
    session_manager,
    settings,
):
    legacy_store = _WxbotStore(mode="contains")
    legacy_hooks = HookRunner()
    legacy_hooks.register(WxbotReplyPolicyHook(legacy_store))
    legacy_hooks.register(WxbotReplyQueueHook(legacy_store))
    legacy, legacy_bus, legacy_cap = _build(
        session_manager,
        settings,
        hook_runner=legacy_hooks,
    )

    flow_store = _WxbotStore(mode="contains")
    flow_registry, owner_permissions = _plugin_flow_registry()
    flow, flow_bus, flow_cap = _build(
        session_manager,
        _target_wechat_flow_settings(settings),
        flow_step_registry=flow_registry,
        flow_owner_permissions=owner_permissions,
        flow_step_executors={
            "plugin.wxbot.normalize_event": CountingFlowStep("not_normalized"),
            "plugin.wxbot.user_ban_pre_command": CountingFlowStep("not_banned"),
            "plugin.memory.control_intents": CountingFlowStep("no_memory_control"),
            "plugin.commands.dispatch": CountingFlowStep("no_command"),
            "plugin.repeater.detect": CountingFlowStep("not_triggered"),
            "plugin.wxbot.user_ban_gate": CountingFlowStep("not_banned"),
            "plugin.wxbot.reply_policy": WxbotReplyPolicyStep(flow_store),
            "plugin.moderation.inspect_input": CountingFlowStep("not_matched"),
            "plugin.wxbot.agent_scope_enrich": CountingFlowStep("not_enriched"),
            "plugin.persona_extract.skill_enrich": CountingFlowStep("not_enriched"),
            "plugin.wxbot.voice_profile_enrich": CountingFlowStep("not_configured"),
            "plugin.memory.load": CountingFlowStep("no_memory"),
            "plugin.wxbot.group_context_load": CountingFlowStep("no_group_context"),
            "plugin.credits.query_command": CountingFlowStep("not_matched"),
            "plugin.moderation.enforce_input": CountingFlowStep("not_replace"),
            "plugin.credits.reserve": CountingFlowStep("not_reserved"),
            "plugin.credits.settle": CountingFlowStep("not_settled"),
            "plugin.moderation.decorate_output": CountingFlowStep("not_appended"),
            "plugin.draw.postprocess_result": CountingFlowStep("no_draw_result"),
            "plugin.memory.save": CountingFlowStep("not_saved"),
            "plugin.wxbot.outbound_policy": WxbotOutboundPolicyStep(flow_store),
        },
    )

    legacy_event = _make_event(
        "机器人，请问这个报价是多少？",
        session_id="legacy-wxbot-room@chatroom",
        channel=Channel.WECHAT,
        metadata={
            "session_kind": "group",
            "session_name": "测试群",
            "sender_name": "群友A",
            "sender_wxid": "wxid_user_a",
            "msg_svr_id": "svr-1",
            "mentioned_me": False,
        },
    )
    flow_event = _make_event(
        "机器人，请问这个报价是多少？",
        session_id="flow-wxbot-room@chatroom",
        channel=Channel.WECHAT,
        metadata={
            "session_kind": "group",
            "session_name": "测试群",
            "sender_name": "群友A",
            "sender_wxid": "wxid_user_a",
            "msg_svr_id": "svr-1",
            "mentioned_me": False,
        },
    )
    legacy_event.received_at = datetime(2026, 7, 16, 4, 0, tzinfo=UTC)
    flow_event.received_at = datetime(2026, 7, 16, 4, 0, tzinfo=UTC)

    await legacy.handle(legacy_event)
    await flow.handle(flow_event)

    assert legacy_cap.calls == 1
    assert flow_cap.calls == 1
    assert legacy_bus.messages == []
    assert flow_bus.messages == []
    assert len(legacy_store.calls) == len(flow_store.calls) == 1
    assert legacy_store.calls[0]["reply_text"] == flow_store.calls[0]["reply_text"] == "answer"
    # The participation decision owns normal group mentions.  A plain group
    # answer has an unambiguous target and must not turn into a notification.
    assert legacy_store.calls[0]["mention_sender"] is False
    assert flow_store.calls[0]["mention_sender"] is False
    assert legacy_store.calls[0]["session_kind"] == "group"
    assert flow_store.calls[0]["session_kind"] == "group"
    assert legacy_store.calls[0]["reply_to_msg_svr_id"] == "svr-1"
    assert flow_store.calls[0]["reply_to_msg_svr_id"] == "svr-1"
    assert legacy_store.calls[0]["command_id"] == "wxbot-reply:demo:msg_1:0"
    assert flow_store.calls[0]["command_id"] == "wxbot-reply:demo:msg_1:0"
    assert legacy_store.calls[0]["delivery"]["participation_status"] == "may_reply"
    assert flow_store.calls[0]["delivery"]["participation_status"] == "may_reply"
    assert legacy_store.calls[0]["delivery"]["not_before"]
    assert flow_store.calls[0]["delivery"]["expires_at"]
    assert legacy_store.policy_calls == [
        {"tenant_id": "demo", "session_id": "legacy-wxbot-room@chatroom"},
    ]
    assert flow_store.policy_calls == [
        {"tenant_id": "demo", "session_id": "flow-wxbot-room@chatroom"},
    ]
    legacy_session = await session_manager.load(
        "demo",
        "u1",
        "legacy-wxbot-room@chatroom",
        Channel.WECHAT,
    )
    flow_session = await session_manager.load(
        "demo",
        "u1",
        "flow-wxbot-room@chatroom",
        Channel.WECHAT,
    )
    assert [turn.role.value for turn in legacy_session.turns] == ["user", "assistant"]
    assert [turn.role.value for turn in flow_session.turns] == ["user", "assistant"]
    assert flow.last_flow_runtime_result is not None
    assert flow.last_flow_runtime_result.status == "completed"
    traces = {step.kind: step for step in flow.last_flow_runtime_result.steps}
    assert traces["plugin.wxbot.reply_policy"].reason == "reply_mode_contains_match"
    assert traces["plugin.wxbot.outbound_policy"].reason == "queued"


async def test_wechat_flow_finalized_plugin_result_still_runs_outbound_policy(
    session_manager,
    settings,
):
    class _FinalizingCreditStep:
        async def run(self, ctx: PipelineContext) -> StepResult:
            _ = ctx
            return StepResult(
                action="stop",
                reason="credit_checkin_action",
                result=CapabilityResult(
                    route=RouteType.CANNED,
                    reply_text="签到成功！+10 积分",
                ),
                finalize=True,
                skip_output_safety=True,
                route_label=RouteType.CANNED.value,
            )

    flow_store = _WxbotStore(mode="contains")
    flow_registry, owner_permissions = _plugin_flow_registry()
    flow, flow_bus, flow_cap = _build(
        session_manager,
        _target_wechat_flow_settings(settings),
        router=FakeRouter(RouteType.LLM),
        extra_capabilities={RouteType.LLM: FakeCapability()},
        flow_step_registry=flow_registry,
        flow_owner_permissions=owner_permissions,
        flow_step_executors={
            "plugin.wxbot.normalize_event": CountingFlowStep("not_normalized"),
            "plugin.wxbot.user_ban_pre_command": CountingFlowStep("not_banned"),
            "plugin.memory.control_intents": CountingFlowStep("no_memory_control"),
            "plugin.commands.dispatch": CountingFlowStep("no_command"),
            "plugin.repeater.detect": CountingFlowStep("not_triggered"),
            "plugin.wxbot.user_ban_gate": CountingFlowStep("not_banned"),
            "plugin.wxbot.reply_policy": WxbotReplyPolicyStep(flow_store),
            "plugin.moderation.inspect_input": CountingFlowStep("not_matched"),
            "plugin.wxbot.agent_scope_enrich": CountingFlowStep("not_enriched"),
            "plugin.persona_extract.skill_enrich": CountingFlowStep("not_enriched"),
            "plugin.wxbot.voice_profile_enrich": CountingFlowStep("not_configured"),
            "plugin.memory.load": CountingFlowStep("no_memory"),
            "plugin.wxbot.group_context_load": CountingFlowStep("no_group_context"),
            "plugin.credits.query_command": _FinalizingCreditStep(),
            "plugin.wxbot.outbound_policy": WxbotOutboundPolicyStep(flow_store),
        },
    )

    await flow.handle(
        _make_event(
            "签到",
            session_id="flow-wxbot-room@chatroom",
            channel=Channel.WECHAT,
            metadata={
                "session_kind": "group",
                "session_name": "测试群",
                "sender_name": "Exager",
                "sender_wxid": "wxid_exager",
                "msg_svr_id": "svr-checkin",
                "mentioned_me": True,
            },
        )
    )

    assert flow_cap.calls == 0
    assert flow_bus.messages == []
    assert len(flow_store.calls) == 1
    assert flow_store.calls[0]["reply_text"] == "签到成功！+10 积分"
    assert flow_store.calls[0]["mention_sender"] is False
    assert flow_store.calls[0]["reply_to_msg_svr_id"] == "svr-checkin"
    assert flow.last_flow_runtime_result is not None
    traces = {step.kind: step for step in flow.last_flow_runtime_result.steps}
    assert traces["plugin.credits.query_command"].reason == "credit_checkin_action"
    assert traces["plugin.wxbot.outbound_policy"].reason == "queued"


async def test_flow_runtime_rejects_auto_without_explicit_allowance(
    session_manager,
    settings,
):
    runtime_settings = Settings(
        session_window_turns=settings.session_window_turns,
        session_lock_ttl_seconds=settings.session_lock_ttl_seconds,
        orchestrator_flow_runtime_enabled=True,
        orchestrator_flow_runtime_name="auto",
        orchestrator_flow_runtime_allowed_names="auto",
        orchestrator_flow_runtime_allow_target_flows=False,
    )
    flow_registry, owner_permissions = _plugin_flow_registry()
    orc, bus, cap = _build(
        session_manager,
        runtime_settings,
        flow_step_registry=flow_registry,
        flow_owner_permissions=owner_permissions,
    )

    outcome = await orc.handle(
        _make_event(
            "hello",
            session_id="discord-channel-1",
            channel=Channel.DISCORD,
            metadata={"session_kind": "group"},
        )
    )

    assert cap.calls == 0
    assert orc.last_flow_runtime_result is None
    assert bus.messages == []
    assert outcome.status == ProcessingStatus.PERMANENT_FAILURE
    assert outcome.error_type == "FlowConfigurationError"


async def test_flow_runtime_rejects_target_flow_without_explicit_allowance(
    session_manager,
    settings,
):
    runtime_settings = Settings(
        session_window_turns=settings.session_window_turns,
        session_lock_ttl_seconds=settings.session_lock_ttl_seconds,
        orchestrator_flow_runtime_enabled=True,
        orchestrator_flow_runtime_name="default_group_channel_flow",
        orchestrator_flow_runtime_allowed_names="default_group_channel_flow",
        orchestrator_flow_runtime_allow_target_flows=False,
    )
    flow_registry, owner_permissions = _plugin_flow_registry()
    plugin_step = CountingFlowStep("plugin_ran")
    orc, bus, cap = _build(
        session_manager,
        runtime_settings,
        flow_step_registry=flow_registry,
        flow_owner_permissions=owner_permissions,
        flow_step_executors={"plugin.memory.load": plugin_step},
    )

    outcome = await orc.handle(
        _make_event(
            "hello",
            session_id="discord-channel-1",
            channel=Channel.DISCORD,
            metadata={"session_kind": "group"},
        )
    )

    assert cap.calls == 0
    assert plugin_step.calls == 0
    assert orc.last_flow_runtime_result is None
    assert bus.messages == []
    assert outcome.status == ProcessingStatus.PERMANENT_FAILURE
    assert outcome.error_type == "FlowConfigurationError"


async def test_flow_shadow_auto_resolves_wechat_group_flow(session_manager, settings):
    shadow_settings = Settings(
        session_window_turns=settings.session_window_turns,
        session_lock_ttl_seconds=settings.session_lock_ttl_seconds,
        orchestrator_flow_shadow_enabled=True,
        orchestrator_flow_shadow_name="auto",
    )
    flow_registry, owner_permissions = _plugin_flow_registry()
    orc, bus, cap = _build(
        session_manager,
        shadow_settings,
        flow_step_registry=flow_registry,
        flow_owner_permissions=owner_permissions,
    )

    await orc.handle(
        _make_event(
            "hello",
            session_id="room@chatroom",
            channel=Channel.WECHAT,
        )
    )

    assert cap.calls == 1
    assert len(bus.messages) == 1
    assert orc.last_flow_shadow_result is not None
    assert orc.last_flow_shadow_result.flow_name == "default_wechat_group_flow"
    assert orc.last_flow_shadow_result.status == "completed"
    assert len(orc.last_flow_shadow_result.steps) == 31
    assert {
        "wxbot_user_ban_pre_command",
        "wxbot_user_ban_gate",
        "memory_control_intents",
        "wxbot_voice_profile_enrich",
        "wxbot_group_context_load",
    } <= {step.id for step in orc.last_flow_shadow_result.steps}
    assert "tibo_reset_intent" not in {step.id for step in orc.last_flow_shadow_result.steps}
    assert {step.status for step in orc.last_flow_shadow_result.steps} == {"shadow"}


async def test_flow_shadow_auto_resolves_generic_group_flow(session_manager, settings):
    shadow_settings = Settings(
        session_window_turns=settings.session_window_turns,
        session_lock_ttl_seconds=settings.session_lock_ttl_seconds,
        orchestrator_flow_shadow_enabled=True,
        orchestrator_flow_shadow_name="auto",
    )
    flow_registry, owner_permissions = _plugin_flow_registry()
    orc, bus, cap = _build(
        session_manager,
        shadow_settings,
        flow_step_registry=flow_registry,
        flow_owner_permissions=owner_permissions,
    )

    await orc.handle(
        _make_event(
            "hello",
            session_id="discord-channel-1",
            channel=Channel.DISCORD,
            metadata={"session_kind": "group"},
        )
    )

    assert cap.calls == 1
    assert len(bus.messages) == 1
    assert orc.last_flow_shadow_result is not None
    assert orc.last_flow_shadow_result.flow_name == "default_group_channel_flow"
    assert orc.last_flow_shadow_result.status == "completed"
    assert len(orc.last_flow_shadow_result.steps) == 23
    assert "memory_control_intents" in {step.id for step in orc.last_flow_shadow_result.steps}
    assert {step.status for step in orc.last_flow_shadow_result.steps} == {"shadow"}


async def test_flow_shadow_core_dry_run_does_not_duplicate_side_effects(
    session_manager,
    settings,
):
    shadow_settings = Settings(
        session_window_turns=settings.session_window_turns,
        session_lock_ttl_seconds=settings.session_lock_ttl_seconds,
        orchestrator_flow_shadow_enabled=True,
        orchestrator_flow_shadow_mode="core_dry_run",
    )
    router = FakeRouter(RouteType.FAQ)
    orc, bus, cap = _build(session_manager, shadow_settings, router=router)

    await orc.handle(_make_event("hello"))

    assert cap.calls == 1
    assert len(bus.messages) == 1
    session = await session_manager.load("demo", "u1", "se_orc_01", Channel.WEB)
    assert [turn.role.value for turn in session.turns] == ["user", "assistant"]
    assert orc.last_flow_shadow_result is not None
    assert orc.last_flow_shadow_result.status == "stopped"
    assert orc.last_flow_shadow_result.stop_reason == "dry_run_skip_capability"
    assert orc.last_flow_shadow_result.steps[-1].id == "capability"


async def test_flow_shadow_core_dry_run_auto_executes_core_and_shadows_plugins(
    session_manager,
    settings,
):
    shadow_settings = Settings(
        session_window_turns=settings.session_window_turns,
        session_lock_ttl_seconds=settings.session_lock_ttl_seconds,
        orchestrator_flow_shadow_enabled=True,
        orchestrator_flow_shadow_name="auto",
        orchestrator_flow_shadow_mode="core_dry_run",
    )
    flow_registry, owner_permissions = _plugin_flow_registry()
    router = FakeRouter(RouteType.FAQ)
    orc, bus, cap = _build(
        session_manager,
        shadow_settings,
        router=router,
        flow_step_registry=flow_registry,
        flow_owner_permissions=owner_permissions,
    )

    await orc.handle(
        _make_event(
            "hello",
            session_id="discord-channel-1",
            channel=Channel.DISCORD,
            metadata={"session_kind": "group"},
        )
    )

    assert cap.calls == 1
    assert len(bus.messages) == 1
    session = await session_manager.load(
        "demo",
        "u1",
        "discord-channel-1",
        Channel.DISCORD,
    )
    assert [turn.role.value for turn in session.turns] == ["user", "assistant"]
    assert orc.last_flow_shadow_result is not None
    assert orc.last_flow_shadow_result.flow_name == "default_group_channel_flow"
    assert orc.last_flow_shadow_result.status == "stopped"
    assert orc.last_flow_shadow_result.stop_reason == "dry_run_skip_capability"
    assert orc.last_flow_shadow_result.steps[-1].id == "capability"
    assert "ok" in {step.status for step in orc.last_flow_shadow_result.steps}
    assert "shadow" in {step.status for step in orc.last_flow_shadow_result.steps}


async def test_flow_shadow_plugin_dry_run_executes_only_non_effectful_plugins(
    session_manager,
    settings,
):
    shadow_settings = Settings(
        session_window_turns=settings.session_window_turns,
        session_lock_ttl_seconds=settings.session_lock_ttl_seconds,
        orchestrator_flow_shadow_enabled=True,
        orchestrator_flow_shadow_name="auto",
        orchestrator_flow_shadow_mode="core_dry_run",
        orchestrator_flow_shadow_plugin_dry_run_enabled=True,
    )
    flow_registry, owner_permissions = _plugin_flow_registry()
    memory_step = CountingFlowStep("memory_loaded")
    credits_step = CountingFlowStep("credits_reserved")
    router = FakeRouter(RouteType.FAQ)
    orc, bus, cap = _build(
        session_manager,
        shadow_settings,
        router=router,
        flow_step_registry=flow_registry,
        flow_owner_permissions=owner_permissions,
        flow_step_executors={
            "plugin.memory.control_intents": CountingFlowStep("no_memory_control"),
            "plugin.memory.load": memory_step,
            "plugin.credits.reserve": credits_step,
        },
    )

    await orc.handle(
        _make_event(
            "hello",
            session_id="discord-channel-1",
            channel=Channel.DISCORD,
            metadata={"session_kind": "group"},
        )
    )

    assert cap.calls == 1
    assert len(bus.messages) == 1
    assert memory_step.calls == 1
    assert credits_step.calls == 0
    assert orc.last_flow_shadow_result is not None
    traces = {step.kind: step for step in orc.last_flow_shadow_result.steps}
    assert traces["plugin.memory.load"].status == "ok"
    assert traces["plugin.memory.load"].reason == "memory_loaded"
    assert traces["plugin.credits.reserve"].status == "shadow"
    assert traces["plugin.credits.reserve"].reason == "effectful_shadow_skip"


async def test_faq_route_passes_preview_to_capability(session_manager, settings):
    cap = FakeFAQCapability(reply_text="faq-answer")
    orc, bus, _ = _build(session_manager, settings, capability=cap)
    await orc.handle(_make_event("hello"))

    assert cap.calls == 1
    assert cap.last_hints is not None
    assert cap.last_hints["faq_preview"]["answer"] == "faq-answer"
    assert cap.last_hints["faq_preview"]["verdict"] == "CLEAR"
    assert bus.messages[0][1]["segments"][0]["content"] == "faq-answer"


async def test_faq_preview_verdict_is_injected_into_router_signals(session_manager, settings):
    cap = FakeFAQCapability(reply_text="faq-answer")
    router = FakeRouter(RouteType.FAQ)
    orc, _bus, _ = _build(session_manager, settings, capability=cap, router=router)
    await orc.handle(_make_event("hello"))

    assert router.last_signals is not None
    assert router.last_signals["faq_verdict"] == "CLEAR"
    assert router.last_signals["faq_similarity"] == 0.97


async def test_first_message_transitions_idle_to_chatting(session_manager, settings):
    orc, _bus, _cap = _build(session_manager, settings)
    await orc.handle(_make_event("hello"))

    session = await session_manager.load("demo", "u1", "se_orc_01", Channel.WEB)
    assert session.state == SessionState.CHATTING
    # Two turns recorded: user + assistant.
    roles = [t.role.value for t in session.turns]
    assert roles == ["user", "assistant"]


class _SuppressReplyHook:
    name = "test.suppress_reply"
    point = HookPoint.BEFORE_ROUTE
    priority = 1

    async def run(self, ctx: PipelineContext) -> None:
        ctx.extras["suppress_outbound"] = True
        ctx.extras["skip_assistant_turn"] = True
        raise HookAbort("", reason="suppressed")


async def test_hook_can_suppress_outbound_and_assistant_turn(session_manager, settings):
    hooks = HookRunner()
    hooks.register(_SuppressReplyHook())
    orc, bus, cap = _build(session_manager, settings, hook_runner=hooks)

    outcome = await orc.handle(_make_event("hello"))

    assert cap.calls == 0
    assert outcome.status == ProcessingStatus.INTENTIONALLY_SUPPRESSED
    assert bus.messages == []
    session = await session_manager.load("demo", "u1", "se_orc_01", Channel.WEB)
    assert [turn.role.value for turn in session.turns] == ["user"]
    assert session.state == SessionState.CHATTING


class _OwnerGatedHook:
    name = "test.owner_gated"
    point = HookPoint.BEFORE_ROUTE
    priority = 1

    async def run(self, ctx: PipelineContext) -> None:
        _ = ctx
        raise AssertionError("hook must not run while its owner gate is unavailable")


async def test_transient_hook_owner_gate_failure_is_not_acknowledged(
    session_manager,
    settings,
):
    async def unavailable(owner: str, ctx: PipelineContext) -> bool:
        _ = owner, ctx
        raise ConnectionError("plugin state unavailable")

    hooks = HookRunner(owner_gate=unavailable)
    hooks.register(_OwnerGatedHook(), owner="guarded")
    orc, bus, cap = _build(session_manager, settings, hook_runner=hooks)

    outcome = await orc.handle(_make_event("hello"))

    assert outcome.status == ProcessingStatus.RETRYABLE_FAILURE
    assert outcome.ackable is False
    assert outcome.reason == "owner_gate_error"
    assert outcome.error_type == "PluginOwnerGateUnavailable"
    assert cap.calls == 0
    assert bus.messages == []


class _SelfSentAuditHook:
    name = "test.self_sent_audit"
    point = HookPoint.BEFORE_ROUTE
    priority = 1

    async def run(self, ctx: PipelineContext) -> None:
        ctx.extras["suppress_outbound"] = True
        ctx.extras["skip_assistant_turn"] = True
        ctx.extras["skip_state_transition"] = True
        raise HookAbort("", reason="self_sent_audit_only")


async def test_self_sent_event_does_not_append_user_turn_or_advance_state(
    session_manager,
    settings,
):
    hooks = HookRunner()
    hooks.register(_SelfSentAuditHook())
    orc, bus, cap = _build(session_manager, settings, hook_runner=hooks)
    event = _make_event("[fake] 已发送")
    event.metadata["is_self_sent"] = True

    await orc.handle(event)

    assert cap.calls == 0
    assert bus.messages == []
    async with session_manager._factory()() as db:
        row = await db.get(
            SessionRow,
            {
                "tenant_id": event.tenant_id,
                "session_id": event.session_id,
            },
        )
        assert row is None
        turns = (await db.execute(select(TurnRow))).scalars().all()
    assert turns == []


class _SlowCapability(FakeCapability):
    async def answer(
        self,
        pre: PreprocessedMessage,
        session: Session,
        hints: dict[str, Any] | None = None,
    ) -> CapabilityResult:
        self.calls += 1
        await asyncio.sleep(0.1)
        return CapabilityResult(route=RouteType.FAQ, reply_text=self.reply_text)


class _CaptureCannedAfterPostprocessHook:
    name = "test.capture_canned_after_postprocess"
    point = HookPoint.AFTER_POSTPROCESS
    priority = 1

    def __init__(self) -> None:
        self.calls: list[PipelineContext] = []

    async def run(self, ctx: PipelineContext) -> None:
        self.calls.append(ctx)
        ctx.extras["suppress_outbound"] = True


async def test_timeout_canned_reply_runs_after_postprocess_hooks(
    session_manager,
    settings,
    monkeypatch,
):
    hooks = HookRunner()
    hook = _CaptureCannedAfterPostprocessHook()
    hooks.register(hook)
    cap = _SlowCapability()
    orc, bus, _ = _build(
        session_manager,
        settings,
        capability=cap,
        hook_runner=hooks,
    )
    monkeypatch.setattr(orc, "handle_timeout", 0.05)

    outcome = await orc.handle(_make_event("slow"))

    # The end-to-end deadline also covers lock acquisition and session loading,
    # so a loaded runner may time out before entering the capability.
    assert outcome.status == ProcessingStatus.INTENTIONALLY_SUPPRESSED
    assert len(hook.calls) == 1
    assert hook.calls[0].reply is not None
    assert hook.calls[0].reply.primary_text
    assert hook.calls[0].result is not None
    assert hook.calls[0].result.route == RouteType.CANNED
    assert bus.messages == []


async def test_pii_map_is_merged_onto_session(session_manager, settings):
    orc, _bus, _cap = _build(session_manager, settings)
    await orc.handle(_make_event("my phone is here"))

    session = await session_manager.load("demo", "u1", "se_orc_01", Channel.WEB)
    assert session.pii_map.get("<PII:phone:1>") == "13800000000"


# ---------------------------------------------------------------------------
# Handoff short-circuit
# ---------------------------------------------------------------------------


async def test_escalated_session_short_circuits_to_handoff(session_manager, settings):
    # Pre-seed the session in ESCALATED state.
    session = await session_manager.load("demo", "u1", "se_esc_01", Channel.WEB)
    session.state = SessionState.CHATTING
    await session_manager.save(session)
    await session_manager.set_state(session, SessionState.ESCALATED)

    orc, bus, cap = _build(session_manager, settings)
    event = _make_event("anything", session_id="se_esc_01")
    await orc.handle(event)

    assert cap.calls == 0  # pipeline skipped
    assert len(bus.messages) == 1
    payload = bus.messages[0][1]
    # Canned handoff-pending text should be present.
    assert payload["segments"][0]["content"]  # non-empty


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------


async def test_capability_exception_degrades_to_canned(session_manager, settings):
    cap = FakeCapability(raise_exc=True)
    orc, bus, _ = _build(session_manager, settings, capability=cap)
    outcome = await orc.handle(_make_event("boom"))

    assert cap.calls == 1  # attempted
    assert outcome.status == ProcessingStatus.COMPLETED
    assert len(bus.messages) == 1
    payload = bus.messages[0][1]
    # Content is the degradation canned string — just assert non-empty & not the capability's text.
    assert payload["segments"][0]["content"] != "answer"


async def test_llm_exception_returns_model_specific_busy(session_manager, settings):
    cap = FakeRouteCapability(RouteType.LLM, raise_exc=True)
    orc, bus, _ = _build(
        session_manager,
        settings,
        router=FakeRouter(RouteType.LLM),
        extra_capabilities={RouteType.LLM: cap},
    )

    await orc.handle(_make_event("随便聊聊"))

    assert cap.calls == 1
    assert _outbound_text(bus) == "模型服务暂时不可用，请稍后再试。"


async def test_command_hook_result_is_not_replaced_by_llm_busy(session_manager, settings):
    hooks = HookRunner()
    hooks.register(CommandCenterHook(_CommandStore(), _command_service()), owner="commands")
    cap = FakeRouteCapability(RouteType.LLM, raise_exc=True)
    orc, bus, _ = _build(
        session_manager,
        settings,
        router=FakeRouter(RouteType.LLM),
        extra_capabilities={RouteType.LLM: cap},
        hook_runner=hooks,
    )

    await orc.handle(_make_event("/ping"))

    assert cap.calls == 0
    assert _outbound_text(bus) == "pong"


async def test_handoff_route_is_not_replaced_by_llm_busy(session_manager, settings):
    llm = FakeRouteCapability(RouteType.LLM, raise_exc=True)
    handoff = FakeRouteCapability(RouteType.HANDOFF, reply_text="已为您转接人工客服，请稍候。")
    orc, bus, _ = _build(
        session_manager,
        settings,
        router=FakeRouter(RouteType.HANDOFF),
        extra_capabilities={RouteType.LLM: llm, RouteType.HANDOFF: handoff},
    )

    await orc.handle(_make_event("转人工"))

    assert llm.calls == 0
    assert handoff.calls == 1
    assert _outbound_text(bus) == "已为您转接人工客服，请稍候。"


async def test_flow_command_step_result_is_not_replaced_by_llm_busy(
    session_manager,
    settings,
):
    flow_registry, owner_permissions = _plugin_flow_registry()
    cap = FakeRouteCapability(RouteType.LLM, raise_exc=True)
    orc, bus, _ = _build(
        session_manager,
        _target_flow_settings(settings),
        router=FakeRouter(RouteType.LLM),
        extra_capabilities={RouteType.LLM: cap},
        flow_step_registry=flow_registry,
        flow_owner_permissions=owner_permissions,
        flow_step_executors={
            "plugin.memory.control_intents": CountingFlowStep("no_memory_control"),
            "plugin.commands.dispatch": CommandDispatchStep(
                _CommandStore(),
                _command_service(),
            ),
            "plugin.repeater.detect": CountingFlowStep("not_triggered"),
            "plugin.moderation.inspect_input": CountingFlowStep("not_matched"),
            "plugin.persona_extract.skill_enrich": CountingFlowStep("not_enriched"),
            "plugin.memory.load": CountingFlowStep("no_memory"),
            "plugin.credits.query_command": CountingFlowStep("not_matched"),
            "plugin.moderation.enforce_input": CountingFlowStep("not_matched"),
            "plugin.credits.reserve": CountingFlowStep("no_cost"),
            "plugin.credits.settle": CountingFlowStep("no_cost"),
            "plugin.moderation.decorate_output": CountingFlowStep("unchanged"),
            "plugin.draw.postprocess_result": CountingFlowStep("no_draw_result"),
            "plugin.memory.save": CountingFlowStep("skipped"),
        },
    )

    await orc.handle(
        _make_event(
            "/ping",
            session_id="group-command",
            metadata={"session_kind": "group"},
        )
    )

    assert cap.calls == 0
    assert _outbound_text(bus) == "pong"
    assert orc.last_flow_runtime_result is not None
    assert orc.last_flow_runtime_result.stop_reason == "test_command"


async def test_credit_checkin_command_is_not_replaced_by_llm_busy(
    session_manager,
    settings,
):
    store = _CreditStore(cost=0, balance=20)
    hooks = HookRunner()
    hooks.register(
        CommandCenterHook(_CommandStore(), _credit_command_service(store)),
        owner="commands",
    )
    cap = FakeRouteCapability(RouteType.LLM, raise_exc=True)
    orc, bus, _ = _build(
        session_manager,
        settings,
        router=FakeRouter(RouteType.LLM),
        extra_capabilities={RouteType.LLM: cap},
        hook_runner=hooks,
    )

    await orc.handle(
        _make_event(
            "/签到",
            session_id="credit-room",
            channel=Channel.DISCORD,
            metadata={"session_kind": "group", "sender_name": "张三"},
        )
    )

    assert cap.calls == 0
    assert len(store.checkin_calls) == 1
    assert "签到成功！+10 积分" in _outbound_text(bus)


async def test_capability_exception_falls_back_to_faq(session_manager, settings):
    # Route picks AGENT but AGENT throws; FAQ (different capability) should answer.
    bad = FakeCapability(raise_exc=True)
    good = FakeCapability(reply_text="faq-fallback-reply")
    orc, bus, _ = _build(
        session_manager,
        settings,
        router=FakeRouter(RouteType.AGENT),
        capability=good,  # registered under FAQ
        extra_capabilities={RouteType.AGENT: bad},
    )
    await orc.handle(_make_event("need agent"))

    assert bad.calls == 1
    assert good.calls == 1
    assert len(bus.messages) == 1
    payload = bus.messages[0][1]
    assert payload["segments"][0]["content"] == "faq-fallback-reply"


# ---------------------------------------------------------------------------
# Safety block
# ---------------------------------------------------------------------------


async def test_input_safety_block_emits_canned(session_manager, settings):
    orc, bus, cap = _build(session_manager, settings, safety=FakeSafety(input_safe=False))
    await orc.handle(_make_event("bad"))

    assert cap.calls == 0
    assert len(bus.messages) == 1
    payload = bus.messages[0][1]
    assert payload["segments"][0]["content"]  # canned safety text


async def test_output_safety_block_overrides_capability(session_manager, settings):
    orc, bus, cap = _build(session_manager, settings, safety=FakeSafety(output_safe=False))
    await orc.handle(_make_event("hello"))

    assert cap.calls == 1
    payload = bus.messages[0][1]
    # Output was replaced; it is no longer the capability's "answer" text.
    assert payload["segments"][0]["content"] != "answer"
