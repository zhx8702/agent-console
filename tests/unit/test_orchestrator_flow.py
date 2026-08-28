from __future__ import annotations

import pytest

from app.common.types import Channel, InboundEvent, Message
from app.orchestrator.flow import (
    CAPABILITY_DISPATCH_TIMEOUT_SECONDS,
    FLOW_STATUS_DEGRADED,
    FLOW_STATUS_INVALID,
    FlowCompiler,
    FlowResolver,
    FlowResolveRequest,
    FlowStepDefinition,
    FlowStepRegistry,
    FlowStepSpec,
    StepResult,
    build_builtin_flow_profiles,
    build_default_compatible_flow_specs,
    build_default_flow_registry,
    build_default_wechat_group_flow_specs,
    compile_builtin_flows,
    compile_default_compatible_flow,
    normalize_flow_session_kind,
    resolve_builtin_flow,
)
from app.orchestrator.pipeline import PipelineContext


def test_flow_compiler_compiles_active_linear_flow() -> None:
    registry = build_default_flow_registry()
    compiler = FlowCompiler(registry)

    flow = compiler.compile(
        name="default_compatible_flow",
        steps=build_default_compatible_flow_specs(),
        required_step_kinds={"core.load_session", "core.commit_turns_and_publish"},
    )

    assert flow.status == FLOW_STATUS_DEGRADED
    assert flow.errors == []
    assert [step.id for step in flow.steps] == [
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


def test_compile_default_compatible_flow_is_active() -> None:
    flow = compile_default_compatible_flow()

    assert flow.status == FLOW_STATUS_DEGRADED
    assert flow.name == "default_compatible_flow"
    assert flow.version == 1
    assert flow.steps[0].kind == "core.load_session"
    assert flow.steps[-1].kind == "core.commit_turns_and_publish"


def test_core_capability_dispatch_has_extended_timeout() -> None:
    flow = compile_default_compatible_flow()
    capability = next(
        step for step in flow.steps if step.kind == "core.capability_dispatch"
    )

    assert capability.timeout_seconds == CAPABILITY_DISPATCH_TIMEOUT_SECONDS
    assert capability.timeout_seconds > 5.0


def test_builtin_flow_profiles_cover_private_and_target_group_flows() -> None:
    profiles = build_builtin_flow_profiles()

    assert [profile.name for profile in profiles] == [
        "default_compatible_flow",
        "default_private_channel_flow",
        "default_group_channel_flow",
        "default_wechat_group_flow",
    ]
    assert profiles[1].bindings[0].session_kind == "private"
    assert profiles[2].bindings[0].session_kind == "group"
    assert profiles[3].bindings[0].channel == "wechat"


def test_builtin_flow_resolver_uses_explicit_private_flow_without_fallback() -> None:
    result = resolve_builtin_flow(
        FlowResolveRequest(
            channel="wechat",
            session_kind="private",
            session_id="wxid_contact",
            message_type="text",
        )
    )

    assert result.profile is not None
    assert result.profile.name == "default_private_channel_flow"
    assert result.binding is not None
    assert result.binding.session_kind == "private"


def test_builtin_flow_resolver_prefers_wechat_group_flow() -> None:
    result = resolve_builtin_flow(
        FlowResolveRequest(
            channel="wechat",
            session_kind="group",
            session_id="room@chatroom",
            message_type="text",
        )
    )

    assert result.profile is not None
    assert result.profile.name == "default_wechat_group_flow"
    assert result.binding is not None
    assert result.binding.channel == "wechat"


def test_wechat_group_ban_gate_runs_before_repeater() -> None:
    step_ids = [step.id for step in build_default_wechat_group_flow_specs()]

    assert step_ids.index("command_dispatch") < step_ids.index("wxbot_user_ban_gate")
    assert step_ids.index("wxbot_user_ban_gate") < step_ids.index("repeater")


def test_wechat_memory_save_runs_after_final_outbound_policy() -> None:
    step_ids = [step.id for step in build_default_wechat_group_flow_specs()]

    assert step_ids.index("wxbot_outbound_policy") < step_ids.index("memory_save")
    assert step_ids.index("memory_save") < step_ids.index("commit")


def test_builtin_flow_resolver_uses_group_channel_flow_for_discord_group() -> None:
    result = resolve_builtin_flow(
        FlowResolveRequest(
            channel="discord",
            session_kind="group",
            session_id="guild-channel",
            message_type="text",
        )
    )

    assert result.profile is not None
    assert result.profile.name == "default_group_channel_flow"


def test_builtin_flow_resolver_uses_private_flow_for_direct_session() -> None:
    result = resolve_builtin_flow(
        FlowResolveRequest(
            channel="web",
            session_kind="private",
            session_id="web-session",
            message_type="text",
        )
    )

    assert result.profile is not None
    assert result.profile.name == "default_private_channel_flow"
    assert result.binding is not None
    assert result.binding.priority == 300


def test_builtin_flow_resolver_keeps_compatible_flow_for_unmodelled_shape() -> None:
    result = resolve_builtin_flow(
        FlowResolveRequest(
            channel="system",
            session_kind="broadcast",
            session_id="broadcast-1",
            message_type="event",
        )
    )

    assert result.profile is not None
    assert result.profile.name == "default_compatible_flow"
    assert result.binding is not None
    assert result.binding.priority == 1000


def test_flow_resolver_supports_session_id_pattern() -> None:
    profiles = build_builtin_flow_profiles()
    custom = profiles[2].__class__(
        name="room_specific",
        version=1,
        description="Room specific",
        steps=[],
        bindings=[
            profiles[2].bindings[0].__class__(
                channel="discord",
                session_kind="group",
                session_id_pattern="guild-*",
                priority=10,
            )
        ],
    )

    result = FlowResolver([*profiles, custom]).resolve(
        FlowResolveRequest(
            channel="discord",
            session_kind="group",
            session_id="guild-123",
            message_type="text",
        )
    )

    assert result.profile is not None
    assert result.profile.name == "room_specific"


def test_normalize_flow_session_kind_uses_metadata_then_wechat_fallback() -> None:
    assert normalize_flow_session_kind(metadata={"session_kind": "GROUP"}) == "group"
    assert normalize_flow_session_kind(metadata={"session_kind": "DIRECT"}) == "private"
    assert normalize_flow_session_kind(
        channel="wechat",
        session_id="room@chatroom",
    ) == "group"
    assert normalize_flow_session_kind(channel="discord", session_id="channel-1") == "private"


def test_compile_builtin_flows_marks_target_flows_invalid_without_plugins() -> None:
    compiled = {flow.name: flow for _profile, flow in compile_builtin_flows()}

    assert compiled["default_compatible_flow"].status == FLOW_STATUS_DEGRADED
    assert compiled["default_private_channel_flow"].status == FLOW_STATUS_DEGRADED
    assert compiled["default_group_channel_flow"].status == FLOW_STATUS_INVALID
    assert compiled["default_wechat_group_flow"].status == FLOW_STATUS_INVALID
    assert any(
        "plugin.commands.dispatch" in error
        for error in compiled["default_group_channel_flow"].errors
    )


def test_flow_compiler_marks_required_unknown_step_invalid() -> None:
    compiler = FlowCompiler(build_default_flow_registry())

    flow = compiler.compile(
        name="bad_flow",
        steps=[
            FlowStepSpec(id="load_session", kind="core.load_session"),
            FlowStepSpec(id="missing", kind="plugin.memory.load"),
        ],
    )

    assert flow.status == FLOW_STATUS_INVALID
    assert flow.errors == ["step missing references unavailable kind: plugin.memory.load"]


def test_flow_compiler_marks_optional_unknown_step_degraded() -> None:
    compiler = FlowCompiler(build_default_flow_registry())

    flow = compiler.compile(
        name="degraded_flow",
        steps=[
            FlowStepSpec(id="load_session", kind="core.load_session"),
            FlowStepSpec(id="memory_load", kind="plugin.memory.load", optional=True),
        ],
    )

    assert flow.status == FLOW_STATUS_DEGRADED
    assert flow.active is True
    assert flow.runnable is True
    assert flow.errors == []
    assert flow.warnings == [
        "step memory_load references unavailable kind: plugin.memory.load"
    ]


def test_flow_compiler_reports_missing_inputs() -> None:
    registry = FlowStepRegistry()
    registry.register(
        FlowStepDefinition(
            kind="plugin.memory.load",
            owner="memory",
            inputs={"session", "pre"},
            outputs={"signals.memory.user_profile"},
            error_policy="fail_open",
        )
    )
    compiler = FlowCompiler(registry, kernel_inputs={"event"})

    flow = compiler.compile(
        name="missing_inputs",
        steps=[FlowStepSpec(id="memory_load", kind="plugin.memory.load")],
    )

    assert flow.status == FLOW_STATUS_INVALID
    assert flow.errors == ["step memory_load missing inputs: pre, session"]


def test_flow_compiler_rejects_signal_output_conflicts() -> None:
    registry = FlowStepRegistry()
    registry.register_many(
        [
            FlowStepDefinition(
                kind="plugin.memory.load",
                owner="memory",
                outputs={"signals.profile.user"},
                error_policy="fail_open",
            ),
            FlowStepDefinition(
                kind="plugin.persona.load",
                owner="persona",
                outputs={"signals.profile.user"},
                error_policy="fail_open",
            ),
        ]
    )
    compiler = FlowCompiler(registry)

    flow = compiler.compile(
        name="conflict",
        steps=[
            FlowStepSpec(id="memory_load", kind="plugin.memory.load"),
            FlowStepSpec(id="persona_load", kind="plugin.persona.load"),
        ],
    )

    assert flow.status == FLOW_STATUS_INVALID
    assert flow.errors == [
        "signal output conflict for signals.profile.user: memory_load and persona_load"
    ]


def test_flow_compiler_rejects_missing_owner_permissions() -> None:
    registry = FlowStepRegistry()
    registry.register(
        FlowStepDefinition(
            kind="plugin.memory.load",
            owner="memory",
            permissions=["storage:shared"],
        )
    )
    compiler = FlowCompiler(registry, owner_permissions={"memory": set()})

    flow = compiler.compile(
        name="missing_permissions",
        steps=[FlowStepSpec(id="memory_load", kind="plugin.memory.load")],
    )

    assert flow.status == FLOW_STATUS_INVALID
    assert flow.errors == [
        "step memory_load missing owner permissions: storage:shared"
    ]


def test_flow_step_registry_unregister_owner_removes_plugin_steps() -> None:
    registry = FlowStepRegistry()
    registry.register(
        FlowStepDefinition(kind="plugin.memory.load", owner="memory")
    )

    assert registry.get("plugin.memory.load") is not None
    assert registry.unregister_owner("memory") == 1
    assert registry.get("plugin.memory.load") is None


def test_flow_step_definition_enforces_plugin_kind_prefix() -> None:
    with pytest.raises(ValueError, match="plugin flow step kind must start"):
        FlowStepDefinition(kind="plugin.other.load", owner="memory")


def test_step_result_validates_action() -> None:
    assert StepResult(action="stop", reason="command_handled").action == "stop"
    with pytest.raises(ValueError, match="unknown step action"):
        StepResult(action="unknown")


def test_pipeline_context_exposes_flow_compat_fields() -> None:
    event = InboundEvent(
        message_id="m-1",
        tenant_id="demo",
        channel=Channel.WEB,
        user_id="u1",
        session_id="s1",
        message=Message(content="hello"),
    )
    ctx = PipelineContext(event=event, trace_id=event.trace_id)

    ctx.signals["router"] = {"faq_similarity": 0.9}
    ctx.effects.append({"type": "publish_outbound"})
    ctx.scratch["test"] = {"local": True}

    assert ctx.extras == {}
    assert ctx.signals["router"]["faq_similarity"] == 0.9
    assert ctx.effects == [{"type": "publish_outbound"}]
    assert ctx.scratch["test"] == {"local": True}
