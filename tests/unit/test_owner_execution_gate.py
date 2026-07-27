from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

import pytest

from app.channel import ChannelRegistry, ChannelSendResult
from app.common.types import Channel, InboundEvent, Message
from app.orchestrator.effect_handlers import (
    EFFECT_HANDLER_STATUS_OWNER_SKIPPED,
    ChannelReplyEffectHandler,
    EffectDispatcher,
    EffectHandlerRegistry,
)
from app.orchestrator.effects import (
    EFFECT_STATUS_COMPLETED,
    EFFECT_STATUS_RUNNING,
    EffectCommitRecord,
    InMemoryEffectCommitter,
)
from app.orchestrator.flow import (
    FLOW_STATUS_ACTIVE,
    CompiledFlow,
    CompiledStep,
    MessageEffect,
    StepResult,
)
from app.orchestrator.outcome import RetryableProcessingError
from app.orchestrator.owner_gate import (
    OwnerExecutionDecision,
    evaluate_owner_execution,
)
from app.orchestrator.pipeline import PipelineContext
from app.orchestrator.runner import (
    FLOW_RUN_COMPLETED,
    STEP_TRACE_OWNER_SKIPPED,
    FlowRunner,
)
from app.plugin.hooks import (
    HOOK_TRACE_SCRATCH_KEY,
    RESULT_PRODUCER_OWNER_KEY,
    HookAbort,
    HookExecutionError,
    HookPoint,
    HookRunner,
    trusted_result_producer_owner,
)


def _ctx() -> PipelineContext:
    event = InboundEvent(
        message_id="message-1",
        tenant_id="tenant-1",
        channel=Channel.WEB,
        user_id="user-1",
        session_id="session-1",
        message=Message(content="hello"),
    )
    return PipelineContext(event=event, trace_id=event.trace_id)


@dataclass
class _Hook:
    name: str
    calls: list[str]
    point: HookPoint = HookPoint.BEFORE_ROUTE
    priority: int = 10
    timeout_seconds: float = 0.1
    error_policy: str = "fail_closed"
    sleep_seconds: float = 0.0
    boom: bool = False

    async def run(self, ctx: PipelineContext) -> None:
        _ = ctx
        self.calls.append(self.name)
        if self.sleep_seconds:
            await asyncio.sleep(self.sleep_seconds)
        if self.boom:
            raise RuntimeError("sensitive plugin detail")


class _Step:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, ctx: PipelineContext) -> StepResult:
        _ = ctx
        self.calls += 1
        return StepResult()


class _EffectStep:
    async def run(self, ctx: PipelineContext) -> StepResult:
        _ = ctx
        return StepResult(
            effects=[
                MessageEffect(
                    type="run",
                    owner="plugin",
                    idempotency_key="plugin:run:lifecycle",
                )
            ]
        )


class _ChannelOutbound:
    def __init__(self) -> None:
        self.calls = 0

    async def get_session_policy(self, target):
        _ = target
        return {}

    async def send_text(self, target, text, options=None) -> ChannelSendResult:
        _ = target, text, options
        self.calls += 1
        return ChannelSendResult(message_id="sent-1", provider="test")

    async def send_image(self, target, media, options=None) -> ChannelSendResult:
        _ = target, media, options
        self.calls += 1
        return ChannelSendResult(message_id="sent-1", provider="test")


class _LifecycleCommitter:
    def __init__(self) -> None:
        self.completed = 0
        self.failed = 0

    async def commit(
        self,
        effect: MessageEffect,
        ctx: PipelineContext,
        *,
        sequence: int = 0,
        dry_run: bool = False,
    ) -> EffectCommitRecord:
        _ = sequence
        return EffectCommitRecord(
            type=effect.type,
            owner=effect.owner,
            idempotency_key=effect.idempotency_key,
            payload=dict(effect.payload),
            status=EFFECT_STATUS_RUNNING,
            tenant_id=ctx.event.tenant_id,
            dry_run=dry_run,
        )

    async def mark_completed(self, record: EffectCommitRecord) -> EffectCommitRecord:
        self.completed += 1
        return replace(record, status=EFFECT_STATUS_COMPLETED)

    async def mark_failed(
        self,
        record: EffectCommitRecord,
        *,
        error: str,
    ) -> EffectCommitRecord:
        _ = error
        self.failed += 1
        return record


def _flow(*, owner: str = "plugin") -> CompiledFlow:
    return CompiledFlow(
        name="owner-gated-flow",
        version=1,
        status=FLOW_STATUS_ACTIVE,
        steps=[
            CompiledStep(
                id="plugin-step",
                kind="plugin.test.step",
                owner=owner,
                name="Plugin step",
                permissions=[],
                inputs=set(),
                outputs=set(),
                timeout_seconds=1.0,
                error_policy="fail_closed",
            )
        ],
    )


