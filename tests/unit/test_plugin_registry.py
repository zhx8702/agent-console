from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import pytest

from app.agent.registry import AgentToolDefinition, AgentToolRegistry
from app.container import Container
from app.orchestrator.flow import FlowStepDefinition
from app.plugin.base import Plugin, PluginContext, PluginMeta
from app.plugin.hooks import HookPoint
from app.plugin.registry import PluginRegistry


@dataclass
class _DummyHook:
    name: str = "dummy.before_capability"
    point: HookPoint = HookPoint.BEFORE_CAPABILITY
    priority: int = 10

    async def run(self, ctx) -> None:
        _ = ctx


class _DummyFlowExecutor:
    kind = "plugin.dummy.step"
    owner = "dummy"
    name = "Dummy flow step"
    permissions: ClassVar[list[str]] = []
    inputs: ClassVar[set[str]] = set()
    outputs: ClassVar[set[str]] = set()
    timeout_seconds = 1.0
    error_policy = "fail_closed"

    async def run(self, ctx):
        _ = ctx


class _InitAwarePlugin(Plugin):
    meta = PluginMeta(name="dummy", version="0.1.0", description="test plugin")

    def __init__(self) -> None:
        self.initialized = False
        self.seen_container = None

    async def initialize(self, ctx: PluginContext) -> None:
        self.initialized = True
        self.seen_container = ctx.container

    def get_pipeline_hooks(self):
        if not self.initialized:
            return []
        return [_DummyHook()]

    def get_agent_tools(self):
        if not self.initialized:
            return []
        return [
            AgentToolDefinition(
                scope="dummy_scope",
                name="dummy_tool",
                description="dummy tool",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                handler=lambda *_args, **_kwargs: None,
            )
        ]

    def get_flow_executors(self):
        if not self.initialized:
            return {}
        return {"plugin.dummy.step": _DummyFlowExecutor()}

    def get_flow_steps(self) -> list[FlowStepDefinition]:
        if not self.initialized:
            return []
        return [
            FlowStepDefinition(
                kind="plugin.dummy.step",
                owner="dummy",
                name="Dummy flow step",
                permissions=[],
                inputs=set(),
                outputs=set(),
                timeout_seconds=1.0,
                error_policy="fail_closed",
            )
        ]


class _CommandCatalogPlugin(Plugin):
    meta = PluginMeta(name="commands")

    def __init__(self) -> None:
        self.tokens: dict[str, tuple[str, ...]] = {}

    async def initialize(self, ctx: PluginContext) -> None:
        _ = ctx

    def command_tokens_by_owner(self, owner: str) -> tuple[str, ...]:
        return self.tokens.get(owner, ())


class _CommandContributorPlugin(Plugin):
    meta = PluginMeta(name="command_contributor")

    async def initialize(self, ctx: PluginContext) -> None:
        registry = ctx.container.plugin_registry
        commands = registry.loaded_plugins["commands"]
        commands.tokens[self.meta.name] = ("/hello", "/你好")

    def get_permissions(self) -> list[str]:
        return ["commands"]


@pytest.mark.asyncio
async def test_initialize_all_registers_hooks_after_plugin_init() -> None:
    registry = PluginRegistry(allow_offline_execution=True)
    plugin = _InitAwarePlugin()
    registry._register(plugin)

    assert registry.hook_runner.summary == {}

    container = Container(agent_tool_registry=AgentToolRegistry())
    await registry.initialize_all(
        PluginContext(
            container=container,
            settings=object(),
            db_ok=True,
            redis_ok=True,
        )
    )

    assert plugin.seen_container is container
    assert registry.is_active("dummy") is True
    assert registry.is_initialized("dummy") is True
    assert registry.is_active("missing") is False
    assert registry.is_initialized("missing") is False
    assert registry.hook_runner.summary == {
        HookPoint.BEFORE_CAPABILITY.value: ["dummy.before_capability"]
    }
    assert container.agent_tool_registry.catalog("dummy_scope") == [
        {
            "scope": "dummy_scope",
            "name": "dummy_tool",
            "description": "dummy tool",
            "owner": "dummy",
        }
    ]
    assert "plugin.dummy.step" in registry.all_flow_executors()


@pytest.mark.asyncio
async def test_descriptor_snapshots_and_fences_command_owner_catalog() -> None:
    registry = PluginRegistry(allow_offline_execution=True)
    commands = _CommandCatalogPlugin()
    contributor = _CommandContributorPlugin()
    registry._register(commands)
    registry._register(contributor)
    container = Container(
        plugin_registry=registry,
        agent_tool_registry=AgentToolRegistry(),
    )

    await registry.initialize_all(
        PluginContext(container=container, settings=object())
    )

    descriptor = registry.descriptor("command_contributor")
    assert descriptor is not None
    assert descriptor.commands == ("/hello", "/你好")
    assert descriptor.as_capabilities()["commands"] == ["/hello", "/你好"]

    commands.tokens["command_contributor"] = ("/hello", "/extra")
    with pytest.raises(
        RuntimeError,
        match=r"plugin descriptor drift: command_contributor\.commands",
    ):
        registry.descriptor("command_contributor")


