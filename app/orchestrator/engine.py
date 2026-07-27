"""
M3 — Dialog Orchestrator.

This is the central coordinator that turns an :class:`InboundEvent` into a
published :class:`OutboundReply`. It wires together the session manager,
preprocessor, router, safety service, capability engines, and postprocessor.

Design notes
------------
* Every pipeline stage is wrapped in an OpenTelemetry span so traces show the
  full request path from ingress to egress.
* Any capability failure is caught and downgraded through a canned fallback
  chain — the contract is that the user always gets *some* reply.
* Metrics: ``cs_e2e_latency_seconds``, ``cs_route_decisions_total``,
  ``cs_pipeline_errors_total`` are emitted with appropriate labels.
* The whole :meth:`handle` call has a configurable wall-clock budget enforced
  by :func:`asyncio.wait_for`. On timeout we still emit a degradation reply.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from opentelemetry import trace

from app.channel import apply_event_scope_to_session
from app.common.capability import CapabilityEngine
from app.common.config import Settings
from app.common.context import set_session_id, set_tenant_id, set_trace_id
from app.common.exceptions import SessionLockLost
from app.common.logging import get_logger
from app.common.types import (
    CapabilityResult,
    Channel,
    Citation,
    InboundEvent,
    OutboundReply,
    PreprocessedMessage,
    ReplySegment,
    ReplyType,
    Role,
    RouteType,
    Session,
    SessionState,
    ToolCall,
    Turn,
)
from app.infra.metrics import E2E_LATENCY, PIPELINE_ERRORS, ROUTE_DECISIONS
from app.orchestrator.effect_handlers import EffectHandlerRegistry
from app.orchestrator.engine_flow_runtime import (
    FlowRuntimeCoordinator,
    FlowRuntimePorts,
)
from app.orchestrator.flow import FlowStep, FlowStepRegistry
from app.orchestrator.outcome import (
    PermanentProcessingError,
    ProcessingOutcome,
    ProcessingStatus,
    RetryableProcessingError,
    normalize_processing_outcome,
)
from app.orchestrator.pipeline import PipelineContext
from app.orchestrator.ports import (
    FaqPreviewCapabilityPort,
    OrchestratorBusPort,
    PluginRegistryPort,
    PostprocessorPort,
    PreprocessorPort,
    RouterPort,
    SafetyPort,
    SessionPort,
)
from app.orchestrator.runner import FlowRunResult
from app.plugin.hooks import HookAbort, HookPoint, HookRunner
from app.postprocessing.response_guards import apply_response_guards
from app.reliability.message_store import (
    MessageClaim,
    MessageReliabilityStore,
    TransactionalOutboxBus,
)

logger = get_logger(__name__)
tracer = trace.get_tracer("orchestrator")


def _canned() -> Any:
    """Lazily import :mod:`app.common.canned`.

    Imported lazily so an unrelated syntax error in that module (it lives
    outside this module's scope to fix) does not prevent this package from
    being imported. A minimal hard-coded fallback keeps the pipeline working
    even if the canned module fails to load.
    """
    try:
        from app.common import canned as _c

        return _c
    except Exception:

        class _Fallback:
            SAFETY_BLOCK = "Your message was blocked by the safety filter."
            DEGRADATION_BUSY = (
                "The system is busy right now. Please try again shortly, or "
                "type 'agent' to reach a human."
            )
            HANDOFF_PENDING = "Transferring you to a human agent. Please hold on."
            NO_ANSWER = "Sorry, I don't have an answer for that right now."

            @staticmethod
            def degradation_text(reason: str = "") -> str:
                normalized = str(reason or "").strip().lower()
                if "llm" in normalized or "model" in normalized:
                    return "The model service is unavailable. Please try again shortly."
                if "command" in normalized:
                    return "The command service is unavailable. Please try again shortly."
                return "The system service is unavailable. Please try again shortly."

        return _Fallback()


class DialogOrchestrator:
    """M3 orchestrator. Constructed once per process; handles one event per call."""

    # Overall budget in seconds for the whole pipeline.
    HANDLE_TIMEOUT = 90.0

    def __init__(
        self,
        session_manager: SessionPort,
        preprocessor: PreprocessorPort,
        router: RouterPort,
        safety: SafetyPort,
        postprocessor: PostprocessorPort,
        capabilities: dict[RouteType, CapabilityEngine],
        bus: OrchestratorBusPort,
        settings: Settings,
        hook_runner: HookRunner | None = None,
        flow_step_registry: FlowStepRegistry | None = None,
        flow_owner_permissions: dict[str, set[str]] | None = None,
        flow_step_executors: dict[str, FlowStep] | None = None,
        flow_effect_handler_registry: EffectHandlerRegistry | None = None,
        message_store: MessageReliabilityStore | None = None,
    ) -> None:
        self.session_manager = session_manager
        self.preprocessor = preprocessor
        self.router = router
        self.safety = safety
        self.postprocessor = postprocessor
        self.capabilities = capabilities
        self.message_store = message_store
        self.bus: OrchestratorBusPort = (
            TransactionalOutboxBus(
                bus,
                message_store,
                outbound_stream=settings.bus_outbound_stream,
            )
            if message_store is not None and not isinstance(bus, TransactionalOutboxBus)
            else bus
        )
        self.settings = settings
        self.hooks = hook_runner or HookRunner()
        self.flow_step_registry = flow_step_registry
        self.flow_owner_permissions = flow_owner_permissions
        self.flow_step_executors = dict(flow_step_executors or {})
        self.flow_effect_handler_registry = flow_effect_handler_registry
        self.plugin_registry: PluginRegistryPort | None = None
        self.handle_timeout = float(
            settings.orchestrator_handle_timeout_seconds or self.HANDLE_TIMEOUT
        )
        self._transaction_owns_session_lock: ContextVar[bool] = ContextVar(
            f"orchestrator_transaction_lock_{id(self)}",
            default=False,
        )
        self._flow_runtime = FlowRuntimeCoordinator(
            FlowRuntimePorts(
                session_manager=self.session_manager,
                preprocessor=self.preprocessor,
                router=self.router,
                safety=self.safety,
                postprocessor=self.postprocessor,
                capabilities=self.capabilities,
                bus=self.bus,
                settings=self.settings,
                hooks=self.hooks,
            ),
            session_lock=self._session_lock,
            suppression_reason=self._suppression_reason,
            step_registry=self.flow_step_registry,
            owner_permissions=self.flow_owner_permissions,
            step_executors=self.flow_step_executors,
            effect_handler_registry=self.flow_effect_handler_registry,
            message_store=self.message_store,
        )

    @property
    def last_flow_shadow_result(self) -> FlowRunResult | None:
        return self._flow_runtime.last_shadow_result

    @property
    def last_flow_runtime_result(self) -> FlowRunResult | None:
        return self._flow_runtime.last_runtime_result

    # -- public entrypoint ------------------------------------------------

    async def handle(self, event: InboundEvent) -> ProcessingOutcome:
        """Run the pipeline and return an explicit bus disposition."""
        set_trace_id(event.trace_id)
        set_tenant_id(event.tenant_id)
        set_session_id(event.session_id)

        start = time.perf_counter()
        route_label = "unknown"
        outcome = ProcessingOutcome.retryable_failure(reason="processing_not_started")
        try:
            runner = (
                self._flow_runtime.run
                if self.settings.orchestrator_flow_runtime_enabled
                else self._run
            )
            max_lock_retries = int(getattr(self.settings, "session_lock_lost_max_retries", 1) or 0)
            for attempt in range(max_lock_retries + 1):
                try:
                    if self.message_store is not None:
                        outcome = await self._run_transactional(event, runner)
                    else:
                        outcome = normalize_processing_outcome(
                            await asyncio.wait_for(runner(event), timeout=self.handle_timeout)
                        )
                    route_label = outcome.route_label
                    break
                except SessionLockLost:
                    if attempt >= max_lock_retries:
                        raise
                    PIPELINE_ERRORS.labels(stage="session_lock", code="lost_retry").inc()
                    logger.warning(
                        "orchestrator.session_lock_retry",
                        trace_id=event.trace_id,
                        session_id=event.session_id,
                        attempt=attempt + 1,
                    )
                    await asyncio.sleep(
                        float(
                            getattr(
                                self.settings,
                                "session_lock_retry_backoff_seconds",
                                0.05,
                            )
                            or 0.0
                        )
                    )
        except TimeoutError:
            PIPELINE_ERRORS.labels(stage="handle", code="timeout").inc()
            logger.error(
                "orchestrator.timeout",
                trace_id=event.trace_id,
                session_id=event.session_id,
            )
            # A timeout is considered handled only if a degradation response
            # is durably published, or a policy intentionally suppresses it.
            if self.message_store is not None:
                outcome = await self._emit_canned_transactional(
                    event,
                    _canned().DEGRADATION_BUSY,
                    RouteType.CANNED,
                    reason="handle_timeout_degraded",
                )
            else:
                outcome = await self._emit_canned(
                    event,
                    _canned().DEGRADATION_BUSY,
                    RouteType.CANNED,
                    reason="handle_timeout_degraded",
                )
            route_label = outcome.route_label
        except RetryableProcessingError as exc:
            PIPELINE_ERRORS.labels(stage="handle", code="retryable").inc()
            logger.warning(
                "orchestrator.retryable_failure",
                trace_id=event.trace_id,
                session_id=event.session_id,
                reason=exc.reason,
                error_type=exc.error_type,
            )
            outcome = ProcessingOutcome.retryable_failure(
                reason=exc.reason,
                error_type=exc.error_type,
            )
        except PermanentProcessingError as exc:
            PIPELINE_ERRORS.labels(stage="handle", code="permanent").inc()
            logger.warning(
                "orchestrator.permanent_failure",
                trace_id=event.trace_id,
                session_id=event.session_id,
                reason=exc.reason,
                error_type=exc.error_type,
            )
            outcome = ProcessingOutcome.permanent_failure(
                reason=exc.reason,
                error_type=exc.error_type,
            )
        except Exception as exc:
            PIPELINE_ERRORS.labels(stage="handle", code="unhandled").inc()
            logger.exception(
                "orchestrator.unhandled",
                trace_id=event.trace_id,
                session_id=event.session_id,
                error=str(exc),
            )
            # Unknown failures are retryable by default. In particular, a DB,
            # Redis, lock or outbound publish failure must not be converted to
            # a successful ACK merely because a canned response exists.
            outcome = ProcessingOutcome.retryable_failure(
                reason=str(exc).strip() or "unhandled_processing_failure",
                error_type=exc.__class__.__name__,
            )
        finally:
            await self._flow_runtime.run_shadow(event, route_label)
            elapsed = time.perf_counter() - start
            E2E_LATENCY.labels(tenant=event.tenant_id, route=route_label).observe(elapsed)

        return outcome

    async def _run_transactional(
        self,
        event: InboundEvent,
        runner: Callable[[InboundEvent], Awaitable[ProcessingOutcome]],
    ) -> ProcessingOutcome:
        assert self.message_store is not None
        async with self.session_manager.lock(
            event.session_id,
            tenant_id=event.tenant_id,
        ):
            lock_token = self._transaction_owns_session_lock.set(True)
            try:
                claim = await self.message_store.acquire(
                    event,
                    lease_seconds=max(
                        self.handle_timeout + 30.0,
                        float(self.settings.inbox_processing_lease_seconds or 180.0),
                    ),
                )
                if not claim.claimed:
                    return self._duplicate_claim_outcome(claim)

                try:
                    async with self.session_manager.stage():
                        with self.message_store.stage():
                            try:
                                outcome = normalize_processing_outcome(
                                    await asyncio.wait_for(
                                        runner(event),
                                        timeout=self.handle_timeout,
                                    )
                                )
                            except PermanentProcessingError as exc:
                                outcome = ProcessingOutcome.permanent_failure(
                                    reason=exc.reason,
                                    error_type=exc.error_type,
                                )
                            if outcome.status == ProcessingStatus.RETRYABLE_FAILURE:
                                raise RetryableProcessingError.from_outcome(outcome)

                            # Only this short transaction owns a DB connection.
                            # The LLM/plugin computation above staged core
                            # session and outbound intent in memory.
                            async with self.session_manager.transaction() as db:
                                with self.message_store.bind(db):
                                    await self.session_manager.flush_stage(db)
                                    await self.message_store.flush_stage(db)
                                    await self.message_store.complete(
                                        db,
                                        event,
                                        outcome,
                                        claim_token=claim.claim_token,
                                    )
                            return outcome
                except BaseException:
                    try:
                        await self.message_store.release(
                            event,
                            claim_token=claim.claim_token,
                        )
                    except Exception as release_exc:
                        logger.error(
                            "orchestrator.inbox_claim_release_failed",
                            tenant_id=event.tenant_id,
                            message_id=event.message_id,
                            error_type=release_exc.__class__.__name__,
                        )
                    raise
            finally:
                self._transaction_owns_session_lock.reset(lock_token)

    @staticmethod
    def _duplicate_claim_outcome(claim: MessageClaim) -> ProcessingOutcome:
        if claim.status == ProcessingStatus.PERMANENT_FAILURE.value:
            # The first worker can commit the terminal inbox row and then fail
            # while persisting the DLQ entry. Preserve that disposition so a
            # redelivery retries DLQ instead of being ACKed as a duplicate.
            return ProcessingOutcome.permanent_failure(
                route_label=claim.route_label or "duplicate",
                reason=claim.reason or "processing_failure",
                error_type=claim.error_type,
            )
        if claim.status == "processing":
            return ProcessingOutcome.retryable_failure(
                route_label=claim.route_label or "duplicate",
                reason="duplicate_message:processing",
                error_type="MessageAlreadyProcessing",
            )
        return ProcessingOutcome.intentionally_suppressed(
            route_label=claim.route_label or "duplicate",
            reason=f"duplicate_message:{claim.status}",
        )

    async def _emit_canned_transactional(
        self,
        event: InboundEvent,
        text: str,
        route: RouteType,
        *,
        reason: str,
    ) -> ProcessingOutcome:
        async def emit(_event: InboundEvent) -> ProcessingOutcome:
            return await self._emit_canned(
                _event,
                text,
                route,
                reason=reason,
            )

        return await self._run_transactional(event, emit)

    @asynccontextmanager
    async def _session_lock(
        self,
        event: InboundEvent,
    ) -> AsyncIterator[None]:
        if self._transaction_owns_session_lock.get():
            yield
            return
        async with self.session_manager.lock(
            event.session_id,
            tenant_id=event.tenant_id,
        ):
            yield

    # -- core pipeline ----------------------------------------------------

    async def _run(self, event: InboundEvent) -> ProcessingOutcome:
        """Internal pipeline body. Returns its explicit processing outcome."""
        ctx = PipelineContext(event=event, trace_id=event.trace_id)

        async with self._session_lock(event):
            # 1. Load session
            with tracer.start_as_current_span("session.load"):
                session = await self.session_manager.load(
                    tenant_id=event.tenant_id,
                    user_id=event.user_id,
                    session_id=event.session_id,
                    channel=event.channel,
                )
                apply_event_scope_to_session(session, event)
            ctx.session = session

            # 2. Preprocess
            try:
                await self.hooks.run(HookPoint.BEFORE_PREPROCESS, ctx)
                with tracer.start_as_current_span("preprocess"):
                    pre = await self.preprocessor.run(event.message)
            except HookAbort as ha:
                result = CapabilityResult(route=RouteType.CANNED, reply_text=ha.reply_text)
                return await self._finalize(ctx, result, skip_output_safety=True)
            except RetryableProcessingError:
                raise
            except Exception as exc:
                PIPELINE_ERRORS.labels(stage="preprocess", code="exception").inc()
                logger.exception("preprocess.failed", error=str(exc))
                result = self._degrade("preprocess_failed")
                return await self._finalize(ctx, result, skip_output_safety=False)
            ctx.pre = pre
            if pre.pii_map:
                session.pii_map.update(pre.pii_map)
            await self.hooks.run(HookPoint.AFTER_PREPROCESS, ctx)

            # 3. Append normalized user turn after preprocess so later history
            #    does not retain raw group @mentions or duplicated current input.
            if not bool(event.metadata.get("is_self_sent")):
                turn_metadata = dict(event.metadata or {})
                turn_metadata.setdefault("original_content", pre.original_text)
                turn_metadata["cleaned_content"] = pre.cleaned_text
                already_recorded = any(
                    turn.role == Role.USER and turn.trace_id == event.trace_id
                    for turn in session.turns
                )
                if not already_recorded:
                    user_turn = Turn(
                        session_id=session.session_id,
                        role=Role.USER,
                        content=pre.cleaned_text or event.message.content,
                        trace_id=event.trace_id,
                        metadata=turn_metadata,
                    )
                    with tracer.start_as_current_span("session.append_user_turn"):
                        await self.session_manager.append_turn(session, user_turn)
            else:
                logger.info(
                    "session.skip_self_sent_user_turn",
                    session_id=session.session_id,
                    trace_id=event.trace_id,
                    msg_svr_id=event.metadata.get("msg_svr_id"),
                )

            # 4. Handoff short-circuit: if the session is already escalated we
            #    do not run the AI pipeline; we let the human agent handle it.
            if session.state == SessionState.ESCALATED:
                with tracer.start_as_current_span("orchestrator.handoff_short_circuit"):
                    outcome = await self._emit_canned(
                        event,
                        _canned().HANDOFF_PENDING,
                        RouteType.HANDOFF,
                        reason="handoff_pending",
                    )
                ROUTE_DECISIONS.labels(tenant=event.tenant_id, route=RouteType.HANDOFF.value).inc()
                return outcome

            # 5. Input safety
            try:
                with tracer.start_as_current_span("safety.input"):
                    safe_in = await self.safety.check_input(pre)
            except Exception as exc:
                PIPELINE_ERRORS.labels(stage="safety_input", code="exception").inc()
                logger.exception("safety.input_failed", error=str(exc))
                safe_in = True  # fail-open for availability; capability may still block
            if not safe_in:
                result = CapabilityResult(route=RouteType.CANNED, reply_text=_canned().SAFETY_BLOCK)
                ROUTE_DECISIONS.labels(tenant=event.tenant_id, route=RouteType.CANNED.value).inc()
                return await self._finalize(ctx, result, skip_output_safety=True)

            # 6. Route
            try:
                await self.hooks.run(HookPoint.BEFORE_ROUTE, ctx)
                router_signals = (
                    dict(ctx.extras.get("router_signals") or {})
                    if isinstance(ctx.extras.get("router_signals"), dict)
                    else {}
                )
                faq_preview: dict[str, Any] | None = None
                faq_engine = self.capabilities.get(RouteType.FAQ)
                if isinstance(faq_engine, FaqPreviewCapabilityPort):
                    try:
                        preview = await faq_engine.preview_match(
                            pre,
                            session,
                            {"trace_id": event.trace_id},
                        )
                        faq_preview = dict(preview)
                        score = float(preview.get("score", 0.0) or 0.0)
                        router_signals["faq_similarity"] = score
                        if preview.get("scope"):
                            router_signals["faq_scope"] = preview.get("scope")
                        if preview.get("faq_id"):
                            router_signals["faq_id"] = preview.get("faq_id")
                        if preview.get("verdict"):
                            router_signals["faq_verdict"] = preview.get("verdict")
                    except Exception as exc:
                        logger.warning(
                            "router.faq_preview_failed",
                            session_id=session.session_id,
                            trace_id=event.trace_id,
                            error=str(exc),
                        )
                with tracer.start_as_current_span("router.decide"):
                    route = await self.router.decide(pre, session, signals=router_signals)
            except HookAbort as ha:
                result = CapabilityResult(route=RouteType.CANNED, reply_text=ha.reply_text)
                return await self._finalize(ctx, result, skip_output_safety=True)
            except RetryableProcessingError:
                raise
            except Exception as exc:
                PIPELINE_ERRORS.labels(stage="route", code="exception").inc()
                logger.exception("router.failed", error=str(exc))
                result = self._degrade("router_failed")
                return await self._finalize(ctx, result, skip_output_safety=False)
            ctx.route = route
            if faq_preview is not None and isinstance(route.hints, dict):
                route.hints.setdefault("faq_preview", faq_preview)
            agent_tool_scope = ctx.extras.get("agent_tool_scope")
            if agent_tool_scope and isinstance(route.hints, dict):
                route.hints.setdefault("agent_tool_scope", agent_tool_scope)
            ROUTE_DECISIONS.labels(tenant=event.tenant_id, route=route.type.value).inc()
            await self.hooks.run(HookPoint.AFTER_ROUTE, ctx)

            # 7. Dispatch to capability
            try:
                await self.hooks.run(HookPoint.BEFORE_CAPABILITY, ctx)
            except HookAbort as ha:
                result = CapabilityResult(route=RouteType.CANNED, reply_text=ha.reply_text)
                ctx.result = result
                return await self._finalize(ctx, result, skip_output_safety=True)
            if isinstance(route.hints, dict):
                route.hints.setdefault("trace_id", event.trace_id)
                route.hints.setdefault("request_metadata", dict(event.metadata or {}))
            engine = self.capabilities.get(route.type)
            if engine is None:
                logger.warning(
                    "capability.missing",
                    route=route.type.value,
                    tenant_id=event.tenant_id,
                )
                result = self._degrade(f"no_engine:{route.type.value}")
            else:
                try:
                    with tracer.start_as_current_span(f"capability.{route.type.value}"):
                        result = await engine.answer(pre, session, route.hints)
                except Exception as exc:
                    PIPELINE_ERRORS.labels(stage="capability", code=route.type.value).inc()
                    logger.exception(
                        "capability.failed",
                        route=route.type.value,
                        error=str(exc),
                    )
                    result = await self._degrade_with_faq_fallback(
                        pre, session, exclude=route.type, failed_route=route.type
                    )
            ctx.result = result
            try:
                await self.hooks.run(HookPoint.AFTER_CAPABILITY, ctx)
            except HookAbort as ha:
                result = CapabilityResult(route=RouteType.CANNED, reply_text=ha.reply_text)
                ctx.result = result
                return await self._finalize(ctx, result, skip_output_safety=True)

            return await self._finalize(ctx, result, skip_output_safety=False)

    # -- finalize (safety_out + postprocess + publish + state + turn) -----

    async def _finalize(
        self,
        ctx: PipelineContext,
        result: CapabilityResult,
        *,
        skip_output_safety: bool,
    ) -> ProcessingOutcome:
        event = ctx.event
        session = ctx.session
        assert session is not None  # seeded in step 1

        # 8. Output safety
        if not skip_output_safety:
            try:
                with tracer.start_as_current_span("safety.output"):
                    safe_out = await self.safety.check_output(result)
            except Exception as exc:
                PIPELINE_ERRORS.labels(stage="safety_output", code="exception").inc()
                logger.exception("safety.output_failed", error=str(exc))
                safe_out = True
            if not safe_out:
                result = CapabilityResult(route=RouteType.CANNED, reply_text=_canned().SAFETY_BLOCK)
        ctx.result = result

        # 9. Postprocess → OutboundReply
        try:
            await self.hooks.run(HookPoint.BEFORE_POSTPROCESS, ctx)
            with tracer.start_as_current_span("postprocess"):
                reply = await self.postprocessor.run(result, session)
        except HookAbort as ha:
            reply = self._fallback_reply(event, ha.reply_text)
        except RetryableProcessingError:
            raise
        except Exception as exc:
            PIPELINE_ERRORS.labels(stage="postprocess", code="exception").inc()
            logger.exception("postprocess.failed", error=str(exc))
            reply = self._fallback_reply(event, _canned().DEGRADATION_BUSY)
        ctx.reply = reply
        apply_response_guards(ctx, settings=self.settings)
        await self.hooks.run(HookPoint.AFTER_POSTPROCESS, ctx)

        suppress_assistant_turn = bool(ctx.extras.get("skip_assistant_turn"))
        suppress_outbound = bool(ctx.extras.get("suppress_outbound"))

        # 10. Append assistant turn
        if not suppress_assistant_turn:
            assistant_turn = Turn(
                session_id=session.session_id,
                role=Role.ASSISTANT,
                content=reply.primary_text or result.reply_text,
                tool_calls=list(result.tool_calls),
                citations=list(result.citations),
                trace_id=event.trace_id,
                metadata={"route": result.route.value},
            )
            with tracer.start_as_current_span("session.append_assistant_turn"):
                await self.session_manager.append_turn(session, assistant_turn)

        # 11. Advance state machine (IDLE → CHATTING on first turn).
        if session.state == SessionState.IDLE and not bool(ctx.extras.get("skip_state_transition")):
            try:
                await self.session_manager.set_state(session, SessionState.CHATTING)
            except ValueError as exc:
                logger.warning("session.state_transition_failed", error=str(exc))

        # 12. Publish outbound
        if not suppress_outbound:
            with tracer.start_as_current_span("bus.publish_outbound"):
                await self.bus.publish(
                    self.settings.bus_outbound_stream,
                    reply.model_dump(mode="json"),
                    partition_key=f"{event.tenant_id}:{session.session_id}",
                )

        if suppress_outbound:
            return ProcessingOutcome.intentionally_suppressed(
                route_label=result.route.value,
                reason=self._suppression_reason(ctx),
            )
        return ProcessingOutcome.completed(
            route_label=result.route.value,
            reason=str(result.metadata.get("degradation_reason") or ""),
        )

    # -- degradation helpers ----------------------------------------------

    def _degrade(self, reason: str) -> CapabilityResult:
        """Return a canned-degradation CapabilityResult with diagnostic metadata."""
        logger.warning("orchestrator.degrade", reason=reason)
        return CapabilityResult(
            route=RouteType.CANNED,
            reply_text=self._degradation_text(reason),
            metadata={"degradation_reason": reason},
        )

    @staticmethod
    def _degradation_text(reason: str) -> str:
        canned = _canned()
        classifier = getattr(canned, "degradation_text", None)
        if callable(classifier):
            return str(classifier(reason))
        return str(getattr(canned, "DEGRADATION_BUSY", ""))

    async def _degrade_with_faq_fallback(
        self,
        pre: PreprocessedMessage,
        session: Session,
        *,
        exclude: RouteType,
        failed_route: RouteType | None = None,
    ) -> CapabilityResult:
        """Degradation chain: try FAQ for non-LLM routes, else classified busy."""
        if failed_route == RouteType.LLM:
            return self._degrade("capability_failed:llm")
        faq = self.capabilities.get(RouteType.FAQ)
        if faq is not None and exclude != RouteType.FAQ:
            try:
                with tracer.start_as_current_span("capability.faq_fallback"):
                    return await faq.answer(pre, session, {"fallback": True})
            except Exception as exc:
                PIPELINE_ERRORS.labels(stage="faq_fallback", code="exception").inc()
                logger.exception("faq_fallback.failed", error=str(exc))
        route_label = (failed_route or exclude).value
        return self._degrade(f"capability_failed:{route_label}")

    # -- canned emitters --------------------------------------------------

    async def _emit_canned(
        self,
        event: InboundEvent,
        text: str,
        route: RouteType,
        *,
        reason: str = "",
    ) -> ProcessingOutcome:
        result = CapabilityResult(route=route, reply_text=text)
        reply = self._fallback_reply(event, text)
        ctx = PipelineContext(
            event=event,
            trace_id=event.trace_id,
            result=result,
            reply=reply,
        )
        try:
            await self.hooks.run(HookPoint.AFTER_POSTPROCESS, ctx)
        except RetryableProcessingError:
            raise
        except Exception as exc:
            PIPELINE_ERRORS.labels(stage="canned_after_postprocess", code="exception").inc()
            logger.exception("canned.after_postprocess_failed", error=str(exc))
        if bool(ctx.extras.get("suppress_outbound")):
            suppression_reason = self._suppression_reason(ctx)
            return ProcessingOutcome.intentionally_suppressed(
                route_label=route.value,
                reason=(
                    suppression_reason
                    if suppression_reason != "suppress_outbound"
                    else reason or suppression_reason
                ),
            )
        try:
            await self.bus.publish(
                self.settings.bus_outbound_stream,
                reply.model_dump(mode="json"),
                partition_key=f"{event.tenant_id}:{event.session_id}",
            )
        except Exception as exc:
            PIPELINE_ERRORS.labels(stage="publish_canned", code="exception").inc()
            logger.exception("canned.publish_failed", error=str(exc))
            raise RetryableProcessingError(
                "canned_publish_failed",
                error_type=exc.__class__.__name__,
            ) from exc
        return ProcessingOutcome.completed(
            route_label=route.value,
            reason=reason,
        )

    @staticmethod
    def _suppression_reason(ctx: PipelineContext) -> str:
        policy = ctx.extras.get("wxbot_reply_policy")
        if isinstance(policy, dict):
            reason = str(policy.get("reason") or "").strip()
            if reason:
                return reason
        explicit = str(ctx.extras.get("suppression_reason") or "").strip()
        return explicit or "suppress_outbound"

    @staticmethod
    def _fallback_reply(event: InboundEvent, text: str) -> OutboundReply:
        return OutboundReply(
            tenant_id=event.tenant_id,
            channel=event.channel,
            adapter_id=event.adapter_id,
            connection_id=event.connection_id,
            user_id=event.user_id,
            session_id=event.session_id,
            conversation_id=event.conversation_id,
            external_conversation_id=event.external_conversation_id,
            canonical_conversation_id=event.canonical_conversation_id,
            external_user_id=event.external_user_id,
            external_participant_id=event.external_participant_id,
            canonical_participant_id=event.canonical_participant_id,
            type=ReplyType.TEXT,
            segments=[ReplySegment(type=ReplyType.TEXT, content=text)],
            trace_id=event.trace_id,
        )


# Keep import list clean for linters.
_ = (Channel, Citation, ToolCall)
