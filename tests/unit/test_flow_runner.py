from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.common.types import (
    CapabilityResult,
    Channel,
    InboundEvent,
    Message,
    OutboundReply,
    PreprocessedMessage,
    ReplySegment,
    ReplyType,
    Role,
    RouteType,
    Session,
    SessionState,
    Turn,
)
from app.orchestrator.core_steps import (
    AppendUserTurnStep,
    CommitTurnsAndPublishStep,
    CoreStepDependencies,
)
from app.orchestrator.effect_handlers import (
    EFFECT_HANDLER_STATUS_DISABLED,
    EFFECT_HANDLER_STATUS_HANDLER_ERROR,
    EFFECT_HANDLER_STATUS_NO_HANDLER,
    EffectDispatcher,
    EffectHandlerRegistry,
    register_core_publish_outbound_handler,
    register_core_session_effect_handlers,
)
from app.orchestrator.effect_log import PostgresEffectLog
from app.orchestrator.effects import (
    EFFECT_COMMIT_SEMANTICS_AUDIT_AFTER_SIDE_EFFECT,
    EFFECT_COMMIT_SEMANTICS_GATE_BEFORE_SIDE_EFFECT,
    EFFECT_STATUS_COMPLETED,
    EFFECT_STATUS_DRY_RUN,
    EFFECT_STATUS_DUPLICATE,
    EFFECT_STATUS_FAILED,
    EFFECT_STATUS_RECORDED,
    AuditedEffectCommitter,
    EffectCommitRecord,
    EffectIdempotencyConflictError,
    InMemoryEffectCommitter,
    RedisEffectCommitter,
    normalize_effect,
)
from app.orchestrator.flow import (
    FLOW_STATUS_ACTIVE,
    FLOW_STATUS_DEGRADED,
    CompiledFlow,
    CompiledStep,
    MessageEffect,
    StepResult,
)
from app.orchestrator.pipeline import PipelineContext
from app.orchestrator.runner import (
    FLOW_RUN_COMPLETED,
    FLOW_RUN_FAILED,
    FLOW_RUN_STOPPED,
    STEP_TRACE_DEGRADED,
    STEP_TRACE_ERROR,
    STEP_TRACE_ERROR_OPEN,
    STEP_TRACE_OPTIONAL_SKIPPED,
    STEP_TRACE_OWNER_SKIPPED,
    STEP_TRACE_SHADOW,
    STEP_TRACE_TIMEOUT,
    STEP_TRACE_TIMEOUT_OPEN,
    FlowRunner,
)
from app.plugin.hooks import (
    RESULT_PRODUCER_OWNER_KEY,
    bind_result_producer_owner,
    trusted_result_producer_owner,
)
from tests.unit._schema_fixtures import bootstrap_effect_log_schema


class _Step:
    def __init__(
        self,
        result: StepResult | None = None,
        *,
        boom: bool = False,
        fail_times: int = 0,
        sleep_seconds: float = 0.0,
    ) -> None:
        self.result = result or StepResult()
        self.boom = boom
        self.fail_times = fail_times
        self.sleep_seconds = sleep_seconds
        self.calls = 0

    async def run(self, ctx: PipelineContext) -> StepResult:
        self.calls += 1
        if self.sleep_seconds > 0:
            await asyncio.sleep(self.sleep_seconds)
        if self.boom or self.calls <= self.fail_times:
            raise RuntimeError("step boom")
        return self.result


class _EffectRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.xadds: list[tuple[str, dict[str, str]]] = []

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool | None:
        _ = ex
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def xadd(self, stream: str, fields: dict[str, str]) -> str:
        self.xadds.append((stream, fields))
        return f"{len(self.xadds)}-0"


class _FailingCommitter:
    async def commit(
        self,
        effect: MessageEffect,
        ctx: PipelineContext,
        *,
        sequence: int = 0,
        dry_run: bool = False,
    ):
        _ = effect, ctx, sequence, dry_run
        raise RuntimeError("redis unavailable")


class _EffectAuditLog:
    def __init__(self, *, boom: bool = False) -> None:
        self.boom = boom
        self.records: list[dict[str, object]] = []

    async def record(self, **kwargs):
        if self.boom:
            raise RuntimeError("postgres unavailable")
        for existing in self.records:
            if (
                existing["tenant_id"] == kwargs["tenant_id"]
                and existing["idempotency_key"] == kwargs["idempotency_key"]
                and existing["dry_run"] == kwargs["dry_run"]
            ):
                return EffectCommitRecord(
                    type=str(existing["type"]),
                    owner=str(existing["owner"]),
                    idempotency_key=str(existing["idempotency_key"]),
                    payload=dict(existing.get("payload") or {}),
                    status=EFFECT_STATUS_DUPLICATE,
                    dry_run=bool(existing.get("dry_run")),
                    tenant_id=str(existing["tenant_id"]),
                )
        self.records.append(dict(kwargs))
        return EffectCommitRecord(
            type=str(kwargs["type"]),
            owner=str(kwargs["owner"]),
            idempotency_key=str(kwargs["idempotency_key"]),
            payload=dict(kwargs.get("payload") or {}),
            status=str(kwargs.get("status") or EFFECT_STATUS_RECORDED),
            dry_run=bool(kwargs.get("dry_run")),
            tenant_id=str(kwargs["tenant_id"]),
        )


class _RecordingEffectHandler:
    def __init__(self) -> None:
        self.calls: list[MessageEffect] = []

    async def __call__(self, effect, ctx, record) -> None:
        _ = ctx, record
        self.calls.append(effect)


class _BoomEffectHandler:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, effect, ctx, record) -> None:
        _ = effect, ctx, record
        self.calls += 1
        raise RuntimeError("handler boom")


class _CoreSessionManager:
    def __init__(self) -> None:
        self.appended: list[Turn] = []
        self.states: list[SessionState] = []

    async def append_turn(self, session: Session, turn: Turn) -> None:
        session.turns.append(turn)
        self.appended.append(turn)

    async def set_state(self, session: Session, new_state: SessionState) -> None:
        session.state = new_state
        self.states.append(new_state)


class _CoreBus:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, object], str | None]] = []

    async def publish(
        self,
        stream: str,
        payload: dict[str, object],
        headers: dict[str, str] | None = None,
        partition_key: str | None = None,
    ) -> str:
        _ = headers
        self.messages.append((stream, payload, partition_key))
        return "1-0"


def _ctx(*, tenant_id: str = "demo") -> PipelineContext:
    event = InboundEvent(
        message_id="m-1",
        tenant_id=tenant_id,
        channel=Channel.WEB,
        user_id="u1",
        session_id="s1",
        message=Message(content="hello"),
    )
    return PipelineContext(event=event, trace_id=event.trace_id)


def _flow(
    *steps: CompiledStep,
    status: str = FLOW_STATUS_ACTIVE,
) -> CompiledFlow:
    return CompiledFlow(
        name="test_flow",
        version=1,
        steps=list(steps),
        status=status,
    )


def _compiled_step(
    step_id: str,
    kind: str,
    *,
    error_policy: str = "fail_closed",
    timeout_seconds: float = 5.0,
    outputs: set[str] | None = None,
    optional: bool = False,
    owner: str = "test",
) -> CompiledStep:
    return CompiledStep(
        id=step_id,
        kind=kind,
        owner=owner,
        name=kind,
        permissions=[],
        inputs=set(),
        outputs=set(outputs or set()),
        timeout_seconds=timeout_seconds,
        error_policy=error_policy,
        optional=optional,
    )


def _effect_dispatcher(handler, *, owner: str = "test") -> EffectDispatcher:
    registry = EffectHandlerRegistry()
    registry.register("publish_outbound", owner, handler)
    return EffectDispatcher(registry, InMemoryEffectCommitter())


def _selective_effect_dispatcher(
    handler,
    *,
    enabled_handlers: object,
    owner: str = "test",
) -> EffectDispatcher:
    registry = EffectHandlerRegistry()
    registry.register("publish_outbound", owner, handler)
    return EffectDispatcher(
        registry,
        InMemoryEffectCommitter(),
        enabled_handlers=enabled_handlers,
    )


def _core_deps(
    session_manager: _CoreSessionManager,
    bus: _CoreBus,
    *,
    effect_handlers_enabled: bool = False,
) -> CoreStepDependencies:
    return CoreStepDependencies(
        session_manager=session_manager,
        preprocessor=object(),
        router=object(),
        safety=object(),
        postprocessor=object(),
        capabilities={},
        bus=bus,
        settings=SimpleNamespace(bus_outbound_stream="outbound"),
        effect_handlers_enabled=effect_handlers_enabled,
    )


@pytest.mark.asyncio
async def test_flow_runner_shadow_mode_traces_missing_executors() -> None:
    flow = _flow(_compiled_step("one", "plugin.test.one"))
    result = await FlowRunner(shadow=True).run(flow, _ctx())

    assert result.status == FLOW_RUN_COMPLETED
    assert result.steps[0].status == STEP_TRACE_SHADOW
    assert result.steps[0].reason == "shadow_noop"


