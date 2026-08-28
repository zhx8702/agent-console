"""Fail CI when built-in manifest, descriptors, and runtime catalogs drift."""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agent.registry import AgentToolRegistry  # noqa: E402
from app.billing import BillingCoordinator  # noqa: E402
from app.channel import ChannelRegistry  # noqa: E402
from app.common.config import get_settings  # noqa: E402
from app.common.types import RouteType  # noqa: E402
from app.infra.db import dispose_engine  # noqa: E402
from app.plugin.base import (  # noqa: E402
    PluginContext,
    PluginDescriptor,
    plugin_capability_digest,
)
from app.plugin.dependencies import parse_plugin_dependency  # noqa: E402
from app.plugin.marketplace import (  # noqa: E402
    MarketplaceItem,
    load_marketplace_manifest,
)
from app.plugin.registry import PluginRegistry  # noqa: E402

MANIFEST_CAPABILITY_FIELDS = ("routes", "hooks", "agent_tools", "commands")

_CONTRACT_IO_STUBS = (
    "plugins.commands.store.CommandStore.ensure_tables",
    "plugins.credits.store.CreditStore.ensure_tables",
    "plugins.draw.store.DrawStore.initialize",
    "plugins.group_activity.store.GroupActivityStore.ensure_tables",
    "plugins.local_agent.store.LocalAgentStore.ensure_tables",
    "plugins.memory.store.MemoryStore.ensure_tables",
    "plugins.moderation.store.ModerationStore.ensure_tables",
    "plugins.persona_extract.store.PersonaExtractStore.ensure_tables",
    "plugins.persona_extract.store.PersonaExtractStore.fail_stale_running_jobs",
    "plugins.repeater.store.RepeaterStore.ensure_tables",
    "plugins.speaker_portrait.store.SpeakerPortraitStore.ensure_tables",
    "plugins.tibo_reset.store.TiboResetStore.ensure_tables",
    "plugins.wxbot.store.WxbotStore.ensure_tables",
    "plugins.wxbot.store.WxbotStore.fail_stale_report_jobs",
    "plugins.wxbot.store.WxbotStore.fail_stale_self_review_jobs",
)


async def _noop_contract_io(*args: object, **kwargs: object) -> None:
    _ = (args, kwargs)


def _disable_contract_io() -> None:
    """Keep the descriptor check hermetic while running real plugin factories."""

    for target in _CONTRACT_IO_STUBS:
        module_name, class_name, attribute = target.rsplit(".", 2)
        module = importlib.import_module(module_name)
        owner = getattr(module, class_name)
        setattr(owner, attribute, _noop_contract_io)


def descriptor_manifest_capabilities(
    descriptor: PluginDescriptor,
) -> dict[str, list[str]]:
    """Project the exact descriptor into the manifest's legacy public schema.

    The manifest intentionally advertises router mount prefixes, hook points and
    tool names rather than the descriptor's method/path, point/name and
    scope/name identities.  This projection is deterministic and compared as an
    exact set; it never performs prefix or subset matching.
    """

    routes = [f"/plugins/{descriptor.name}"] if descriptor.admin_routes else []
    hooks = [value.split(":", 1)[0] for value in descriptor.hooks]
    tools = [value.split(":", 1)[1] for value in descriptor.agent_tools]
    return {
        "routes": sorted(set(routes)),
        "hooks": sorted(set(hooks)),
        "agent_tools": sorted(set(tools)),
        "commands": sorted(set(descriptor.commands)),
    }


def capability_drift_errors(
    name: str,
    declared: dict[str, list[str]],
    descriptor: PluginDescriptor,
) -> list[str]:
    errors: list[str] = []
    unknown = sorted(set(declared) - set(MANIFEST_CAPABILITY_FIELDS))
    if unknown:
        errors.append(f"{name}: unsupported manifest capability fields: {unknown!r}")

    expected = descriptor_manifest_capabilities(descriptor)
    for field in MANIFEST_CAPABILITY_FIELDS:
        raw_values = [str(item).strip() for item in declared.get(field, [])]
        actual = sorted({item for item in raw_values if item})
        if len(actual) != len(raw_values):
            errors.append(f"{name}: capability {field!r} contains blank or duplicate entries")
        if actual != expected[field]:
            errors.append(
                f"{name}: capability {field!r} drift "
                f"descriptor={expected[field]!r} manifest={actual!r}"
            )
    return errors


