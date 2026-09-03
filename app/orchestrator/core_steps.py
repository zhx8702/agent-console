"""Core FlowStep executors for the message flow runner.

These executors mirror small, testable pieces of ``DialogOrchestrator``. They
are not wired into the production orchestrator yet; they give FlowRunner a
compatibility path that can be expanded behind tests.
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from numbers import Real
from typing import Any, ClassVar

from app.channel import apply_event_scope_to_session
from app.common.exceptions import CapabilityError, UpstreamUnavailable
from app.common.intent_classify import classify_context_from_event
from app.common.intent_runtime import decision_from_pre, persist_decision
from app.common.logging import get_logger
from app.common.types import (
    CapabilityResult,
    OutboundReply,
    ReplySegment,
    ReplyType,
    Role,
    RouteType,
    SessionState,
    Turn,
)
from app.orchestrator.effect_handlers import effect_handler_opt_in_enabled
from app.orchestrator.effects import (
    EFFECT_COMMIT_SEMANTICS_AUDIT_AFTER_SIDE_EFFECT,
    EFFECT_COMMIT_SEMANTICS_DRY_RUN_ONLY,
    EFFECT_COMMIT_SEMANTICS_GATE_BEFORE_SIDE_EFFECT,
)
from app.orchestrator.flow import (
    CAPABILITY_DISPATCH_TIMEOUT_SECONDS,
    MessageEffect,
    StepResult,
    resolve_capability_dispatch_timeout_seconds,
)
from app.orchestrator.outcome import RetryableProcessingError
from app.orchestrator.owner_gate import (
    DEFAULT_OWNER_GATE_TIMEOUT_SECONDS,
    OwnerExecutionDecision,
    OwnerExecutionGate,
    evaluate_owner_execution,
    owner_gate_failure_is_retryable,
)
from app.orchestrator.pipeline import PipelineContext
from app.plugin.hooks import (
    HookAbort,
    HookPoint,
    HookRunner,
    trusted_result_producer_owner,
)
from app.postprocessing.response_guards import apply_response_guards

logger = get_logger(__name__)

_DISPATCH_ATTEMPTS = 3


def _dispatch_retryable(exc: Exception) -> bool:
    """Whether a capability failure is worth another dispatch attempt.

    Only known-transient upstream failures are replayed. Everything else is
    either deterministic (e.g. ``UpstreamRejected``: HTTP 4xx, bugs), retried
    at the right layer already (worker redelivery), or unsafe to replay
    because the engine may have performed side effects before failing.
    """

    return isinstance(exc, (UpstreamUnavailable, TimeoutError))


_CONSECUTIVE_FALLBACKS_KEY = "consecutive_fallbacks"
_SUCCESSFUL_ANSWER_ROUTES = frozenset(
    {RouteType.FAQ, RouteType.RAG, RouteType.AGENT, RouteType.LLM}
)
_FAQ_VERDICTS = frozenset({"CLEAR", "AMBIGUOUS", "INSUFFICIENT", "LOW"})


def _canned_text(name: str, fallback: str) -> str:
    try:
        from app.common import canned

        return str(getattr(canned, name))
    except Exception:
        return fallback


def _safety_block() -> str:
    return _canned_text("SAFETY_BLOCK", "Your message was blocked by the safety filter.")


def _degradation_busy() -> str:
    return _canned_text(
        "DEGRADATION_BUSY",
        "The system is busy right now. Please try again shortly.",
    )


def _degradation_text(reason: str = "") -> str:
    try:
        from app.common import canned

        classifier = getattr(canned, "degradation_text", None)
        if callable(classifier):
            return str(classifier(reason))
        return str(canned.DEGRADATION_BUSY)
    except Exception:
        normalized = str(reason or "").lower()
        if "llm" in normalized or "model" in normalized:
            return "The model service is unavailable. Please try again shortly."
        if "command" in normalized:
            return "The command service is unavailable. Please try again shortly."
        return "The system is busy right now. Please try again shortly."


def _handoff_pending() -> str:
    return _canned_text("HANDOFF_PENDING", "Transferring you to a human agent.")


def _is_group_session(session: Any) -> bool:
    kind = str((session.metadata or {}).get("session_kind") or "").strip().lower()
    return kind in {"group", "chatroom", "channel", "guild"} or str(
        session.session_id or ""
    ).endswith("@chatroom")


def _non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or not numeric.is_integer() or numeric < 0:
        return None
    return int(numeric)


def _finite_similarity(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    score = float(value)
    if not math.isfinite(score) or score < 0.0 or score > 1.0:
        return None
    return score


def _inject_persisted_fallback_count(
    router_signals: dict[str, Any],
    session: Any,
) -> None:
    if _is_group_session(session):
        # Group sessions are shared by multiple actors; never let a shared
        # counter trigger group-wide automatic escalation.
        router_signals.pop(_CONSECUTIVE_FALLBACKS_KEY, None)
        return
    persisted = _non_negative_int(
        (session.variables or {}).get(_CONSECUTIVE_FALLBACKS_KEY)
    )
    router_signals[_CONSECUTIVE_FALLBACKS_KEY] = persisted or 0


def _update_persisted_fallback_count(
    session: Any,
    result: CapabilityResult,
) -> bool:
    if _is_group_session(session):
        return False
    variables = session.variables
    current = _non_negative_int(variables.get(_CONSECUTIVE_FALLBACKS_KEY)) or 0
    fallback_reason = str(
        result.metadata.get("fallback_reason")
        or result.metadata.get("degradation_reason")
        or ""
    ).strip()
    if fallback_reason:
        updated = min(current + 1, 1000)
    elif result.route in _SUCCESSFUL_ANSWER_ROUTES and result.reply_text.strip():
        updated = 0
    else:
        return False
    changed = variables.get(_CONSECUTIVE_FALLBACKS_KEY) != updated
    variables[_CONSECUTIVE_FALLBACKS_KEY] = updated
    return changed


def _assistant_turn_metadata(
    ctx: PipelineContext,
    result: CapabilityResult,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"route": result.route.value}
    if ctx.pre is not None and ctx.pre.intent_coarse is not None:
        metadata["intent_coarse"] = ctx.pre.intent_coarse.value
    if ctx.route is not None:
        metadata["route_confidence"] = float(ctx.route.confidence)
        rule = ctx.route.hints.get("rule") if isinstance(ctx.route.hints, dict) else None
        if isinstance(rule, str) and rule.strip():
            metadata["route_rule"] = rule.strip()

    semantic_intent = result.metadata.get("semantic_intent")
    if isinstance(semantic_intent, dict):
        metadata["semantic_intent"] = dict(semantic_intent)
        method = result.metadata.get("semantic_intent_method")
        if isinstance(method, str) and method.strip():
            metadata["semantic_intent_method"] = method.strip()

    fallback_from = result.metadata.get("fallback_from")
    fallback_reason = (
        result.metadata.get("fallback_reason")
        or result.metadata.get("degradation_reason")
    )
    if fallback_from:
        metadata["fallback_from"] = str(fallback_from)
    elif fallback_reason and ctx.route is not None:
        metadata["fallback_from"] = ctx.route.type.value
    if fallback_reason:
        metadata["fallback_reason"] = str(fallback_reason)
    return metadata


def _tool_intent_matched(router_signals: dict[str, Any]) -> bool:
    if "tool_intent_matched" in router_signals:
        return router_signals.get("tool_intent_matched") is True
    return router_signals.get("tools_available") is True


def _merge_faq_preview_signals(
    router_signals: dict[str, Any],
    faq_preview: dict[str, Any],
) -> None:
    matched = faq_preview.get("matched")
    if type(matched) is bool:
        router_signals["faq_matched"] = matched
    score = _finite_similarity(faq_preview.get("score"))
    if score is not None:
        router_signals["faq_similarity"] = score
    verdict = faq_preview.get("verdict")
    if isinstance(verdict, str) and verdict.strip().upper() in _FAQ_VERDICTS:
        router_signals["faq_verdict"] = verdict.strip().upper()
    if faq_preview.get("scope"):
        router_signals["faq_scope"] = faq_preview.get("scope")
    if faq_preview.get("faq_id"):
        router_signals["faq_id"] = faq_preview.get("faq_id")


@dataclass
class CoreStepDependencies:
    session_manager: Any
    preprocessor: Any
    router: Any
    safety: Any
    postprocessor: Any
    capabilities: dict[RouteType, Any]
    bus: Any
    settings: Any
    hook_runner: HookRunner | None = None
    hooks_enabled: bool = True
    side_effects_enabled: bool = True
    capability_dispatch_enabled: bool = True
    faq_preview_enabled: bool = True
    effect_handlers_enabled: bool = False
    owner_gate: OwnerExecutionGate | None = None
    owner_gate_timeout_seconds: float | None = None


class _BaseCoreStep:
    kind = ""
    owner = "core"
    name = ""
    permissions: ClassVar[list[str]] = []
    inputs: ClassVar[set[str]] = set()
    outputs: ClassVar[set[str]] = set()
    timeout_seconds = 5.0
    error_policy = "fail_closed"

    def __init__(self, deps: CoreStepDependencies) -> None:
        self.deps = deps


class LegacyHookStep(_BaseCoreStep):
    def __init__(
        self,
        deps: CoreStepDependencies,
        *,
        kind: str,
        hook_point: HookPoint,
    ) -> None:
        super().__init__(deps)
        self.kind = kind
        self.name = f"Legacy {hook_point.value} hooks"
        self.hook_point = hook_point

    async def run(self, ctx: PipelineContext) -> StepResult:
        if not self.deps.hooks_enabled:
            return StepResult(reason="hooks_disabled")
        hooks = self.deps.hook_runner
        if hooks is None:
            return StepResult()
        try:
            await hooks.run(self.hook_point, ctx)
        except HookAbort as exc:
            result = CapabilityResult(route=RouteType.CANNED, reply_text=exc.reply_text)
            return StepResult(
                action="stop",
                reason=exc.reason,
                result=result,
                finalize=True,
                skip_output_safety=True,
            )
        return StepResult()


class LoadSessionStep(_BaseCoreStep):
    kind = "core.load_session"
    name = "Load session"

    async def run(self, ctx: PipelineContext) -> StepResult:
        event = ctx.event
        ctx.session = await self.deps.session_manager.load(
            tenant_id=event.tenant_id,
            user_id=event.user_id,
            session_id=event.session_id,
            channel=event.channel,
        )
        apply_event_scope_to_session(ctx.session, event)
        return StepResult()


class PreprocessStep(_BaseCoreStep):
    kind = "core.preprocess"
    name = "Preprocess"
    timeout_seconds = 90.0

    async def run(self, ctx: PipelineContext) -> StepResult:
        if ctx.session is None:
            raise RuntimeError("session_required")
        pre = await self.deps.preprocessor.run(
            ctx.event.message,
            context=classify_context_from_event(
                ctx.event,
                has_attachment=bool(getattr(ctx.event.message, "attachments", None)),
            ),
        )
        ctx.pre = pre
        if pre.semantic_intent:
            persist_decision(
                decision_from_pre(pre),
                pre=pre,
                session=ctx.session,
                extras=ctx.extras,
            )
        if pre.pii_map:
            if self.deps.side_effects_enabled:
                ctx.session.pii_map.update(pre.pii_map)
            else:
                ctx.scratch.setdefault("core.preprocess", {})["pii_map"] = dict(pre.pii_map)
        return StepResult()


class AppendUserTurnStep(_BaseCoreStep):
    kind = "core.append_user_turn"
    name = "Append user turn"

    async def run(self, ctx: PipelineContext) -> StepResult:
        if ctx.session is None or ctx.pre is None:
            raise RuntimeError("session_and_pre_required")
        event = ctx.event
        if bool(event.metadata.get("is_self_sent")):
            return StepResult(reason="self_sent")
        if not self.deps.side_effects_enabled:
            return StepResult(
                reason="dry_run_skip_append_user_turn",
                effects=[
                    _append_turn_effect(
                        ctx,
                        Turn(
                            session_id=ctx.session.session_id,
                            role=Role.USER,
                            content=ctx.pre.cleaned_text or event.message.content,
                            trace_id=event.trace_id,
                            metadata={"dry_run": True},
                        ),
                        effect_type="append_user_turn",
                        commit_semantics=EFFECT_COMMIT_SEMANTICS_DRY_RUN_ONLY,
                        side_effects_executed_before_commit=False,
                    )
                ],
            )
        turn_metadata = dict(event.metadata or {})
        turn_metadata.setdefault("user_id", event.user_id)
        turn_metadata.setdefault(
            "external_participant_id",
            event.external_participant_id or event.external_user_id or event.user_id,
        )
        turn_metadata.setdefault(
            "canonical_participant_id",
            event.canonical_participant_id or event.user_id,
        )
        turn_metadata.setdefault("original_content", ctx.pre.original_text)
        turn_metadata["cleaned_content"] = ctx.pre.cleaned_text
        turn = Turn(
            session_id=ctx.session.session_id,
            role=Role.USER,
            content=ctx.pre.cleaned_text or event.message.content,
            trace_id=event.trace_id,
            metadata=turn_metadata,
        )
        append_as_effect = self.deps.effect_handlers_enabled or effect_handler_opt_in_enabled(
            ctx,
            effect_type="append_user_turn",
            owner="core",
        )
        if not append_as_effect:
            await self.deps.session_manager.append_turn(ctx.session, turn)
        return StepResult(
            effects=[
                _append_turn_effect(
                    ctx,
                    turn,
                    effect_type="append_user_turn",
                    commit_semantics=(
                        EFFECT_COMMIT_SEMANTICS_GATE_BEFORE_SIDE_EFFECT
                        if append_as_effect
                        else EFFECT_COMMIT_SEMANTICS_AUDIT_AFTER_SIDE_EFFECT
                    ),
                    side_effects_executed_before_commit=not append_as_effect,
                )
            ]
        )


class HandoffShortCircuitStep(_BaseCoreStep):
    kind = "core.handoff_short_circuit"
    name = "Handoff short-circuit"

    async def run(self, ctx: PipelineContext) -> StepResult:
        if ctx.session is None:
            raise RuntimeError("session_required")
        if ctx.session.state != SessionState.ESCALATED:
            return StepResult()
        return StepResult(
            action="stop",
            reason="handoff_short_circuit",
            result=CapabilityResult(route=RouteType.HANDOFF, reply_text=_handoff_pending()),
            finalize=True,
            skip_output_safety=True,
            append_assistant_turn=False,
        )


class InputSafetyStep(_BaseCoreStep):
    kind = "core.input_safety"
    name = "Input safety"

    async def run(self, ctx: PipelineContext) -> StepResult:
        if ctx.pre is None:
            raise RuntimeError("pre_required")
        safe = await self.deps.safety.check_input(ctx.pre)
        ctx.signals.setdefault("safety", {})["input"] = {"safe": bool(safe)}
        if safe:
            return StepResult()
        return StepResult(
            action="stop",
            reason="input_safety_block",
            result=CapabilityResult(route=RouteType.CANNED, reply_text=_safety_block()),
            finalize=True,
            skip_output_safety=True,
        )


class RouterSignalMergeStep(_BaseCoreStep):
    kind = "core.router_signal_merge"
    name = "Router signal merge"

    async def run(self, ctx: PipelineContext) -> StepResult:
        merged = dict(ctx.signals.get("router") or {})
        legacy = ctx.extras.get("router_signals")
        if isinstance(legacy, dict):
            merged.update(legacy)
        ctx.signals["router"] = merged
        return StepResult()


class RouteStep(_BaseCoreStep):
    kind = "core.route"
    name = "Route"

    async def run(self, ctx: PipelineContext) -> StepResult:
        if ctx.session is None or ctx.pre is None:
            raise RuntimeError("session_and_pre_required")
        router_signals = dict(ctx.signals.get("router") or {})
        _inject_persisted_fallback_count(router_signals, ctx.session)
        faq_preview: dict[str, Any] | None = None
        faq_engine = self.deps.capabilities.get(RouteType.FAQ)
        if (
            self.deps.faq_preview_enabled
            and faq_engine is not None
            and hasattr(faq_engine, "preview_match")
        ):
            try:
                preview = await faq_engine.preview_match(
                    ctx.pre,
                    ctx.session,
                    {"trace_id": ctx.event.trace_id},
                )
                faq_preview = dict(preview)
                _merge_faq_preview_signals(router_signals, faq_preview)
            except Exception as exc:
                router_signals["faq_preview_failed"] = True
                router_signals["faq_preview_error_class"] = exc.__class__.__name__
                logger.warning(
                    "router.faq_preview_failed",
                    session_id=ctx.session.session_id,
                    trace_id=ctx.event.trace_id,
                    error=str(exc),
                )

        agent_tool_scope = ctx.signals.get("agent", {}).get("tool_scope")
        if not agent_tool_scope:
            agent_tool_scope = ctx.extras.get("agent_tool_scope")
        agent_required_effect = ctx.extras.get("agent_required_effect")
        if _tool_intent_matched(router_signals):
            router_signals.setdefault("tool_intent_matched", True)
            agent_engine = self.deps.capabilities.get(RouteType.AGENT)
            preview_availability = getattr(
                agent_engine,
                "preview_availability",
                None,
            )
            if callable(preview_availability):
                try:
                    availability = await preview_availability(
                        ctx.pre,
                        ctx.session,
                        {
                            "trace_id": ctx.event.trace_id,
                            "agent_tool_scope": agent_tool_scope,
                            "tool_scope": agent_tool_scope,
                            "request_metadata": dict(ctx.event.metadata or {}),
                        },
                    )
                    if not isinstance(availability, dict):
                        raise TypeError("tool availability preview must be a mapping")
                    effective_count = _non_negative_int(
                        availability.get("effective_tool_count")
                    )
                    if effective_count is None:
                        raise ValueError(
                            "tool availability preview has invalid effective_tool_count"
                        )
                    router_signals["effective_tool_count"] = effective_count
                    router_signals["tools_available"] = effective_count > 0
                    policy_allowed = availability.get("policy_allowed")
                    if type(policy_allowed) is bool:
                        router_signals["policy_allowed"] = policy_allowed
                    denial_reason = availability.get("denial_reason")
                    if isinstance(denial_reason, str) and denial_reason.strip():
                        router_signals["tool_denial_reason"] = denial_reason.strip()
                except Exception as exc:
                    # Availability is an authorization boundary. If its
                    # preflight cannot produce a valid result, do not route as
                    # though tools were usable merely because intent matched.
                    router_signals["tools_available"] = False
                    router_signals["effective_tool_count"] = 0
                    router_signals["policy_allowed"] = False
                    router_signals["tool_denial_reason"] = "preflight_failed"
                    router_signals["tool_preflight_failed"] = True
                    router_signals["tool_preflight_error_class"] = (
                        exc.__class__.__name__
                    )
                    logger.warning(
                        "router.tool_preflight_failed",
                        session_id=ctx.session.session_id,
                        trace_id=ctx.event.trace_id,
                        error=str(exc),
                    )
            elif type(router_signals.get("tools_available")) is not bool:
                # Older Agent engines did not expose a preflight. Preserve the
                # former intent-match behavior until all deployments upgrade.
                router_signals["tools_available"] = True

        ctx.signals["router"] = router_signals
        route = await self.deps.router.decide(ctx.pre, ctx.session, signals=router_signals)
        if faq_preview is not None and isinstance(route.hints, dict):
            route.hints.setdefault("faq_preview", faq_preview)
        if agent_tool_scope and isinstance(route.hints, dict):
            route.hints.setdefault("agent_tool_scope", agent_tool_scope)
        if isinstance(agent_required_effect, dict) and isinstance(route.hints, dict):
            route.hints.setdefault("agent_required_effect", dict(agent_required_effect))
        ctx.route = route
        return StepResult(route_label=route.type.value)


class CapabilityDispatchStep(_BaseCoreStep):
    kind = "core.capability_dispatch"
    name = "Capability dispatch"
    timeout_seconds = CAPABILITY_DISPATCH_TIMEOUT_SECONDS

    def __init__(self, deps: CoreStepDependencies) -> None:
        super().__init__(deps)
        self.timeout_seconds = resolve_capability_dispatch_timeout_seconds(
            getattr(deps.settings, "orchestrator_capability_dispatch_timeout_seconds", None),
            handle_timeout_seconds=getattr(
                deps.settings,
                "orchestrator_handle_timeout_seconds",
                None,
            ),
        )

    @staticmethod
    def _recall_miss_reason(exc: Exception) -> str:
        if not isinstance(exc, CapabilityError):
            return ""
        reason = str(exc).strip()
        if reason in {"no_faq_hit", "no_context"}:
            return reason
        return ""

    async def _fallback_to_llm(
        self,
        ctx: PipelineContext,
        *,
        failed_route: RouteType,
        reason: str,
    ) -> CapabilityResult | None:
        llm_engine = self.deps.capabilities.get(RouteType.LLM)
        if llm_engine is None or ctx.pre is None or ctx.session is None or ctx.route is None:
            return None
        hints = dict(ctx.route.hints or {})
        hints.pop("faq_preview", None)
        hints["fallback_from"] = failed_route.value
        hints["fallback_reason"] = reason
        try:
            result = await llm_engine.answer(ctx.pre, ctx.session, hints)
        except Exception as exc:
            ctx.signals["capability"] = {
                "failed": True,
                "reason": f"fallback_failed:{failed_route.value}:{reason}",
                "route": failed_route.value,
                "fallback_route": RouteType.LLM.value,
                "error_class": exc.__class__.__name__,
            }
            return None
        result.metadata.setdefault("fallback_from", failed_route.value)
        result.metadata.setdefault("fallback_reason", reason)
        ctx.signals["capability"] = {
            "fallback": True,
            "reason": reason,
            "route": failed_route.value,
            "fallback_route": RouteType.LLM.value,
        }
        return result

    async def run(self, ctx: PipelineContext) -> StepResult:
        if ctx.session is None or ctx.pre is None or ctx.route is None:
            raise RuntimeError("session_pre_route_required")
        if not self.deps.capability_dispatch_enabled:
            ctx.signals["capability"] = {
                "skipped": True,
                "reason": "dry_run_skip_capability",
                "route": ctx.route.type.value,
            }
            return StepResult(action="stop", reason="dry_run_skip_capability")
        engine = self.deps.capabilities.get(ctx.route.type)
        if isinstance(ctx.route.hints, dict):
            ctx.route.hints.setdefault("trace_id", ctx.event.trace_id)
            ctx.route.hints.setdefault("request_metadata", dict(ctx.event.metadata or {}))
        if engine is None:
            reason = f"no_engine:{ctx.route.type.value}"
            result = None
            if ctx.route.type in {RouteType.FAQ, RouteType.RAG}:
                result = await self._fallback_to_llm(
                    ctx,
                    failed_route=ctx.route.type,
                    reason=reason,
                )
            if result is None:
                result = CapabilityResult(
                    route=RouteType.CANNED,
                    reply_text=_degradation_text(reason),
                    metadata={"degradation_reason": reason},
                )
        else:
            result = None
            last_exc: Exception | None = None
            for attempt in range(1, _DISPATCH_ATTEMPTS + 1):
                try:
                    result = await engine.answer(ctx.pre, ctx.session, ctx.route.hints)
                    last_exc = None
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    last_exc = exc
                    miss_reason = self._recall_miss_reason(exc)
                    if miss_reason:
                        break
                    if not _dispatch_retryable(exc):
                        logger.warning(
                            "capability.dispatch_not_retryable",
                            attempt=attempt,
                            route=ctx.route.type.value,
                            error_class=exc.__class__.__name__,
                        )
                        break
                    logger.warning(
                        "capability.dispatch_retry",
                        attempt=attempt,
                        attempts=_DISPATCH_ATTEMPTS,
                        route=ctx.route.type.value,
                        error_class=exc.__class__.__name__,
                    )
                    if attempt < _DISPATCH_ATTEMPTS:
                        await asyncio.sleep(0.4 * attempt)
            if result is None and last_exc is not None:
                exc = last_exc
                miss_reason = self._recall_miss_reason(exc)
                if miss_reason:
                    result = await self._fallback_to_llm(
                        ctx,
                        failed_route=ctx.route.type,
                        reason=miss_reason,
                    )
                    if result is not None:
                        ctx.result = result
                        return StepResult(result=result, route_label=result.route.value)
                if ctx.route.type != RouteType.LLM:
                    raise exc
                reason = f"capability_failed:{ctx.route.type.value}"
                ctx.signals["capability"] = {
                    "failed": True,
                    "reason": reason,
                    "route": ctx.route.type.value,
                    "error_class": exc.__class__.__name__,
                }
                result = CapabilityResult(
                    route=RouteType.CANNED,
                    reply_text=_degradation_text(reason),
                    metadata={
                        "degradation_reason": reason,
                        "failed_route": ctx.route.type.value,
                        "error_class": exc.__class__.__name__,
                    },
                )
        ctx.result = result
        if result.metadata.get("suppress_outbound") is True:
            ctx.extras["suppress_outbound"] = True
        if (
            result.metadata.get("skip_assistant_turn") is True
            or result.metadata.get("suppress_final_reply") is True
        ):
            ctx.extras["skip_assistant_turn"] = True
        channel_reply_effects = _channel_reply_effects_from_capability_result(result)
        if channel_reply_effects:
            ctx.extras["pending_channel_reply_effects"] = channel_reply_effects
        return StepResult(result=result, route_label=result.route.value)


class OutputSafetyStep(_BaseCoreStep):
    kind = "core.output_safety"
    name = "Output safety"

    async def run(self, ctx: PipelineContext) -> StepResult:
        if ctx.result is None:
            raise RuntimeError("result_required")
        safe = await self.deps.safety.check_output(ctx.result)
        ctx.signals.setdefault("safety", {})["output"] = {"safe": bool(safe)}
        if safe:
            return StepResult()
        ctx.extras.pop("pending_channel_reply_effects", None)
        ctx.extras.pop("suppress_outbound", None)
        ctx.extras.pop("skip_assistant_turn", None)
        result = CapabilityResult(route=RouteType.CANNED, reply_text=_safety_block())
        return StepResult(action="replace_result", reason="output_safety_block", result=result)


class PostprocessStep(_BaseCoreStep):
    kind = "core.postprocess"
    name = "Postprocess"

    async def run(self, ctx: PipelineContext) -> StepResult:
        if ctx.session is None or ctx.result is None:
            raise RuntimeError("session_and_result_required")
        ctx.reply = await self.deps.postprocessor.run(ctx.result, ctx.session)
        apply_response_guards(ctx, settings=self.deps.settings)
        return StepResult()


class CommitTurnsAndPublishStep(_BaseCoreStep):
    kind = "core.commit_turns_and_publish"
    name = "Commit turns and publish"

    async def run(self, ctx: PipelineContext) -> StepResult:
        if ctx.session is None or ctx.result is None or ctx.reply is None:
            raise RuntimeError("session_result_reply_required")
        session = ctx.session
        result = ctx.result
        reply = ctx.reply
        if not self.deps.side_effects_enabled:
            return StepResult(
                reason="dry_run_skip_commit",
                effects=[
                    MessageEffect(
                        type="commit_turns_and_publish",
                        owner="core",
                        payload={
                            "commit_semantics": EFFECT_COMMIT_SEMANTICS_DRY_RUN_ONLY,
                            "dry_run": True,
                            "side_effects_executed_before_commit": False,
                            "session_id": session.session_id,
                            "route": result.route.value,
                            "publish_outbound": not bool(
                                ctx.extras.get("suppress_outbound")
                            ),
                            "append_assistant_turn": not bool(
                                ctx.extras.get("skip_assistant_turn")
                            ),
                        },
                    )
                ],
            )
        result_producer_owner = trusted_result_producer_owner(ctx) or "core"
        result_owner_denial_reason = ""
        result_decision = await _evaluate_result_owner_execution(
            self.deps,
            ctx,
            owner=result_producer_owner,
        )
        if not result_decision.allowed:
            return _suppress_denied_result(ctx, result_decision)
        append_assistant_turn = not bool(ctx.extras.get("skip_assistant_turn"))
        publish_outbound = not bool(ctx.extras.get("suppress_outbound"))
        fallback_count_changed = _update_persisted_fallback_count(session, result)
        append_assistant_turn_as_effect = append_assistant_turn and (
            self.deps.effect_handlers_enabled
            or effect_handler_opt_in_enabled(
                ctx,
                effect_type="append_assistant_turn",
                owner="core",
            )
        )
        state_transition = session.state == SessionState.IDLE and not bool(
            ctx.extras.get("skip_state_transition")
        )
        state_transition_as_effect = state_transition and (
            self.deps.effect_handlers_enabled
            or effect_handler_opt_in_enabled(
                ctx,
                effect_type="set_session_state",
                owner="core",
            )
        )
        publish_outbound_as_effect = publish_outbound and (
            self.deps.effect_handlers_enabled
            or effect_handler_opt_in_enabled(
                ctx,
                effect_type="publish_outbound",
                owner="core",
            )
        )
        assistant_turn = Turn(
            session_id=session.session_id,
            role=Role.ASSISTANT,
            content=reply.primary_text or result.reply_text,
            tool_calls=list(result.tool_calls),
            citations=list(result.citations),
            trace_id=ctx.event.trace_id,
            metadata=_assistant_turn_metadata(ctx, result),
        )
        if append_assistant_turn:
            if not append_assistant_turn_as_effect:
                append_decision = await _evaluate_result_owner_execution(
                    self.deps,
                    ctx,
                    owner=result_producer_owner,
                )
                if not append_decision.allowed:
                    return _suppress_denied_result(ctx, append_decision)
                await self.deps.session_manager.append_turn(session, assistant_turn)
        if state_transition and not state_transition_as_effect:
            await self.deps.session_manager.set_state(session, SessionState.CHATTING)
        if (
            fallback_count_changed
            and not append_assistant_turn
            and not state_transition
        ):
            save = getattr(self.deps.session_manager, "save", None)
            if callable(save):
                await save(session)
        if publish_outbound and not publish_outbound_as_effect:
            publish_decision = await _evaluate_result_owner_execution(
                self.deps,
                ctx,
                owner=result_producer_owner,
            )
            if publish_decision.allowed:
                await self.deps.bus.publish(
                    self.deps.settings.bus_outbound_stream,
                    reply.model_dump(mode="json"),
                    partition_key=f"{session.tenant_id}:{session.session_id}",
                )
            else:
                _raise_for_retryable_result_owner_denial(publish_decision)
                publish_outbound = False
                ctx.extras["suppress_outbound"] = True
                result_owner_denial_reason = (
                    publish_decision.reason or "result_owner_disabled"
                )
        effects = [
            *(
                [
                    _append_turn_effect(
                        ctx,
                        assistant_turn,
                        effect_type="append_assistant_turn",
                        commit_semantics=EFFECT_COMMIT_SEMANTICS_GATE_BEFORE_SIDE_EFFECT,
                        side_effects_executed_before_commit=False,
                        producer_owner=result_producer_owner,
                    )
                ]
                if append_assistant_turn_as_effect
                else []
            ),
            *(
                [
                    _set_session_state_effect(
                        ctx,
                        state=SessionState.CHATTING,
                    )
                ]
                if state_transition_as_effect
                else []
            ),
            *(
                effect
                for effect in ctx.extras.get("pending_channel_reply_effects", [])
                if isinstance(effect, MessageEffect)
            ),
            *(
                [
                    _publish_outbound_effect(
                        ctx,
                        reply,
                        stream=str(self.deps.settings.bus_outbound_stream),
                        producer_owner=result_producer_owner,
                    )
                ]
                if publish_outbound_as_effect
                else []
            ),
            MessageEffect(
                type="commit_turns_and_publish",
                owner="core",
                payload={
                    "commit_semantics": (
                        EFFECT_COMMIT_SEMANTICS_AUDIT_AFTER_SIDE_EFFECT
                    ),
                    "dry_run": False,
                    "session_id": session.session_id,
                    "route": result.route.value,
                    "publish_outbound": publish_outbound,
                    "publish_outbound_as_effect": publish_outbound_as_effect,
                    "publish_outbound_side_effect_executed_before_commit": (
                        publish_outbound and not publish_outbound_as_effect
                    ),
                    "append_assistant_turn": append_assistant_turn,
                    "append_assistant_turn_as_effect": append_assistant_turn_as_effect,
                    "append_assistant_turn_side_effect_executed_before_commit": (
                        append_assistant_turn and not append_assistant_turn_as_effect
                    ),
                    "state_transition": state_transition,
                    "state_transition_as_effect": state_transition_as_effect,
                    "state_transition_side_effect_executed_before_commit": (
                        state_transition and not state_transition_as_effect
                    ),
                    "side_effects_executed_before_commit": (
                        (append_assistant_turn and not append_assistant_turn_as_effect)
                        or (state_transition and not state_transition_as_effect)
                        or (publish_outbound and not publish_outbound_as_effect)
                    ),
                },
                idempotency_key=(
                    "core:commit_turns_and_publish:"
                    f"{ctx.event.tenant_id}:{ctx.event.session_id}:{ctx.event.trace_id}"
                ),
            ),
        ]
        ctx.extras.pop("pending_channel_reply_effects", None)
        return StepResult(
            action="suppress_outbound" if result_owner_denial_reason else "continue",
            reason=result_owner_denial_reason,
            effects=effects,
        )


class FallbackReplyStep(_BaseCoreStep):
    async def _fallback_reply(self, ctx: PipelineContext, text: str) -> OutboundReply:
        return OutboundReply(
            tenant_id=ctx.event.tenant_id,
            channel=ctx.event.channel,
            adapter_id=ctx.event.adapter_id,
            connection_id=ctx.event.connection_id,
            user_id=ctx.event.user_id,
            session_id=ctx.event.session_id,
            conversation_id=ctx.event.conversation_id,
            external_conversation_id=ctx.event.external_conversation_id,
            canonical_conversation_id=ctx.event.canonical_conversation_id,
            external_user_id=ctx.event.external_user_id,
            external_participant_id=ctx.event.external_participant_id,
            canonical_participant_id=ctx.event.canonical_participant_id,
            type=ReplyType.TEXT,
            segments=[ReplySegment(type=ReplyType.TEXT, content=text)],
            trace_id=ctx.event.trace_id,
        )


def _channel_reply_effects_from_capability_result(
    result: CapabilityResult,
) -> list[MessageEffect]:
    raw_effects = result.metadata.get("channel_reply_effects")
    if not isinstance(raw_effects, list):
        return []
    effects: list[MessageEffect] = []
    for raw in raw_effects:
        if not isinstance(raw, dict):
            continue
        effect_type = str(raw.get("type") or "enqueue_channel_reply").strip()
        owner = str(raw.get("owner") or "").strip()
        payload = raw.get("payload")
        if not effect_type or not owner or not isinstance(payload, dict):
            continue
        effects.append(
            MessageEffect(
                type=effect_type,
                owner=owner,
                payload=dict(payload),
                idempotency_key=str(raw.get("idempotency_key") or "").strip(),
                # Capability engines are trusted core collaborators.  The
                # agent engine binds this value to the registered tool owner,
                # rather than accepting the tool handler's claimed identity.
                # FlowRunner preserves it only for this core dispatch step.
                producer_owner=str(raw.get("producer_owner") or "").strip(),
            )
        )
    return effects


def _append_turn_effect(
    ctx: PipelineContext,
    turn: Turn,
    *,
    effect_type: str,
    commit_semantics: str,
    side_effects_executed_before_commit: bool,
    producer_owner: str = "",
) -> MessageEffect:
    return MessageEffect(
        type=effect_type,
        owner="core",
        payload={
            "commit_semantics": commit_semantics,
            "dry_run": commit_semantics == EFFECT_COMMIT_SEMANTICS_DRY_RUN_ONLY,
            "side_effects_executed_before_commit": side_effects_executed_before_commit,
            "role": turn.role.value,
            "session_id": turn.session_id,
            "trace_id": ctx.event.trace_id,
            "turn": turn.model_dump(mode="json"),
        },
        idempotency_key=(
            f"core:{effect_type}:"
            f"{ctx.event.tenant_id}:{ctx.event.session_id}:{ctx.event.trace_id}"
        ),
        producer_owner=str(producer_owner or "").strip(),
    )


def _set_session_state_effect(
    ctx: PipelineContext,
    *,
    state: SessionState,
) -> MessageEffect:
    return MessageEffect(
        type="set_session_state",
        owner="core",
        payload={
            "commit_semantics": EFFECT_COMMIT_SEMANTICS_GATE_BEFORE_SIDE_EFFECT,
            "dry_run": False,
            "side_effects_executed_before_commit": False,
            "session_id": ctx.event.session_id,
            "trace_id": ctx.event.trace_id,
            "state": state.value,
        },
        idempotency_key=(
            "core:set_session_state:"
            f"{ctx.event.tenant_id}:{ctx.event.session_id}:{ctx.event.trace_id}:{state.value}"
        ),
    )


def _publish_outbound_effect(
    ctx: PipelineContext,
    reply: OutboundReply,
    *,
    stream: str,
    producer_owner: str = "",
) -> MessageEffect:
    payload = reply.model_dump(mode="json")
    return MessageEffect(
        type="publish_outbound",
        owner="core",
        payload={
            "commit_semantics": EFFECT_COMMIT_SEMANTICS_GATE_BEFORE_SIDE_EFFECT,
            "tenant_id": ctx.event.tenant_id,
            "session_id": ctx.event.session_id,
            "trace_id": ctx.event.trace_id,
            "stream": stream,
            "partition_key": (
                f"{ctx.event.tenant_id}:{ctx.event.session_id}"
            ),
            "payload": payload,
        },
        idempotency_key=(
            "core:publish_outbound:"
            f"{ctx.event.tenant_id}:{ctx.event.session_id}:{ctx.event.trace_id}"
        ),
        producer_owner=str(producer_owner or "").strip(),
    )


async def _evaluate_result_owner_execution(
    deps: CoreStepDependencies,
    ctx: PipelineContext,
    *,
    owner: str,
) -> OwnerExecutionDecision:
    gate = deps.owner_gate
    timeout_seconds = deps.owner_gate_timeout_seconds
    if gate is None and deps.hook_runner is not None:
        gate = deps.hook_runner.owner_gate
        if timeout_seconds is None:
            timeout_seconds = deps.hook_runner.owner_gate_timeout_seconds
    return await evaluate_owner_execution(
        gate,
        owner,
        ctx,
        timeout_seconds=(
            DEFAULT_OWNER_GATE_TIMEOUT_SECONDS
            if timeout_seconds is None
            else timeout_seconds
        ),
    )


def _raise_for_retryable_result_owner_denial(decision: OwnerExecutionDecision) -> None:
    if owner_gate_failure_is_retryable(decision.reason):
        raise RetryableProcessingError(
            decision.reason,
            error_type="ResultOwnerGateUnavailable",
        )


def _suppress_denied_result(
    ctx: PipelineContext,
    decision: OwnerExecutionDecision,
) -> StepResult:
    _raise_for_retryable_result_owner_denial(decision)
    ctx.extras["suppress_outbound"] = True
    ctx.extras["skip_assistant_turn"] = True
    ctx.extras.pop("pending_channel_reply_effects", None)
    return StepResult(
        action="suppress_outbound",
        reason=decision.reason or "result_owner_disabled",
        append_assistant_turn=False,
        publish_outbound=False,
    )


def build_core_step_executors(deps: CoreStepDependencies) -> dict[str, Any]:
    return {
        "core.load_session": LoadSessionStep(deps),
        "core.legacy_hooks.before_preprocess": LegacyHookStep(
            deps,
            kind="core.legacy_hooks.before_preprocess",
            hook_point=HookPoint.BEFORE_PREPROCESS,
        ),
        "core.preprocess": PreprocessStep(deps),
        "core.legacy_hooks.after_preprocess": LegacyHookStep(
            deps,
            kind="core.legacy_hooks.after_preprocess",
            hook_point=HookPoint.AFTER_PREPROCESS,
        ),
        "core.append_user_turn": AppendUserTurnStep(deps),
        "core.handoff_short_circuit": HandoffShortCircuitStep(deps),
        "core.input_safety": InputSafetyStep(deps),
        "core.legacy_hooks.before_route": LegacyHookStep(
            deps,
            kind="core.legacy_hooks.before_route",
            hook_point=HookPoint.BEFORE_ROUTE,
        ),
        "core.router_signal_merge": RouterSignalMergeStep(deps),
        "core.route": RouteStep(deps),
        "core.legacy_hooks.after_route": LegacyHookStep(
            deps,
            kind="core.legacy_hooks.after_route",
            hook_point=HookPoint.AFTER_ROUTE,
        ),
        "core.legacy_hooks.before_capability": LegacyHookStep(
            deps,
            kind="core.legacy_hooks.before_capability",
            hook_point=HookPoint.BEFORE_CAPABILITY,
        ),
        "core.capability_dispatch": CapabilityDispatchStep(deps),
        "core.legacy_hooks.after_capability": LegacyHookStep(
            deps,
            kind="core.legacy_hooks.after_capability",
            hook_point=HookPoint.AFTER_CAPABILITY,
        ),
        "core.output_safety": OutputSafetyStep(deps),
        "core.legacy_hooks.before_postprocess": LegacyHookStep(
            deps,
            kind="core.legacy_hooks.before_postprocess",
            hook_point=HookPoint.BEFORE_POSTPROCESS,
        ),
        "core.postprocess": PostprocessStep(deps),
        "core.legacy_hooks.after_postprocess": LegacyHookStep(
            deps,
            kind="core.legacy_hooks.after_postprocess",
            hook_point=HookPoint.AFTER_POSTPROCESS,
        ),
        "core.commit_turns_and_publish": CommitTurnsAndPublishStep(deps),
    }