@pytest.mark.asyncio
async def test_flow_runner_fails_missing_executor_outside_shadow_mode() -> None:
    flow = _flow(_compiled_step("one", "plugin.test.one"))
    result = await FlowRunner().run(flow, _ctx())

    assert result.status == FLOW_RUN_FAILED
    assert result.error == "missing_flow_step_executor:plugin.test.one"


@pytest.mark.asyncio
async def test_flow_runner_skips_missing_optional_executor_in_degraded_flow() -> None:
    flow = _flow(
        _compiled_step("optional", "plugin.test.optional", optional=True),
        status=FLOW_STATUS_DEGRADED,
    )

    result = await FlowRunner().run(flow, _ctx())

    assert result.status == FLOW_RUN_COMPLETED
    assert result.steps[0].status == STEP_TRACE_OPTIONAL_SKIPPED
    assert result.steps[0].reason == "optional_executor_unavailable"
    assert result.steps[0].attempts == 0


@pytest.mark.asyncio
async def test_flow_runner_applies_step_result_to_context() -> None:
    capability = CapabilityResult(route=RouteType.CANNED, reply_text="done")
    effect = MessageEffect(type="publish_outbound", owner="test")
    step = _Step(
        StepResult(
            action="suppress_outbound",
            result=capability,
            append_assistant_turn=False,
            effects=[effect],
        )
    )
    ctx = _ctx()

    result = await FlowRunner({"plugin.test.one": step}).run(
        _flow(_compiled_step("one", "plugin.test.one")),
        ctx,
    )

    assert result.status == FLOW_RUN_COMPLETED
    assert ctx.result == capability
    assert ctx.extras["suppress_outbound"] is True
    assert ctx.extras["skip_assistant_turn"] is True
    assert ctx.effects == [
        MessageEffect(type="publish_outbound", owner="test", producer_owner="test")
    ]


@pytest.mark.asyncio
async def test_flow_runner_binds_plain_result_to_compiled_owner_not_plugin_claim() -> None:
    capability = CapabilityResult(route=RouteType.CANNED, reply_text="plugin reply")

    class _SpoofingResultStep:
        async def run(self, ctx: PipelineContext) -> StepResult:
            ctx.extras[RESULT_PRODUCER_OWNER_KEY] = "core"
            return StepResult(result=capability, reason="one_degraded")

    ctx = _ctx()
    result = await FlowRunner({"plugin.test.one": _SpoofingResultStep()}).run(
        _flow(_compiled_step("one", "plugin.test.one")),
        ctx,
    )

    assert result.status == FLOW_RUN_COMPLETED
    assert ctx.result == capability
    assert trusted_result_producer_owner(ctx) == "test"
    assert ctx.extras[RESULT_PRODUCER_OWNER_KEY] == "test"


@pytest.mark.asyncio
async def test_flow_runner_preserves_core_result_ownership() -> None:
    capability = CapabilityResult(route=RouteType.CANNED, reply_text="core reply")
    ctx = _ctx()
    ctx.extras[RESULT_PRODUCER_OWNER_KEY] = "plugin"

    await FlowRunner({"core.result": _Step(StepResult(result=capability))}).run(
        _flow(_compiled_step("core", "core.result", owner="core")),
        ctx,
    )

    assert ctx.result == capability
    assert trusted_result_producer_owner(ctx) == "core"
    assert ctx.extras[RESULT_PRODUCER_OWNER_KEY] == "core"


@pytest.mark.asyncio
async def test_flow_runner_drops_plain_result_when_owner_disables_during_step() -> None:
    gate_calls = 0

    async def gate(owner: str, ctx: PipelineContext) -> bool:
        nonlocal gate_calls
        _ = (owner, ctx)
        gate_calls += 1
        return gate_calls == 1

    capability = CapabilityResult(route=RouteType.CANNED, reply_text="stale reply")
    ctx = _ctx()
    result = await FlowRunner(
        {"plugin.test.one": _Step(StepResult(result=capability))},
        owner_gate=gate,
    ).run(_flow(_compiled_step("one", "plugin.test.one")), ctx)

    assert gate_calls == 2
    assert result.steps[0].status == STEP_TRACE_OWNER_SKIPPED
    assert ctx.result is None
    assert trusted_result_producer_owner(ctx) == ""


def _plain_reply_context(text: str) -> PipelineContext:
    ctx = _ctx()
    ctx.session = Session(
        tenant_id="demo",
        session_id="s1",
        user_id="u1",
        channel=Channel.WEB,
    )
    ctx.reply = OutboundReply(
        tenant_id="demo",
        channel=Channel.WEB,
        user_id="u1",
        session_id="s1",
        type=ReplyType.TEXT,
        segments=[ReplySegment(type=ReplyType.TEXT, content=text)],
    )
    return ctx


def _plain_result_flow() -> CompiledFlow:
    return _flow(
        _compiled_step("plugin", "plugin.test.result"),
        _compiled_step("commit", "core.commit_turns_and_publish", owner="core"),
    )


@pytest.mark.asyncio
async def test_final_commit_rechecks_result_owner_and_blocks_append_publish_race() -> None:
    gate_calls = 0

    async def gate(owner: str, ctx: PipelineContext) -> bool:
        nonlocal gate_calls
        _ = (owner, ctx)
        gate_calls += 1
        return gate_calls < 3

    sessions = _CoreSessionManager()
    bus = _CoreBus()
    deps = _core_deps(sessions, bus, effect_handlers_enabled=True)
    ctx = _plain_reply_context("stale reply")
    result = await FlowRunner(
        {
            "plugin.test.result": _Step(
                StepResult(
                    result=CapabilityResult(
                        route=RouteType.CANNED,
                        reply_text="stale reply",
                    )
                )
            ),
            "core.commit_turns_and_publish": CommitTurnsAndPublishStep(deps),
        },
        effect_committer=InMemoryEffectCommitter(),
        owner_gate=gate,
    ).run(_plain_result_flow(), ctx)

    assert gate_calls == 3
    assert result.steps[-1].status == STEP_TRACE_OWNER_SKIPPED
    assert sessions.appended == []
    assert bus.messages == []
    assert ctx.effects == []
    assert ctx.extras["suppress_outbound"] is True


@pytest.mark.asyncio
async def test_core_commit_propagates_plain_result_owner_to_durable_reply_effects() -> None:
    sessions = _CoreSessionManager()
    bus = _CoreBus()
    deps = _core_deps(sessions, bus, effect_handlers_enabled=True)
    committer = InMemoryEffectCommitter()
    ctx = _plain_reply_context("plugin reply")
    await FlowRunner(
        {
            "plugin.test.result": _Step(
                StepResult(
                    result=CapabilityResult(
                        route=RouteType.CANNED,
                        reply_text="plugin reply",
                    )
                )
            ),
            "core.commit_turns_and_publish": CommitTurnsAndPublishStep(deps),
        },
        effect_committer=committer,
    ).run(_plain_result_flow(), ctx)

    producers = {record.type: record.producer_owner for record in committer.records}
    assert producers["append_assistant_turn"] == "test"
    assert producers["publish_outbound"] == "test"
    assert producers["set_session_state"] == "core"
    assert producers["commit_turns_and_publish"] == "core"


@pytest.mark.asyncio
async def test_core_commit_fails_closed_at_direct_append_boundary() -> None:
    gate_calls = 0

    async def gate(owner: str, ctx: PipelineContext) -> bool:
        nonlocal gate_calls
        _ = (owner, ctx)
        gate_calls += 1
        return gate_calls == 1

    sessions = _CoreSessionManager()
    bus = _CoreBus()
    deps = _core_deps(sessions, bus)
    deps.owner_gate = gate
    ctx = _plain_reply_context("stale reply")
    ctx.result = CapabilityResult(route=RouteType.CANNED, reply_text="stale reply")
    bind_result_producer_owner(ctx, "test")

    result = await CommitTurnsAndPublishStep(deps).run(ctx)

    assert gate_calls == 2
    assert result.action == "suppress_outbound"
    assert sessions.appended == []
    assert sessions.states == []
    assert bus.messages == []


