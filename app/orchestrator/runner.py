"""Runtime skeleton for executing compiled message flows.

The current production path still uses ``DialogOrchestrator._run``. This
runner is intentionally small and side-effect agnostic so we can validate
``StepResult`` semantics and traces before moving existing core logic behind
real step executors.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Protocol

from app.common.types import CapabilityResult, RouteType
from app.llm.activity import wait_for_llm_activity
from app.orchestrator.effect_handlers import (
    EFFECT_HANDLER_STATUS_HANDLER_ERROR,
    EFFECT_HANDLER_STATUS_OWNER_SKIPPED,
    EffectDispatcher,
    EffectDispatchRecord,
)
from app.orchestrator.effects import (
    EFFECT_STATUS_RECORDED,
    EFFECT_STATUS_RUNNING,
    EffectCommitRecord,
    EffectCommitter,
    mark_effect_completed,
    mark_effect_failed,
)
from app.orchestrator.flow import (
    CompiledFlow,
    CompiledStep,
    FlowStep,
    MessageEffect,
    StepResult,
)
from app.orchestrator.owner_gate import (
    DEFAULT_OWNER_GATE_TIMEOUT_SECONDS,
    OwnerExecutionDecision,
    OwnerExecutionGate,
    evaluate_owner_execution,
    owner_gate_failure_is_retryable,
)
from app.orchestrator.pipeline import PipelineContext
from app.plugin.hooks import (
    RESULT_PRODUCER_OWNER_KEY,
    bind_result_producer_owner,
    trusted_result_producer_owner,
)


class EffectIntentRecorder(Protocol):
    async def stage_effect(
        self,
        effect: MessageEffect,
        ctx: PipelineContext,
        *,
        sequence: int = 0,
        deferred: bool = False,
        dry_run: bool = False,
    ) -> EffectCommitRecord: ...


FLOW_RUN_COMPLETED = "completed"
FLOW_RUN_STOPPED = "stopped"
FLOW_RUN_DEFERRED = "deferred"
FLOW_RUN_FAILED = "failed"

STEP_TRACE_OK = "ok"
STEP_TRACE_SHADOW = "shadow"
STEP_TRACE_OPTIONAL_SKIPPED = "optional_skipped"
STEP_TRACE_ERROR_OPEN = "error_open"
STEP_TRACE_DEGRADED = "degraded"
STEP_TRACE_ERROR = "error"
STEP_TRACE_TIMEOUT = "timeout"
STEP_TRACE_TIMEOUT_OPEN = "timeout_open"
STEP_TRACE_OWNER_SKIPPED = "owner_skipped"

FINALIZE_STEP_KINDS = {
    "core.output_safety",
    "core.legacy_hooks.before_postprocess",
    "core.postprocess",
    "core.legacy_hooks.after_postprocess",
    "core.commit_turns_and_publish",
}
FINALIZE_STEP_KIND_SUFFIXES = (".outbound_policy",)
RESULT_COMMIT_STEP_KIND = "core.commit_turns_and_publish"
DELEGATED_RESULT_STEP_KINDS = {
    "plugin.commands.dispatch",
}
DELEGATED_HOOK_RESULT_STEP_PREFIX = "core.legacy_hooks."
_TURN_SCOPED_PERSONA_VARIABLES = ("persona_skill", "persona_profile")


def clear_turn_scoped_persona_variables(session: object | None) -> None:
    """Discard reply-style state that must be resolved independently each turn."""

    variables = getattr(session, "variables", None)
    if not isinstance(variables, dict):
        return
    for key in _TURN_SCOPED_PERSONA_VARIABLES:
        variables.pop(key, None)


@dataclass(frozen=True)
class FlowRunStepTrace:
    id: str
    kind: str
    owner: str
    status: str
    action: str = "continue"
    reason: str = ""
    error: str = ""
    elapsed_ms: float = 0.0
    attempts: int = 1


@dataclass(frozen=True)
class FlowRunResult:
    flow_name: str
    flow_version: int
    status: str
    trace_id: str = ""
    tenant_id: str = ""
    session_id: str = ""
    steps: list[FlowRunStepTrace] = field(default_factory=list)
    effect_commits: list[dict[str, object]] = field(default_factory=list)
    effect_dispatches: list[dict[str, object]] = field(default_factory=list)
    stop_reason: str = ""
    error: str = ""
    decision_trace: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {FLOW_RUN_COMPLETED, FLOW_RUN_STOPPED, FLOW_RUN_DEFERRED}


class FlowRunner:
    """Execute a ``CompiledFlow`` using registered step executors.

    ``shadow=True`` makes missing executors a no-op trace instead of a failure.
    Outside shadow mode a missing required executor fails closed, while a
    missing optional executor is recorded and skipped.  This is useful while
    the runtime is still backed by the legacy ``DialogOrchestrator`` pipeline.
    """

    def __init__(
        self,
        executors: Mapping[str, FlowStep] | None = None,
        *,
        shadow: bool = False,
        shadow_skip_reasons: Mapping[str, str] | None = None,
        max_retry_attempts: int = 2,
        retry_backoff_seconds: float = 0.0,
        effect_committer: EffectCommitter | None = None,
        effect_dispatcher: EffectDispatcher | None = None,
        effect_handlers_enabled: bool = False,
        effect_dry_run: bool = False,
        effect_intent_recorder: EffectIntentRecorder | None = None,
        deferred_effect_handlers: set[tuple[str, str]] | None = None,
        owner_gate: OwnerExecutionGate | None = None,
        owner_gate_timeout_seconds: float = DEFAULT_OWNER_GATE_TIMEOUT_SECONDS,
    ) -> None:
        self._executors = dict(executors or {})
        self._shadow = shadow
        self._shadow_skip_reasons = dict(shadow_skip_reasons or {})
        self._max_retry_attempts = max(1, int(max_retry_attempts or 1))
        self._retry_backoff_seconds = max(0.0, float(retry_backoff_seconds or 0.0))
        self._effect_committer = effect_committer
        self._effect_dispatcher = effect_dispatcher
        self._effect_handlers_enabled = bool(effect_handlers_enabled)
        self._effect_dry_run = bool(effect_dry_run)
        self._effect_intent_recorder = effect_intent_recorder
        self._deferred_effect_handlers = set(deferred_effect_handlers or set())
        self._owner_gate = owner_gate
        self._owner_gate_timeout_seconds = owner_gate_timeout_seconds

    async def run(self, flow: CompiledFlow, ctx: PipelineContext) -> FlowRunResult:
        traces: list[FlowRunStepTrace] = []
        if not flow.runnable:
            return self._flow_result(
                ctx,
                flow_name=flow.name,
                flow_version=flow.version,
                status=FLOW_RUN_FAILED,
                steps=traces,
                error=f"flow_not_active:{flow.status}",
            )

        if not self._shadow:
            clear_turn_scoped_persona_variables(ctx.session)
        for index, step in enumerate(flow.steps):
            result, trace = await self._run_step(step, ctx)
            if not self._shadow and step.kind == "core.load_session":
                clear_turn_scoped_persona_variables(ctx.session)
            traces.append(trace)
            if trace.status in {STEP_TRACE_ERROR, STEP_TRACE_TIMEOUT}:
                return self._flow_result(
                    ctx,
                    flow_name=flow.name,
                    flow_version=flow.version,
                    status=FLOW_RUN_FAILED,
                    steps=traces,
                    error=trace.error,
                )
            apply_traces = await self._apply_result_or_traces(
                ctx,
                result,
                step,
                execution_trace=trace,
            )
            traces.extend(apply_traces)
            apply_error = _first_fatal_trace(apply_traces)
            if apply_error is not None:
                return self._flow_result(
                    ctx,
                    flow_name=flow.name,
                    flow_version=flow.version,
                    status=FLOW_RUN_FAILED,
                    steps=traces,
                    error=apply_error.error,
                )
            if result.finalize:
                metadata = dict(getattr(result.result, "metadata", None) or {})
                if metadata.get("degradation_reason"):
                    ctx.extras["wxbot_force_send"] = True
                    ctx.extras["degraded_reply_pending"] = True
                return await self._run_finalize_steps(
                    flow,
                    ctx,
                    traces,
                    start_index=index + 1,
                    trigger=result,
                )
            if result.action == "defer":
                return self._flow_result(
                    ctx,
                    flow_name=flow.name,
                    flow_version=flow.version,
                    status=FLOW_RUN_DEFERRED,
                    steps=traces,
                    stop_reason=result.reason,
                )
            if result.action == "stop":
                return self._flow_result(
                    ctx,
                    flow_name=flow.name,
                    flow_version=flow.version,
                    status=FLOW_RUN_STOPPED,
                    steps=traces,
                    stop_reason=result.reason,
                )

        return self._flow_result(
            ctx,
            flow_name=flow.name,
            flow_version=flow.version,
            status=FLOW_RUN_COMPLETED,
            steps=traces,
        )

    async def _run_finalize_steps(
        self,
        flow: CompiledFlow,
        ctx: PipelineContext,
        traces: list[FlowRunStepTrace],
        *,
        start_index: int,
        trigger: StepResult,
    ) -> FlowRunResult:
        for step in flow.steps[start_index:]:
            if not _is_finalize_step(step):
                continue
            if trigger.skip_output_safety and step.kind == "core.output_safety":
                continue

            result, trace = await self._run_step(step, ctx)
            traces.append(trace)
            if trace.status in {STEP_TRACE_ERROR, STEP_TRACE_TIMEOUT}:
                return self._flow_result(
                    ctx,
                    flow_name=flow.name,
                    flow_version=flow.version,
                    status=FLOW_RUN_FAILED,
                    steps=traces,
                    stop_reason=trigger.reason,
                    error=trace.error,
                )
            apply_traces = await self._apply_result_or_traces(
                ctx,
                result,
                step,
                execution_trace=trace,
            )
            traces.extend(apply_traces)
            apply_error = _first_fatal_trace(apply_traces)
            if apply_error is not None:
                return self._flow_result(
                    ctx,
                    flow_name=flow.name,
                    flow_version=flow.version,
                    status=FLOW_RUN_FAILED,
                    steps=traces,
                    stop_reason=trigger.reason,
                    error=apply_error.error,
                )
            if result.action == "defer":
                return self._flow_result(
                    ctx,
                    flow_name=flow.name,
                    flow_version=flow.version,
                    status=FLOW_RUN_DEFERRED,
                    steps=traces,
                    stop_reason=result.reason or trigger.reason,
                )
            if result.action == "stop":
                return self._flow_result(
                    ctx,
                    flow_name=flow.name,
                    flow_version=flow.version,
                    status=FLOW_RUN_STOPPED,
                    steps=traces,
                    stop_reason=result.reason or trigger.reason,
                )

        status = FLOW_RUN_STOPPED if trigger.action == "stop" else FLOW_RUN_COMPLETED
        return self._flow_result(
            ctx,
            flow_name=flow.name,
            flow_version=flow.version,
            status=status,
            steps=traces,
            stop_reason=trigger.reason,
        )

    def _flow_result(
        self,
        ctx: PipelineContext,
        *,
        flow_name: str,
        flow_version: int,
        status: str,
        steps: list[FlowRunStepTrace],
        stop_reason: str = "",
        error: str = "",
    ) -> FlowRunResult:
        effects_signal = ctx.signals.get("effects")
        effects = effects_signal if isinstance(effects_signal, dict) else {}
        return FlowRunResult(
            flow_name=flow_name,
            flow_version=flow_version,
            status=status,
            trace_id=ctx.trace_id or ctx.event.trace_id,
            tenant_id=ctx.event.tenant_id,
            session_id=ctx.event.session_id,
            steps=steps,
            effect_commits=_effect_signal_records(effects.get("commits")),
            effect_dispatches=_effect_signal_records(effects.get("dispatches")),
            stop_reason=stop_reason,
            error=error,
            decision_trace=_decision_trace(ctx),
        )

    async def _run_step(
        self,
        step: CompiledStep,
        ctx: PipelineContext,
    ) -> tuple[StepResult, FlowRunStepTrace]:
        started = time.perf_counter()
        executor = self._executors.get(step.kind)
        if executor is None:
            elapsed_ms = (time.perf_counter() - started) * 1000
            if self._shadow:
                result = StepResult(reason=self._shadow_skip_reasons.get(step.kind, "shadow_noop"))
                return result, FlowRunStepTrace(
                    id=step.id,
                    kind=step.kind,
                    owner=step.owner,
                    status=STEP_TRACE_SHADOW,
                    action=result.action,
                    reason=result.reason,
                    elapsed_ms=elapsed_ms,
                    attempts=0,
                )
            if step.optional:
                result = StepResult(reason="optional_executor_unavailable")
                return result, FlowRunStepTrace(
                    id=step.id,
                    kind=step.kind,
                    owner=step.owner,
                    status=STEP_TRACE_OPTIONAL_SKIPPED,
                    action=result.action,
                    reason=result.reason,
                    elapsed_ms=elapsed_ms,
                    attempts=0,
                )
            error = f"missing_flow_step_executor:{step.kind}"
            return StepResult(error=error), FlowRunStepTrace(
                id=step.id,
                kind=step.kind,
                owner=step.owner,
                status=STEP_TRACE_ERROR,
                error=error,
                elapsed_ms=elapsed_ms,
                attempts=0,
            )

        decision = await evaluate_owner_execution(
            self._owner_gate,
            step.owner,
            ctx,
            timeout_seconds=self._owner_gate_timeout_seconds,
        )
        if not decision.allowed:
            return self._owner_denied_result_and_trace(
                step,
                decision,
                started=started,
                attempts=0,
            )

        if step.kind == RESULT_COMMIT_STEP_KIND:
            result_owner = trusted_result_producer_owner(ctx) or "core"
            result_decision = await evaluate_owner_execution(
                self._owner_gate,
                result_owner,
                ctx,
                timeout_seconds=self._owner_gate_timeout_seconds,
            )
            if not result_decision.allowed:
                return self._owner_denied_result_and_trace(
                    step,
                    result_decision,
                    started=started,
                    attempts=0,
                    suppress_final_result=True,
                )

        retry_enabled = step.error_policy == "retry" and not _step_has_effects(step)
        max_attempts = self._max_retry_attempts if retry_enabled else 1
        last_error = ""
        last_timeout = False
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                retry_decision = await evaluate_owner_execution(
                    self._owner_gate,
                    step.owner,
                    ctx,
                    timeout_seconds=self._owner_gate_timeout_seconds,
                )
                if not retry_decision.allowed:
                    return self._owner_denied_result_and_trace(
                        step,
                        retry_decision,
                        started=started,
                        attempts=attempt - 1,
                    )
            try:
                result = await self._run_executor_once(executor, step, ctx)
            except TimeoutError:
                timeout_seconds = float(step.timeout_seconds or 0)
                last_error = f"step_timeout:{timeout_seconds:g}s"
                last_timeout = True
            except Exception as exc:
                last_error = str(exc).strip() or exc.__class__.__name__
                last_timeout = False
            else:
                fresh_decision = await evaluate_owner_execution(
                    self._owner_gate,
                    step.owner,
                    ctx,
                    timeout_seconds=self._owner_gate_timeout_seconds,
                )
                if not fresh_decision.allowed:
                    return self._owner_denied_result_and_trace(
                        step,
                        fresh_decision,
                        started=started,
                        attempts=attempt,
                    )
                elapsed_ms = (time.perf_counter() - started) * 1000
                return result, FlowRunStepTrace(
                    id=step.id,
                    kind=step.kind,
                    owner=step.owner,
                    status=STEP_TRACE_OK,
                    action=result.action,
                    reason=result.reason,
                    error=result.error,
                    elapsed_ms=elapsed_ms,
                    attempts=attempt,
                )

            if attempt < max_attempts and self._retry_backoff_seconds > 0:
                await asyncio.sleep(self._retry_backoff_seconds)

        elapsed_ms = (time.perf_counter() - started) * 1000
        reason = ""
        if step.error_policy == "retry":
            reason = (
                "retry_disabled_effectful_step" if _step_has_effects(step) else "retry_exhausted"
            )
        return self._error_result_and_trace(
            step,
            error=last_error,
            elapsed_ms=elapsed_ms,
            timeout=last_timeout,
            attempts=max_attempts,
            reason=reason,
        )

    @staticmethod
    async def _run_executor_once(
        executor: FlowStep,
        step: CompiledStep,
        ctx: PipelineContext,
    ) -> StepResult:
        timeout_seconds = float(step.timeout_seconds or 0)
        if timeout_seconds > 0:
            return await wait_for_llm_activity(
                executor.run(ctx),
                timeout=timeout_seconds,
            )
        return await executor.run(ctx)

    @staticmethod
    def _owner_denied_result_and_trace(
        step: CompiledStep,
        decision: OwnerExecutionDecision,
        *,
        started: float,
        attempts: int,
        suppress_final_result: bool = False,
    ) -> tuple[StepResult, FlowRunStepTrace]:
        elapsed_ms = (time.perf_counter() - started) * 1000
        if owner_gate_failure_is_retryable(decision.reason):
            error = decision.reason
            return StepResult(error=error), FlowRunStepTrace(
                id=step.id,
                kind=step.kind,
                owner=step.owner,
                status=STEP_TRACE_ERROR,
                action="continue",
                reason=decision.reason,
                error=error,
                elapsed_ms=elapsed_ms,
                attempts=attempts,
            )
        result = StepResult(
            action="suppress_outbound" if suppress_final_result else "continue",
            reason=decision.reason,
            append_assistant_turn=False if suppress_final_result else None,
            publish_outbound=False if suppress_final_result else None,
        )
        return result, FlowRunStepTrace(
            id=step.id,
            kind=step.kind,
            owner=step.owner,
            status=STEP_TRACE_OWNER_SKIPPED,
            action=result.action,
            reason=decision.reason,
            elapsed_ms=elapsed_ms,
            attempts=attempts,
        )

    def _error_result_and_trace(
        self,
        step: CompiledStep,
        *,
        error: str,
        elapsed_ms: float,
        timeout: bool = False,
        attempts: int = 1,
        reason: str = "",
    ) -> tuple[StepResult, FlowRunStepTrace]:
        if step.error_policy == "degrade":
            result = self._degrade_result(step, error)
            return result, FlowRunStepTrace(
                id=step.id,
                kind=step.kind,
                owner=step.owner,
                status=STEP_TRACE_DEGRADED,
                action=result.action,
                reason=result.reason,
                error=error,
                elapsed_ms=elapsed_ms,
                attempts=attempts,
            )
        if step.error_policy == "fail_open":
            result = StepResult(error=error)
            return result, FlowRunStepTrace(
                id=step.id,
                kind=step.kind,
                owner=step.owner,
                status=STEP_TRACE_TIMEOUT_OPEN if timeout else STEP_TRACE_ERROR_OPEN,
                action=result.action,
                reason=reason,
                error=error,
                elapsed_ms=elapsed_ms,
                attempts=attempts,
            )
        return StepResult(error=error), FlowRunStepTrace(
            id=step.id,
            kind=step.kind,
            owner=step.owner,
            status=STEP_TRACE_TIMEOUT if timeout else STEP_TRACE_ERROR,
            reason=reason,
            error=error,
            elapsed_ms=elapsed_ms,
            attempts=attempts,
        )

    @staticmethod
    def _degrade_result(step: CompiledStep, error: str) -> StepResult:
        reason = f"{step.kind}_failed"
        result = CapabilityResult(
            route=RouteType.CANNED,
            reply_text=_degradation_text(reason),
            metadata={
                "degradation_reason": reason,
                "failed_step_id": step.id,
                "failed_step_kind": step.kind,
                "failed_step_owner": step.owner,
                "error": error,
            },
        )
        return StepResult(
            action="stop",
            reason=f"{step.id}_degraded",
            result=result,
            finalize=True,
            skip_output_safety=False,
            route_label=RouteType.CANNED.value,
            error=error,
        )

    async def _apply_result_or_traces(
        self,
        ctx: PipelineContext,
        result: StepResult,
        step: CompiledStep,
        *,
        execution_trace: FlowRunStepTrace,
    ) -> list[FlowRunStepTrace]:
        started = time.perf_counter()
        try:
            result_owner = _trusted_result_owner_for_step(
                ctx,
                result,
                step,
                execution_trace=execution_trace,
            )
            dispatches = await self._apply_result(
                ctx,
                result,
                producer_owner=step.owner,
                result_producer_owner=result_owner,
                # Core steps may carry a producer that an earlier trusted core
                # stage bound to a plugin (for example, an agent-tool effect
                # committed by core.commit_turns_and_publish). Plugin steps
                # are always rebound to their compiled owner below.
                allow_delegated_producer=step.owner == "core",
            )
        except Exception as exc:
            error = str(exc).strip() or exc.__class__.__name__
            return [
                FlowRunStepTrace(
                    id=f"{step.id}:effects",
                    kind="core.effect_commit",
                    owner=step.owner,
                    status=STEP_TRACE_ERROR,
                    error=f"effect_commit_failed:{error}",
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                )
            ]
        handler_errors = [
            record for record in dispatches if record.status == EFFECT_HANDLER_STATUS_HANDLER_ERROR
        ]
        if not handler_errors:
            return []
        return [
            FlowRunStepTrace(
                id=f"{step.id}:effect_dispatch",
                kind="core.effect_dispatch",
                owner=step.owner,
                # A failed side effect must keep the inbound message
                # retryable. Treating it as fail-open would commit the inbox
                # and permanently strand the durable failed effect.
                status=STEP_TRACE_ERROR,
                reason="effect_handler_failed",
                error=_format_dispatch_errors(handler_errors),
                elapsed_ms=(time.perf_counter() - started) * 1000,
            )
        ]

    async def _apply_result(
        self,
        ctx: PipelineContext,
        result: StepResult,
        *,
        producer_owner: str,
        result_producer_owner: str = "",
        allow_delegated_producer: bool = False,
    ) -> list[EffectDispatchRecord]:
        if result.result is not None:
            ctx.result = result.result
            bind_result_producer_owner(
                ctx,
                result_producer_owner or producer_owner or "core",
            )
        if result.action == "suppress_outbound" or result.publish_outbound is False:
            ctx.extras["suppress_outbound"] = True
        if result.append_assistant_turn is False:
            ctx.extras["skip_assistant_turn"] = True
        dispatches: list[EffectDispatchRecord] = []
        if result.effects:
            effects = [
                replace(
                    effect,
                    producer_owner=(
                        str(effect.producer_owner or "").strip()
                        if allow_delegated_producer
                        and str(effect.producer_owner or "").strip()
                        else producer_owner
                    ),
                )
                for effect in result.effects
            ]
            ctx.effects.extend(effects)
            if self._effect_intent_recorder is not None:
                commits = ctx.signals.setdefault("effects", {}).setdefault("commits", [])
                base_sequence = len(commits)
                for index, effect in enumerate(effects):
                    deferred = self._effect_is_deferred(effect)
                    record = await self._effect_intent_recorder.stage_effect(
                        effect,
                        ctx,
                        sequence=base_sequence + index,
                        deferred=deferred,
                        dry_run=self._effect_dry_run or self._shadow,
                    )
                    commits.append(
                        {
                            "type": record.type,
                            "owner": record.owner,
                            "producer_owner": str(
                                record.producer_owner
                                or effect.producer_owner
                                or record.owner
                            ).strip(),
                            "idempotency_key": record.idempotency_key,
                            "status": record.status,
                            "error": record.error,
                            "dry_run": record.dry_run,
                        }
                    )
            elif self._effect_handlers_enabled and self._effect_dispatcher is not None:
                commits = ctx.signals.setdefault("effects", {}).setdefault("commits", [])
                dispatch_signals = ctx.signals.setdefault("effects", {}).setdefault(
                    "dispatches",
                    [],
                )
                base_sequence = len(commits)
                lifecycle_committer = self._effect_lifecycle_committer()
                for index, effect in enumerate(effects):
                    record = await self._effect_dispatcher.dispatch(
                        effect,
                        ctx,
                        sequence=base_sequence + index,
                        dry_run=self._effect_dry_run or self._shadow,
                    )
                    dispatches.append(record)
                    commit_record = _commit_record_from_dispatch(
                        record,
                        tenant_id=ctx.event.tenant_id,
                        producer_owner=effect.producer_owner,
                    )
                    if (
                        lifecycle_committer is not None
                        and commit_record.status == EFFECT_STATUS_RUNNING
                    ):
                        if record.status in {
                            EFFECT_STATUS_RECORDED,
                            EFFECT_HANDLER_STATUS_OWNER_SKIPPED,
                        }:
                            commit_record = await mark_effect_completed(
                                lifecycle_committer,
                                commit_record,
                            )
                        else:
                            commit_record = await mark_effect_failed(
                                lifecycle_committer,
                                commit_record,
                                error=record.error or record.status,
                            )
                    commit_signal: dict[str, object] = {
                        "type": commit_record.type,
                        "owner": commit_record.owner,
                        "producer_owner": commit_record.producer_owner,
                        "idempotency_key": commit_record.idempotency_key,
                        "status": commit_record.status,
                        "error": commit_record.error,
                        "dry_run": commit_record.dry_run,
                    }
                    if commit_record.claim_owner:
                        commit_signal["claim_owner"] = commit_record.claim_owner
                    if commit_record.lease_expires_at:
                        commit_signal["lease_expires_at"] = commit_record.lease_expires_at
                    if commit_record.attempt:
                        commit_signal["attempt"] = commit_record.attempt
                    commits.append(commit_signal)
                    dispatch_signals.append(
                        {
                            "type": record.type,
                            "owner": record.owner,
                            "producer_owner": commit_record.producer_owner,
                            "idempotency_key": record.idempotency_key,
                            "status": record.status,
                            "commit_status": commit_record.status,
                            "error": record.error,
                            "dry_run": record.dry_run,
                        }
                    )
            elif self._effect_committer is not None:
                commits = ctx.signals.setdefault("effects", {}).setdefault("commits", [])
                base_sequence = len(commits)
                for index, effect in enumerate(effects):
                    commit_record = await self._effect_committer.commit(
                        effect,
                        ctx,
                        sequence=base_sequence + index,
                        dry_run=self._effect_dry_run,
                    )
                    commits.append(
                        {
                            "type": commit_record.type,
                            "owner": commit_record.owner,
                            "producer_owner": str(
                                commit_record.producer_owner
                                or effect.producer_owner
                                or commit_record.owner
                            ).strip(),
                            "idempotency_key": commit_record.idempotency_key,
                            "status": commit_record.status,
                            "error": commit_record.error,
                            "dry_run": commit_record.dry_run,
                        }
                    )
        return dispatches

    def _effect_is_deferred(self, effect: MessageEffect) -> bool:
        identity = (str(effect.owner or "").strip(), str(effect.type or "").strip())
        if identity in self._deferred_effect_handlers:
            return True
        return (
            identity[1] == "enqueue_channel_reply"
            and ("channel", identity[1]) in self._deferred_effect_handlers
        )

    def _effect_lifecycle_committer(self) -> object | None:
        if callable(getattr(self._effect_committer, "mark_completed", None)):
            return self._effect_committer
        dispatcher_committer = getattr(self._effect_dispatcher, "_committer", None)
        if callable(getattr(dispatcher_committer, "mark_completed", None)):
            return dispatcher_committer
        return None


def _degradation_busy() -> str:
    try:
        from app.common import canned

        return str(canned.DEGRADATION_BUSY)
    except Exception:
        return "The system is busy right now. Please try again shortly."


def _degradation_text(reason: str = "") -> str:
    try:
        from app.common import canned

        classifier = getattr(canned, "degradation_text", None)
        if callable(classifier):
            return str(classifier(reason))
        return str(canned.DEGRADATION_BUSY)
    except Exception:
        return _degradation_busy()


def _step_has_effects(step: CompiledStep) -> bool:
    return any(str(output).startswith("effects.") for output in step.outputs)


def _trusted_result_owner_for_step(
    ctx: PipelineContext,
    result: StepResult,
    step: CompiledStep,
    *,
    execution_trace: FlowRunStepTrace,
) -> str:
    if result.result is None:
        return ""
    if execution_trace.status == STEP_TRACE_DEGRADED:
        return "core"
    if step.kind.startswith(DELEGATED_HOOK_RESULT_STEP_PREFIX):
        return trusted_result_producer_owner(ctx) or "core"
    if step.kind in DELEGATED_RESULT_STEP_KINDS:
        return str(ctx.extras.get(RESULT_PRODUCER_OWNER_KEY) or "").strip() or step.owner
    return str(step.owner or "").strip() or "core"


def _is_finalize_step(step: CompiledStep) -> bool:
    return step.kind in FINALIZE_STEP_KINDS or step.kind.endswith(FINALIZE_STEP_KIND_SUFFIXES)


def _first_fatal_trace(traces: list[FlowRunStepTrace]) -> FlowRunStepTrace | None:
    for trace in traces:
        if trace.status in {STEP_TRACE_ERROR, STEP_TRACE_TIMEOUT}:
            return trace
    return None


def _format_dispatch_errors(records: list[EffectDispatchRecord]) -> str:
    errors: list[str] = []
    for record in records:
        effect_name = f"{record.owner}:{record.type}:{record.idempotency_key}"
        error = record.error or EFFECT_HANDLER_STATUS_HANDLER_ERROR
        errors.append(f"{effect_name}:{error}")
    return "effect_handler_failed:" + ";".join(errors)


_DECISION_ROUTER_SIGNAL_KEYS = (
    "faq_matched",
    "faq_similarity",
    "faq_verdict",
    "faq_preview_failed",
    "faq_preview_error_class",
    "tool_intent_matched",
    "tools_available",
    "effective_tool_count",
    "policy_allowed",
    "tool_denial_reason",
    "tool_preflight_failed",
    "tool_preflight_error_class",
    "consecutive_fallbacks",
)


def _decision_trace(ctx: PipelineContext) -> dict[str, object]:
    """Build a bounded, payload-free trace of intent and routing decisions."""

    trace: dict[str, object] = {}
    if ctx.pre is not None:
        trace["intent"] = {
            "coarse": str(getattr(ctx.pre.intent_coarse, "value", ctx.pre.intent_coarse) or ""),
            "language": str(ctx.pre.language or ""),
            "sensitive": bool(ctx.pre.sensitive),
        }

    if ctx.route is not None:
        route_trace: dict[str, object] = {
            "type": str(getattr(ctx.route.type, "value", ctx.route.type) or ""),
            "confidence": _bounded_trace_float(ctx.route.confidence),
            "reason": _bounded_trace_text(ctx.route.reason),
        }
        if isinstance(ctx.route.hints, dict):
            rule = ctx.route.hints.get("rule")
            if rule:
                route_trace["rule"] = _bounded_trace_text(rule)
            confidence_basis = ctx.route.hints.get("confidence_basis")
            if confidence_basis:
                route_trace["confidence_basis"] = _bounded_trace_text(confidence_basis)
            matched_conditions = _bounded_trace_text_list(
                ctx.route.hints.get("matched_conditions")
            )
            if matched_conditions:
                route_trace["matched_conditions"] = matched_conditions
        trace["route"] = route_trace

    router_signals: dict[str, object] = {}
    structured = ctx.signals.get("router")
    if isinstance(structured, dict):
        router_signals.update(structured)
    legacy = ctx.extras.get("router_signals")
    if isinstance(legacy, dict):
        for key, value in legacy.items():
            router_signals.setdefault(str(key), value)
    safe_signals: dict[str, object] = {}
    for key in _DECISION_ROUTER_SIGNAL_KEYS:
        if key not in router_signals:
            continue
        value = _safe_decision_scalar(router_signals[key])
        if value is not None:
            safe_signals[key] = value
    if safe_signals:
        trace["router_signals"] = safe_signals

    scope = ""
    agent_signals = ctx.signals.get("agent")
    if isinstance(agent_signals, dict):
        scope = str(agent_signals.get("tool_scope") or "").strip()
    scope = scope or str(ctx.extras.get("agent_tool_scope") or "").strip()
    if scope:
        trace["agent"] = {"tool_scope": _bounded_trace_text(scope)}

    if ctx.result is not None:
        metadata = dict(ctx.result.metadata or {})
        result_trace: dict[str, object] = {
            "route": str(getattr(ctx.result.route, "value", ctx.result.route) or ""),
        }
        for key in (
            "fallback_from",
            "fallback_reason",
            "degradation_reason",
            "tool_preselection_verdict",
        ):
            value = metadata.get(key)
            if value:
                result_trace[key] = _bounded_trace_text(value)
        selected = _bounded_trace_text_list(metadata.get("tool_preselection_selected"))
        if selected:
            result_trace["tool_preselection_selected"] = selected
        scores = _bounded_trace_score_map(metadata.get("tool_preselection_scores"))
        if scores:
            result_trace["tool_preselection_scores"] = scores
        trace["result"] = result_trace
    return trace


def _safe_decision_scalar(value: object) -> object | None:
    if isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _bounded_trace_text(value)
    return None


def _bounded_trace_float(value: object) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _bounded_trace_text(value: object, *, limit: int = 256) -> str:
    return str(value or "").strip()[:limit]


def _bounded_trace_text_list(value: object, *, limit: int = 32) -> list[str]:
    if not isinstance(value, list | tuple | set):
        return []
    return [
        text
        for item in list(value)[:limit]
        if (text := _bounded_trace_text(item, limit=128))
    ]


def _bounded_trace_score_map(value: object, *, limit: int = 32) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    scores: dict[str, float] = {}
    for raw_name, raw_score in list(value.items())[:limit]:
        name = _bounded_trace_text(raw_name, limit=128)
        score = _bounded_trace_float(raw_score)
        if name:
            scores[name] = score
    return scores


def _effect_signal_records(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    records: list[dict[str, object]] = []
    allowed_keys = {
        "type",
        "owner",
        "producer_owner",
        "idempotency_key",
        "status",
        "commit_status",
        "error",
        "dry_run",
        "claim_owner",
        "lease_expires_at",
        "attempt",
    }
    for item in value:
        if not isinstance(item, dict):
            continue
        records.append(
            {
                key: item[key]
                for key in allowed_keys
                if key in item and _safe_effect_trace_value(item[key])
            }
        )
    return records


def _safe_effect_trace_value(value: object) -> bool:
    return isinstance(value, str | int | float | bool) or value is None


def _commit_record_from_dispatch(
    record: EffectDispatchRecord,
    *,
    tenant_id: str,
    producer_owner: str,
) -> EffectCommitRecord:
    return EffectCommitRecord(
        type=record.type,
        owner=record.owner,
        idempotency_key=record.idempotency_key,
        producer_owner=str(producer_owner or "").strip() or record.owner,
        payload=dict(record.payload),
        status=record.commit_status,
        error=record.commit_error,
        dry_run=record.dry_run,
        tenant_id=str(tenant_id or "").strip(),
    )
