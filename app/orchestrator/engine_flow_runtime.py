"""Flow runtime, shadow execution, effect wiring, and trace snapshots.

Keeping this concern outside :mod:`app.orchestrator.engine` makes the legacy
dialog pipeline and the compiled-flow runtime independently understandable.
The coordinator accepts typed ports and owns all flow-specific mutable state.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import cast

from app.common.capability import CapabilityEngine
from app.common.config import Settings
from app.common.logging import get_logger
from app.common.types import InboundEvent, RouteType, channel_id_value
from app.infra.metrics import ROUTE_DECISIONS
from app.orchestrator.core_steps import CoreStepDependencies, build_core_step_executors
from app.orchestrator.effect_handlers import EffectDispatcher, EffectHandlerRegistry
from app.orchestrator.effects import (
    AuditedEffectCommitter,
    EffectCommitter,
    InMemoryEffectCommitter,
    RedisEffectCommitter,
)
from app.orchestrator.flow import (
    BuiltinFlowProfile,
    CompiledFlow,
    FlowCompiler,
    FlowResolveRequest,
    FlowStep,
    FlowStepRegistry,
    build_builtin_flow_profiles,
    build_default_flow_registry,
    normalize_flow_session_kind,
    resolve_builtin_flow,
)
from app.orchestrator.flow_runtime_config import flow_runtime_allowed
from app.orchestrator.outcome import PermanentProcessingError, ProcessingOutcome
from app.orchestrator.owner_gate import (
    DEFAULT_OWNER_GATE_TIMEOUT_SECONDS,
    OwnerExecutionGate,
)
from app.orchestrator.pipeline import PipelineContext
from app.orchestrator.ports import (
    FlowSessionPort,
    OrchestratorBusPort,
    PostprocessorPort,
    PreprocessorPort,
    RouterPort,
    SafetyPort,
)
from app.orchestrator.runner import FlowRunner, FlowRunResult
from app.orchestrator.trace_store import write_flow_trace_snapshot
from app.plugin.hooks import HookRunner
from app.reliability.message_store import MessageReliabilityStore

logger = get_logger(__name__)

SessionLockFactory = Callable[[InboundEvent], AbstractAsyncContextManager[None]]
SuppressionReasonResolver = Callable[[PipelineContext], str]


@dataclass(frozen=True)
class FlowRuntimePorts:
    """Typed core dependencies shared by runtime and shadow flow steps."""

    session_manager: FlowSessionPort
    preprocessor: PreprocessorPort
    router: RouterPort
    safety: SafetyPort
    postprocessor: PostprocessorPort
    capabilities: dict[RouteType, CapabilityEngine]
    bus: OrchestratorBusPort
    settings: Settings
    hooks: HookRunner


class FlowRuntimeCoordinator:
    """Compile and execute configured flows without owning the legacy pipeline."""

    def __init__(
        self,
        ports: FlowRuntimePorts,
        *,
        session_lock: SessionLockFactory,
        suppression_reason: SuppressionReasonResolver,
        step_registry: FlowStepRegistry | None = None,
        owner_permissions: dict[str, set[str]] | None = None,
        step_executors: dict[str, FlowStep] | None = None,
        effect_handler_registry: EffectHandlerRegistry | None = None,
        message_store: MessageReliabilityStore | None = None,
        owner_gate: OwnerExecutionGate | None = None,
        owner_gate_timeout_seconds: float | None = None,
    ) -> None:
        self._ports = ports
        self._session_lock = session_lock
        self._suppression_reason = suppression_reason
        self._step_registry = step_registry
        self._owner_permissions = owner_permissions
        self._step_executors = step_executors if step_executors is not None else {}
        self._effect_handler_registry = effect_handler_registry
        self._message_store = message_store
        inherited_gate = getattr(ports.hooks, "owner_gate", None)
        self._owner_gate = owner_gate if owner_gate is not None else inherited_gate
        inherited_timeout = getattr(
            ports.hooks,
            "owner_gate_timeout_seconds",
            DEFAULT_OWNER_GATE_TIMEOUT_SECONDS,
        )
        self._owner_gate_timeout_seconds = (
            inherited_timeout if owner_gate_timeout_seconds is None else owner_gate_timeout_seconds
        )
        self.last_shadow_result: FlowRunResult | None = None
        self.last_runtime_result: FlowRunResult | None = None

    async def run(self, event: InboundEvent) -> ProcessingOutcome:
        settings = self._ports.settings
        flow_name = settings.orchestrator_flow_runtime_name or "default_compatible_flow"
        profile = self._resolve_flow_profile(
            flow_name=flow_name,
            event=event,
            log_prefix="orchestrator.flow_runtime",
        )
        if profile is None:
            raise PermanentProcessingError(
                f"flow_runtime_profile_not_found:{flow_name}",
                error_type="FlowConfigurationError",
            )
        allowed, reason = self._flow_runtime_allowed(flow_name, profile.name)
        if not allowed:
            logger.warning(
                "orchestrator.flow_runtime.not_allowed",
                requested_flow=flow_name,
                resolved_flow=profile.name,
                reason=reason,
                trace_id=event.trace_id,
                session_id=event.session_id,
            )
            raise PermanentProcessingError(
                f"flow_runtime_not_allowed:{reason}",
                error_type="FlowConfigurationError",
            )

        flow = self._compile(profile)
        if not flow.runnable:
            raise PermanentProcessingError(
                f"flow_runtime_inactive:{flow.name}:{flow.status}",
                error_type="FlowConfigurationError",
            )

        effect_handlers_enabled = settings.orchestrator_flow_effect_handlers_enabled
        transactional_intent_mode = bool(
            self._message_store is not None and self._message_store.stage_active
        )
        deferred_effect_handlers = (
            self._transactional_effect_handler_keys() if transactional_intent_mode else set()
        )
        effect_handler_allowlist = self._effect_handler_allowlist()
        effect_handlers_global = effect_handlers_enabled and not effect_handler_allowlist
        deps = self._core_dependencies(
            hook_runner=self._ports.hooks,
            hooks_enabled=True,
            side_effects_enabled=True,
            capability_dispatch_enabled=True,
            faq_preview_enabled=True,
            effect_handlers_enabled=(effect_handlers_global and not transactional_intent_mode),
        )
        executors = self._core_step_executors(deps)
        executors.update(self._step_executors)
        ctx = self._flow_context(event)
        if transactional_intent_mode and deferred_effect_handlers:
            selectors = [
                effect_type if owner == "channel" else f"{owner}:{effect_type}"
                for owner, effect_type in sorted(deferred_effect_handlers)
            ]
            ctx.signals.setdefault("effects", {})["handler_opt_in"] = selectors
        async with self._session_lock(event):
            effect_committer = (
                None if transactional_intent_mode else self._build_effect_committer(dry_run=False)
            )
            effect_dispatcher = (
                None
                if transactional_intent_mode
                else self._build_effect_dispatcher(effect_committer)
            )
            if (
                effect_handlers_enabled
                and not transactional_intent_mode
                and effect_dispatcher is None
            ):
                raise PermanentProcessingError(
                    "flow_effect_handlers_require_committer_and_registry",
                    error_type="FlowConfigurationError",
                )
            result = await FlowRunner(
                executors,
                effect_committer=effect_committer,
                effect_dispatcher=effect_dispatcher,
                effect_handlers_enabled=(effect_handlers_enabled and not transactional_intent_mode),
                effect_intent_recorder=(self._message_store if transactional_intent_mode else None),
                deferred_effect_handlers=deferred_effect_handlers,
                owner_gate=self._owner_gate,
                owner_gate_timeout_seconds=self._owner_gate_timeout_seconds,
            ).run(flow, ctx)
        self.last_runtime_result = result
        await self._write_trace_snapshot(result, mode="runtime")
        if not result.ok:
            return ProcessingOutcome.retryable_failure(
                reason=result.error or result.stop_reason or "flow_runtime_failed",
                error_type="FlowRuntimeFailure",
            )

        route_label = self._runtime_route_label(ctx)
        ROUTE_DECISIONS.labels(tenant=event.tenant_id, route=route_label).inc()
        logger.info(
            "orchestrator.flow_runtime.completed",
            flow_name=result.flow_name,
            flow_version=result.flow_version,
            status=result.status,
            step_count=len(result.steps),
            route_label=route_label,
            trace_id=event.trace_id,
            session_id=event.session_id,
        )
        if bool(ctx.extras.get("suppress_outbound")) or result.status in {
            "stopped",
            "deferred",
        }:
            return ProcessingOutcome.intentionally_suppressed(
                route_label=route_label,
                reason=result.stop_reason or self._suppression_reason(ctx),
            )
        return ProcessingOutcome.completed(route_label=route_label)

    async def run_shadow(self, event: InboundEvent, route_label: str) -> None:
        settings = self._ports.settings
        if not settings.orchestrator_flow_shadow_enabled:
            return
        flow_name = settings.orchestrator_flow_shadow_name or "default_compatible_flow"
        self.last_shadow_result = None
        profile = self._resolve_flow_profile(
            flow_name=flow_name,
            event=event,
            log_prefix="orchestrator.flow_shadow",
        )
        if profile is None:
            return
        mode = settings.orchestrator_flow_shadow_mode or "noop"
        try:
            flow = self._compile(profile)
            if not flow.runnable:
                logger.warning(
                    "orchestrator.flow_shadow.inactive_flow",
                    flow_name=flow.name,
                    flow_version=flow.version,
                    status=flow.status,
                    warnings=flow.warnings,
                    errors=flow.errors,
                    trace_id=event.trace_id,
                    session_id=event.session_id,
                )
                return
            if mode == "noop":
                result = await FlowRunner(shadow=True).run(
                    flow,
                    self._flow_context(event),
                )
            elif mode == "core_dry_run":
                effect_handler_allowlist = self._effect_handler_allowlist()
                effect_handlers_global = (
                    settings.orchestrator_flow_effect_handlers_enabled
                    and not effect_handler_allowlist
                )
                deps = self._core_dependencies(
                    hook_runner=None,
                    hooks_enabled=False,
                    side_effects_enabled=False,
                    capability_dispatch_enabled=False,
                    faq_preview_enabled=(settings.orchestrator_flow_shadow_core_preview_enabled),
                    effect_handlers_enabled=effect_handlers_global,
                )
                effect_committer = (
                    self._build_effect_committer(dry_run=True)
                    if settings.orchestrator_flow_shadow_effect_dry_run_enabled
                    else None
                )
                result = await FlowRunner(
                    self._shadow_executors(flow, deps),
                    shadow=True,
                    shadow_skip_reasons=self._shadow_skip_reasons(flow),
                    effect_committer=effect_committer,
                    effect_dispatcher=self._build_effect_dispatcher(effect_committer),
                    effect_handlers_enabled=(settings.orchestrator_flow_effect_handlers_enabled),
                    effect_dry_run=True,
                    owner_gate=self._owner_gate,
                    owner_gate_timeout_seconds=self._owner_gate_timeout_seconds,
                ).run(
                    flow,
                    self._flow_context(event),
                )
            else:
                logger.warning(
                    "orchestrator.flow_shadow.unsupported_mode",
                    mode=mode,
                    flow_name=flow_name,
                    trace_id=event.trace_id,
                )
                return
            self.last_shadow_result = result
            await self._write_trace_snapshot(result, mode="shadow")
            logger.info(
                "orchestrator.flow_shadow.completed",
                flow_name=result.flow_name,
                flow_version=result.flow_version,
                mode=mode,
                status=result.status,
                step_count=len(result.steps),
                route_label=route_label,
                trace_id=event.trace_id,
                session_id=event.session_id,
            )
        except Exception as exc:
            logger.warning(
                "orchestrator.flow_shadow.failed",
                flow_name=flow_name,
                route_label=route_label,
                trace_id=event.trace_id,
                session_id=event.session_id,
                error=str(exc),
            )

    def _compile(self, profile: BuiltinFlowProfile) -> CompiledFlow:
        return FlowCompiler(
            self._step_registry or build_default_flow_registry(),
            owner_permissions=self._owner_permissions,
        ).compile(
            name=profile.name,
            version=profile.version,
            steps=profile.steps,
            required_step_kinds=profile.required_step_kinds,
        )

    def _resolve_flow_profile(
        self,
        *,
        flow_name: str,
        event: InboundEvent,
        log_prefix: str,
    ) -> BuiltinFlowProfile | None:
        if flow_name == "auto":
            request = FlowResolveRequest(
                tenant_id=event.tenant_id,
                channel=channel_id_value(event.channel),
                session_kind=normalize_flow_session_kind(
                    channel=channel_id_value(event.channel),
                    session_id=event.session_id,
                    metadata=event.metadata,
                ),
                session_id=event.session_id,
                message_type=event.message.type.value,
            )
            resolved = resolve_builtin_flow(request)
            if resolved.profile is None:
                logger.warning(
                    f"{log_prefix}.unresolved_flow",
                    trace_id=event.trace_id,
                    session_id=event.session_id,
                    channel=request.channel,
                    session_kind=request.session_kind,
                    message_type=request.message_type,
                )
            return resolved.profile
        profile = next(
            (
                candidate
                for candidate in build_builtin_flow_profiles()
                if candidate.name == flow_name
            ),
            None,
        )
        if profile is None:
            logger.warning(
                f"{log_prefix}.unsupported_flow",
                flow_name=flow_name,
                trace_id=event.trace_id,
            )
        return profile

    def _flow_runtime_allowed(
        self,
        requested_name: str,
        resolved_name: str,
    ) -> tuple[bool, str]:
        return flow_runtime_allowed(
            self._ports.settings,
            requested_name,
            resolved_name,
        )

    def _core_dependencies(
        self,
        *,
        hook_runner: HookRunner | None,
        hooks_enabled: bool,
        side_effects_enabled: bool,
        capability_dispatch_enabled: bool,
        faq_preview_enabled: bool,
        effect_handlers_enabled: bool,
    ) -> CoreStepDependencies:
        ports = self._ports
        return CoreStepDependencies(
            session_manager=ports.session_manager,
            preprocessor=ports.preprocessor,
            router=ports.router,
            safety=ports.safety,
            postprocessor=ports.postprocessor,
            capabilities=ports.capabilities,
            bus=ports.bus,
            settings=ports.settings,
            hook_runner=hook_runner,
            hooks_enabled=hooks_enabled,
            side_effects_enabled=side_effects_enabled,
            capability_dispatch_enabled=capability_dispatch_enabled,
            faq_preview_enabled=faq_preview_enabled,
            effect_handlers_enabled=effect_handlers_enabled,
        )

    @staticmethod
    def _core_step_executors(deps: CoreStepDependencies) -> dict[str, FlowStep]:
        return cast(dict[str, FlowStep], build_core_step_executors(deps))

    @staticmethod
    def _runtime_route_label(ctx: PipelineContext) -> str:
        if ctx.result is not None:
            return str(ctx.result.route.value)
        if ctx.route is not None:
            return str(ctx.route.type.value)
        return "unknown"

    def _transactional_effect_handler_keys(self) -> set[tuple[str, str]]:
        registry = self._effect_handler_registry
        if registry is None:
            return set()
        keys: set[tuple[str, str]] = set()
        for item in registry.list_handlers():
            owner = str(item.get("owner") or "").strip()
            effect_type = str(item.get("type") or "").strip()
            if owner and owner != "core" and effect_type:
                keys.add((owner, effect_type))
        return keys

    async def _write_trace_snapshot(self, result: FlowRunResult, *, mode: str) -> None:
        settings = self._ports.settings
        if not settings.orchestrator_flow_trace_snapshot_enabled:
            return
        try:
            from app.infra.redis_client import get_redis

            await asyncio.wait_for(
                write_flow_trace_snapshot(
                    get_redis(),
                    result,
                    mode=mode,
                    ttl_seconds=settings.orchestrator_flow_trace_snapshot_ttl_seconds,
                    key_prefix=settings.orchestrator_flow_trace_snapshot_key_prefix,
                ),
                timeout=settings.orchestrator_flow_trace_snapshot_timeout_seconds,
            )
        except Exception as exc:
            logger.warning(
                "orchestrator.flow_trace_snapshot.write_failed",
                mode=mode,
                trace_id=result.trace_id,
                error=str(exc),
            )

    def _build_effect_committer(self, *, dry_run: bool) -> EffectCommitter | None:
        settings = self._ports.settings
        backend = settings.orchestrator_flow_effect_commit_backend.strip().lower()
        committer: EffectCommitter | None
        if backend == "redis":
            from app.infra.redis_client import get_redis

            committer = RedisEffectCommitter(
                get_redis(),
                key_prefix=settings.orchestrator_flow_effect_commit_key_prefix,
                ttl_seconds=settings.orchestrator_flow_effect_commit_ttl_seconds,
                log_stream=(settings.orchestrator_flow_effect_commit_stream or None),
            )
        elif backend == "memory" or dry_run:
            committer = InMemoryEffectCommitter()
        elif backend in {"", "none"}:
            committer = None
        else:
            logger.warning("orchestrator.effect_commit.unsupported_backend", backend=backend)
            committer = None
        if committer is None:
            return None
        log_backend = settings.orchestrator_flow_effect_log_backend.strip().lower()
        if log_backend not in {"postgres", "postgresql", "sql"}:
            return committer
        from app.orchestrator.effect_log import PostgresEffectLog

        failure_policy = settings.orchestrator_flow_effect_log_failure_policy.strip().lower()
        return AuditedEffectCommitter(
            committer,
            PostgresEffectLog(),
            fail_closed=failure_policy != "fail_open",
        )

    def _build_effect_dispatcher(
        self,
        committer: EffectCommitter | None,
    ) -> EffectDispatcher | None:
        if (
            committer is None
            or self._effect_handler_registry is None
            or not self._ports.settings.orchestrator_flow_effect_handlers_enabled
        ):
            return None
        allowlist = self._effect_handler_allowlist()
        return EffectDispatcher(
            self._effect_handler_registry,
            committer,
            enabled_handlers=allowlist or True,
            owner_gate=self._owner_gate,
            owner_gate_timeout_seconds=self._owner_gate_timeout_seconds,
        )

    def _effect_handler_allowlist(self) -> list[str]:
        raw = self._ports.settings.orchestrator_flow_effect_handler_allowlist.strip()
        return [item.strip() for chunk in raw.split(",") for item in chunk.split() if item.strip()]

    def _flow_context(self, event: InboundEvent) -> PipelineContext:
        ctx = PipelineContext(event=event, trace_id=event.trace_id)
        allowlist = self._effect_handler_allowlist()
        if allowlist:
            ctx.extras["enabled_handlers"] = list(allowlist)
            ctx.signals.setdefault("effects", {})["enabled_handlers"] = list(allowlist)
        return ctx

    def _shadow_executors(
        self,
        flow: CompiledFlow,
        deps: CoreStepDependencies,
    ) -> dict[str, FlowStep]:
        executors = self._core_step_executors(deps)
        if not self._ports.settings.orchestrator_flow_shadow_plugin_dry_run_enabled:
            return executors
        for step in flow.steps:
            if step.owner == "core" or self._flow_step_has_effects(step.outputs):
                continue
            executor = self._step_executors.get(step.kind)
            if executor is not None:
                executors[step.kind] = executor
        return executors

    def _shadow_skip_reasons(self, flow: CompiledFlow) -> dict[str, str]:
        reasons: dict[str, str] = {}
        for step in flow.steps:
            if step.owner == "core":
                continue
            if self._flow_step_has_effects(step.outputs):
                reasons[step.kind] = "effectful_shadow_skip"
            elif (
                self._ports.settings.orchestrator_flow_shadow_plugin_dry_run_enabled
                and step.kind not in self._step_executors
            ):
                reasons[step.kind] = "missing_plugin_executor"
        return reasons

    @staticmethod
    def _flow_step_has_effects(outputs: set[str]) -> bool:
        return any(output.startswith("effects.") for output in outputs)
