from __future__ import annotations

from app.orchestrator.flow import (
    CAPABILITY_DISPATCH_TIMEOUT_SECONDS,
    FLOW_STATUS_ACTIVE,
    FLOW_STATUS_DEGRADED,
    FlowStepDefinition,
    FlowStepRegistry,
    build_default_flow_registry,
    compile_builtin_flows,
)
from plugins.commands.plugin import plugin as commands_plugin
from plugins.credits.plugin import plugin as credits_plugin
from plugins.draw.plugin import plugin as draw_plugin
from plugins.memory.plugin import plugin as memory_plugin
from plugins.moderation.plugin import plugin as moderation_plugin
from plugins.persona_extract.plugin import plugin as persona_extract_plugin
from plugins.repeater.plugin import plugin as repeater_plugin
from plugins.tibo_reset.plugin import plugin as tibo_reset_plugin
from plugins.wxbot.plugin import plugin as wxbot_plugin


def _all_plugin_flow_steps() -> list[FlowStepDefinition]:
    return [
        step
        for plugin in (
            commands_plugin,
            credits_plugin,
            draw_plugin,
            memory_plugin,
            moderation_plugin,
            persona_extract_plugin,
            repeater_plugin,
            tibo_reset_plugin,
            wxbot_plugin,
        )
        for step in plugin.get_flow_steps()
    ]


def test_plugins_expose_valid_flow_step_definitions() -> None:
    steps = _all_plugin_flow_steps()

    assert steps
    assert all(step.kind.startswith(f"plugin.{step.owner}.") for step in steps)
    assert all(step.error_policy for step in steps)

    registry = FlowStepRegistry()
    assert registry.register_many(steps) == len(steps)
    assert registry.get("plugin.commands.dispatch") is not None
    assert registry.get("plugin.wxbot.reply_policy") is not None
    credits_query = registry.get("plugin.credits.query_command")
    repeater = registry.get("plugin.repeater.detect")
    assert credits_query is not None
    assert "effects.auto_checkin" in credits_query.outputs
    assert repeater is not None
    assert "effects.record_repeater_trigger" in repeater.outputs


def test_flow_step_catalog_separates_channel_specific_steps() -> None:
    steps = _all_plugin_flow_steps()

    generic_kinds = {step.kind for step in steps if step.owner != "wxbot"}
    wxbot_kinds = {step.kind for step in steps if step.owner == "wxbot"}

    assert "plugin.commands.dispatch" in generic_kinds
    assert "plugin.memory.load" in generic_kinds
    memory_load = next(step for step in steps if step.kind == "plugin.memory.load")
    assert memory_load.timeout_seconds == 3.5
    assert "plugin.wxbot.reply_policy" in wxbot_kinds
    assert "plugin.wxbot.outbound_policy" in wxbot_kinds
    assert not any(kind.startswith("plugin.wxbot.") for kind in generic_kinds)


def test_target_group_flows_compile_with_current_plugin_catalog() -> None:
    registry = build_default_flow_registry()
    plugins = (
        commands_plugin,
        credits_plugin,
        draw_plugin,
        memory_plugin,
        moderation_plugin,
        persona_extract_plugin,
        repeater_plugin,
        tibo_reset_plugin,
        wxbot_plugin,
    )
    registry.register_many([step for plugin in plugins for step in plugin.get_flow_steps()])
    owner_permissions = {plugin.meta.name: set(plugin.get_permissions()) for plugin in plugins}

    compiled = {
        flow.name: flow
        for _profile, flow in compile_builtin_flows(
            registry,
            owner_permissions=owner_permissions,
        )
    }

    assert compiled["default_compatible_flow"].status == FLOW_STATUS_ACTIVE
    assert compiled["default_private_channel_flow"].status == FLOW_STATUS_ACTIVE
    assert compiled["default_group_channel_flow"].status == FLOW_STATUS_ACTIVE
    assert compiled["default_wechat_group_flow"].status == FLOW_STATUS_ACTIVE
    assert [
        step.kind
        for step in compiled["default_wechat_group_flow"].steps
        if step.owner == "wxbot"
    ] == [
        "plugin.wxbot.normalize_event",
        "plugin.wxbot.user_ban_pre_command",
        "plugin.wxbot.user_ban_gate",
        "plugin.wxbot.reply_policy",
        "plugin.wxbot.agent_scope_enrich",
        "plugin.wxbot.voice_profile_enrich",
        "plugin.wxbot.group_context_load",
        "plugin.wxbot.outbound_policy",
    ]

    wechat_kinds = [step.kind for step in compiled["default_wechat_group_flow"].steps]
    assert wechat_kinds.index("core.append_user_turn") < wechat_kinds.index(
        "plugin.wxbot.user_ban_pre_command"
    )
    assert wechat_kinds.index("plugin.wxbot.user_ban_pre_command") < wechat_kinds.index(
        "plugin.commands.dispatch"
    )
    assert wechat_kinds.index("plugin.commands.dispatch") < wechat_kinds.index(
        "plugin.repeater.detect"
    )
    assert wechat_kinds.index("plugin.wxbot.user_ban_gate") < wechat_kinds.index(
        "plugin.repeater.detect"
    )
    assert wechat_kinds.index("plugin.repeater.detect") < wechat_kinds.index(
        "plugin.tibo_reset.intent"
    )
    assert wechat_kinds.index("plugin.tibo_reset.intent") < wechat_kinds.index(
        "plugin.wxbot.reply_policy"
    )
    assert wechat_kinds.index("plugin.wxbot.user_ban_gate") < wechat_kinds.index(
        "plugin.wxbot.reply_policy"
    )
    assert wechat_kinds.index("plugin.wxbot.user_ban_gate") < wechat_kinds.index(
        "plugin.memory.control_intents"
    )
    assert wechat_kinds.index("plugin.memory.load") < wechat_kinds.index(
        "plugin.wxbot.group_context_load"
    )
    assert wechat_kinds.index("plugin.wxbot.group_context_load") < wechat_kinds.index(
        "core.capability_dispatch"
    )

    for flow_name in ("default_group_channel_flow", "default_wechat_group_flow"):
        kinds = [step.kind for step in compiled[flow_name].steps]
        capability = next(
            step
            for step in compiled[flow_name].steps
            if step.kind == "core.capability_dispatch"
        )

        assert capability.timeout_seconds == CAPABILITY_DISPATCH_TIMEOUT_SECONDS
        assert capability.timeout_seconds > 5.0
        assert kinds.index("plugin.credits.query_command") < kinds.index(
            "plugin.moderation.enforce_input"
        )
        assert kinds.index("plugin.moderation.enforce_input") < kinds.index(
            "plugin.credits.reserve"
        )
        assert kinds.index("plugin.credits.reserve") < kinds.index(
            "core.capability_dispatch"
        )


def test_wechat_flow_remains_runnable_without_optional_tibo_plugin() -> None:
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
    owner_permissions = {plugin.meta.name: set(plugin.get_permissions()) for plugin in plugins}

    compiled = {
        flow.name: flow
        for _profile, flow in compile_builtin_flows(
            registry,
            owner_permissions=owner_permissions,
        )
    }
    wechat = compiled["default_wechat_group_flow"]

    assert wechat.status == FLOW_STATUS_DEGRADED
    assert wechat.runnable is True
    assert wechat.errors == []
    assert wechat.warnings == [
        "step tibo_reset_intent references unavailable kind: plugin.tibo_reset.intent"
    ]
    assert "plugin.tibo_reset.intent" not in {step.kind for step in wechat.steps}