@pytest.mark.asyncio
async def test_flow_runner_commits_step_effects_when_committer_is_configured() -> None:
    effect = MessageEffect(type="publish_outbound", owner="test")
    committer = InMemoryEffectCommitter()
    step = _Step(StepResult(effects=[effect]))
    ctx = _ctx()

    result = await FlowRunner(
        {"plugin.test.one": step},
        effect_committer=committer,
        effect_dry_run=True,
    ).run(
        _flow(_compiled_step("one", "plugin.test.one")),
        ctx,
    )

    assert result.status == FLOW_RUN_COMPLETED
    assert ctx.effects == [
        MessageEffect(type="publish_outbound", owner="test", producer_owner="test")
    ]
    assert len(committer.records) == 1
    assert committer.records[0].status == EFFECT_STATUS_DRY_RUN
    assert committer.records[0].producer_owner == "test"
    expected_key = f"effect:demo:s1:{ctx.event.trace_id}:test:publish_outbound:0"
    assert committer.records[0].idempotency_key == expected_key
    assert ctx.signals["effects"]["commits"] == [
        {
            "type": "publish_outbound",
            "owner": "test",
            "producer_owner": "test",
            "idempotency_key": expected_key,
            "status": EFFECT_STATUS_DRY_RUN,
            "error": "",
            "dry_run": True,
        }
    ]
    assert result.effect_commits == ctx.signals["effects"]["commits"]
    assert result.effect_dispatches == []


@pytest.mark.asyncio
async def test_flow_runner_does_not_dispatch_effect_handlers_without_opt_in() -> None:
    effect = MessageEffect(type="publish_outbound", owner="test")
    committer = InMemoryEffectCommitter()
    handler = _RecordingEffectHandler()
    registry = EffectHandlerRegistry()
    registry.register("publish_outbound", "test", handler)
    dispatcher = EffectDispatcher(registry, InMemoryEffectCommitter())
    step = _Step(StepResult(effects=[effect]))
    ctx = _ctx()

    result = await FlowRunner(
        {"plugin.test.one": step},
        effect_committer=committer,
        effect_dispatcher=dispatcher,
    ).run(
        _flow(_compiled_step("one", "plugin.test.one")),
        ctx,
    )

    assert result.status == FLOW_RUN_COMPLETED
    assert len(committer.records) == 1
    assert handler.calls == []
    assert "dispatches" not in ctx.signals["effects"]


@pytest.mark.asyncio
async def test_flow_runner_dispatches_effect_handlers_when_enabled() -> None:
    effect = MessageEffect(
        type="publish_outbound",
        owner="test",
        payload={"text": "ok"},
    )
    handler = _RecordingEffectHandler()
    step = _Step(StepResult(effects=[effect]))
    ctx = _ctx()

    result = await FlowRunner(
        {"plugin.test.one": step},
        effect_dispatcher=_effect_dispatcher(handler),
        effect_handlers_enabled=True,
    ).run(
        _flow(_compiled_step("one", "plugin.test.one")),
        ctx,
    )

    assert result.status == FLOW_RUN_COMPLETED
    assert len(handler.calls) == 1
    assert handler.calls[0].payload == {"text": "ok"}
    expected_key = f"effect:demo:s1:{ctx.event.trace_id}:test:publish_outbound:0"
    assert ctx.signals["effects"]["commits"] == [
        {
            "type": "publish_outbound",
            "owner": "test",
            "producer_owner": "test",
            "idempotency_key": expected_key,
            "status": EFFECT_STATUS_RECORDED,
            "error": "",
            "dry_run": False,
        }
    ]
    assert ctx.signals["effects"]["dispatches"] == [
        {
            "type": "publish_outbound",
            "owner": "test",
            "producer_owner": "test",
            "idempotency_key": expected_key,
            "status": EFFECT_STATUS_RECORDED,
            "commit_status": EFFECT_STATUS_RECORDED,
            "error": "",
            "dry_run": False,
        }
    ]
    assert result.effect_commits == ctx.signals["effects"]["commits"]
    assert result.effect_dispatches == ctx.signals["effects"]["dispatches"]


@pytest.mark.asyncio
async def test_effect_dispatcher_gates_both_owners_and_passes_producer_to_handler() -> None:
    gated: list[str] = []
    received: list[tuple[MessageEffect, EffectCommitRecord]] = []

    async def gate(owner: str, ctx: PipelineContext) -> bool:
        _ = ctx
        gated.append(owner)
        return True

    async def handler(
        effect: MessageEffect,
        ctx: PipelineContext,
        record: EffectCommitRecord,
    ) -> None:
        _ = ctx
        received.append((effect, record))

    registry = EffectHandlerRegistry()
    registry.register("publish_outbound", "wxbot", handler)
    dispatcher = EffectDispatcher(
        registry,
        InMemoryEffectCommitter(),
        owner_gate=gate,
    )

    dispatch = await dispatcher.dispatch(
        MessageEffect(
            type="publish_outbound",
            owner="wxbot",
            producer_owner="draw",
            idempotency_key="draw:publish:1",
        ),
        _ctx(),
    )

    assert dispatch.status == EFFECT_STATUS_RECORDED
    assert gated == ["draw", "wxbot"]
    assert len(received) == 1
    assert received[0][0].producer_owner == "draw"
    assert received[0][1].producer_owner == "draw"


@pytest.mark.asyncio
async def test_flow_runner_effect_dispatch_dry_run_skips_handler() -> None:
    effect = MessageEffect(type="publish_outbound", owner="test")
    handler = _RecordingEffectHandler()
    step = _Step(StepResult(effects=[effect]))
    ctx = _ctx()

    result = await FlowRunner(
        {"plugin.test.one": step},
        effect_dispatcher=_effect_dispatcher(handler),
        effect_handlers_enabled=True,
        effect_dry_run=True,
    ).run(
        _flow(_compiled_step("one", "plugin.test.one")),
        ctx,
    )

    assert result.status == FLOW_RUN_COMPLETED
    assert handler.calls == []
    assert ctx.signals["effects"]["commits"][0]["status"] == EFFECT_STATUS_DRY_RUN
    assert ctx.signals["effects"]["dispatches"][0]["status"] == EFFECT_STATUS_DRY_RUN
    assert ctx.signals["effects"]["dispatches"][0]["dry_run"] is True


@pytest.mark.asyncio
async def test_flow_runner_rejects_delegated_producer_from_plugin_step() -> None:
    effect = MessageEffect(
        type="publish_outbound",
        owner="test",
        producer_owner="amap",
    )
    ctx = _ctx()

    result = await FlowRunner(
        {"plugin.test.one": _Step(StepResult(effects=[effect]))},
        effect_committer=InMemoryEffectCommitter(),
    ).run(
        _flow(_compiled_step("one", "plugin.test.one")),
        ctx,
    )

    assert result.status == FLOW_RUN_COMPLETED
    assert ctx.effects[0].producer_owner == "test"


@pytest.mark.asyncio
async def test_flow_runner_selective_effect_dispatch_skips_disabled_handler() -> None:
    effect = MessageEffect(
        type="publish_outbound",
        owner="test",
        payload={"text": "ok"},
    )
    handler = _RecordingEffectHandler()
    step = _Step(StepResult(effects=[effect]))
    ctx = _ctx()

    result = await FlowRunner(
        {"plugin.test.one": step},
        effect_dispatcher=_selective_effect_dispatcher(
            handler,
            enabled_handlers=["memory:save_memory"],
        ),
        effect_handlers_enabled=True,
    ).run(
        _flow(_compiled_step("one", "plugin.test.one")),
        ctx,
    )

    assert result.status == FLOW_RUN_COMPLETED
    assert handler.calls == []
    assert ctx.signals["effects"]["dispatches"][0]["status"] == (EFFECT_HANDLER_STATUS_DISABLED)
    assert ctx.signals["effects"]["dispatches"][0]["commit_status"] == (EFFECT_STATUS_RECORDED)


@pytest.mark.asyncio
async def test_flow_runner_selective_effect_dispatch_accepts_matching_handler() -> None:
    effect = MessageEffect(
        type="publish_outbound",
        owner="test",
        payload={"text": "ok"},
    )
    handler = _RecordingEffectHandler()
    step = _Step(StepResult(effects=[effect]))
    ctx = _ctx()

    result = await FlowRunner(
        {"plugin.test.one": step},
        effect_dispatcher=_selective_effect_dispatcher(
            handler,
            enabled_handlers="test:publish_outbound",
        ),
        effect_handlers_enabled=True,
    ).run(
        _flow(_compiled_step("one", "plugin.test.one")),
        ctx,
    )

    assert result.status == FLOW_RUN_COMPLETED
    assert len(handler.calls) == 1
    assert ctx.signals["effects"]["dispatches"][0]["status"] == EFFECT_STATUS_RECORDED


@pytest.mark.asyncio
async def test_flow_runner_effect_dispatch_shadow_skips_handler() -> None:
    effect = MessageEffect(type="publish_outbound", owner="test")
    handler = _RecordingEffectHandler()
    step = _Step(StepResult(effects=[effect]))
    ctx = _ctx()

    result = await FlowRunner(
        {"plugin.test.one": step},
        shadow=True,
        effect_dispatcher=_effect_dispatcher(handler),
        effect_handlers_enabled=True,
    ).run(
        _flow(_compiled_step("one", "plugin.test.one")),
        ctx,
    )

    assert result.status == FLOW_RUN_COMPLETED
    assert handler.calls == []
    assert ctx.signals["effects"]["commits"][0]["status"] == EFFECT_STATUS_DRY_RUN
    assert ctx.signals["effects"]["dispatches"][0]["status"] == EFFECT_STATUS_DRY_RUN


