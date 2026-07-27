from __future__ import annotations

import pytest

from app.agent.registry import AgentToolDefinition, AgentToolRegistry
from app.orchestrator.flow import FlowStepDefinition, FlowStepRegistry


async def _tool_handler(_session, _arguments):
    return {"ok": True}


def _tool(name: str, *, owner: str = "") -> AgentToolDefinition:
    metadata = {"owner": owner} if owner else {}
    return AgentToolDefinition(
        scope="group",
        name=name,
        description=name,
        parameters={"type": "object"},
        handler=_tool_handler,
        metadata=metadata,
    )


def test_agent_tool_batch_rejects_duplicates_without_partial_publish() -> None:
    registry = AgentToolRegistry()
    registry.register(_tool("existing"), owner="alpha")

    with pytest.raises(ValueError, match="duplicate agent tool registration"):
        registry.register_many(
            [_tool("new"), _tool("existing")],
            owner="beta",
        )

    assert [item.name for item in registry.list_tools("group")] == ["existing"]
    assert registry.catalog_by_owner().keys() == {"alpha"}


def test_agent_tool_registration_rejects_owner_spoofing() -> None:
    registry = AgentToolRegistry()

    with pytest.raises(ValueError, match="owner mismatch"):
        registry.register(_tool("spoofed", owner="other"), owner="plugin")

    assert registry.list_tools("group") == []


def test_flow_step_batch_rejects_existing_kind_without_partial_publish() -> None:
    registry = FlowStepRegistry()
    existing = FlowStepDefinition(kind="plugin.alpha.existing", owner="alpha")
    registry.register(existing)

    with pytest.raises(ValueError, match="duplicate flow step registration"):
        registry.register_many(
            [
                FlowStepDefinition(kind="plugin.beta.new", owner="beta"),
                FlowStepDefinition(kind="plugin.alpha.existing", owner="alpha"),
            ]
        )

    assert registry.get("plugin.beta.new") is None
    assert registry.get("plugin.alpha.existing") is existing


def test_flow_step_batch_rejects_internal_duplicate() -> None:
    registry = FlowStepRegistry()

    with pytest.raises(ValueError, match="duplicate flow step in registration batch"):
        registry.register_many(
            [
                FlowStepDefinition(kind="plugin.alpha.same", owner="alpha"),
                FlowStepDefinition(kind="plugin.alpha.same", owner="alpha"),
            ]
        )

    assert registry.list_definitions() == []
