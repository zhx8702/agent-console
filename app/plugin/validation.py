"""Validate live plugin declarations against the initialized descriptor."""
from __future__ import annotations

from typing import Any

from app.plugin.base import PluginDescriptor


def assert_descriptor_values(descriptor: PluginDescriptor, field_name: str, actual: set[str]) -> None:
    expected = set(getattr(descriptor, field_name))
    if actual != expected:
        raise RuntimeError(
            f"plugin descriptor drift: {descriptor.name}.{field_name}: "
            f"expected={sorted(expected)!r} actual={sorted(actual)!r}"
        )


def validate_runtime_descriptor(
    plugin: Any,
    descriptor: PluginDescriptor,
    *,
    command_tokens: tuple[str, ...],
) -> None:
    name = descriptor.name
    if (
        plugin.meta.name != descriptor.name
        or plugin.meta.version != descriptor.version
        or plugin.meta.description != descriptor.description
        or tuple(plugin.meta.dependencies) != descriptor.dependencies
    ):
        raise RuntimeError(f"plugin descriptor drift: {name}.metadata")

    permissions = {
        str(item).strip()
        for item in plugin.get_permissions()
        if str(item).strip()
    }
    assert_descriptor_values(descriptor, "permissions", permissions)
    assert_descriptor_values(
        descriptor,
        "storage_permissions",
        {item for item in permissions if item.startswith("storage:")},
    )
    assert_descriptor_values(
        descriptor,
        "network_permissions",
        {item for item in permissions if item.startswith("network:")},
    )

    hooks = plugin.get_pipeline_hooks()
    assert_descriptor_values(
        descriptor,
        "hooks",
        {
            (
                f"{getattr(getattr(item, 'point', ''), 'value', getattr(item, 'point', ''))}:"
                f"{getattr(item, 'name', '')}"
            )
            for item in hooks
        },
    )
    tools = plugin.get_agent_tools()
    assert_descriptor_values(
        descriptor,
        "agent_tools",
        {
            f"{getattr(item, 'scope', '')}:{getattr(item, 'name', '')}"
            for item in tools
        },
    )

    router = plugin.get_api_router()
    routes: set[str] = set()
    if router is not None:
        for route in getattr(router, "routes", ()):
            path = str(getattr(route, "path", "") or "")
            for method in sorted(getattr(route, "methods", ()) or ()):
                routes.add(f"{str(method).upper()} {path}")
    assert_descriptor_values(descriptor, "admin_routes", routes)

    steps = {str(item.kind) for item in plugin.get_flow_steps()}
    assert_descriptor_values(descriptor, "flow_steps", steps)
    assert_descriptor_values(
        descriptor,
        "flow_steps",
        {str(kind) for kind in plugin.get_flow_executors()},
    )

    handlers = plugin.get_effect_handlers()
    invalid_owners = sorted(
        {
            str(item[1])
            for item in handlers
            if str(item[1]).strip() != name
        }
    )
    if invalid_owners:
        raise RuntimeError(
            f"plugin descriptor drift: {name}.effects owners: "
            f"expected={name!r} actual={invalid_owners!r}"
        )
    assert_descriptor_values(
        descriptor,
        "effects",
        {str(item[0]) for item in handlers},
    )
    assert_descriptor_values(
        descriptor,
        "commands",
        set(command_tokens),
    )
    assert_descriptor_values(
        descriptor,
        "channel_adapters",
        {
            str(getattr(getattr(item, "descriptor", None), "adapter_id", "") or "")
            for item in plugin.get_channel_adapters()
        },
    )
    assert_descriptor_values(
        descriptor,
        "capability_engines",
        {
            str(getattr(route_type, "value", route_type))
            for route_type in plugin.get_capability_engines()
        },
    )
    admin_media_provider = plugin.get_admin_media_event_provider()
    assert_descriptor_values(
        descriptor,
        "admin_media_providers",
        (
            {
                str(
                    getattr(admin_media_provider, "name", "")
                    or name
                )
            }
            if admin_media_provider is not None
            else set()
        ),
    )

