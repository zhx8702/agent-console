from __future__ import annotations

import pytest

from app.container import (
    ApiContainer,
    ContainerDependencyError,
    CoreRuntimeContainer,
    InboundContainer,
    OutboundContainer,
    SchedulerContainer,
    WxbotBridgeContainer,
    get_container,
    reset_container,
    set_container,
)


def _core(**updates: object) -> CoreRuntimeContainer:
    values: dict[str, object] = {
        "session_manager": object(),
        "preprocessor": object(),
        "router": object(),
        "postprocessor": object(),
        "safety": object(),
        "faq_engine": None,
        "rag_engine": None,
        "llm_service": object(),
        "llm_provider": object(),
        "vector_store": None,
        "capabilities": {},
        "plugin_registry": object(),
        "plugin_manager": None,
        "agent_tool_registry": object(),
        "channel_registry": object(),
        "flow_step_registry": object(),
        "flow_step_executors": {},
        "flow_effect_handler_registry": object(),
        "flow_effect_log": None,
        "billing": object(),
        "faq_store": None,
        "kb_service": None,
        "agent_store": None,
        "vector_backend": "disabled",
        "persistence_backend": "disabled",
        "knowledge_features_enabled": False,
    }
    values.update(updates)
    return CoreRuntimeContainer(**values)  # type: ignore[arg-type]


def _scheduler(**updates: object) -> SchedulerContainer:
    values: dict[str, object] = {
        "plugin_registry": object(),
        "plugin_manager": object(),
        "llm_service": object(),
        "vector_store": None,
        "capabilities": {},
        "agent_tool_registry": object(),
        "channel_registry": object(),
        "billing": object(),
        "kb_service": None,
        "agent_store": object(),
        "social_policy_store": object(),
        "vector_backend": "disabled",
        "persistence_backend": "postgres",
        "knowledge_features_enabled": False,
    }
    values.update(updates)
    return SchedulerContainer(**values)  # type: ignore[arg-type]


def test_role_promotions_are_concrete_slotted_containers() -> None:
    core = _core()
    inbound = InboundContainer.from_core(
        core,
        bus=object(),  # type: ignore[arg-type]
        orchestrator=object(),  # type: ignore[arg-type]
        message_store=object(),  # type: ignore[arg-type]
    )
    api = ApiContainer.from_core(
        core,
        bus=object(),  # type: ignore[arg-type]
        orchestrator=object(),  # type: ignore[arg-type]
        message_store=object(),  # type: ignore[arg-type]
        dlq_admin_service=object(),  # type: ignore[arg-type]
        stream_admin_service=object(),  # type: ignore[arg-type]
        social_policy_store=object(),  # type: ignore[arg-type]
    )
    scheduler = _scheduler()

    assert isinstance(api, ApiContainer)
    assert isinstance(inbound, InboundContainer)
    assert isinstance(scheduler, SchedulerContainer)
    assert not hasattr(api, "__dict__")
    assert not hasattr(inbound, "__dict__")
    assert not hasattr(scheduler, "__dict__")
    assert not hasattr(scheduler, "bus")
    assert not hasattr(scheduler, "orchestrator")
    assert not hasattr(scheduler, "preprocessor")


def test_api_container_rejects_missing_role_dependency() -> None:
    with pytest.raises(
        ContainerDependencyError,
        match=r"api container is missing required dependencies: bus",
    ):
        ApiContainer.from_core(
            _core(),
            bus=None,  # type: ignore[arg-type]
            orchestrator=object(),  # type: ignore[arg-type]
            message_store=object(),  # type: ignore[arg-type]
            dlq_admin_service=object(),  # type: ignore[arg-type]
            stream_admin_service=object(),  # type: ignore[arg-type]
            social_policy_store=object(),  # type: ignore[arg-type]
        )


def test_core_rejects_incomplete_enabled_feature_bundle() -> None:
    with pytest.raises(
        ContainerDependencyError,
        match="knowledge-enabled core runtime container is missing required dependencies",
    ):
        _core(knowledge_features_enabled=True, vector_backend="qdrant")


def test_outbound_container_has_only_egress_dependencies_and_fails_closed() -> None:
    with pytest.raises(
        ContainerDependencyError,
        match=r"outbound container is missing required dependencies: outbox_relay",
    ):
        OutboundContainer(
            bus=object(),  # type: ignore[arg-type]
            dispatcher=object(),  # type: ignore[arg-type]
            http_client=object(),  # type: ignore[arg-type]
            message_store=object(),  # type: ignore[arg-type]
            outbox_relay=None,  # type: ignore[arg-type]
        )


def test_wxbot_bridge_container_has_only_bridge_dependencies() -> None:
    bridge = WxbotBridgeContainer(
        bus=object(),  # type: ignore[arg-type]
        wxbot_store=object(),  # type: ignore[arg-type]
        social_policy_store=object(),  # type: ignore[arg-type]
    )

    assert not hasattr(bridge, "llm_service")
    assert not hasattr(bridge, "vector_store")
    assert not hasattr(bridge, "plugin_registry")
    assert not hasattr(bridge, "__dict__")


def test_global_container_must_be_initialized_explicitly() -> None:
    reset_container()
    with pytest.raises(RuntimeError, match="has not been initialized"):
        get_container()

    scheduler = _scheduler()
    try:
        set_container(scheduler)
        assert get_container() is scheduler
    finally:
        reset_container()
