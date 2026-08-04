from __future__ import annotations

from typing import Any

import pytest

from app.common.config import Settings
from app.common.exceptions import CapabilityError
from app.common.types import (
    CapabilityResult,
    Channel,
    InboundEvent,
    Message,
    OutboundReply,
    PreprocessedMessage,
    ReplySegment,
    ReplyType,
    RouteDecision,
    RouteType,
    Session,
    SessionState,
    Turn,
)
from app.orchestrator.core_steps import CoreStepDependencies, build_core_step_executors
from app.orchestrator.effects import InMemoryEffectCommitter
from app.orchestrator.flow import (
    CAPABILITY_DISPATCH_TIMEOUT_SECONDS,
    FlowCompiler,
    FlowStepSpec,
    build_default_flow_registry,
)
from app.orchestrator.pipeline import PipelineContext
from app.orchestrator.runner import FLOW_RUN_COMPLETED, FLOW_RUN_STOPPED, FlowRunner
from app.plugin.hooks import HookAbort, HookPoint, HookRunner


class _SessionManager:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}

    async def load(
        self,
        tenant_id: str,
        user_id: str,
        session_id: str,
        channel: Channel,
    ) -> Session:
        session = self.sessions.get(session_id)
        if session is None:
            session = Session(
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
                channel=channel,
            )
            self.sessions[session_id] = session
        return session

    async def append_turn(self, session: Session, turn: Turn) -> None:
        session.turns.append(turn)
        self.sessions[session.session_id] = session

    async def set_state(self, session: Session, new_state: SessionState) -> None:
        session.state = new_state
        self.sessions[session.session_id] = session


class _Preprocessor:
    async def run(self, message: Message) -> PreprocessedMessage:
        return PreprocessedMessage(
            original_text=message.content,
            cleaned_text=message.content.strip(),
            pii_map={"<PII:phone:1>": "13800000000"} if "phone" in message.content else {},
        )


class _Router:
    def __init__(self, *, boom: bool = False, route: RouteType = RouteType.FAQ) -> None:
        self.signals: dict[str, Any] | None = None
        self.boom = boom
        self.route = route

    async def decide(
        self,
        pre: PreprocessedMessage,
        session: Session,
        signals: dict[str, Any] | None = None,
    ) -> RouteDecision:
        if self.boom:
            raise RuntimeError("router boom")
        self.signals = dict(signals or {})
        return RouteDecision(
            type=self.route,
            confidence=0.9,
            reason="test route",
            hints={"rule": "test_rule"},
        )


class _Safety:
    def __init__(self, *, input_safe: bool = True, output_safe: bool = True) -> None:
        self.input_safe = input_safe
        self.output_safe = output_safe

    async def check_input(self, pre: PreprocessedMessage) -> bool:
        return self.input_safe

    async def check_output(self, result: CapabilityResult) -> bool:
        return self.output_safe


class _Postprocessor:
    async def run(self, result: CapabilityResult, session: Session) -> OutboundReply:
        return OutboundReply(
            tenant_id=session.tenant_id,
            channel=session.channel,
            user_id=session.user_id,
            session_id=session.session_id,
            type=ReplyType.TEXT,
            segments=[ReplySegment(type=ReplyType.TEXT, content=result.reply_text)],
            trace_id="trace",
        )


