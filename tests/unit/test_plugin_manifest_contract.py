from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.infra.db import dispose_engine
from app.plugin.base import PluginDescriptor, plugin_capability_digest
from app.plugin.marketplace import load_marketplace_manifest
from scripts.check_plugin_manifest import (
    capability_drift_errors,
    descriptor_manifest_capabilities,
    load_runtime_registry,
    manifest_descriptor_errors,
)


def _descriptor() -> PluginDescriptor:
    return PluginDescriptor(
        name="demo",
        version="1.0.0",
        description="demo",
        dependencies=(),
        permissions=("admin_api", "commands", "hooks:pipeline"),
        hooks=("before_route:demo.command",),
        agent_tools=("group:lookup",),
        commands=("/hello", "/hi"),
        flow_steps=("plugin.demo.dispatch",),
        effects=("publish_demo",),
        admin_routes=("GET /config", "PUT /config"),
        storage_permissions=(),
        network_permissions=(),
    )


def test_descriptor_manifest_projection_is_exact_and_deterministic() -> None:
    assert descriptor_manifest_capabilities(_descriptor()) == {
        "routes": ["/plugins/demo"],
        "hooks": ["before_route"],
        "agent_tools": ["lookup"],
        "commands": ["/hello", "/hi"],
    }


def test_manifest_capability_comparison_rejects_missing_entry() -> None:
    errors = capability_drift_errors(
        "demo",
        {
            "routes": ["/plugins/demo"],
            "hooks": ["before_route"],
            "agent_tools": ["lookup"],
            "commands": ["/hello"],
        },
        _descriptor(),
    )

    assert errors == [
        "demo: capability 'commands' drift "
        "descriptor=['/hello', '/hi'] manifest=['/hello']"
    ]


def test_manifest_capability_comparison_rejects_extra_entry() -> None:
    errors = capability_drift_errors(
        "demo",
        {
            "routes": ["/plugins/demo"],
            "hooks": ["before_route", "after_route"],
            "agent_tools": ["lookup"],
            "commands": ["/hello", "/hi"],
        },
        _descriptor(),
    )

    assert errors == [
        "demo: capability 'hooks' drift "
        "descriptor=['before_route'] manifest=['after_route', 'before_route']"
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("flow_steps", ("plugin.demo.other",)),
        ("effects", ("publish_other",)),
        ("channel_adapters", ("wechat",)),
        ("capability_engines", ("agent",)),
        ("admin_media_providers", ("media",)),
    ],
)
def test_full_capability_digest_covers_non_legacy_contributions(
    field: str,
    value: tuple[str, ...],
) -> None:
    descriptor = _descriptor()
    changed = replace(descriptor, **{field: value})

    assert plugin_capability_digest(changed) != plugin_capability_digest(descriptor)


@pytest.mark.asyncio
async def test_builtin_manifest_matches_initialized_runtime_descriptors_without_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def noop(*args: object, **kwargs: object) -> None:
        _ = (args, kwargs)

    for target in (
        "plugins.commands.store.CommandStore.ensure_tables",
        "plugins.credits.store.CreditStore.ensure_tables",
        "plugins.draw.store.DrawStore.initialize",
        "plugins.group_activity.store.GroupActivityStore.ensure_tables",
        "plugins.memory.store.MemoryStore.ensure_tables",
        "plugins.moderation.store.ModerationStore.ensure_tables",
        "plugins.persona_extract.store.PersonaExtractStore.ensure_tables",
        "plugins.persona_extract.store.PersonaExtractStore.fail_stale_running_jobs",
        "plugins.repeater.store.RepeaterStore.ensure_tables",
        "plugins.tibo_reset.store.TiboResetStore.ensure_tables",
        "plugins.wxbot.store.WxbotStore.ensure_tables",
        "plugins.wxbot.store.WxbotStore.fail_stale_report_jobs",
        "plugins.wxbot.store.WxbotStore.fail_stale_self_review_jobs",
    ):
        monkeypatch.setattr(target, noop)

    root = Path(__file__).resolve().parents[2]
    registry = await load_runtime_registry(root)
    try:
        assert registry.initialization_failures == {}
        registry.all_api_routers()
        registry.all_flow_steps()
        registry.all_flow_executors()
        registry.all_effect_handlers()
        registry.all_permissions()
        manifest = load_marketplace_manifest(root / "config" / "plugin-marketplace.yaml")
        errors = manifest_descriptor_errors(
            registry.loaded_plugins,
            registry.descriptors,
            manifest.by_name(),
        )
        assert errors == [], "\n".join(errors)
    finally:
        await registry.shutdown_all()
        await dispose_engine()