@pytest.mark.asyncio
async def test_flow_runner_effect_handler_error_fails_closed_for_retry() -> None:
    effect = MessageEffect(type="publish_outbound", owner="test")
    first = _Step(StepResult(effects=[effect]))
    second = _Step()
    handler = _BoomEffectHandler()
    ctx = _ctx()

    result = await FlowRunner(
        {"plugin.test.one": first, "plugin.test.two": second},
        effect_dispatcher=_effect_dispatcher(handler),
        effect_handlers_enabled=True,
    ).run(
        _flow(
            _compiled_step("one", "plugin.test.one"),
            _compiled_step("two", "plugin.test.two"),
        ),
        ctx,
    )

    assert result.status == FLOW_RUN_FAILED
    assert [step.id for step in result.steps] == [
        "one",
        "one:effect_dispatch",
    ]
    assert result.steps[1].kind == "core.effect_dispatch"
    assert result.steps[1].status == STEP_TRACE_ERROR
    assert result.steps[1].reason == "effect_handler_failed"
    assert "handler boom" in result.steps[1].error
    assert second.calls == 0
    assert ctx.signals["effects"]["dispatches"][0]["status"] == (
        EFFECT_HANDLER_STATUS_HANDLER_ERROR
    )
    assert ctx.signals["effects"]["dispatches"][0]["commit_status"] == (EFFECT_STATUS_RECORDED)


@pytest.mark.asyncio
async def test_flow_runner_reports_missing_effect_handler_without_dispatching() -> None:
    effect = MessageEffect(
        type="publish_outbound",
        owner="test",
        payload={"text": "ok"},
    )
    registry = EffectHandlerRegistry()
    dispatcher = EffectDispatcher(registry, InMemoryEffectCommitter())
    step = _Step(StepResult(effects=[effect]))
    ctx = _ctx()

    result = await FlowRunner(
        {"plugin.test.one": step},
        effect_dispatcher=dispatcher,
        effect_handlers_enabled=True,
    ).run(
        _flow(_compiled_step("one", "plugin.test.one")),
        ctx,
    )

    assert result.status == FLOW_RUN_COMPLETED
    assert "one:effect_dispatch" not in [trace.id for trace in result.steps]
    assert ctx.signals["effects"]["dispatches"][0]["status"] == "no_handler"
    assert ctx.signals["effects"]["dispatches"][0]["commit_status"] == (EFFECT_STATUS_RECORDED)


@pytest.mark.asyncio
async def test_flow_runner_effect_committer_dedupes_idempotency_key() -> None:
    effect = MessageEffect(
        type="reserve_credits",
        owner="credits",
        idempotency_key="credits:reservation:1",
    )
    committer = InMemoryEffectCommitter()
    step = _Step(StepResult(effects=[effect]))
    ctx = _ctx()

    result = await FlowRunner(
        {"plugin.test.one": step, "plugin.test.two": step},
        effect_committer=committer,
    ).run(
        _flow(
            _compiled_step("one", "plugin.test.one"),
            _compiled_step("two", "plugin.test.two"),
        ),
        ctx,
    )

    assert result.status == FLOW_RUN_COMPLETED
    assert len(committer.records) == 1
    commits = ctx.signals["effects"]["commits"]
    assert [item["status"] for item in commits] == ["recorded", EFFECT_STATUS_DUPLICATE]


@pytest.mark.asyncio
async def test_in_memory_committer_scopes_explicit_command_id_by_tenant() -> None:
    committer = InMemoryEffectCommitter()
    effect = MessageEffect(
        type="enqueue_channel_reply",
        owner="wxbot",
        idempotency_key="shared-command-id",
        payload={"command_id": "shared-command-id"},
    )

    tenant_a = await committer.commit(effect, _ctx(tenant_id="tenant-a"))
    tenant_b = await committer.commit(effect, _ctx(tenant_id="tenant-b"))
    duplicate_a = await committer.commit(effect, _ctx(tenant_id="tenant-a"))

    assert tenant_a.status == EFFECT_STATUS_RECORDED
    assert tenant_b.status == EFFECT_STATUS_RECORDED
    assert duplicate_a.status == EFFECT_STATUS_DUPLICATE
    assert [record.tenant_id for record in committer.records] == ["tenant-a", "tenant-b"]


@pytest.mark.asyncio
async def test_in_memory_committer_rejects_same_key_for_different_effect() -> None:
    committer = InMemoryEffectCommitter()
    ctx = _ctx(tenant_id="tenant-a")
    first = MessageEffect(
        type="enqueue_channel_reply",
        owner="wxbot",
        idempotency_key="shared-command-id",
        payload={"body": {"text": "first"}},
    )
    conflicting = MessageEffect(
        type="enqueue_channel_reply",
        owner="wxbot",
        idempotency_key="shared-command-id",
        payload={"body": {"text": "different"}},
    )

    await committer.commit(first, ctx)

    with pytest.raises(EffectIdempotencyConflictError, match="effect_idempotency_conflict"):
        await committer.commit(conflicting, ctx)


@pytest.mark.asyncio
async def test_in_memory_committer_rejects_same_key_for_different_producer() -> None:
    committer = InMemoryEffectCommitter()
    ctx = _ctx(tenant_id="tenant-a")
    first = MessageEffect(
        type="enqueue_channel_reply",
        owner="wxbot",
        producer_owner="draw",
        idempotency_key="shared-command-id",
        payload={"body": {"text": "same"}},
    )
    conflicting = MessageEffect(
        type="enqueue_channel_reply",
        owner="wxbot",
        producer_owner="memory",
        idempotency_key="shared-command-id",
        payload={"body": {"text": "same"}},
    )

    record = await committer.commit(first, ctx)

    assert record.producer_owner == "draw"
    with pytest.raises(EffectIdempotencyConflictError, match="effect_idempotency_conflict"):
        await committer.commit(conflicting, ctx)


@pytest.mark.asyncio
async def test_flow_runner_fails_closed_when_effect_commit_fails() -> None:
    effect = MessageEffect(type="publish_outbound", owner="test")
    first = _Step(StepResult(effects=[effect]))
    second = _Step()
    ctx = _ctx()

    result = await FlowRunner(
        {"plugin.test.one": first, "plugin.test.two": second},
        effect_committer=_FailingCommitter(),
    ).run(
        _flow(
            _compiled_step("one", "plugin.test.one"),
            _compiled_step("two", "plugin.test.two"),
        ),
        ctx,
    )

    assert result.status == FLOW_RUN_FAILED
    assert result.error == "effect_commit_failed:redis unavailable"
    assert [step.id for step in result.steps] == ["one", "one:effects"]
    assert result.steps[1].kind == "core.effect_commit"
    assert second.calls == 0


@pytest.mark.asyncio
async def test_flow_runner_audited_committer_fail_closed_blocks_flow() -> None:
    effect = MessageEffect(type="publish_outbound", owner="test")
    first = _Step(StepResult(effects=[effect]))
    second = _Step()
    committer = AuditedEffectCommitter(
        InMemoryEffectCommitter(),
        _EffectAuditLog(boom=True),
        fail_closed=True,
    )
    ctx = _ctx()

    result = await FlowRunner(
        {"plugin.test.one": first, "plugin.test.two": second},
        effect_committer=committer,
    ).run(
        _flow(
            _compiled_step("one", "plugin.test.one"),
            _compiled_step("two", "plugin.test.two"),
        ),
        ctx,
    )

    assert result.status == FLOW_RUN_FAILED
    assert result.error == "effect_commit_failed:effect_log_failed:postgres unavailable"
    assert second.calls == 0
    assert ctx.signals["effects"]["commits"] == []


@pytest.mark.asyncio
async def test_flow_runner_audited_dispatch_fail_open_keeps_commit_error_visible() -> None:
    handler = _RecordingEffectHandler()
    registry = EffectHandlerRegistry()
    registry.register("publish_outbound", "test", handler)
    dispatcher = EffectDispatcher(
        registry,
        AuditedEffectCommitter(
            InMemoryEffectCommitter(),
            _EffectAuditLog(boom=True),
            fail_closed=False,
        ),
    )
    effect = MessageEffect(type="publish_outbound", owner="test")
    step = _Step(StepResult(effects=[effect]))
    ctx = _ctx()

    result = await FlowRunner(
        {"plugin.test.one": step},
        effect_dispatcher=dispatcher,
        effect_handlers_enabled=True,
    ).run(
        _flow(_compiled_step("one", "plugin.test.one")),
        ctx,
    )

    assert result.status == FLOW_RUN_COMPLETED
    assert len(handler.calls) == 1
    assert ctx.signals["effects"]["commits"][0]["status"] == EFFECT_STATUS_RECORDED
    assert ctx.signals["effects"]["commits"][0]["error"] == (
        "effect_log_failed:postgres unavailable"
    )
    assert ctx.signals["effects"]["dispatches"][0]["status"] == EFFECT_STATUS_RECORDED


