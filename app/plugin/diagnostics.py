"""Bounded read-only runtime diagnostics, independent of desired plugin state."""

from __future__ import annotations

import asyncio
from typing import Any

from app.common.logging import get_logger

logger = get_logger(__name__)
RUNTIME_QUERY_TIMEOUT_SECONDS = 5.0


async def query_runtime(plugin: Any, registry: Any, state_store: Any, name: str) -> dict[str, Any]:
    state = None

    async def inspect() -> tuple[dict[str, Any], str]:
        nonlocal state
        state = await state_store.get(name)

        async def allowed() -> bool:
            if not (
                state is not None and state.installed and state.enabled
                and state.status == "active" and not state.restart_required
                and state.version == plugin.meta.version
            ):
                return False
            gate = getattr(registry, "global_execution_allowed", None)
            if callable(gate):
                return await gate(name) is True
            return True

        blocked = {"running": False, "execution_allowed": False}
        if not await allowed():
            return blocked, "blocked"
        result = await plugin.get_runtime_status()
        if not isinstance(result, dict):
            raise TypeError("invalid runtime status")
        # Disabling a plugin while its diagnostic probe runs must not publish
        # an apparently live result from the retired generation.
        if not await allowed():
            return blocked, "blocked"
        return result, "ready"

    error = ""
    runtime = None
    try:
        runtime, status = await asyncio.wait_for(inspect(), RUNTIME_QUERY_TIMEOUT_SECONDS)
    except TimeoutError:
        status, error = "timeout", "plugin_runtime_timeout"
    except Exception as exc:
        status, error = "error", "plugin_runtime_query_failed"
        logger.warning("plugin.runtime_query_failed", plugin=name, error_class=type(exc).__name__)
    return {
        "plugin_name": name,
        "state": state.as_dict() if state is not None else None,
        "runtime_query": {"status": status, "error": error},
        "runtime_status": runtime,
        "runtime": runtime,
    }
