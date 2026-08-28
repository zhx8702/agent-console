"""Message flow definitions and compile-time validation.

This module is intentionally runtime-light. It gives the project a typed
surface for the flow concepts described in ``docs/message-flow-orchestration-
plan.md`` without changing the current ``DialogOrchestrator`` execution path.
The first consumer is expected to be a read-only admin view and compiler tests;
FlowRunner can build on these contracts later.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any, Protocol

FLOW_STATUS_ACTIVE = "active"
FLOW_STATUS_DEGRADED = "degraded"
FLOW_STATUS_INVALID = "invalid"

ERROR_POLICIES = {"fail_open", "fail_closed", "degrade", "retry"}
STEP_ACTIONS = {"continue", "stop", "replace_result", "suppress_outbound", "defer"}

KERNEL_INPUTS = {
    "event",
    "trace_id",
}

DEFAULT_COMPATIBLE_FLOW_NAME = "default_compatible_flow"
DEFAULT_COMPATIBLE_FLOW_VERSION = 1
CAPABILITY_DISPATCH_TIMEOUT_SECONDS = 90.0
DEFAULT_REQUIRED_STEP_KINDS = {
    "core.load_session",
    "core.commit_turns_and_publish",
}


@dataclass(frozen=True)
class MessageEffect:
    type: str
    owner: str
    payload: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""
    # Set by FlowRunner from the executing step, never trusted from ordinary
    # plugin output.  The core capability-dispatch step may delegate the
    # producer recorded by a trusted core capability engine (the agent engine
    # binds it to its tool registry). ``owner`` selects the handler;
    # producer_owner controls whether the initiating plugin may still cause
    # the side effect.
    producer_owner: str = ""


@dataclass(frozen=True)
class StepResult:
    action: str = "continue"
    reason: str = ""
    route_label: str = "unknown"
    result: Any | None = None
    finalize: bool = False
    skip_output_safety: bool = False
    append_assistant_turn: bool | None = None
    publish_outbound: bool | None = None
    effects: list[MessageEffect] = field(default_factory=list)
    error: str = ""

    def __post_init__(self) -> None:
        if self.action not in STEP_ACTIONS:
            raise ValueError(f"unknown step action: {self.action}")


class FlowStep(Protocol):
    kind: str
    owner: str
    name: str
    permissions: list[str]
    inputs: set[str]
    outputs: set[str]
    timeout_seconds: float
    error_policy: str

    async def run(self, ctx: Any) -> StepResult:
        ...


@dataclass(frozen=True)
class FlowStepDefinition:
    kind: str
    owner: str
    name: str = ""
    permissions: list[str] = field(default_factory=list)
    inputs: set[str] = field(default_factory=set)
    outputs: set[str] = field(default_factory=set)
    replace_outputs: set[str] = field(default_factory=set)
    timeout_seconds: float = 5.0
    error_policy: str = "fail_closed"
    optional: bool = False
    enabled: bool = True
    schema_version: int = 1

    def __post_init__(self) -> None:
        kind = str(self.kind or "").strip()
        owner = str(self.owner or "").strip()
        if not kind:
            raise ValueError("flow step kind cannot be empty")
        if not owner:
            raise ValueError("flow step owner cannot be empty")
        if self.error_policy not in ERROR_POLICIES:
            raise ValueError(f"unknown error_policy: {self.error_policy}")
        if owner == "core":
            if not kind.startswith("core."):
                raise ValueError("core flow step kind must start with core.")
        elif not kind.startswith(f"plugin.{owner}."):
            raise ValueError(
                f"plugin flow step kind must start with plugin.{owner}."
            )


@dataclass(frozen=True)
class FlowStepSpec:
    id: str
    kind: str
    when: dict[str, Any] = field(default_factory=dict)
    optional: bool | None = None


@dataclass(frozen=True)
class FlowBinding:
    tenant_id: str = ""
    channel: str = "*"
    session_kind: str = ""
    session_id_pattern: str = ""
    message_type: str = "*"
    priority: int = 100
    source: str = "builtin"


@dataclass(frozen=True)
class BuiltinFlowProfile:
    name: str
    version: int
    description: str
    steps: list[FlowStepSpec]
    bindings: list[FlowBinding] = field(default_factory=list)
    required_step_kinds: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class FlowResolveRequest:
    tenant_id: str = ""
    channel: str = "*"
    session_kind: str = ""
    session_id: str = ""
    message_type: str = "*"


@dataclass(frozen=True)
class FlowResolveCandidate:
    profile_name: str
    profile_version: int
    binding: FlowBinding
    matched: bool
    specificity: int = 0
    reason: str = ""


@dataclass(frozen=True)
class FlowResolveResult:
    request: FlowResolveRequest
    profile: BuiltinFlowProfile | None
    binding: FlowBinding | None
    candidates: list[FlowResolveCandidate] = field(default_factory=list)


@dataclass(frozen=True)
class CompiledStep:
    id: str
    kind: str
    owner: str
    name: str
    permissions: list[str]
    inputs: set[str]
    outputs: set[str]
    when: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: float = 5.0
    error_policy: str = "fail_closed"
    optional: bool = False


@dataclass(frozen=True)
class CompiledFlow:
    name: str
    version: int
    steps: list[CompiledStep]
    status: str
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        """Return whether the flow may accept executions.

        ``degraded`` means optional steps were omitted, not that the whole
        flow is disabled.  ``status`` still distinguishes a fully healthy
        flow from a degraded one for operators.
        """

        return self.runnable

    @property
    def runnable(self) -> bool:
        """Return whether the flow is safe to execute.

        A degraded flow only omitted explicitly optional steps.  Treating it
        as inactive would make an optional marketplace plugin a hard runtime
        dependency, which defeats the compiler's degraded-state contract.
        Invalid flows remain non-runnable.
        """

        return self.status in {FLOW_STATUS_ACTIVE, FLOW_STATUS_DEGRADED}


class FlowStepRegistry:
    def __init__(self) -> None:
        self._steps: dict[str, FlowStepDefinition] = {}
        self._owners: dict[str, set[str]] = defaultdict(set)

    def register(self, definition: FlowStepDefinition) -> None:
        self.register_many([definition])

    def register_many(self, definitions: list[FlowStepDefinition]) -> int:
        """Register a definition batch without partial publication.

        Step kinds are executable protocol identifiers.  Rejecting duplicate
        ownership is safer than allowing plugin discovery order to replace a
        core or peer definition while leaving stale owner indexes behind.
        """

        prepared = list(definitions)
        batch_kinds: dict[str, str] = {}
        for definition in prepared:
            kind = str(definition.kind or "").strip()
            owner = str(definition.owner or "").strip()
            if not kind or not owner:
                raise ValueError("flow step definition requires kind and owner")
            if kind != definition.kind or owner != definition.owner:
                raise ValueError("flow step kind and owner must be normalized")
            previous_batch_owner = batch_kinds.get(kind)
            if previous_batch_owner is not None:
                raise ValueError(
                    "duplicate flow step in registration batch: "
                    f"{kind} ({previous_batch_owner}, {owner})"
                )
            batch_kinds[kind] = owner
            existing = self._steps.get(kind)
            if existing is not None:
                raise ValueError(
                    "duplicate flow step registration: "
                    f"{kind} ({existing.owner}, {owner})"
                )

        for definition in prepared:
            self._steps[definition.kind] = definition
            self._owners[definition.owner].add(definition.kind)
        return len(prepared)

    def get(self, kind: str) -> FlowStepDefinition | None:
        return self._steps.get(str(kind or "").strip())

    def list_definitions(self) -> list[FlowStepDefinition]:
        return [self._steps[kind] for kind in sorted(self._steps)]

    def unregister_owner(self, owner: str) -> int:
        owner = str(owner or "").strip()
        if not owner:
            return 0
        kinds = self._owners.pop(owner, set())
        removed = 0
        for kind in kinds:
            if self._steps.pop(kind, None) is not None:
                removed += 1
        return removed


class FlowCompiler:
    def __init__(
        self,
        registry: FlowStepRegistry,
        *,
        kernel_inputs: set[str] | None = None,
        owner_permissions: dict[str, set[str]] | None = None,
    ) -> None:
        self._registry = registry
        self._kernel_inputs = set(kernel_inputs or KERNEL_INPUTS)
        self._owner_permissions = {
            str(owner): set(permissions)
            for owner, permissions in (owner_permissions or {}).items()
        }

    def compile(
        self,
        *,
        name: str,
        version: int = 1,
        steps: list[FlowStepSpec],
        required_step_kinds: set[str] | None = None,
    ) -> CompiledFlow:
        errors: list[str] = []
        warnings: list[str] = []
        compiled: list[CompiledStep] = []
        available = set(self._kernel_inputs)
        signal_writers: dict[str, str] = {}
        seen_ids: set[str] = set()
        seen_kinds: set[str] = set()
        required = set(required_step_kinds or set())

        for spec in steps:
            step_id = str(spec.id or "").strip()
            kind = str(spec.kind or "").strip()
            if not step_id:
                errors.append("step id cannot be empty")
                continue
            if step_id in seen_ids:
                errors.append(f"duplicate step id: {step_id}")
                continue
            seen_ids.add(step_id)
            if not kind:
                errors.append(f"step {step_id} kind cannot be empty")
                continue

            definition = self._registry.get(kind)
            if definition is None or not definition.enabled:
                optional = bool(spec.optional) if spec.optional is not None else False
                message = f"step {step_id} references unavailable kind: {kind}"
                if optional:
                    warnings.append(message)
                else:
                    errors.append(message)
                continue

            optional = definition.optional if spec.optional is None else bool(spec.optional)
            seen_kinds.add(kind)
            missing_permissions = self._missing_permissions(definition)
            if missing_permissions:
                errors.append(
                    f"step {step_id} missing owner permissions: "
                    f"{', '.join(missing_permissions)}"
                )
            missing_inputs = sorted(definition.inputs - available)
            if missing_inputs:
                errors.append(
                    f"step {step_id} missing inputs: {', '.join(missing_inputs)}"
                )

            for output in sorted(definition.outputs):
                if not output.startswith("signals."):
                    continue
                previous = signal_writers.get(output)
                if previous and output not in definition.replace_outputs:
                    errors.append(
                        f"signal output conflict for {output}: {previous} and {step_id}"
                    )
                signal_writers[output] = step_id

            available.update(definition.outputs)
            compiled.append(
                CompiledStep(
                    id=step_id,
                    kind=definition.kind,
                    owner=definition.owner,
                    name=definition.name or definition.kind,
                    permissions=list(definition.permissions),
                    inputs=set(definition.inputs),
                    outputs=set(definition.outputs),
                    when=dict(spec.when or {}),
                    timeout_seconds=definition.timeout_seconds,
                    error_policy=definition.error_policy,
                    optional=optional,
                )
            )

        for kind in sorted(required - seen_kinds):
            errors.append(f"required step kind missing: {kind}")

        if errors:
            status = FLOW_STATUS_INVALID
        elif warnings:
            status = FLOW_STATUS_DEGRADED
        else:
            status = FLOW_STATUS_ACTIVE
        return CompiledFlow(
            name=str(name or "").strip() or "unnamed_flow",
            version=int(version or 1),
            steps=compiled,
            status=status,
            warnings=warnings,
            errors=errors,
        )

    def _missing_permissions(self, definition: FlowStepDefinition) -> list[str]:
        if definition.owner == "core" or not definition.permissions:
            return []
        if not self._owner_permissions:
            return []
        granted = self._owner_permissions.get(definition.owner, set())
        return sorted(set(definition.permissions) - granted)


class FlowResolver:
    def __init__(self, profiles: list[BuiltinFlowProfile]) -> None:
        self._profiles = list(profiles)

    def resolve(self, request: FlowResolveRequest) -> FlowResolveResult:
        candidates: list[FlowResolveCandidate] = []
        matched: list[tuple[int, int, int, BuiltinFlowProfile, FlowBinding]] = []

        for profile_index, profile in enumerate(self._profiles):
            for binding in profile.bindings:
                ok, reason, specificity = self._match_binding(binding, request)
                candidate = FlowResolveCandidate(
                    profile_name=profile.name,
                    profile_version=profile.version,
                    binding=binding,
                    matched=ok,
                    specificity=specificity,
                    reason=reason,
                )
                candidates.append(candidate)
                if ok:
                    matched.append(
                        (
                            int(binding.priority),
                            -specificity,
                            profile_index,
                            profile,
                            binding,
                        )
                    )

        if not matched:
            return FlowResolveResult(
                request=request,
                profile=None,
                binding=None,
                candidates=candidates,
            )
        _, _, _, profile, binding = sorted(matched, key=lambda item: item[:3])[0]
        return FlowResolveResult(
            request=request,
            profile=profile,
            binding=binding,
            candidates=candidates,
        )

    def _match_binding(
        self,
        binding: FlowBinding,
        request: FlowResolveRequest,
    ) -> tuple[bool, str, int]:
        specificity = 0
        for field_name, wildcard in (
            ("tenant_id", ""),
            ("channel", "*"),
            ("session_kind", ""),
            ("message_type", "*"),
        ):
            binding_value = str(getattr(binding, field_name) or "").strip().lower()
            request_value = str(getattr(request, field_name) or "").strip().lower()
            if binding_value in {"", wildcard}:
                continue
            if binding_value != request_value:
                return False, f"{field_name}_mismatch", specificity
            specificity += self._specificity_weight(field_name)

        pattern = str(binding.session_id_pattern or "").strip()
        if pattern:
            if not fnmatchcase(str(request.session_id or ""), pattern):
                return False, "session_id_pattern_mismatch", specificity
            specificity += 1

        return True, "matched", specificity

    @staticmethod
    def _specificity_weight(field_name: str) -> int:
        return {
            "tenant_id": 8,
            "channel": 4,
            "session_kind": 2,
            "message_type": 1,
        }.get(field_name, 0)


def normalize_flow_session_kind(
    *,
    channel: str = "",
    session_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> str:
    metadata = metadata or {}
    value = str(metadata.get("session_kind") or metadata.get("kind") or "").strip().lower()
    if value in {"group", "chatroom", "channel", "guild"}:
        return "group"
    if value in {"private", "direct", "dm", "one_to_one"}:
        return "private"
    if value:
        return value
    if str(channel or "").strip().lower() == "wechat" and str(
        session_id or ""
    ).endswith("@chatroom"):
        return "group"
    if str(session_id or "").strip():
        return "private"
    return ""


def build_default_flow_registry() -> FlowStepRegistry:
    registry = FlowStepRegistry()
    registry.register_many(
        [
            FlowStepDefinition(
                kind="core.load_session",
                owner="core",
                name="Load session",
                inputs={"event"},
                outputs={"session"},
            ),
            FlowStepDefinition(
                kind="core.legacy_hooks.before_preprocess",
                owner="core",
                name="Before preprocess hooks",
                inputs={"event", "session"},
                outputs={"signals.legacy.before_preprocess"},
                error_policy="degrade",
            ),
            FlowStepDefinition(
                kind="core.preprocess",
                owner="core",
                name="Preprocess",
                inputs={"event"},
                outputs={"pre"},
                timeout_seconds=90.0,
                error_policy="degrade",
            ),
            FlowStepDefinition(
                kind="core.legacy_hooks.after_preprocess",
                owner="core",
                name="After preprocess hooks",
                inputs={"event", "session", "pre"},
                outputs={"signals.legacy.after_preprocess"},
                error_policy="degrade",
            ),
            FlowStepDefinition(
                kind="core.append_user_turn",
                owner="core",
                name="Append user turn",
                inputs={"event", "session", "pre"},
                outputs={"effects.append_user_turn"},
            ),
            FlowStepDefinition(
                kind="core.handoff_short_circuit",
                owner="core",
                name="Handoff short-circuit",
                inputs={"event", "session"},
                outputs={"signals.handoff"},
            ),
            FlowStepDefinition(
                kind="core.input_safety",
                owner="core",
                name="Input safety",
                inputs={"pre"},
                outputs={"signals.safety.input"},
                error_policy="fail_closed",
            ),
            FlowStepDefinition(
                kind="core.legacy_hooks.before_route",
                owner="core",
                name="Before route hooks",
                inputs={"event", "session", "pre"},
                outputs={"signals.legacy.before_route"},
                error_policy="degrade",
            ),
            FlowStepDefinition(
                kind="core.router_signal_merge",
                owner="core",
                name="Router signal merge",
                inputs={"event", "session", "pre"},
                outputs={"signals.router"},
                error_policy="fail_open",
            ),
            FlowStepDefinition(
                kind="core.route",
                owner="core",
                name="Route",
                inputs={"event", "session", "pre"},
                outputs={"route"},
                error_policy="degrade",
            ),
            FlowStepDefinition(
                kind="core.legacy_hooks.after_route",
                owner="core",
                name="After route hooks",
                inputs={"event", "session", "pre", "route"},
                outputs={"signals.legacy.after_route"},
                error_policy="degrade",
            ),
            FlowStepDefinition(
                kind="core.legacy_hooks.before_capability",
                owner="core",
                name="Before capability hooks",
                inputs={"event", "session", "pre", "route"},
                outputs={"signals.legacy.before_capability"},
                error_policy="degrade",
            ),
            FlowStepDefinition(
                kind="core.capability_dispatch",
                owner="core",
                name="Capability dispatch",
                inputs={"event", "session", "pre", "route"},
                outputs={"result"},
                timeout_seconds=CAPABILITY_DISPATCH_TIMEOUT_SECONDS,
                error_policy="degrade",
            ),
            FlowStepDefinition(
                kind="core.legacy_hooks.after_capability",
                owner="core",
                name="After capability hooks",
                inputs={"event", "session", "pre", "route", "result"},
                outputs={"signals.legacy.after_capability"},
                error_policy="degrade",
            ),
            FlowStepDefinition(
                kind="core.output_safety",
                owner="core",
                name="Output safety",
                inputs={"result"},
                outputs={"signals.safety.output"},
                error_policy="fail_open",
            ),
            FlowStepDefinition(
                kind="core.legacy_hooks.before_postprocess",
                owner="core",
                name="Before postprocess hooks",
                inputs={"event", "session", "pre", "route", "result"},
                outputs={"signals.legacy.before_postprocess"},
                error_policy="degrade",
            ),
            FlowStepDefinition(
                kind="core.postprocess",
                owner="core",
                name="Postprocess",
                inputs={"result", "session"},
                outputs={"reply"},
            ),
            FlowStepDefinition(
                kind="core.legacy_hooks.after_postprocess",
                owner="core",
                name="After postprocess hooks",
                inputs={"event", "session", "pre", "route", "result", "reply"},
                outputs={"signals.legacy.after_postprocess"},
                error_policy="degrade",
            ),
            FlowStepDefinition(
                kind="core.commit_turns_and_publish",
                owner="core",
                name="Commit turns and publish",
                inputs={"event", "session", "reply"},
                outputs={"effects.commit"},
            ),
        ]
    )
    return registry


def build_default_compatible_flow_specs() -> list[FlowStepSpec]:
    """Return the built-in flow that mirrors DialogOrchestrator._run order."""

    return [
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
        FlowStepSpec(
            id="speaker_portrait_note",
            kind="plugin.speaker_portrait.note",
            optional=True,
        ),
        FlowStepSpec(id="handoff_short_circuit", kind="core.handoff_short_circuit"),
        FlowStepSpec(id="input_safety", kind="core.input_safety"),
        FlowStepSpec(id="before_route_hooks", kind="core.legacy_hooks.before_route"),
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
    ]


def build_default_group_channel_flow_specs() -> list[FlowStepSpec]:
    """Return the target generic group flow for Discord/Feishu-like channels."""

    return [
        FlowStepSpec(id="load_session", kind="core.load_session"),
        FlowStepSpec(id="preprocess", kind="core.preprocess"),
        FlowStepSpec(id="append_user_turn", kind="core.append_user_turn"),
        FlowStepSpec(
            id="speaker_portrait_note",
            kind="plugin.speaker_portrait.note",
            optional=True,
        ),
        FlowStepSpec(id="handoff_short_circuit", kind="core.handoff_short_circuit"),
        FlowStepSpec(id="input_safety", kind="core.input_safety"),
        FlowStepSpec(id="memory_control_intents", kind="plugin.memory.control_intents"),
        FlowStepSpec(id="command_dispatch", kind="plugin.commands.dispatch"),
        FlowStepSpec(id="repeater", kind="plugin.repeater.detect"),
        FlowStepSpec(id="moderation_inspect", kind="plugin.moderation.inspect_input"),
        FlowStepSpec(id="route", kind="core.route"),
        FlowStepSpec(
            id="persona_skill_enrich",
            kind="plugin.persona_extract.skill_enrich",
        ),
        FlowStepSpec(id="memory_load", kind="plugin.memory.load"),
        FlowStepSpec(
            id="speaker_portrait_enrich",
            kind="plugin.speaker_portrait.enrich",
            optional=True,
        ),
        FlowStepSpec(id="credits_query_command", kind="plugin.credits.query_command"),
        FlowStepSpec(
            id="moderation_enforce",
            kind="plugin.moderation.enforce_input",
        ),
        FlowStepSpec(id="credits_reserve", kind="plugin.credits.reserve"),
        FlowStepSpec(id="capability", kind="core.capability_dispatch"),
        FlowStepSpec(id="credits_settle", kind="plugin.credits.settle"),
        FlowStepSpec(
            id="moderation_decorate",
            kind="plugin.moderation.decorate_output",
        ),
        FlowStepSpec(id="draw_postprocess", kind="plugin.draw.postprocess_result"),
        FlowStepSpec(id="output_safety", kind="core.output_safety"),
        FlowStepSpec(id="postprocess", kind="core.postprocess"),
        FlowStepSpec(id="memory_save", kind="plugin.memory.save"),
        FlowStepSpec(id="commit", kind="core.commit_turns_and_publish"),
    ]


def build_default_private_channel_flow_specs() -> list[FlowStepSpec]:
    """Return the explicit channel-neutral direct-conversation flow.

    It intentionally preserves the established hook extension points while
    making private traffic an explicit binding instead of a rollout fallback.
    Channel plugins can therefore apply their own reply policy without the
    platform router knowing about WeChat or any other adapter.
    """

    return build_default_compatible_flow_specs()


def build_default_wechat_group_flow_specs() -> list[FlowStepSpec]:
    """Return the target WeChat group flow with wxbot channel-specific steps."""

    return [
        FlowStepSpec(id="load_session", kind="core.load_session"),
        FlowStepSpec(id="wxbot_normalize", kind="plugin.wxbot.normalize_event"),
        FlowStepSpec(id="preprocess", kind="core.preprocess"),
        FlowStepSpec(id="append_user_turn", kind="core.append_user_turn"),
        FlowStepSpec(
            id="speaker_portrait_note",
            kind="plugin.speaker_portrait.note",
            optional=True,
        ),
        FlowStepSpec(id="handoff_short_circuit", kind="core.handoff_short_circuit"),
        FlowStepSpec(id="input_safety", kind="core.input_safety"),
        FlowStepSpec(id="wxbot_user_ban_pre_command", kind="plugin.wxbot.user_ban_pre_command"),
        FlowStepSpec(id="command_dispatch", kind="plugin.commands.dispatch"),
        FlowStepSpec(id="wxbot_user_ban_gate", kind="plugin.wxbot.user_ban_gate"),
        FlowStepSpec(id="repeater", kind="plugin.repeater.detect"),
        FlowStepSpec(
            id="tibo_reset_intent",
            kind="plugin.tibo_reset.intent",
            optional=True,
        ),
        FlowStepSpec(
            id="persona_skill_enrich",
            kind="plugin.persona_extract.skill_enrich",
        ),
        FlowStepSpec(id="wxbot_reply_policy", kind="plugin.wxbot.reply_policy"),
        FlowStepSpec(id="memory_control_intents", kind="plugin.memory.control_intents"),
        FlowStepSpec(id="moderation_inspect", kind="plugin.moderation.inspect_input"),
        FlowStepSpec(
            id="wxbot_agent_scope_enrich",
            kind="plugin.wxbot.agent_scope_enrich",
        ),
        FlowStepSpec(id="route", kind="core.route"),
        FlowStepSpec(
            id="wxbot_voice_profile_enrich",
            kind="plugin.wxbot.voice_profile_enrich",
        ),
        FlowStepSpec(id="memory_load", kind="plugin.memory.load"),
        FlowStepSpec(
            id="speaker_portrait_enrich",
            kind="plugin.speaker_portrait.enrich",
            optional=True,
        ),
        FlowStepSpec(
            id="wxbot_group_context_load",
            kind="plugin.wxbot.group_context_load",
        ),
        FlowStepSpec(id="credits_query_command", kind="plugin.credits.query_command"),
        FlowStepSpec(
            id="moderation_enforce",
            kind="plugin.moderation.enforce_input",
        ),
        FlowStepSpec(id="credits_reserve", kind="plugin.credits.reserve"),
        FlowStepSpec(id="capability", kind="core.capability_dispatch"),
        FlowStepSpec(id="credits_settle", kind="plugin.credits.settle"),
        FlowStepSpec(
            id="moderation_decorate",
            kind="plugin.moderation.decorate_output",
        ),
        FlowStepSpec(id="draw_postprocess", kind="plugin.draw.postprocess_result"),
        FlowStepSpec(id="output_safety", kind="core.output_safety"),
        FlowStepSpec(id="postprocess", kind="core.postprocess"),
        FlowStepSpec(id="wxbot_outbound_policy", kind="plugin.wxbot.outbound_policy"),
        FlowStepSpec(id="memory_save", kind="plugin.memory.save"),
        FlowStepSpec(id="commit", kind="core.commit_turns_and_publish"),
    ]


def build_builtin_flow_profiles() -> list[BuiltinFlowProfile]:
    return [
        BuiltinFlowProfile(
            name=DEFAULT_COMPATIBLE_FLOW_NAME,
            version=DEFAULT_COMPATIBLE_FLOW_VERSION,
            description="Current DialogOrchestrator-compatible flow.",
            steps=build_default_compatible_flow_specs(),
            bindings=[FlowBinding(priority=1000)],
            required_step_kinds=DEFAULT_REQUIRED_STEP_KINDS,
        ),
        BuiltinFlowProfile(
            name="default_private_channel_flow",
            version=1,
            description="Explicit platform flow for direct conversations.",
            steps=build_default_private_channel_flow_specs(),
            bindings=[
                FlowBinding(
                    channel="*",
                    session_kind="private",
                    priority=300,
                )
            ],
            required_step_kinds=DEFAULT_REQUIRED_STEP_KINDS,
        ),
        BuiltinFlowProfile(
            name="default_group_channel_flow",
            version=1,
            description="Target generic group flow for non-WeChat channels.",
            steps=build_default_group_channel_flow_specs(),
            bindings=[
                FlowBinding(
                    channel="*",
                    session_kind="group",
                    priority=200,
                )
            ],
            required_step_kinds={
                "core.load_session",
                "plugin.commands.dispatch",
                "core.commit_turns_and_publish",
            },
        ),
        BuiltinFlowProfile(
            name="default_wechat_group_flow",
            version=1,
            description="Target WeChat group flow with wxbot channel steps.",
            steps=build_default_wechat_group_flow_specs(),
            bindings=[
                FlowBinding(
                    channel="wechat",
                    session_kind="group",
                    priority=100,
                )
            ],
            required_step_kinds={
                "core.load_session",
                "plugin.wxbot.reply_policy",
                "plugin.wxbot.outbound_policy",
                "core.commit_turns_and_publish",
            },
        ),
    ]


def compile_default_compatible_flow(
    registry: FlowStepRegistry | None = None,
    *,
    owner_permissions: dict[str, set[str]] | None = None,
) -> CompiledFlow:
    compiler = FlowCompiler(
        registry or build_default_flow_registry(),
        owner_permissions=owner_permissions,
    )
    return compiler.compile(
        name=DEFAULT_COMPATIBLE_FLOW_NAME,
        version=DEFAULT_COMPATIBLE_FLOW_VERSION,
        steps=build_default_compatible_flow_specs(),
        required_step_kinds=DEFAULT_REQUIRED_STEP_KINDS,
    )


def compile_builtin_flows(
    registry: FlowStepRegistry | None = None,
    *,
    owner_permissions: dict[str, set[str]] | None = None,
) -> list[tuple[BuiltinFlowProfile, CompiledFlow]]:
    flow_registry = registry or build_default_flow_registry()
    compiler = FlowCompiler(flow_registry, owner_permissions=owner_permissions)
    return [
        (
            profile,
            compiler.compile(
                name=profile.name,
                version=profile.version,
                steps=profile.steps,
                required_step_kinds=profile.required_step_kinds,
            ),
        )
        for profile in build_builtin_flow_profiles()
    ]


def resolve_builtin_flow(request: FlowResolveRequest) -> FlowResolveResult:
    return FlowResolver(build_builtin_flow_profiles()).resolve(request)