def manifest_descriptor_errors(
    plugins: dict[str, Any],
    descriptors: dict[str, PluginDescriptor],
    items: dict[str, MarketplaceItem],
) -> list[str]:
    errors: list[str] = []
    missing = sorted(set(plugins) - set(items))
    extra = sorted(set(items) - set(plugins))
    if missing:
        errors.append(f"manifest missing built-ins: {missing}")
    if extra:
        errors.append(f"manifest references missing built-ins: {extra}")

    for name in sorted(set(plugins) & set(items)):
        item = items[name]
        descriptor = descriptors.get(name)
        expected_uri = f"plugins/{name}"
        if item.source != "builtin" or item.package.type != "builtin":
            errors.append(f"{name}: production built-in manifest must use builtin source/package")
        if item.package.uri != expected_uri:
            errors.append(
                f"{name}: builtin package uri drift expected={expected_uri!r} "
                f"actual={item.package.uri!r}"
            )
        if item.package.checksum or item.package.signature:
            errors.append(
                f"{name}: built-in integrity comes from the pinned image; "
                "package checksum/signature metadata must be empty"
            )
        if descriptor is None:
            errors.append(f"{name}: initialized runtime descriptor missing")
            continue
        if descriptor.version != item.version:
            errors.append(
                f"{name}: version drift descriptor={descriptor.version!r} "
                f"manifest={item.version!r}"
            )
        if descriptor.description != item.description:
            errors.append(f"{name}: description drift")
        expected_capability_digest = plugin_capability_digest(descriptor)
        if item.capability_digest != expected_capability_digest:
            errors.append(
                f"{name}: capability digest drift "
                f"descriptor={expected_capability_digest!r} "
                f"manifest={item.capability_digest!r}"
            )
        runtime_config_schema = plugins[name].get_config_schema()
        if runtime_config_schema != item.config_schema:
            errors.append(
                f"{name}: config schema drift between runtime and manifest"
            )
        manifest_permissions = sorted(set(item.permission_ids))
        if list(descriptor.permissions) != manifest_permissions:
            errors.append(
                f"{name}: permission drift descriptor={list(descriptor.permissions)!r} "
                f"manifest={manifest_permissions!r}"
            )
        runtime_dependencies = sorted(
            parse_plugin_dependency(value).label for value in descriptor.dependencies
        )
        manifest_dependencies = sorted(
            dependency.name + (dependency.version if dependency.version else "")
            for dependency in item.dependencies
            if dependency.required
        )
        if runtime_dependencies != manifest_dependencies:
            errors.append(
                f"{name}: required dependency drift descriptor={runtime_dependencies!r} "
                f"manifest={manifest_dependencies!r}"
            )

        errors.extend(capability_drift_errors(name, item.capabilities, descriptor))
        permission_set = set(manifest_permissions)
        capability_permissions = {
            "routes": "admin_api",
            "hooks": "hooks:pipeline",
            "agent_tools": "agent_tools",
            "commands": "commands",
        }
        for capability, permission in capability_permissions.items():
            if item.capabilities.get(capability) and permission not in permission_set:
                errors.append(
                    f"{name}: capability {capability!r} requires permission {permission!r}"
                )
    return errors


async def load_runtime_registry(root: Path) -> PluginRegistry:
    """Initialize built-ins without calling any external integration."""

    _disable_contract_io()
    settings = get_settings()
    registry = PluginRegistry(allow_offline_execution=True)
    registry.discover_directory(root / "plugins")
    container = SimpleNamespace(
        plugin_registry=registry,
        agent_tool_registry=AgentToolRegistry(),
        billing=BillingCoordinator(),
        channel_registry=ChannelRegistry(),
        capabilities={RouteType.AGENT: object()},
        llm_service=None,
        vector_store=None,
        social_policy_store=None,
    )
    await registry.initialize_all(
        PluginContext(
            container=container,
            settings=settings,
            db_ok=False,
            redis_ok=False,
        )
    )
    return registry


async def async_main() -> int:
    registry: PluginRegistry | None = None
    try:
        registry = await load_runtime_registry(ROOT)
        errors = [
            f"{name}: runtime initialization failed: {reason}"
            for name, reason in sorted(registry.initialization_failures.items())
        ]
        try:
            # Exercise every runtime collector. Each collector is descriptor-
            # fenced, including command and agent-tool owner catalogs.
            registry.all_api_routers()
            registry.all_flow_steps()
            registry.all_flow_executors()
            registry.all_effect_handlers()
            registry.all_permissions()
            descriptors = registry.descriptors
        except RuntimeError as exc:
            errors.append(str(exc))
            descriptors = {}

        manifest = load_marketplace_manifest(ROOT / "config" / "plugin-marketplace.yaml")
        errors.extend(
            manifest_descriptor_errors(
                registry.loaded_plugins,
                descriptors,
                manifest.by_name(),
            )
        )
        if errors:
            raise SystemExit("\n".join(errors))
        print(f"plugin contracts consistent: {len(descriptors)} built-ins")
        return 0
    finally:
        if registry is not None:
            await registry.shutdown_all()
        await dispose_engine()


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