@pytest.mark.asyncio
async def test_plugin_registry_unregisters_owner_capabilities() -> None:
    registry = PluginRegistry(allow_offline_execution=True)
    plugin = _InitAwarePlugin()
    registry._register(plugin)
    container = Container(agent_tool_registry=AgentToolRegistry())
    ctx = PluginContext(container=container, settings=object(), db_ok=True, redis_ok=True)

    await registry.initialize_all(ctx)
    removed = await registry.deactivate_plugin("dummy", container)
    assert registry.is_active("dummy") is False
    assert registry.is_initialized("dummy") is True
    await registry.initialize_plugin("dummy", ctx)

    assert registry.is_active("dummy") is True
    assert registry.is_initialized("dummy") is True

    assert removed == {
        "hooks": 1,
        "agent_tools": 1,
        "commands": 0,
        "cleanup_errors": 0,
    }
    assert registry.hook_runner.summary == {
        HookPoint.BEFORE_CAPABILITY.value: ["dummy.before_capability"]
    }
    assert container.agent_tool_registry.catalog("dummy_scope") == [
        {
            "scope": "dummy_scope",
            "name": "dummy_tool",
            "description": "dummy tool",
            "owner": "dummy",
        }
    ]


@pytest.mark.asyncio
async def test_failed_reactivation_invokes_disable_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _LifecyclePlugin(Plugin):
        meta = PluginMeta(name="lifecycle_cleanup", version="1.0.0")

        def __init__(self) -> None:
            self.enable_calls = 0
            self.disable_calls = 0

        async def initialize(self, ctx: PluginContext) -> None:
            _ = ctx

        async def on_enable(self, scope=None) -> None:
            _ = scope
            self.enable_calls += 1

        async def on_disable(self, scope=None) -> None:
            _ = scope
            self.disable_calls += 1

    registry = PluginRegistry(allow_offline_execution=True)
    plugin = _LifecyclePlugin()
    registry._register(plugin)
    container = Container(agent_tool_registry=AgentToolRegistry())
    ctx = PluginContext(container=container, settings=object())
    await registry.initialize_all(ctx)
    await registry.deactivate_plugin("lifecycle_cleanup", container)
    assert plugin.disable_calls == 1

    def fail_publish(*_args, **_kwargs) -> None:
        raise RuntimeError("publish failed")

    monkeypatch.setattr(registry, "_register_plugin_hooks", fail_publish)
    with pytest.raises(RuntimeError, match="publish failed"):
        await registry.reactivate_plugin("lifecycle_cleanup", ctx)

    assert plugin.enable_calls == 1
    assert plugin.disable_calls == 2
    assert registry.is_active("lifecycle_cleanup") is False


def test_agent_tool_registry_unregister_owner_and_catalog_by_owner() -> None:
    registry = AgentToolRegistry()
    registry.register(
        AgentToolDefinition(
            scope="dummy_scope",
            name="dummy_tool",
            description="dummy tool",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=lambda *_args, **_kwargs: None,
        ),
        owner="dummy",
    )

    assert "dummy" in registry.catalog_by_owner()
    assert registry.unregister_owner("dummy") == 1
    assert registry.catalog("dummy_scope") == []


def test_agent_tool_registry_normalizes_descriptor_metadata() -> None:
    registry = AgentToolRegistry()

    async def _handler(session, arguments: dict[str, object]) -> dict[str, object]:
        return {"ok": True}

    registry.register(
        AgentToolDefinition(
            scope=" descriptor_scope ",
            name=" descriptor_tool ",
            description="descriptor tool",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=_handler,
            metadata={"embed_text": "metadata embed"},
            embed_text="top-level embed",
            tree_text="tree node",
            verb_type="query",
            scopes=["wxbot", "group"],
        ),
        owner="dummy",
    )

    tools = registry.list_tools("descriptor_scope")
    assert len(tools) == 1
    assert tools[0].embed_text == "top-level embed"
    assert tools[0].tree_text == "tree node"
    assert tools[0].required_params == ["query"]
    assert tools[0].verb_type == "query"
    assert tools[0].scopes == ["wxbot", "group"]
    assert tools[0].metadata["required_params"] == ["query"]

    assert registry.catalog("descriptor_scope") == [
        {
            "scope": "descriptor_scope",
            "name": "descriptor_tool",
            "description": "descriptor tool",
            "owner": "dummy",
            "embed_text": "top-level embed",
            "tree_text": "tree node",
            "required_params": ["query"],
            "verb_type": "query",
            "scopes": ["wxbot", "group"],
        }
    ]


def test_agent_tool_registry_accepts_metadata_only_descriptors() -> None:
    registry = AgentToolRegistry()

    async def _handler(session, arguments: dict[str, object]) -> dict[str, object]:
        return {"ok": True}

    registry.register(
        AgentToolDefinition(
            scope="descriptor_scope",
            name="metadata_tool",
            description="metadata tool",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=_handler,
            metadata={
                "embed_text": " metadata embed ",
                "tree_text": " metadata tree ",
                "required_params": "single_param",
                "verb_type": " query ",
                "scopes": "metadata_scope",
            },
        )
    )

    tool = registry.list_tools("descriptor_scope")[0]
    assert tool.embed_text == "metadata embed"
    assert tool.tree_text == "metadata tree"
    assert tool.required_params == ["single_param"]
    assert tool.verb_type == "query"
    assert tool.scopes == ["metadata_scope"]
    assert registry.catalog("descriptor_scope")[0] == {
        "scope": "descriptor_scope",
        "name": "metadata_tool",
        "description": "metadata tool",
        "embed_text": "metadata embed",
        "tree_text": "metadata tree",
        "required_params": ["single_param"],
        "verb_type": "query",
        "scopes": ["metadata_scope"],
    }