@pytest.mark.asyncio
async def test_owner_gate_caches_decision_and_never_blocks_core() -> None:
    calls: list[str] = []

    async def gate(owner: str, ctx: PipelineContext) -> OwnerExecutionDecision:
        _ = ctx
        calls.append(owner)
        return OwnerExecutionDecision(False, "scope disabled by operator")

    ctx = _ctx()
    first = await evaluate_owner_execution(gate, "plugin", ctx)
    second = await evaluate_owner_execution(gate, "plugin", ctx)
    core = await evaluate_owner_execution(gate, "core", ctx)

    assert first == second == OwnerExecutionDecision(False, "scope_disabled_by_operator")
    assert core.allowed is True
    assert calls == ["plugin"]


@pytest.mark.asyncio
async def test_hook_runner_uses_deterministic_tie_break_and_owner_gate() -> None:
    calls: list[str] = []

    async def gate(owner: str, ctx: PipelineContext) -> bool:
        _ = ctx
        return owner != "blocked"

    runner = HookRunner(owner_gate=gate)
    runner.register(_Hook("z-last", calls), owner="z-owner")
    runner.register(_Hook("a-first", calls), owner="a-owner")
    runner.register(_Hook("blocked", calls), owner="blocked")
    ctx = _ctx()

    await runner.run(HookPoint.BEFORE_ROUTE, ctx)

    assert calls == ["a-first", "z-last"]
    trace = ctx.scratch[HOOK_TRACE_SCRATCH_KEY]
    assert [item["owner"] for item in trace] == ["a-owner", "blocked", "z-owner"]
    assert trace[1]["status"] == "owner_skipped"
    assert trace[1]["reason"] == "owner_execution_denied"


@pytest.mark.asyncio
async def test_hook_abort_provenance_is_rebound_to_registered_owner() -> None:
    class _SpoofingAbortHook:
        name = "spoofing.abort"
        point = HookPoint.BEFORE_ROUTE
        priority = 1

        async def run(self, ctx: PipelineContext) -> None:
            _ = ctx
            abort = HookAbort("plugin reply", reason="plugin_abort")
            abort.bind_result_producer_owner("core")
            raise abort

    runner = HookRunner()
    runner.register(_SpoofingAbortHook(), owner="plugin")
    ctx = _ctx()

    with pytest.raises(HookAbort) as raised:
        await runner.run(HookPoint.BEFORE_ROUTE, ctx)

    assert raised.value.result_producer_owner == "plugin"
    assert trusted_result_producer_owner(ctx) == "plugin"
    assert ctx.extras[RESULT_PRODUCER_OWNER_KEY] == "plugin"


@pytest.mark.asyncio
async def test_hook_abort_is_dropped_when_owner_is_disabled_during_hook() -> None:
    enabled = True
    gate_calls: list[str] = []

    async def gate(owner: str, ctx: PipelineContext) -> bool:
        _ = ctx
        gate_calls.append(owner)
        return enabled

    class _DisableThenAbortHook:
        name = "racing.abort"
        point = HookPoint.BEFORE_ROUTE
        priority = 1

        async def run(self, ctx: PipelineContext) -> None:
            nonlocal enabled
            _ = ctx
            enabled = False
            raise HookAbort("stale reply", reason="racing_abort")

    runner = HookRunner(owner_gate=gate)
    runner.register(_DisableThenAbortHook(), owner="plugin")
    ctx = _ctx()

    await runner.run(HookPoint.BEFORE_ROUTE, ctx)

    assert gate_calls == ["plugin", "plugin"]
    assert trusted_result_producer_owner(ctx) == ""
    assert ctx.scratch[HOOK_TRACE_SCRATCH_KEY][0]["status"] == "owner_skipped"


@pytest.mark.asyncio
async def test_hook_runner_propagates_transient_owner_gate_failure_for_retry() -> None:
    async def unavailable(owner: str, ctx: PipelineContext) -> bool:
        _ = owner, ctx
        raise RuntimeError("state store unavailable")

    runner = HookRunner(owner_gate=unavailable)
    runner.register(_Hook("never-runs", []), owner="plugin")
    ctx = _ctx()

    with pytest.raises(RetryableProcessingError) as raised:
        await runner.run(HookPoint.BEFORE_ROUTE, ctx)

    assert raised.value.reason == "owner_gate_error"
    assert raised.value.error_type == "PluginOwnerGateUnavailable"
    assert ctx.scratch[HOOK_TRACE_SCRATCH_KEY][0]["status"] == (
        "owner_gate_retryable"
    )


@pytest.mark.asyncio
async def test_hook_runner_bounds_timeout_and_honors_error_policy() -> None:
    calls: list[str] = []
    runner = HookRunner()
    runner.register(
        _Hook(
            "slow-open",
            calls,
            priority=1,
            timeout_seconds=0.01,
            error_policy="fail_open",
            sleep_seconds=0.1,
        ),
        owner="plugin",
    )
    runner.register(_Hook("after", calls, priority=2), owner="plugin")
    ctx = _ctx()

    await runner.run(HookPoint.BEFORE_ROUTE, ctx)

    assert calls == ["slow-open", "after"]
    assert [item["status"] for item in ctx.scratch[HOOK_TRACE_SCRATCH_KEY]] == [
        "timeout_open",
        "ok",
    ]

    failed = HookRunner()
    failed.register(_Hook("closed", [], boom=True), owner="plugin")
    failed_ctx = _ctx()
    with pytest.raises(HookExecutionError, match="plugin_hook_failed"):
        await failed.run(HookPoint.BEFORE_ROUTE, failed_ctx)
    assert "sensitive plugin detail" not in str(failed_ctx.scratch[HOOK_TRACE_SCRATCH_KEY])


