"""Owner-bound cleanup for framework registrations and plugin resources."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from app.agent.registry import AgentToolRegistry


class PluginResources:
    """LIFO finalizers for one initialized generation.

    Failed/cancelled finalizers remain registered for a later cleanup attempt.
    Plugins should stop work at their existing cancellation-safe on_disable and
    shutdown boundaries before finalizers release long-lived clients/resources.
    """

    def __init__(self, owner: str) -> None:
        self.owner = owner
        self._callbacks: list[Callable[[], Awaitable[Any]]] = []
        self._closing = False
        self._lock = asyncio.Lock()

    def add_cleanup(self, callback: Callable[[], Awaitable[Any]]) -> None:
        if self._closing:
            raise RuntimeError(f"plugin resources already closing: {self.owner}")
        self._callbacks.append(callback)

    async def close(self) -> None:
        async with self._lock:
            self._closing = True
            errors = []
            for callback in tuple(reversed(self._callbacks)):
                try:
                    await callback()
                except Exception as exc:
                    errors.append(exc)
                else:
                    self._callbacks.remove(callback)
            if errors:
                raise ExceptionGroup(f"plugin resource cleanup failed: {self.owner}", errors)


def revoke_contributions(owner: str, hooks: Any, container: Any, commands_plugin: Any) -> dict[str, int]:
    """Shared revoke path for disable, failed activation, and shutdown.

    The caller closes the local/durable execution gates first. Static mounted
    contributions remain fenced by their last-hop gates and restart policy.
    """
    counts = {"hooks": 0, "agent_tools": 0, "commands": 0, "cleanup_errors": 0}
    tool_registry = getattr(container, "agent_tool_registry", None)
    callbacks = [("hooks", lambda: hooks.unregister_owner(owner))]
    if isinstance(tool_registry, AgentToolRegistry):
        callbacks.append(("agent_tools", lambda: tool_registry.unregister_owner(owner)))
    unregister = getattr(commands_plugin, "unregister_owner", None)
    if callable(unregister):
        callbacks.append(("commands", lambda: unregister(owner)))
    for kind, callback in callbacks:
        try:
            counts[kind] = int(callback())
        except Exception:
            counts["cleanup_errors"] += 1
    return counts