@pytest.mark.asyncio
async def test_flow_runner_audited_dispatch_skips_handler_on_audit_duplicate() -> None:
    audit_log = _EffectAuditLog()
    handler = _RecordingEffectHandler()
    effect = MessageEffect(
        type="publish_outbound",
        owner="test",
        idempotency_key="send:audit-duplicate",
    )

    first_registry = EffectHandlerRegistry()
    first_registry.register("publish_outbound", "test", handler)
    first_dispatcher = EffectDispatcher(
        first_registry,
        AuditedEffectCommitter(InMemoryEffectCommitter(), audit_log),
    )
    first_ctx = _ctx()
    first_result = await FlowRunner(
        {"plugin.test.one": _Step(StepResult(effects=[effect]))},
        effect_dispatcher=first_dispatcher,
        effect_handlers_enabled=True,
    ).run(
        _flow(_compiled_step("one", "plugin.test.one")),
        first_ctx,
    )

    second_registry = EffectHandlerRegistry()
    second_registry.register("publish_outbound", "test", handler)
    second_dispatcher = EffectDispatcher(
        second_registry,
        AuditedEffectCommitter(InMemoryEffectCommitter(), audit_log),
    )
    second_ctx = _ctx()
    second_result = await FlowRunner(
        {"plugin.test.one": _Step(StepResult(effects=[effect]))},
        effect_dispatcher=second_dispatcher,
        effect_handlers_enabled=True,
    ).run(
        _flow(_compiled_step("one", "plugin.test.one")),
        second_ctx,
    )

    assert first_result.status == FLOW_RUN_COMPLETED
    assert second_result.status == FLOW_RUN_COMPLETED
    assert len(audit_log.records) == 1
    assert len(handler.calls) == 1
    assert second_ctx.signals["effects"]["commits"][0]["status"] == (EFFECT_STATUS_DUPLICATE)
    assert second_ctx.signals["effects"]["dispatches"][0]["status"] == (EFFECT_STATUS_DUPLICATE)


@pytest.mark.asyncio
async def test_audited_effect_committer_writes_log_after_gate() -> None:
    gate = InMemoryEffectCommitter()
    audit_log = _EffectAuditLog()
    committer = AuditedEffectCommitter(gate, audit_log)
    ctx = _ctx()
    effect = MessageEffect(type="publish_outbound", owner="test", payload={"text": "ok"})

    first = await committer.commit(effect, ctx)
    second = await committer.commit(effect, ctx)

    assert first.status == EFFECT_STATUS_RECORDED
    assert second.status == EFFECT_STATUS_DUPLICATE
    assert len(gate.records) == 1
    assert len(audit_log.records) == 1
    assert audit_log.records[0]["idempotency_key"] == first.idempotency_key
    assert audit_log.records[0]["payload"] == {"text": "ok"}


@pytest.mark.asyncio
async def test_audited_effect_committer_can_fail_open_on_log_error() -> None:
    gate = InMemoryEffectCommitter()
    audit_log = _EffectAuditLog(boom=True)
    committer = AuditedEffectCommitter(gate, audit_log, fail_closed=False)
    ctx = _ctx()
    effect = MessageEffect(type="publish_outbound", owner="test")

    record = await committer.commit(effect, ctx)

    assert record.status == EFFECT_STATUS_RECORDED
    assert record.error == "effect_log_failed:postgres unavailable"
    assert len(gate.records) == 1


@pytest.mark.asyncio
async def test_redis_effect_committer_records_and_dedupes_real_commits() -> None:
    redis = _EffectRedis()
    committer = RedisEffectCommitter(redis, key_prefix="test:flow:effect", ttl_seconds=60)
    ctx = _ctx()
    effect = MessageEffect(
        type="publish_outbound",
        owner="core",
        payload={"reply": "ok"},
        idempotency_key="reply:one",
    )

    first = await committer.commit(effect, ctx, sequence=3)
    second = await committer.commit(effect, ctx, sequence=3)

    assert first.status == EFFECT_STATUS_RECORDED
    assert first.payload == {"reply": "ok"}
    assert first.producer_owner == "core"
    assert second.status == EFFECT_STATUS_DUPLICATE
    assert second.payload == {"reply": "ok"}
    assert second.producer_owner == "core"
    assert "test:flow:effect:commit:demo:reply%3Aone" in redis.values
    assert len(redis.xadds) == 1
    assert redis.xadds[0][0] == "cs:flow:effects"
    assert json.loads(redis.xadds[0][1]["record"])["producer_owner"] == "core"


@pytest.mark.asyncio
async def test_redis_effect_committer_keeps_dry_run_separate_from_real_commit() -> None:
    redis = _EffectRedis()
    committer = RedisEffectCommitter(redis, key_prefix="test:flow:effect", log_stream=None)
    ctx = _ctx()
    effect = MessageEffect(
        type="reserve_credits",
        owner="credits",
        idempotency_key="credits:reservation:1",
    )

    dry_run = await committer.commit(effect, ctx, dry_run=True)
    real = await committer.commit(effect, ctx, dry_run=False)

    assert dry_run.status == EFFECT_STATUS_DRY_RUN
    assert real.status == EFFECT_STATUS_RECORDED
    assert "test:flow:effect:dryrun:demo:credits%3Areservation%3A1" in redis.values
    assert "test:flow:effect:commit:demo:credits%3Areservation%3A1" in redis.values


@pytest.mark.asyncio
async def test_redis_committer_scopes_explicit_command_id_by_tenant() -> None:
    redis = _EffectRedis()
    committer = RedisEffectCommitter(redis, key_prefix="test:flow:effect", log_stream=None)
    effect = MessageEffect(
        type="enqueue_channel_reply",
        owner="wxbot",
        idempotency_key="shared-command-id",
        payload={"command_id": "shared-command-id"},
    )

    tenant_a = await committer.commit(effect, _ctx(tenant_id="tenant-a"))
    tenant_b = await committer.commit(effect, _ctx(tenant_id="tenant-b"))
    duplicate_a = await committer.commit(effect, _ctx(tenant_id="tenant-a"))

    assert tenant_a.status == EFFECT_STATUS_RECORDED
    assert tenant_b.status == EFFECT_STATUS_RECORDED
    assert duplicate_a.status == EFFECT_STATUS_DUPLICATE
    assert set(redis.values) == {
        "test:flow:effect:commit:tenant-a:shared-command-id",
        "test:flow:effect:commit:tenant-b:shared-command-id",
    }


@pytest.mark.asyncio
async def test_redis_committer_rejects_same_key_for_different_effect() -> None:
    redis = _EffectRedis()
    committer = RedisEffectCommitter(redis, key_prefix="test:flow:effect", log_stream=None)
    ctx = _ctx(tenant_id="tenant-a")
    first = MessageEffect(
        type="enqueue_channel_reply",
        owner="wxbot",
        idempotency_key="shared-command-id",
        payload={"body": {"text": "first"}},
    )
    conflicting = MessageEffect(
        type="enqueue_channel_reply",
        owner="wxbot",
        idempotency_key="shared-command-id",
        payload={"body": {"text": "different"}},
    )

    await committer.commit(first, ctx)

    with pytest.raises(EffectIdempotencyConflictError, match="effect_idempotency_conflict"):
        await committer.commit(conflicting, ctx)


@pytest.mark.asyncio
async def test_redis_committer_rejects_same_key_for_different_producer() -> None:
    redis = _EffectRedis()
    committer = RedisEffectCommitter(redis, key_prefix="test:flow:effect", log_stream=None)
    ctx = _ctx(tenant_id="tenant-a")
    first = MessageEffect(
        type="enqueue_channel_reply",
        owner="wxbot",
        producer_owner="draw",
        idempotency_key="shared-command-id",
        payload={"body": {"text": "same"}},
    )
    conflicting = MessageEffect(
        type="enqueue_channel_reply",
        owner="wxbot",
        producer_owner="memory",
        idempotency_key="shared-command-id",
        payload={"body": {"text": "same"}},
    )

    await committer.commit(first, ctx)

    with pytest.raises(EffectIdempotencyConflictError, match="effect_idempotency_conflict"):
        await committer.commit(conflicting, ctx)