@pytest.mark.asyncio
async def test_flow_runner_skips_denied_plugin_step_but_never_core() -> None:
    async def deny(owner: str, ctx: PipelineContext) -> bool:
        _ = owner, ctx
        return False

    plugin_step = _Step()
    result = await FlowRunner(
        {"plugin.test.step": plugin_step},
        owner_gate=deny,
    ).run(_flow(), _ctx())

    assert result.status == FLOW_RUN_COMPLETED
    assert result.steps[0].status == STEP_TRACE_OWNER_SKIPPED
    assert result.steps[0].reason == "owner_execution_denied"
    assert plugin_step.calls == 0

    core_step = _Step()
    core_result = await FlowRunner(
        {"plugin.test.step": core_step},
        owner_gate=deny,
    ).run(_flow(owner="core"), _ctx())
    assert core_result.status == FLOW_RUN_COMPLETED
    assert core_step.calls == 1


@pytest.mark.asyncio
async def test_flow_runner_fails_for_retry_when_owner_gate_is_unavailable() -> None:
    async def unavailable(owner: str, ctx: PipelineContext) -> bool:
        _ = owner, ctx
        raise RuntimeError("state store unavailable")

    step = _Step()
    result = await FlowRunner(
        {"plugin.test.step": step},
        owner_gate=unavailable,
    ).run(_flow(), _ctx())

    assert result.status == "failed"
    assert result.error == "owner_gate_error"
    assert result.steps[0].status == "error"
    assert result.steps[0].attempts == 0
    assert step.calls == 0


@pytest.mark.asyncio
async def test_effect_dispatcher_gates_immediately_before_plugin_handler() -> None:
    calls: list[str] = []

    async def handler(effect, ctx, record) -> None:
        _ = ctx, record
        calls.append(effect.idempotency_key)

    async def deny(owner: str, ctx: PipelineContext) -> bool:
        _ = owner, ctx
        return False

    registry = EffectHandlerRegistry()
    registry.register("run", "plugin", handler)
    dispatcher = EffectDispatcher(
        registry,
        InMemoryEffectCommitter(),
        owner_gate=deny,
    )
    result = await dispatcher.dispatch(
        MessageEffect(
            type="run",
            owner="plugin",
            payload={},
            idempotency_key="plugin:run:1",
        ),
        _ctx(),
    )

    assert result.status == EFFECT_HANDLER_STATUS_OWNER_SKIPPED
    assert result.error == "owner_execution_denied"
    assert calls == []


@pytest.mark.asyncio
async def test_flow_runner_completes_claim_when_owner_is_intentionally_skipped() -> None:
    async def deny(owner: str, ctx: PipelineContext) -> bool:
        _ = owner, ctx
        return False

    committer = _LifecycleCommitter()
    registry = EffectHandlerRegistry()

    async def handler(*_args) -> None:
        raise AssertionError("denied owner handler must not execute")

    registry.register("run", "plugin", handler)
    dispatcher = EffectDispatcher(registry, committer, owner_gate=deny)

    result = await FlowRunner(
        {"plugin.test.step": _EffectStep()},
        effect_dispatcher=dispatcher,
        effect_handlers_enabled=True,
    ).run(_flow(), _ctx())

    assert result.status == FLOW_RUN_COMPLETED
    assert result.effect_commits[0]["status"] == EFFECT_STATUS_COMPLETED
    assert result.effect_dispatches[0]["status"] == EFFECT_HANDLER_STATUS_OWNER_SKIPPED
    assert committer.completed == 1
    assert committer.failed == 0


@pytest.mark.asyncio
async def test_channel_reply_rechecks_selected_binding_owner_before_send() -> None:
    gate_calls: list[str] = []

    async def gate(owner: str, ctx: PipelineContext) -> bool:
        _ = ctx
        gate_calls.append(owner)
        return owner == "channel"

    outbound = _ChannelOutbound()
    channel_registry = ChannelRegistry()
    channel_registry.register_outbound(
        "wechat",
        outbound,
        owner="wxbot",
    )
    registry = EffectHandlerRegistry()
    registry.register(
        "enqueue_channel_reply",
        "channel",
        ChannelReplyEffectHandler(channel_registry, owner_gate=gate),
    )
    dispatcher = EffectDispatcher(
        registry,
        InMemoryEffectCommitter(),
        owner_gate=gate,
    )

    result = await dispatcher.dispatch(
        MessageEffect(
            type="enqueue_channel_reply",
            owner="channel",
            payload={"channel": "wechat", "body": "hello"},
            idempotency_key="channel:binding-owner:1",
        ),
        _ctx(),
    )

    assert result.status == EFFECT_HANDLER_STATUS_OWNER_SKIPPED
    assert result.error == "owner_execution_denied"
    assert outbound.calls == 0
    assert gate_calls == ["channel", "wxbot"]