class _FAQCapability:
    def __init__(
        self,
        reply_text: str = "answer",
        *,
        boom: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.reply_text = reply_text
        self.boom = boom
        self.metadata = dict(metadata or {})
        self.hints: dict[str, Any] | None = None

    async def preview_match(
        self,
        pre: PreprocessedMessage,
        session: Session,
        hints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "matched": True,
            "score": 0.91,
            "scope": "global",
            "faq_id": "faq-1",
            "verdict": "CLEAR",
            "answer": self.reply_text,
        }

    async def answer(
        self,
        pre: PreprocessedMessage,
        session: Session,
        hints: dict[str, Any] | None = None,
    ) -> CapabilityResult:
        if self.boom:
            raise RuntimeError("capability boom")
        self.hints = dict(hints or {})
        return CapabilityResult(
            route=RouteType.FAQ,
            reply_text=self.reply_text,
            metadata=dict(self.metadata),
        )


class _PreviewFailingFAQCapability(_FAQCapability):
    async def preview_match(
        self,
        pre: PreprocessedMessage,
        session: Session,
        hints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _ = pre, session, hints
        raise RuntimeError("faq preview unavailable")


class _RecallMissCapability:
    async def answer(
        self,
        pre: PreprocessedMessage,
        session: Session,
        hints: dict[str, Any] | None = None,
    ) -> CapabilityResult:
        _ = pre, session, hints
        raise CapabilityError("no_context")


class _LLMCapability:
    def __init__(self) -> None:
        self.hints: dict[str, Any] | None = None

    async def answer(
        self,
        pre: PreprocessedMessage,
        session: Session,
        hints: dict[str, Any] | None = None,
    ) -> CapabilityResult:
        _ = pre, session
        self.hints = dict(hints or {})
        return CapabilityResult(route=RouteType.LLM, reply_text="通用聊天回复")


class _AgentCapability:
    def __init__(
        self,
        *,
        effective_tool_count: int = 0,
        preflight_error: Exception | None = None,
    ) -> None:
        self.effective_tool_count = effective_tool_count
        self.preflight_error = preflight_error
        self.preview_hints: dict[str, Any] | None = None
        self.answer_hints: dict[str, Any] | None = None

    async def preview_availability(
        self,
        pre: PreprocessedMessage,
        session: Session,
        hints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _ = pre, session
        self.preview_hints = dict(hints or {})
        if self.preflight_error is not None:
            raise self.preflight_error
        return {
            "effective_tool_count": self.effective_tool_count,
            "policy_allowed": self.effective_tool_count > 0,
            "denial_reason": (
                "" if self.effective_tool_count > 0 else "role_denied"
            ),
        }

    async def answer(
        self,
        pre: PreprocessedMessage,
        session: Session,
        hints: dict[str, Any] | None = None,
    ) -> CapabilityResult:
        _ = pre, session
        self.answer_hints = dict(hints or {})
        return CapabilityResult(route=RouteType.AGENT, reply_text="agent answer")


class _Bus:
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
        return "1-0"


class _AbortBeforeRouteHook:
    name = "test.abort"
    point = HookPoint.BEFORE_ROUTE
    priority = 1

    async def run(self, ctx: PipelineContext) -> None:
        raise HookAbort("blocked by hook", reason="test_abort")


def _event(content: str = "hello", *, self_sent: bool = False) -> InboundEvent:
    event = InboundEvent(
        message_id="m-1",
        tenant_id="demo",
        channel=Channel.WEB,
        user_id="u1",
        session_id="s1",
        message=Message(content=content),
    )
    if self_sent:
        event.metadata["is_self_sent"] = True
    return event


def _deps(
    *,
    safety: _Safety | None = None,
    hooks: HookRunner | None = None,
    router_boom: bool = False,
    capability_boom: bool = False,
    capability: _FAQCapability | None = None,
    route: RouteType = RouteType.FAQ,
    side_effects_enabled: bool = True,
    capability_dispatch_enabled: bool = True,
    faq_preview_enabled: bool = True,
) -> tuple[CoreStepDependencies, _SessionManager, _Router, _FAQCapability, _Bus]:
    sessions = _SessionManager()
    router = _Router(boom=router_boom, route=route)
    capability = capability or _FAQCapability(boom=capability_boom)
    capabilities: dict[RouteType, Any] = {RouteType.FAQ: capability}
    if route != RouteType.FAQ:
        capabilities[route] = capability
    bus = _Bus()
    deps = CoreStepDependencies(
        session_manager=sessions,
        preprocessor=_Preprocessor(),
        router=router,
        safety=safety or _Safety(),
        postprocessor=_Postprocessor(),
        capabilities=capabilities,
        bus=bus,
        settings=Settings(),
        hook_runner=hooks,
        side_effects_enabled=side_effects_enabled,
        capability_dispatch_enabled=capability_dispatch_enabled,
        faq_preview_enabled=faq_preview_enabled,
    )
    return deps, sessions, router, capability, bus


def _compile_flow(step_ids: list[tuple[str, str]]):
    return FlowCompiler(build_default_flow_registry()).compile(
        name="core_test_flow",
        steps=[FlowStepSpec(id=step_id, kind=kind) for step_id, kind in step_ids],
    )


def _happy_flow():
    return _compile_flow(
        [
            ("load_session", "core.load_session"),
            ("preprocess", "core.preprocess"),
            ("append_user_turn", "core.append_user_turn"),
            ("router_signal_merge", "core.router_signal_merge"),
            ("route", "core.route"),
            ("capability", "core.capability_dispatch"),
            ("output_safety", "core.output_safety"),
            ("postprocess", "core.postprocess"),
            ("commit", "core.commit_turns_and_publish"),
        ]
    )


def _default_compatible_core_flow():
    return FlowCompiler(build_default_flow_registry()).compile(
        name="default_compatible_flow",
        steps=[
            FlowStepSpec(id="load_session", kind="core.load_session"),
            FlowStepSpec(
                id="before_preprocess_hooks",
                kind="core.legacy_hooks.before_preprocess",
            ),
            FlowStepSpec(id="preprocess", kind="core.preprocess"),
            FlowStepSpec(
                id="after_preprocess_hooks",
                kind="core.legacy_hooks.after_preprocess",
            ),
            FlowStepSpec(id="append_user_turn", kind="core.append_user_turn"),
            FlowStepSpec(id="handoff_short_circuit", kind="core.handoff_short_circuit"),
            FlowStepSpec(id="input_safety", kind="core.input_safety"),
            FlowStepSpec(
                id="before_route_hooks",
                kind="core.legacy_hooks.before_route",
            ),
            FlowStepSpec(id="router_signal_merge", kind="core.router_signal_merge"),
            FlowStepSpec(id="route", kind="core.route"),
            FlowStepSpec(id="after_route_hooks", kind="core.legacy_hooks.after_route"),
            FlowStepSpec(
                id="before_capability_hooks",
                kind="core.legacy_hooks.before_capability",
            ),
            FlowStepSpec(id="capability", kind="core.capability_dispatch"),
            FlowStepSpec(
                id="after_capability_hooks",
                kind="core.legacy_hooks.after_capability",
            ),
            FlowStepSpec(id="output_safety", kind="core.output_safety"),
            FlowStepSpec(
                id="before_postprocess_hooks",
                kind="core.legacy_hooks.before_postprocess",
            ),
            FlowStepSpec(id="postprocess", kind="core.postprocess"),
            FlowStepSpec(
                id="after_postprocess_hooks",
                kind="core.legacy_hooks.after_postprocess",
            ),
            FlowStepSpec(id="commit", kind="core.commit_turns_and_publish"),
        ],
    )


def _finalizable_flow():
    return _compile_flow(
        [
            ("load_session", "core.load_session"),
            ("preprocess", "core.preprocess"),
            ("append_user_turn", "core.append_user_turn"),
            ("before_route", "core.legacy_hooks.before_route"),
            ("router_signal_merge", "core.router_signal_merge"),
            ("route", "core.route"),
            ("capability", "core.capability_dispatch"),
            ("output_safety", "core.output_safety"),
            ("before_postprocess", "core.legacy_hooks.before_postprocess"),
            ("postprocess", "core.postprocess"),
            ("after_postprocess", "core.legacy_hooks.after_postprocess"),
            ("commit", "core.commit_turns_and_publish"),
        ]
    )


def test_capability_dispatch_executor_uses_extended_timeout() -> None:
    deps, _sessions, _router, _capability, _bus = _deps()
    executors = build_core_step_executors(deps)

    assert (
        executors["core.capability_dispatch"].timeout_seconds
        == CAPABILITY_DISPATCH_TIMEOUT_SECONDS
    )


@pytest.mark.asyncio
async def test_core_steps_run_happy_path_flow() -> None:
    deps, sessions, router, capability, bus = _deps()
    ctx = PipelineContext(event=_event("hello phone"), trace_id="trace")

    result = await FlowRunner(build_core_step_executors(deps)).run(_happy_flow(), ctx)

    assert result.status == FLOW_RUN_COMPLETED
    assert ctx.session is not None
    assert ctx.session.pii_map["<PII:phone:1>"] == "13800000000"
    assert router.signals is not None
    assert router.signals["faq_similarity"] == 0.91
    assert router.signals["faq_matched"] is True
    assert router.signals["faq_verdict"] == "CLEAR"
    assert router.signals["consecutive_fallbacks"] == 0
    assert capability.hints is not None
    assert capability.hints["faq_preview"]["faq_id"] == "faq-1"
    assert bus.messages[0][2] == "demo:s1"
    assert bus.messages[0][1]["segments"][0]["content"] == "answer"
    assert [effect.type for effect in ctx.effects] == [
        "append_user_turn",
        "commit_turns_and_publish",
    ]
    assert ctx.effects[0].payload["dry_run"] is False
    assert ctx.effects[1].payload["publish_outbound"] is True
    session = sessions.sessions["s1"]
    assert session.state == SessionState.CHATTING
    assert [turn.role.value for turn in session.turns] == ["user", "assistant"]
    assert session.variables["consecutive_fallbacks"] == 0
    assert session.turns[-1].metadata == {
        "route": "faq",
        "intent_coarse": "unknown",
        "route_confidence": 0.9,
        "route_rule": "test_rule",
    }


@pytest.mark.asyncio
async def test_core_route_continues_when_faq_preview_is_unavailable() -> None:
    capability = _PreviewFailingFAQCapability()
    deps, _sessions, router, _capability, bus = _deps(capability=capability)
    ctx = PipelineContext(event=_event("hello"), trace_id="trace")

    result = await FlowRunner(build_core_step_executors(deps)).run(_happy_flow(), ctx)

    assert result.status == FLOW_RUN_COMPLETED
    assert router.signals is not None
    assert router.signals["faq_preview_failed"] is True
    assert router.signals["faq_preview_error_class"] == "RuntimeError"
    assert bus.messages[0][1]["segments"][0]["content"] == "answer"


@pytest.mark.asyncio
async def test_core_route_uses_effective_agent_tool_preflight() -> None:
    capability = _AgentCapability(effective_tool_count=0)
    deps, _sessions, router, _capability, _bus = _deps(
        capability=capability,  # type: ignore[arg-type]
        route=RouteType.AGENT,
    )
    ctx = PipelineContext(event=_event("查地图"), trace_id="trace")
    ctx.signals["router"] = {
        "tool_intent_matched": True,
        "tools_available": True,
    }
    ctx.signals["agent"] = {"tool_scope": "map"}

    result = await FlowRunner(build_core_step_executors(deps)).run(
        _happy_flow(),
        ctx,
    )

    assert result.status == FLOW_RUN_COMPLETED
    assert router.signals is not None
    assert router.signals["tool_intent_matched"] is True
    assert router.signals["tools_available"] is False
    assert router.signals["effective_tool_count"] == 0
    assert router.signals["policy_allowed"] is False
    assert router.signals["tool_denial_reason"] == "role_denied"
    assert capability.preview_hints is not None
    assert capability.preview_hints["agent_tool_scope"] == "map"


@pytest.mark.asyncio
async def test_core_route_passes_required_agent_effect_to_capability() -> None:
    capability = _AgentCapability(effective_tool_count=1)
    deps, _sessions, _router, _capability, _bus = _deps(
        capability=capability,  # type: ignore[arg-type]
        route=RouteType.AGENT,
    )
    ctx = PipelineContext(event=_event("生成文件发给我"), trace_id="trace")
    ctx.signals["router"] = {
        "tool_intent_matched": True,
        "tools_available": True,
    }
    ctx.signals["agent"] = {"tool_scope": "file_analysis"}
    required_effect = {
        "type": "outbound_file",
        "scope": "file_analysis",
        "tool": "generate_text_file",
        "operation": "generate",
        "format": "txt",
    }
    ctx.extras["agent_required_effect"] = required_effect

    result = await FlowRunner(build_core_step_executors(deps)).run(
        _happy_flow(),
        ctx,
    )

    assert result.status == FLOW_RUN_COMPLETED
    assert capability.answer_hints is not None
    assert capability.answer_hints["agent_required_effect"] == required_effect


@pytest.mark.asyncio
async def test_core_tool_preflight_failure_fails_closed() -> None:
    capability = _AgentCapability(preflight_error=RuntimeError("unavailable"))
    deps, _sessions, router, _capability, _bus = _deps(
        capability=capability,  # type: ignore[arg-type]
        route=RouteType.AGENT,
    )
    ctx = PipelineContext(event=_event("查地图"), trace_id="trace")
    ctx.signals["router"] = {
        "tool_intent_matched": True,
        "tools_available": True,
    }

    result = await FlowRunner(build_core_step_executors(deps)).run(
        _happy_flow(),
        ctx,
    )

    assert result.status == FLOW_RUN_COMPLETED
    assert router.signals is not None
    assert router.signals["tools_available"] is False
    assert router.signals["effective_tool_count"] == 0
    assert router.signals["policy_allowed"] is False
    assert router.signals["tool_denial_reason"] == "preflight_failed"
    assert router.signals["tool_preflight_failed"] is True
    assert router.signals["tool_preflight_error_class"] == "RuntimeError"


@pytest.mark.asyncio
async def test_core_flow_reads_increments_and_clears_fallback_counter() -> None:
    deps, sessions, router, _capability, _bus = _deps(capability_boom=True)
    sessions.sessions["s1"] = Session(
        tenant_id="demo",
        user_id="u1",
        session_id="s1",
        channel=Channel.WEB,
        variables={"consecutive_fallbacks": 1},
    )
    failed_ctx = PipelineContext(event=_event("hello"), trace_id="trace")

    failed = await FlowRunner(build_core_step_executors(deps)).run(
        _default_compatible_core_flow(),
        failed_ctx,
    )

    assert failed.status == FLOW_RUN_STOPPED
    assert router.signals is not None
    assert router.signals["consecutive_fallbacks"] == 1
    assert sessions.sessions["s1"].variables["consecutive_fallbacks"] == 2

    success_deps, success_sessions, success_router, _capability, _bus = _deps()
    success_sessions.sessions["s1"] = Session(
        tenant_id="demo",
        user_id="u1",
        session_id="s1",
        channel=Channel.WEB,
        variables={"consecutive_fallbacks": 2},
    )
    success_ctx = PipelineContext(event=_event("hello"), trace_id="trace-success")

    succeeded = await FlowRunner(build_core_step_executors(success_deps)).run(
        _happy_flow(),
        success_ctx,
    )

    assert succeeded.status == FLOW_RUN_COMPLETED
    assert success_router.signals is not None
    assert success_router.signals["consecutive_fallbacks"] == 2
    assert success_sessions.sessions["s1"].variables["consecutive_fallbacks"] == 0


@pytest.mark.asyncio
async def test_core_flow_never_injects_shared_group_fallback_counter() -> None:
    deps, sessions, router, _capability, _bus = _deps()
    sessions.sessions["s1"] = Session(
        tenant_id="demo",
        user_id="u1",
        session_id="s1",
        channel=Channel.WEB,
        variables={"consecutive_fallbacks": 9},
        metadata={"session_kind": "group"},
    )
    ctx = PipelineContext(event=_event("hello"), trace_id="trace")
    ctx.signals["router"] = {"consecutive_fallbacks": 99}

    result = await FlowRunner(build_core_step_executors(deps)).run(
        _happy_flow(),
        ctx,
    )

    assert result.status == FLOW_RUN_COMPLETED
    assert router.signals is not None
    assert "consecutive_fallbacks" not in router.signals
    assert sessions.sessions["s1"].variables["consecutive_fallbacks"] == 9


@pytest.mark.asyncio
async def test_core_rag_miss_falls_back_to_generic_llm_chat() -> None:
    deps, sessions, _router, _capability, bus = _deps(
        capability=_RecallMissCapability(),  # type: ignore[arg-type]
        route=RouteType.RAG,
    )
    llm = _LLMCapability()
    deps.capabilities[RouteType.LLM] = llm
    ctx = PipelineContext(event=_event("随便聊聊"), trace_id="trace")

    result = await FlowRunner(build_core_step_executors(deps)).run(_happy_flow(), ctx)

    assert result.status == FLOW_RUN_COMPLETED
    assert ctx.result is not None
    assert ctx.result.route == RouteType.LLM
    assert ctx.result.metadata["fallback_from"] == RouteType.RAG.value
    assert ctx.result.metadata["fallback_reason"] == "no_context"
    assert llm.hints is not None
    assert llm.hints["fallback_from"] == RouteType.RAG.value
    assert bus.messages[0][1]["segments"][0]["content"] == "通用聊天回复"
    assert [turn.role.value for turn in sessions.sessions["s1"].turns] == [
        "user",
        "assistant",
    ]
    user_turn = sessions.sessions["s1"].turns[0]
    assert user_turn.metadata["user_id"] == "u1"
    assert user_turn.metadata["external_participant_id"] == "u1"
    assert user_turn.metadata["canonical_participant_id"] == "u1"
    assert sessions.sessions["s1"].turns[-1].metadata["fallback_from"] == "rag"
    assert (
        sessions.sessions["s1"].turns[-1].metadata["fallback_reason"]
        == "no_context"
    )
    assert sessions.sessions["s1"].variables["consecutive_fallbacks"] == 1


@pytest.mark.asyncio
async def test_core_postprocess_guard_runs_before_flow_commit_publish() -> None:
    capability = _FAQCapability(reply_text="当前连接的是什么模型？")
    deps, sessions, _router, _capability, bus = _deps(capability=capability)
    ctx = PipelineContext(event=_event("当前连接的是什么模型？"), trace_id="trace")

    result = await FlowRunner(build_core_step_executors(deps)).run(_happy_flow(), ctx)

    assert result.status == FLOW_RUN_COMPLETED
    published_text = bus.messages[0][1]["segments"][0]["content"]
    assert published_text.startswith("我刚才没有生成有效答案。")
    assert sessions.sessions["s1"].turns[-1].content == published_text


@pytest.mark.asyncio
async def test_core_commit_emits_channel_reply_effects_after_output_safety() -> None:
    capability = _FAQCapability(
        metadata={
            "channel_reply_effects": [
                {
                    "type": "enqueue_channel_reply",
                    "owner": "wxbot",
                    "producer_owner": "amap",
                    "idempotency_key": "channel-reply:demo:trace:1",
                    "payload": {
                        "channel": "wechat",
                        "session_id": "s1",
                        "body": {"type": "text", "text": "地图已发送"},
                    },
                }
            ]
        }
    )
    deps, _sessions, _router, _capability, _bus = _deps(capability=capability)
    ctx = PipelineContext(event=_event("hello"), trace_id="trace")

    result = await FlowRunner(build_core_step_executors(deps)).run(
        _default_compatible_core_flow(),
        ctx,
    )

    assert result.status == FLOW_RUN_COMPLETED
    effects = [
        effect
        for effect in ctx.effects
        if effect.type == "enqueue_channel_reply" and effect.owner == "wxbot"
    ]
    assert len(effects) == 1
    assert effects[0].idempotency_key == "channel-reply:demo:trace:1"
    assert effects[0].producer_owner == "amap"
    assert effects[0].payload["body"]["text"] == "地图已发送"


@pytest.mark.asyncio
async def test_core_output_safety_clears_pending_channel_reply_effects() -> None:
    capability = _FAQCapability(
        metadata={
            "channel_reply_effects": [
                {
                    "type": "enqueue_channel_reply",
                    "owner": "wxbot",
                    "idempotency_key": "channel-reply:demo:trace:unsafe",
                    "payload": {
                        "channel": "wechat",
                        "session_id": "s1",
                        "body": {"type": "text", "text": "unsafe send"},
                    },
                }
            ],
            "skip_assistant_turn": True,
            "suppress_outbound": True,
        }
    )
    deps, sessions, _router, _capability, bus = _deps(
        safety=_Safety(output_safe=False),
        capability=capability,
    )
    ctx = PipelineContext(event=_event("hello"), trace_id="trace")

    result = await FlowRunner(build_core_step_executors(deps)).run(
        _default_compatible_core_flow(),
        ctx,
    )

    assert result.status == FLOW_RUN_COMPLETED
    assert all(effect.type != "enqueue_channel_reply" for effect in ctx.effects)
    assert bus.messages
    assert bus.messages[0][1]["segments"][0]["content"]
    assert [turn.role.value for turn in sessions.sessions["s1"].turns] == [
        "user",
        "assistant",
    ]
    assert "pending_channel_reply_effects" not in ctx.extras


@pytest.mark.asyncio
async def test_core_steps_run_default_compatible_core_flow() -> None:
    deps, sessions, router, capability, bus = _deps()
    ctx = PipelineContext(event=_event("hello"), trace_id="trace")

    result = await FlowRunner(build_core_step_executors(deps)).run(
        _default_compatible_core_flow(),
        ctx,
    )

    assert result.status == FLOW_RUN_COMPLETED
    assert len(result.steps) == 19
    assert router.signals is not None
    assert capability.hints is not None
    assert bus.messages[0][1]["segments"][0]["content"] == "answer"
    assert [turn.role.value for turn in sessions.sessions["s1"].turns] == [
        "user",
        "assistant",
    ]


@pytest.mark.asyncio
async def test_core_steps_dry_run_stops_before_capability_without_side_effects() -> None:
    deps, sessions, router, capability, bus = _deps(
        side_effects_enabled=False,
        capability_dispatch_enabled=False,
        faq_preview_enabled=False,
    )
    ctx = PipelineContext(event=_event("hello"), trace_id="trace")

    result = await FlowRunner(build_core_step_executors(deps)).run(
        _default_compatible_core_flow(),
        ctx,
    )

    assert result.status == FLOW_RUN_STOPPED
    assert result.stop_reason == "dry_run_skip_capability"
    assert [step.id for step in result.steps][-1] == "capability"
    assert router.signals == {"consecutive_fallbacks": 0}
    assert capability.hints is None
    assert bus.messages == []
    assert sessions.sessions["s1"].turns == []
    assert len(ctx.effects) == 1
    assert ctx.effects[0].type == "append_user_turn"
    assert ctx.effects[0].owner == "core"
    assert ctx.effects[0].payload["dry_run"] is True
    assert ctx.effects[0].payload["role"] == "user"
    assert ctx.signals["capability"] == {
        "skipped": True,
        "reason": "dry_run_skip_capability",
        "route": "faq",
    }


@pytest.mark.asyncio
async def test_core_dry_run_effects_can_be_committed_as_dry_run_records() -> None:
    deps, _sessions, _router, _capability, _bus = _deps(
        side_effects_enabled=False,
        capability_dispatch_enabled=False,
        faq_preview_enabled=False,
    )
    ctx = PipelineContext(event=_event("hello"), trace_id="trace")
    committer = InMemoryEffectCommitter()

    result = await FlowRunner(
        build_core_step_executors(deps),
        effect_committer=committer,
        effect_dry_run=True,
    ).run(
        _default_compatible_core_flow(),
        ctx,
    )

    assert result.status == FLOW_RUN_STOPPED
    assert len(committer.records) == 1
    assert committer.records[0].type == "append_user_turn"
    assert committer.records[0].owner == "core"
    assert committer.records[0].dry_run is True
    assert ctx.signals["effects"]["commits"][0]["status"] == "dry_run"


@pytest.mark.asyncio
async def test_core_append_user_turn_skips_self_sent_event() -> None:
    deps, sessions, _router, _capability, bus = _deps()
    ctx = PipelineContext(event=_event("self sent", self_sent=True), trace_id="trace")

    result = await FlowRunner(build_core_step_executors(deps)).run(_happy_flow(), ctx)

    assert result.status == FLOW_RUN_COMPLETED
    assert bus.messages
    session = sessions.sessions["s1"]
    assert [turn.role.value for turn in session.turns] == ["assistant"]


@pytest.mark.asyncio
async def test_core_input_safety_step_stops_flow() -> None:
    deps, _sessions, _router, _capability, bus = _deps(
        safety=_Safety(input_safe=False)
    )
    flow = _compile_flow(
        [
            ("load_session", "core.load_session"),
            ("preprocess", "core.preprocess"),
            ("input_safety", "core.input_safety"),
            ("route", "core.route"),
        ]
    )
    ctx = PipelineContext(event=_event("bad"), trace_id="trace")

    result = await FlowRunner(build_core_step_executors(deps)).run(flow, ctx)

    assert result.status == FLOW_RUN_STOPPED
    assert result.stop_reason == "input_safety_block"
    assert ctx.result is not None
    assert ctx.result.route == RouteType.CANNED
    assert bus.messages == []


@pytest.mark.asyncio
async def test_core_output_safety_replaces_result_before_commit() -> None:
    deps, _sessions, _router, _capability, bus = _deps(
        safety=_Safety(output_safe=False)
    )
    ctx = PipelineContext(event=_event("hello"), trace_id="trace")

    result = await FlowRunner(build_core_step_executors(deps)).run(_happy_flow(), ctx)

    assert result.status == FLOW_RUN_COMPLETED
    assert ctx.result is not None
    assert ctx.result.route == RouteType.CANNED
    assert bus.messages[0][1]["segments"][0]["content"] != "answer"


@pytest.mark.asyncio
async def test_legacy_hook_step_translates_hook_abort_to_step_result() -> None:
    hooks = HookRunner()
    hooks.register(_AbortBeforeRouteHook())
    deps, _sessions, _router, _capability, _bus = _deps(hooks=hooks)
    flow = _compile_flow(
        [
            ("load_session", "core.load_session"),
            ("preprocess", "core.preprocess"),
            ("before_route", "core.legacy_hooks.before_route"),
            ("route", "core.route"),
        ]
    )
    ctx = PipelineContext(event=_event("hello"), trace_id="trace")

    result = await FlowRunner(build_core_step_executors(deps)).run(flow, ctx)

    assert result.status == FLOW_RUN_STOPPED
    assert result.stop_reason == "test_abort"
    assert ctx.result is not None
    assert ctx.result.reply_text == "blocked by hook"


@pytest.mark.asyncio
async def test_legacy_hook_abort_finalizes_with_postprocess_and_commit() -> None:
    hooks = HookRunner()
    hooks.register(_AbortBeforeRouteHook())
    deps, sessions, router, capability, bus = _deps(hooks=hooks)
    ctx = PipelineContext(event=_event("hello"), trace_id="trace")

    result = await FlowRunner(build_core_step_executors(deps)).run(
        _finalizable_flow(),
        ctx,
    )

    assert result.status == FLOW_RUN_STOPPED
    assert result.stop_reason == "test_abort"
    assert [step.id for step in result.steps] == [
        "load_session",
        "preprocess",
        "append_user_turn",
        "before_route",
        "before_postprocess",
        "postprocess",
        "after_postprocess",
        "commit",
    ]
    assert router.signals is None
    assert capability.hints is None
    assert bus.messages[0][1]["segments"][0]["content"] == "blocked by hook"
    session = sessions.sessions["s1"]
    assert [turn.role.value for turn in session.turns] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_core_route_degrade_finalizes_busy_reply() -> None:
    deps, sessions, _router, capability, bus = _deps(router_boom=True)
    ctx = PipelineContext(event=_event("hello"), trace_id="trace")

    result = await FlowRunner(build_core_step_executors(deps)).run(
        _default_compatible_core_flow(),
        ctx,
    )

    assert result.status == FLOW_RUN_STOPPED
    assert result.stop_reason == "route_degraded"
    assert [step.id for step in result.steps] == [
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
        "output_safety",
        "before_postprocess_hooks",
        "postprocess",
        "after_postprocess_hooks",
        "commit",
    ]
    assert capability.hints is None
    assert bus.messages
    assert ctx.result is not None
    assert ctx.result.route == RouteType.CANNED
    assert ctx.result.metadata["degradation_reason"] == "core.route_failed"
    assert [turn.role.value for turn in sessions.sessions["s1"].turns] == [
        "user",
        "assistant",
    ]


@pytest.mark.asyncio
async def test_core_capability_degrade_finalizes_busy_reply() -> None:
    deps, sessions, _router, _capability, bus = _deps(capability_boom=True)
    ctx = PipelineContext(event=_event("hello"), trace_id="trace")

    result = await FlowRunner(build_core_step_executors(deps)).run(
        _default_compatible_core_flow(),
        ctx,
    )

    assert result.status == FLOW_RUN_STOPPED
    assert result.stop_reason == "capability_degraded"
    assert bus.messages
    assert ctx.result is not None
    assert ctx.result.route == RouteType.CANNED
    assert ctx.result.metadata["degradation_reason"] == "core.capability_dispatch_failed"
    assert [turn.role.value for turn in sessions.sessions["s1"].turns] == [
        "user",
        "assistant",
    ]


@pytest.mark.asyncio
async def test_core_llm_capability_failure_returns_model_busy_reply() -> None:
    deps, _sessions, _router, _capability, bus = _deps(
        capability_boom=True,
        route=RouteType.LLM,
    )
    ctx = PipelineContext(event=_event("hello"), trace_id="trace")

    result = await FlowRunner(build_core_step_executors(deps)).run(
        _default_compatible_core_flow(),
        ctx,
    )

    assert result.status == FLOW_RUN_COMPLETED
    assert ctx.result is not None
    assert ctx.result.metadata["degradation_reason"] == "capability_failed:llm"
    assert bus.messages[0][1]["segments"][0]["content"] == "模型服务暂时不可用，请稍后再试。"


@pytest.mark.asyncio
async def test_core_non_llm_capability_failure_uses_step_degrade() -> None:
    deps, _sessions, _router, _capability, _bus = _deps(capability_boom=True)
    ctx = PipelineContext(event=_event("hello"), trace_id="trace")

    result = await FlowRunner(build_core_step_executors(deps)).run(
        _default_compatible_core_flow(),
        ctx,
    )

    assert result.status == FLOW_RUN_STOPPED
    assert result.stop_reason == "capability_degraded"
    assert ctx.result is not None
    assert ctx.result.metadata["degradation_reason"] == "core.capability_dispatch_failed"