@pytest.mark.asyncio
async def test_flow_runner_failed_handler_is_retryable_then_completed_duplicate() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await bootstrap_effect_log_schema(factory)
    effect_log = PostgresEffectLog(factory)
    await effect_log.ensure_schema()
    effect = MessageEffect(
        type="publish_outbound",
        owner="test",
        payload={"text": "ok"},
        idempotency_key="effect:runner:retry",
    )
    try:
        failing_handler = _BoomEffectHandler()
        first_registry = EffectHandlerRegistry()
        first_registry.register("publish_outbound", "test", failing_handler)
        first_committer = AuditedEffectCommitter(
            InMemoryEffectCommitter(),
            effect_log,
            claim_owner="worker-a",
        )
        first = await FlowRunner(
            {"plugin.test.one": _Step(StepResult(effects=[effect]))},
            effect_dispatcher=EffectDispatcher(first_registry, first_committer),
            effect_handlers_enabled=True,
        ).run(_flow(_compiled_step("one", "plugin.test.one")), _ctx())

        retry_handler = _RecordingEffectHandler()
        retry_registry = EffectHandlerRegistry()
        retry_registry.register("publish_outbound", "test", retry_handler)
        retry_committer = AuditedEffectCommitter(
            InMemoryEffectCommitter(),
            effect_log,
            claim_owner="worker-b",
        )
        second = await FlowRunner(
            {"plugin.test.one": _Step(StepResult(effects=[effect]))},
            effect_dispatcher=EffectDispatcher(retry_registry, retry_committer),
            effect_handlers_enabled=True,
        ).run(_flow(_compiled_step("one", "plugin.test.one")), _ctx())

        duplicate_committer = AuditedEffectCommitter(
            InMemoryEffectCommitter(),
            effect_log,
            claim_owner="worker-c",
        )
        third = await FlowRunner(
            {"plugin.test.one": _Step(StepResult(effects=[effect]))},
            effect_dispatcher=EffectDispatcher(retry_registry, duplicate_committer),
            effect_handlers_enabled=True,
        ).run(_flow(_compiled_step("one", "plugin.test.one")), _ctx())
    finally:
        await engine.dispose()

    assert first.effect_commits[0]["status"] == EFFECT_STATUS_FAILED
    assert first.status == FLOW_RUN_FAILED
    assert second.effect_commits[0]["status"] == EFFECT_STATUS_COMPLETED
    assert second.status == FLOW_RUN_COMPLETED
    assert second.effect_commits[0]["attempt"] == 2
    assert third.effect_commits[0]["status"] == EFFECT_STATUS_DUPLICATE
    assert failing_handler.calls == 1
    assert len(retry_handler.calls) == 1


@pytest.mark.asyncio
async def test_flow_runner_recovers_expired_claim_after_worker_crash() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    current_time = [datetime(2030, 1, 1, tzinfo=UTC)]
    effect_log = PostgresEffectLog(
        factory,
        clock=lambda: current_time[0],
    )
    await bootstrap_effect_log_schema(factory)
    await effect_log.ensure_schema()
    effect = MessageEffect(
        type="publish_outbound",
        owner="test",
        idempotency_key="effect:runner:crash",
    )

    async def crash_handler(effect, ctx, record) -> None:
        _ = effect, ctx, record
        raise asyncio.CancelledError

    crash_registry = EffectHandlerRegistry()
    crash_registry.register("publish_outbound", "test", crash_handler)
    crash_committer = AuditedEffectCommitter(
        InMemoryEffectCommitter(),
        effect_log,
        claim_owner="crashed-worker",
        lease_seconds=10,
    )
    try:
        with pytest.raises(asyncio.CancelledError):
            await FlowRunner(
                {"plugin.test.one": _Step(StepResult(effects=[effect]))},
                effect_dispatcher=EffectDispatcher(crash_registry, crash_committer),
                effect_handlers_enabled=True,
            ).run(_flow(_compiled_step("one", "plugin.test.one")), _ctx())

        rows_after_crash = await effect_log.list_recent()
        current_time[0] += timedelta(seconds=11)

        retry_handler = _RecordingEffectHandler()
        retry_registry = EffectHandlerRegistry()
        retry_registry.register("publish_outbound", "test", retry_handler)
        retry_committer = AuditedEffectCommitter(
            InMemoryEffectCommitter(),
            effect_log,
            claim_owner="recovery-worker",
            lease_seconds=10,
        )
        recovered = await FlowRunner(
            {"plugin.test.one": _Step(StepResult(effects=[effect]))},
            effect_dispatcher=EffectDispatcher(retry_registry, retry_committer),
            effect_handlers_enabled=True,
        ).run(_flow(_compiled_step("one", "plugin.test.one")), _ctx())
    finally:
        await engine.dispose()

    assert rows_after_crash[0]["lifecycle_status"] == "running"
    assert recovered.status == FLOW_RUN_COMPLETED
    assert recovered.effect_commits[0]["status"] == EFFECT_STATUS_COMPLETED
    assert recovered.effect_commits[0]["attempt"] == 2
    assert len(retry_handler.calls) == 1


@pytest.mark.asyncio
async def test_concurrent_flow_runners_execute_claimed_effect_only_once() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await bootstrap_effect_log_schema(factory)
    effect_log = PostgresEffectLog(factory)
    await effect_log.ensure_schema()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def blocking_handler(effect, ctx, record) -> None:
        nonlocal calls
        _ = effect, ctx, record
        calls += 1
        started.set()
        await release.wait()

    effect = MessageEffect(
        type="publish_outbound",
        owner="test",
        idempotency_key="effect:runner:concurrent",
    )

    def build_runner(worker: str) -> FlowRunner:
        registry = EffectHandlerRegistry()
        registry.register("publish_outbound", "test", blocking_handler)
        committer = AuditedEffectCommitter(
            InMemoryEffectCommitter(),
            effect_log,
            claim_owner=worker,
            lease_seconds=30,
        )
        return FlowRunner(
            {"plugin.test.one": _Step(StepResult(effects=[effect]))},
            effect_dispatcher=EffectDispatcher(registry, committer),
            effect_handlers_enabled=True,
        )

    try:
        first_task = asyncio.create_task(
            build_runner("worker-a").run(
                _flow(_compiled_step("one", "plugin.test.one")),
                _ctx(),
            )
        )
        await started.wait()
        second = await build_runner("worker-b").run(
            _flow(_compiled_step("one", "plugin.test.one")),
            _ctx(),
        )
        release.set()
        first = await first_task
    finally:
        release.set()
        await engine.dispose()

    assert first.status == FLOW_RUN_COMPLETED
    assert second.status == FLOW_RUN_FAILED
    assert "effect_claim_unavailable" in second.error
    assert calls == 1


@pytest.mark.asyncio
async def test_concurrent_identical_command_ids_execute_once_per_tenant() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await bootstrap_effect_log_schema(factory)
    effect_log = PostgresEffectLog(factory)
    await effect_log.ensure_schema()
    both_started = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def blocking_handler(effect, ctx, record) -> None:
        _ = effect, record
        calls.append(ctx.event.tenant_id)
        if len(calls) == 2:
            both_started.set()
        await release.wait()

    effect = MessageEffect(
        type="enqueue_channel_reply",
        owner="wxbot",
        idempotency_key="shared-command-id",
        payload={"command_id": "shared-command-id"},
    )
    registry = EffectHandlerRegistry()
    registry.register("enqueue_channel_reply", "wxbot", blocking_handler)
    committer = AuditedEffectCommitter(
        InMemoryEffectCommitter(),
        effect_log,
        claim_owner="shared-worker",
        lease_seconds=30,
    )

    def runner() -> FlowRunner:
        return FlowRunner(
            {"plugin.test.one": _Step(StepResult(effects=[effect]))},
            effect_dispatcher=EffectDispatcher(registry, committer),
            effect_handlers_enabled=True,
        )

    tasks: list[asyncio.Task] = []
    try:
        tasks = [
            asyncio.create_task(
                runner().run(
                    _flow(_compiled_step("one", "plugin.test.one")),
                    _ctx(tenant_id=tenant_id),
                )
            )
            for tenant_id in ("tenant-a", "tenant-b")
        ]
        try:
            await asyncio.wait_for(both_started.wait(), timeout=2)
        finally:
            release.set()
        results = await asyncio.gather(*tasks)
        rows = await effect_log.list_recent()
    finally:
        release.set()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()

    assert [result.status for result in results] == [
        FLOW_RUN_COMPLETED,
        FLOW_RUN_COMPLETED,
    ]
    assert set(calls) == {"tenant-a", "tenant-b"}
    assert {row["tenant_id"] for row in rows} == {"tenant-a", "tenant-b"}
    assert {row["lifecycle_status"] for row in rows} == {EFFECT_STATUS_COMPLETED}


@pytest.mark.asyncio
async def test_core_append_user_turn_effect_is_marked_audit_after_side_effect() -> None:
    sessions = _CoreSessionManager()
    bus = _CoreBus()
    ctx = _ctx()
    ctx.session = Session(
        tenant_id=ctx.event.tenant_id,
        user_id=ctx.event.user_id,
        session_id=ctx.event.session_id,
        channel=ctx.event.channel,
    )
    ctx.pre = PreprocessedMessage(original_text=" hello ", cleaned_text="hello")

    result = await AppendUserTurnStep(_core_deps(sessions, bus)).run(ctx)

    assert [turn.role for turn in sessions.appended] == [Role.USER]
    assert result.effects[0].type == "append_user_turn"
    assert result.effects[0].payload["commit_semantics"] == (
        EFFECT_COMMIT_SEMANTICS_AUDIT_AFTER_SIDE_EFFECT
    )
    assert result.effects[0].payload["side_effects_executed_before_commit"] is True


