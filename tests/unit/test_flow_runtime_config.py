from app.common.config import Settings
from app.orchestrator.flow_runtime_config import (
    build_flow_runtime_config_payload,
    flow_runtime_allowed,
)


def _auto_runtime_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "orchestrator_flow_runtime_enabled": True,
        "orchestrator_flow_runtime_name": "auto",
        "orchestrator_flow_runtime_allowed_names": (
            "auto,default_compatible_flow,default_private_channel_flow,"
            "default_wechat_group_flow"
        ),
        "orchestrator_flow_runtime_allow_target_flows": True,
        "orchestrator_flow_runtime_allow_compatible_fallback": False,
    }
    values.update(overrides)
    return Settings(**values)


def test_private_flow_is_allowed_without_enabling_compatible_fallback() -> None:
    allowed, reason = flow_runtime_allowed(
        _auto_runtime_settings(),
        "auto",
        "default_private_channel_flow",
    )

    assert allowed is True
    assert reason == "allowed"


def test_unmodelled_compatible_fallback_remains_fail_closed() -> None:
    allowed, reason = flow_runtime_allowed(
        _auto_runtime_settings(),
        "auto",
        "default_compatible_flow",
    )

    assert allowed is False
    assert reason == "compatible_fallback_not_allowed"


def test_runtime_payload_is_channel_neutral() -> None:
    payload = build_flow_runtime_config_payload(_auto_runtime_settings())

    assert payload["enabled"] is True
    assert payload["name"] == "auto"
    assert payload["allowed"] is True
    assert "session_coverage" not in payload
