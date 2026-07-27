from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from app.admin.kb_router import PluginScopeStateRequest, _validated_plugin_scope_config
from app.plugin.config_schema import (
    PluginConfigLimits,
    PluginConfigSchemaError,
    PluginConfigValidationError,
    validate_config_payload_bounds,
    validate_config_schema,
    validate_plugin_config,
)


def _full_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "session_name": {"type": "string", "minLength": 1, "maxLength": 128},
            "mode": {"type": "string", "enum": ["quiet", "active"]},
            "threshold": {"type": "number", "minimum": 0, "maximum": 1},
            "retries": {"type": "integer", "minimum": 0, "maximum": 5},
            "enabled": {"type": "boolean"},
            "tags": {
                "type": "array",
                "items": {"type": "string", "pattern": "^[a-z]+$"},
                "maxItems": 3,
                "uniqueItems": True,
            },
            "limits": {
                "type": "object",
                "properties": {"daily": {"type": "integer", "multipleOf": 5}},
                "required": ["daily"],
                "additionalProperties": False,
            },
        },
        "required": ["session_name", "mode"],
        "additionalProperties": False,
    }


def test_plugin_config_schema_accepts_common_local_subset() -> None:
    schema = _full_schema()
    config = {
        "session_name": "测试群",
        "mode": "active",
        "threshold": 0.75,
        "retries": 2,
        "enabled": True,
        "tags": ["ops", "chat"],
        "limits": {"daily": 10},
    }

    validate_config_schema(schema)
    validate_plugin_config(config, schema)


@pytest.mark.parametrize(
    ("config", "path"),
    [
        ({"mode": "active"}, "$/session_name"),
        ({"session_name": "群", "mode": "unknown"}, "$/mode"),
        ({"session_name": "群", "mode": "active", "retries": True}, "$/retries"),
        ({"session_name": "群", "mode": "active", "threshold": 2}, "$/threshold"),
        ({"session_name": "群", "mode": "active", "extra": 1}, "$/extra"),
        (
            {"session_name": "群", "mode": "active", "tags": ["ops", "ops"]},
            "$/tags",
        ),
        ({"session_name": "群", "mode": "active", "limits": {"daily": 7}}, "$/limits/daily"),
    ],
)
def test_plugin_config_rejects_contract_violations(
    config: dict[str, Any], path: str
) -> None:
    with pytest.raises(PluginConfigValidationError) as raised:
        validate_plugin_config(config, _full_schema())

    assert raised.value.code == "plugin_config_invalid"
    assert raised.value.path == path


def test_empty_schema_explicitly_accepts_only_empty_config() -> None:
    validate_plugin_config({}, {})

    with pytest.raises(PluginConfigValidationError) as raised:
        validate_plugin_config({"session_name": "测试群"}, {})

    assert raised.value.code == "plugin_config_not_supported"


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "$ref": "https://example.test/schema.json"},
        {"type": ["object", "null"]},
        {"type": "string"},
        {"type": "object", "properties": [], "additionalProperties": False},
        {
            "type": "object",
            "properties": {"name": {"type": "string", "maxLength": -1}},
        },
        {
            "type": "object",
            "properties": {},
            "required": ["missing"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"value": {"type": "number", "multipleOf": 0}},
        },
    ],
)
def test_plugin_config_schema_fails_closed_for_invalid_or_remote_contracts(
    schema: dict[str, Any],
) -> None:
    with pytest.raises(PluginConfigSchemaError):
        validate_config_schema(schema)


def test_plugin_config_payload_enforces_depth_nodes_keys_and_bytes() -> None:
    limits = PluginConfigLimits(
        max_config_bytes=64,
        max_depth=2,
        max_config_nodes=5,
        max_object_properties=3,
        max_array_items=2,
        max_key_length=4,
        max_string_length=8,
    )

    for invalid in (
        {"abcde": 1},
        {"a": {"b": {"c": {"d": 1}}}},
        {"a": [1, 2, 3]},
        {"a": "123456789"},
        {"a": float("inf")},
    ):
        with pytest.raises(PluginConfigValidationError):
            validate_config_payload_bounds(invalid, limits=limits)


def test_scope_request_accepts_generic_plugin_config_and_preserves_tibo_field() -> None:
    request = PluginScopeStateRequest.model_validate(
        {
            "tenant_id": "demo",
            "session_id": "room-1",
            "enabled": True,
            "config": {
                "session_name": "测试群",
                "mode": "active",
                "limits": {"daily": 10},
            },
        }
    )

    assert request.config == {
        "session_name": "测试群",
        "mode": "active",
        "limits": {"daily": 10},
    }


class _SchemaPlugin:
    def __init__(self, schema: Any) -> None:
        self.schema = schema

    def get_config_schema(self) -> Any:
        return self.schema


class _SchemaRegistry:
    def __init__(self, plugins: dict[str, _SchemaPlugin]) -> None:
        self.loaded_plugins = plugins


class _SchemaManager:
    def __init__(self, plugins: dict[str, _SchemaPlugin]) -> None:
        self.registry = _SchemaRegistry(plugins)


def test_scope_write_validation_rejects_unknown_plugin() -> None:
    manager = _SchemaManager({})

    with pytest.raises(HTTPException) as raised:
        _validated_plugin_scope_config(manager, None, "unknown", {})  # type: ignore[arg-type]

    assert raised.value.status_code == 404
    assert raised.value.detail == "plugin_not_found"


def test_scope_write_validation_maps_bad_value_and_bad_schema_safely() -> None:
    valid_manager = _SchemaManager({"tibo_reset": _SchemaPlugin(_full_schema())})
    with pytest.raises(HTTPException) as bad_value:
        _validated_plugin_scope_config(  # type: ignore[arg-type]
            valid_manager,
            None,
            "tibo_reset",
            {"session_name": "群", "mode": "invalid"},
        )
    assert bad_value.value.status_code == 422
    assert bad_value.value.detail["code"] == "plugin_config_invalid"

    invalid_manager = _SchemaManager(
        {"broken": _SchemaPlugin({"type": "object", "$ref": "https://example.test"})}
    )
    with pytest.raises(HTTPException) as bad_schema:
        _validated_plugin_scope_config(  # type: ignore[arg-type]
            invalid_manager,
            None,
            "broken",
            {},
        )
    assert bad_schema.value.status_code == 503
    assert bad_schema.value.detail["code"] == "plugin_config_schema_invalid"