@pytest.mark.asyncio
async def test_core_append_user_turn_can_defer_to_effect_handler() -> None:
    sessions = _CoreSessionManager()
    bus = _CoreBus()
    ctx = _ctx()
    ctx.session = Session(
        tenant_id=ctx.event.tenant_id,
        user_id=ctx.event.user_id,
        session_id=ctx.event.session_id,
        channel=ctx.event.channel,
    )
    ctx.pre = PreprocessedMessage(original_text=" hello ", cleaned_text="hello")

    result = await AppendUserTurnStep(_core_deps(sessions, bus, effect_handlers_enabled=True)).run(
        ctx
    )

    assert sessions.appended == []
    assert result.effects[0].type == "append_user_turn"
    assert result.effects[0].payload["commit_semantics"] == (
        EFFECT_COMMIT_SEMANTICS_GATE_BEFORE_SIDE_EFFECT
    )
    assert result.effects[0].payload["side_effects_executed_before_commit"] is False
    assert result.effects[0].payload["turn"]["role"] == Role.USER.value
    assert result.effects[0].payload["turn"]["content"] == "hello"


@pytest.mark.asyncio
async def test_core_commit_publish_effect_is_marked_audit_after_side_effect() -> None:
    sessions = _CoreSessionManager()
    bus = _CoreBus()
    ctx = _ctx()
    ctx.session = Session(
        tenant_id=ctx.event.tenant_id,
        user_id=ctx.event.user_id,
        session_id=ctx.event.session_id,
        channel=ctx.event.channel,
    )
    ctx.result = CapabilityResult(route=RouteType.FAQ, reply_text="answer")
    ctx.reply = OutboundReply(
        tenant_id=ctx.event.tenant_id,
        channel=ctx.event.channel,
        user_id=ctx.event.user_id,
        session_id=ctx.event.session_id,
        type=ReplyType.TEXT,
        segments=[ReplySegment(type=ReplyType.TEXT, content="answer")],
        trace_id=ctx.event.trace_id,
    )

    result = await CommitTurnsAndPublishStep(_core_deps(sessions, bus)).run(ctx)

    assert [turn.role for turn in sessions.appended] == [Role.ASSISTANT]
    assert sessions.states == [SessionState.CHATTING]
    assert bus.messages[0][0] == "outbound"
    assert result.effects[0].type == "commit_turns_and_publish"
    assert result.effects[0].payload["commit_semantics"] == (
        EFFECT_COMMIT_SEMANTICS_AUDIT_AFTER_SIDE_EFFECT
    )
    assert result.effects[0].payload["side_effects_executed_before_commit"] is True


@pytest.mark.asyncio
async def test_core_commit_can_defer_outbound_publish_to_effect_handler() -> None:
    sessions = _CoreSessionManager()
    bus = _CoreBus()
    ctx = _ctx()
    ctx.session = Session(
        tenant_id=ctx.event.tenant_id,
        user_id=ctx.event.user_id,
        session_id=ctx.event.session_id,
        channel=ctx.event.channel,
    )
    ctx.result = CapabilityResult(route=RouteType.FAQ, reply_text="answer")
    ctx.reply = OutboundReply(
        tenant_id=ctx.event.tenant_id,
        channel=ctx.event.channel,
        user_id=ctx.event.user_id,
        session_id=ctx.event.session_id,
        type=ReplyType.TEXT,
        segments=[ReplySegment(type=ReplyType.TEXT, content="answer")],
        trace_id=ctx.event.trace_id,
    )

    result = await CommitTurnsAndPublishStep(
        _core_deps(sessions, bus, effect_handlers_enabled=True)
    ).run(ctx)

    assert sessions.appended == []
    assert sessions.states == []
    assert bus.messages == []
    assert [effect.type for effect in result.effects] == [
        "append_assistant_turn",
        "set_session_state",
        "publish_outbound",
        "commit_turns_and_publish",
    ]
    assert result.effects[0].owner == "core"
    assert result.effects[0].payload["commit_semantics"] == (
        EFFECT_COMMIT_SEMANTICS_GATE_BEFORE_SIDE_EFFECT
    )
    assert result.effects[0].payload["turn"]["role"] == Role.ASSISTANT.value
    assert result.effects[1].payload["state"] == SessionState.CHATTING.value
    assert result.effects[2].payload["commit_semantics"] == (
        EFFECT_COMMIT_SEMANTICS_GATE_BEFORE_SIDE_EFFECT
    )
    assert result.effects[2].payload["stream"] == "outbound"
    assert result.effects[2].payload["partition_key"] == "demo:s1"
    assert result.effects[2].payload["payload"]["segments"][0]["content"] == "answer"
    assert result.effects[3].payload["publish_outbound"] is True
    assert result.effects[3].payload["publish_outbound_as_effect"] is True
    assert result.effects[3].payload["publish_outbound_side_effect_executed_before_commit"] is False
    assert result.effects[3].payload["append_assistant_turn_as_effect"] is True
    assert result.effects[3].payload["state_transition_as_effect"] is True
    assert result.effects[3].payload["side_effects_executed_before_commit"] is False


@pytest.mark.asyncio
async def test_flow_runner_dispatches_core_session_and_publish_handlers() -> None:
    sessions = _CoreSessionManager()
    bus = _CoreBus()
    registry = EffectHandlerRegistry()
    register_core_session_effect_handlers(registry, sessions)
    register_core_publish_outbound_handler(registry, bus, default_stream="outbound")
    committer = InMemoryEffectCommitter()
    ctx = _ctx()
    ctx.session = Session(
        tenant_id=ctx.event.tenant_id,
        user_id=ctx.event.user_id,
        session_id=ctx.event.session_id,
        channel=ctx.event.channel,
    )
    ctx.pre = PreprocessedMessage(original_text=" hello ", cleaned_text="hello")
    ctx.result = CapabilityResult(route=RouteType.FAQ, reply_text="answer")
    ctx.reply = OutboundReply(
        tenant_id=ctx.event.tenant_id,
        channel=ctx.event.channel,
        user_id=ctx.event.user_id,
        session_id=ctx.event.session_id,
        type=ReplyType.TEXT,
        segments=[ReplySegment(type=ReplyType.TEXT, content="answer")],
        trace_id=ctx.event.trace_id,
    )
    flow = _flow(
        _compiled_step("append", "core.append_user_turn"),
        _compiled_step("commit", "core.commit_turns_and_publish"),
    )
    deps = _core_deps(sessions, bus, effect_handlers_enabled=True)
    runner = FlowRunner(
        {
            "core.append_user_turn": AppendUserTurnStep(deps),
            "core.commit_turns_and_publish": CommitTurnsAndPublishStep(deps),
        },
        effect_committer=committer,
        effect_dispatcher=EffectDispatcher(registry, committer),
        effect_handlers_enabled=True,
    )

    result = await runner.run(flow, ctx)

    assert result.status == FLOW_RUN_COMPLETED
    assert [turn.role for turn in sessions.appended] == [Role.USER, Role.ASSISTANT]
    assert ctx.session.turns == sessions.appended
    assert sessions.states == [SessionState.CHATTING]
    assert bus.messages[0][0] == "outbound"
    assert bus.messages[0][2] == "demo:s1"
    assert [record.type for record in committer.records] == [
        "append_user_turn",
        "append_assistant_turn",
        "set_session_state",
        "publish_outbound",
        "commit_turns_and_publish",
    ]
    assert [item["status"] for item in ctx.signals["effects"]["dispatches"]] == [
        EFFECT_STATUS_RECORDED,
        EFFECT_STATUS_RECORDED,
        EFFECT_STATUS_RECORDED,
        EFFECT_STATUS_RECORDED,
        EFFECT_HANDLER_STATUS_NO_HANDLER,
    ]


def test_normalize_effect_requires_type_and_owner() -> None:
    ctx = _ctx()

    with pytest.raises(ValueError, match="effect type"):
        normalize_effect(MessageEffect(type="", owner="test"), ctx)
    with pytest.raises(ValueError, match="effect owner"):
        normalize_effect(MessageEffect(type="publish_outbound", owner=""), ctx)


@pytest.mark.asyncio
async def test_flow_runner_stops_on_stop_action() -> None:
    first = _Step(StepResult(action="stop", reason="command_handled"))
    second = _Step()
    flow = _flow(
        _compiled_step("one", "plugin.test.one"),
        _compiled_step("two", "plugin.test.two"),
    )

    result = await FlowRunner({"plugin.test.one": first, "plugin.test.two": second}).run(
        flow, _ctx()
    )

    assert result.status == FLOW_RUN_STOPPED
    assert result.stop_reason == "command_handled"
    assert first.calls == 1
    assert second.calls == 0


