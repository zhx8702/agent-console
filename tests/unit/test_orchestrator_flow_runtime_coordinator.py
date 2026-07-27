"""Focused tests for the flow concern extracted from DialogOrchestrator."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

import pytest

from app.common.config import Settings
from app.common.types import Channel, InboundEvent, Message
from app.orchestrator.engine_flow_runtime import (
    FlowRuntimeCoordinator,
    FlowRuntimePorts,
)
from app.orchestrator.outcome import PermanentProcessingError
from app.orchestrator.ports import (
    FlowSessionPort,
    OrchestratorBusPort,
    PostprocessorPort,
    PreprocessorPort,
    RouterPort,
    SafetyPort,
)
from app.plugin.hooks import HookRunner


def _event() -> InboundEvent:
    return InboundEvent(
        message_id="flow-coordinator-1",
        tenant_id="demo",
        channel=Channel.WEB,
        user_id="user-1",
        session_id="session-1",
        message=Message(content="hello"),
    )


@asynccontextmanager
async def _session_lock(_event: InboundEvent) -> AsyncIterator[None]:
    yield


def _coordinator(settings: Settings) -> FlowRuntimeCoordinator:
    unused = object()
    return FlowRuntimeCoordinator(
        FlowRuntimePorts(
            session_manager=cast(FlowSessionPort, unused),
            preprocessor=cast(PreprocessorPort, unused),
            router=cast(RouterPort, unused),
            safety=cast(SafetyPort, unused),
            postprocessor=cast(PostprocessorPort, unused),
            capabilities={},
            bus=cast(OrchestratorBusPort, unused),
            settings=settings,
            hooks=HookRunner(),
        ),
        session_lock=_session_lock,
        suppression_reason=lambda _ctx: "suppressed",
    )


async def test_noop_shadow_is_owned_and_traced_by_flow_coordinator() -> None:
    coordinator = _coordinator(
        Settings(
            orchestrator_flow_shadow_enabled=True,
            orchestrator_flow_shadow_mode="noop",
            orchestrator_flow_trace_snapshot_enabled=False,
        )
    )

    await coordinator.run_shadow(_event(), route_label="faq")

    result = coordinator.last_shadow_result
    assert result is not None
    assert result.flow_name == "default_compatible_flow"
    assert result.status == "completed"
    assert {step.status for step in result.steps} == {"shadow"}


async def test_runtime_policy_rejects_target_flow_before_dependency_use() -> None:
    coordinator = _coordinator(
        Settings(
            orchestrator_flow_runtime_name="default_group_channel_flow",
            orchestrator_flow_runtime_allowed_names="default_group_channel_flow",
            orchestrator_flow_runtime_allow_target_flows=False,
            orchestrator_flow_trace_snapshot_enabled=False,
        )
    )

    with pytest.raises(PermanentProcessingError, match="target_flow_not_allowed"):
        await coordinator.run(_event())


async def test_auto_runtime_rejects_the_compatible_fallback_before_dependency_use() -> None:
    coordinator = _coordinator(
        Settings(
            orchestrator_flow_runtime_name="auto",
            orchestrator_flow_runtime_allowed_names="auto",
            orchestrator_flow_runtime_allow_target_flows=True,
            orchestrator_flow_runtime_allow_compatible_fallback=False,
            orchestrator_flow_trace_snapshot_enabled=False,
        )
    )

    event = _event().model_copy(
        update={"metadata": {"session_kind": "broadcast"}},
    )

    with pytest.raises(
        PermanentProcessingError,
        match="compatible_fallback_not_allowed",
    ):
        await coordinator.run(event)


def test_auto_runtime_accepts_explicit_private_flow_with_fallback_disabled() -> None:
    coordinator = _coordinator(
        Settings(
            orchestrator_flow_runtime_name="auto",
            orchestrator_flow_runtime_allowed_names="auto",
            orchestrator_flow_runtime_allow_target_flows=True,
            orchestrator_flow_runtime_allow_compatible_fallback=False,
            orchestrator_flow_trace_snapshot_enabled=False,
        )
    )

    profile = coordinator._resolve_flow_profile(
        flow_name="auto",
        event=_event(),
        log_prefix="test.flow_runtime",
    )

    assert profile is not None
    assert profile.name == "default_private_channel_flow"
    assert coordinator._flow_runtime_allowed("auto", profile.name) == (True, "allowed")
