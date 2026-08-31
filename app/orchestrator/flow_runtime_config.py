"""Operator-facing Flow Runtime configuration and runtime gate decisions."""

from __future__ import annotations

from typing import Any

from app.common.config import Settings
from app.orchestrator.flow import (
    DEFAULT_COMPATIBLE_FLOW_NAME,
    resolve_capability_dispatch_timeout_seconds,
)

TARGET_FLOW_NAMES = {
    "default_group_channel_flow",
    "default_private_channel_flow",
    "default_wechat_group_flow",
}


def _csv_items(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def flow_runtime_allowed(
    settings: Settings,
    requested_name: str,
    resolved_name: str,
) -> tuple[bool, str]:
    """Return the allowlist decision used by the inbound coordinator."""

    allowed_names = set(_csv_items(settings.orchestrator_flow_runtime_allowed_names))
    if "*" not in allowed_names and requested_name not in allowed_names:
        return False, "requested_flow_not_allowed"
    if requested_name == "auto" and not settings.orchestrator_flow_runtime_allow_target_flows:
        return False, "auto_flow_not_allowed"
    if (
        requested_name == "auto"
        and resolved_name == DEFAULT_COMPATIBLE_FLOW_NAME
        and not settings.orchestrator_flow_runtime_allow_compatible_fallback
    ):
        return False, "compatible_fallback_not_allowed"
    if (
        resolved_name in TARGET_FLOW_NAMES
        and not settings.orchestrator_flow_runtime_allow_target_flows
    ):
        return False, "target_flow_not_allowed"
    return True, "allowed"


def build_flow_runtime_config_payload(settings: Settings) -> dict[str, Any]:
    """Build channel-neutral Flow Runtime rollout diagnostics."""

    requested_name = str(
        settings.orchestrator_flow_runtime_name or DEFAULT_COMPATIBLE_FLOW_NAME
    )
    allowed_names = _csv_items(settings.orchestrator_flow_runtime_allowed_names)
    allow_target_flows = bool(settings.orchestrator_flow_runtime_allow_target_flows)
    reason = "allowed"
    allowed = True
    if "*" not in allowed_names and requested_name not in allowed_names:
        allowed = False
        reason = "requested_flow_not_allowed"
    elif requested_name == "auto" and not allow_target_flows:
        allowed = False
        reason = "auto_flow_not_allowed"
    elif requested_name in TARGET_FLOW_NAMES and not allow_target_flows:
        allowed = False
        reason = "target_flow_not_allowed"

    return {
        "enabled": bool(settings.orchestrator_flow_runtime_enabled),
        "name": requested_name,
        "allowed_names": allowed_names,
        "allow_target_flows": allow_target_flows,
        "allow_compatible_fallback": bool(
            settings.orchestrator_flow_runtime_allow_compatible_fallback
        ),
        "capability_dispatch_timeout_seconds": resolve_capability_dispatch_timeout_seconds(
            settings.orchestrator_capability_dispatch_timeout_seconds,
            handle_timeout_seconds=settings.orchestrator_handle_timeout_seconds,
        ),
        "allowed": allowed,
        "reason": reason,
    }