@pytest.mark.asyncio
async def test_flow_runner_finalize_runs_only_finalize_steps() -> None:
    trigger = _Step(
        StepResult(
            action="stop",
            reason="command_handled",
            result=CapabilityResult(route=RouteType.CANNED, reply_text="handled"),
            finalize=True,
            skip_output_safety=True,
        )
    )
    skipped_business = _Step()
    output_safety = _Step()
    postprocess = _Step()
    outbound_policy = _Step()
    commit = _Step()
    flow = _flow(
        _compiled_step("command", "plugin.test.command"),
        _compiled_step("business", "plugin.test.business"),
        _compiled_step("output_safety", "core.output_safety"),
        _compiled_step("postprocess", "core.postprocess"),
        _compiled_step("channel_outbound", "plugin.channel.outbound_policy"),
        _compiled_step("commit", "core.commit_turns_and_publish"),
    )

    result = await FlowRunner(
        {
            "plugin.test.command": trigger,
            "plugin.test.business": skipped_business,
            "core.output_safety": output_safety,
            "core.postprocess": postprocess,
            "plugin.channel.outbound_policy": outbound_policy,
            "core.commit_turns_and_publish": commit,
        }
    ).run(flow, _ctx())

    assert result.status == FLOW_RUN_STOPPED
    assert result.stop_reason == "command_handled"
    assert [step.id for step in result.steps] == [
        "command",
        "postprocess",
        "channel_outbound",
        "commit",
    ]
    assert skipped_business.calls == 0
    assert output_safety.calls == 0
    assert postprocess.calls == 1
    assert outbound_policy.calls == 1
    assert commit.calls == 1


@pytest.mark.asyncio
async def test_flow_runner_fail_open_errors_continue() -> None:
    first = _Step(boom=True)
    second = _Step()
    flow = _flow(
        _compiled_step("one", "plugin.test.one", error_policy="fail_open"),
        _compiled_step("two", "plugin.test.two"),
    )

    result = await FlowRunner({"plugin.test.one": first, "plugin.test.two": second}).run(
        flow, _ctx()
    )

    assert result.status == FLOW_RUN_COMPLETED
    assert result.steps[0].status == STEP_TRACE_ERROR_OPEN
    assert result.steps[0].error == "step boom"
    assert second.calls == 1


@pytest.mark.asyncio
async def test_flow_runner_fail_open_timeout_continues() -> None:
    first = _Step(sleep_seconds=0.05)
    second = _Step()
    flow = _flow(
        _compiled_step(
            "one",
            "plugin.test.one",
            error_policy="fail_open",
            timeout_seconds=0.001,
        ),
        _compiled_step("two", "plugin.test.two"),
    )

    result = await FlowRunner({"plugin.test.one": first, "plugin.test.two": second}).run(
        flow, _ctx()
    )

    assert result.status == FLOW_RUN_COMPLETED
    assert result.steps[0].status == STEP_TRACE_TIMEOUT_OPEN
    assert result.steps[0].error == "step_timeout:0.001s"
    assert first.calls == 1
    assert second.calls == 1


@pytest.mark.asyncio
async def test_flow_runner_retry_policy_succeeds_after_transient_error() -> None:
    first = _Step(fail_times=1)
    flow = _flow(
        _compiled_step("one", "plugin.test.one", error_policy="retry"),
    )

    result = await FlowRunner(
        {"plugin.test.one": first},
        max_retry_attempts=2,
    ).run(flow, _ctx())

    assert result.status == FLOW_RUN_COMPLETED
    assert first.calls == 2
    assert result.steps[0].status == "ok"
    assert result.steps[0].attempts == 2
    assert result.steps[0].error == ""


@pytest.mark.asyncio
async def test_flow_runner_retry_policy_exhaustion_fails_closed() -> None:
    first = _Step(boom=True)
    second = _Step()
    flow = _flow(
        _compiled_step("one", "plugin.test.one", error_policy="retry"),
        _compiled_step("two", "plugin.test.two"),
    )

    result = await FlowRunner(
        {"plugin.test.one": first, "plugin.test.two": second},
        max_retry_attempts=2,
    ).run(flow, _ctx())

    assert result.status == FLOW_RUN_FAILED
    assert result.steps[0].status == "error"
    assert result.steps[0].reason == "retry_exhausted"
    assert result.steps[0].attempts == 2
    assert result.error == "step boom"
    assert first.calls == 2
    assert second.calls == 0


@pytest.mark.asyncio
async def test_flow_runner_retry_policy_does_not_retry_effectful_steps() -> None:
    first = _Step(boom=True)
    flow = _flow(
        _compiled_step(
            "one",
            "plugin.test.one",
            error_policy="retry",
            outputs={"effects.reserve_credits"},
        ),
    )

    result = await FlowRunner(
        {"plugin.test.one": first},
        max_retry_attempts=3,
    ).run(flow, _ctx())

    assert result.status == FLOW_RUN_FAILED
    assert first.calls == 1
    assert result.steps[0].status == "error"
    assert result.steps[0].reason == "retry_disabled_effectful_step"
    assert result.steps[0].attempts == 1


@pytest.mark.asyncio
async def test_flow_runner_fail_closed_timeout_fails() -> None:
    first = _Step(sleep_seconds=0.05)
    second = _Step()
    flow = _flow(
        _compiled_step("one", "plugin.test.one", timeout_seconds=0.001),
        _compiled_step("two", "plugin.test.two"),
    )

    result = await FlowRunner({"plugin.test.one": first, "plugin.test.two": second}).run(
        flow, _ctx()
    )

    assert result.status == FLOW_RUN_FAILED
    assert result.error == "step_timeout:0.001s"
    assert result.steps[0].status == STEP_TRACE_TIMEOUT
    assert first.calls == 1
    assert second.calls == 0


@pytest.mark.asyncio
async def test_flow_runner_degrade_timeout_finalizes() -> None:
    first = _Step(sleep_seconds=0.05)
    commit = _Step()
    flow = _flow(
        _compiled_step(
            "route",
            "core.route",
            error_policy="degrade",
            timeout_seconds=0.001,
        ),
        _compiled_step("commit", "core.commit_turns_and_publish"),
    )
    ctx = _ctx()

    result = await FlowRunner(
        {
            "core.route": first,
            "core.commit_turns_and_publish": commit,
        }
    ).run(flow, ctx)

    assert result.status == FLOW_RUN_STOPPED
    assert result.stop_reason == "route_degraded"
    assert result.steps[0].status == STEP_TRACE_DEGRADED
    assert result.steps[0].error == "step_timeout:0.001s"
    assert commit.calls == 1
    assert ctx.result is not None
    assert ctx.result.metadata["error"] == "step_timeout:0.001s"


@pytest.mark.asyncio
async def test_flow_runner_degrade_errors_finalize_with_canned_result() -> None:
    first = _Step(boom=True)
    skipped = _Step()
    output_safety = _Step()
    postprocess = _Step()
    commit = _Step()
    flow = _flow(
        _compiled_step("route", "core.route", error_policy="degrade"),
        _compiled_step("capability", "core.capability_dispatch"),
        _compiled_step("output_safety", "core.output_safety"),
        _compiled_step("postprocess", "core.postprocess"),
        _compiled_step("commit", "core.commit_turns_and_publish"),
    )
    ctx = _ctx()

    result = await FlowRunner(
        {
            "core.route": first,
            "core.capability_dispatch": skipped,
            "core.output_safety": output_safety,
            "core.postprocess": postprocess,
            "core.commit_turns_and_publish": commit,
        }
    ).run(flow, ctx)

    assert result.status == FLOW_RUN_STOPPED
    assert result.stop_reason == "route_degraded"
    assert result.steps[0].status == STEP_TRACE_DEGRADED
    assert result.steps[0].error == "step boom"
    assert [step.id for step in result.steps] == [
        "route",
        "output_safety",
        "postprocess",
        "commit",
    ]
    assert skipped.calls == 0
    assert output_safety.calls == 1
    assert postprocess.calls == 1
    assert commit.calls == 1
    assert ctx.result is not None
    assert ctx.result.route == RouteType.CANNED
    assert ctx.result.metadata["degradation_reason"] == "core.route_failed"
    assert ctx.result.metadata["failed_step_id"] == "route"


@pytest.mark.asyncio
async def test_flow_runner_fail_closed_errors_stop() -> None:
    first = _Step(boom=True)
    second = _Step()
    flow = _flow(
        _compiled_step("one", "plugin.test.one"),
        _compiled_step("two", "plugin.test.two"),
    )

    result = await FlowRunner({"plugin.test.one": first, "plugin.test.two": second}).run(
        flow, _ctx()
    )

    assert result.status == FLOW_RUN_FAILED
    assert result.error == "step boom"
    assert second.calls == 0
