"""Bounded, local configuration contracts for plugin scope settings.

This module intentionally implements a small JSON Schema subset instead of
resolving arbitrary schemas.  Plugin configuration is persisted and later
consumed by in-process code, so both the schema and the submitted value must be
bounded before they cross that trust boundary.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PluginConfigLimits:
    max_schema_bytes: int = 64 * 1024
    max_config_bytes: int = 64 * 1024
    max_depth: int = 12
    max_schema_nodes: int = 512
    max_config_nodes: int = 1024
    max_object_properties: int = 128
    max_array_items: int = 256
    max_key_length: int = 128
    max_string_length: int = 16 * 1024
    max_pattern_length: int = 256


DEFAULT_PLUGIN_CONFIG_LIMITS = PluginConfigLimits()

_JSON_TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})
_ANNOTATION_KEYS = frozenset({"title", "description", "default", "examples"})
_COMMON_KEYS = frozenset({"type", "enum", "const"}) | _ANNOTATION_KEYS
_TYPE_KEYS: dict[str, frozenset[str]] = {
    "object": frozenset(
        {"properties", "required", "additionalProperties", "minProperties", "maxProperties"}
    ),
    "array": frozenset({"items", "minItems", "maxItems", "uniqueItems"}),
    "string": frozenset({"minLength", "maxLength", "pattern"}),
    "number": frozenset(
        {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}
    ),
    "integer": frozenset(
        {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"}
    ),
    "boolean": frozenset(),
    "null": frozenset(),
}


class PluginConfigContractError(ValueError):
    """Base error carrying a stable, non-sensitive API detail."""

    def __init__(self, code: str, message: str, *, path: str = "$") -> None:
        super().__init__(message)
        self.code = code
        self.path = path

    def as_detail(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": str(self)}


class PluginConfigSchemaError(PluginConfigContractError):
    def __init__(self, message: str, *, path: str = "$") -> None:
        super().__init__("plugin_config_schema_invalid", message, path=path)


class PluginConfigValidationError(PluginConfigContractError):
    def __init__(
        self,
        message: str,
        *,
        path: str = "$",
        code: str = "plugin_config_invalid",
    ) -> None:
        super().__init__(code, message, path=path)


def validate_config_schema(
    schema: dict[str, Any],
    *,
    limits: PluginConfigLimits = DEFAULT_PLUGIN_CONFIG_LIMITS,
) -> None:
    """Validate the supported schema subset without resolving external input.

    An empty schema is a deliberate declaration that the plugin exposes no
    configurable values.  It is accepted here, but only an empty config object
    will validate against it.
    """

    if not isinstance(schema, dict):
        raise PluginConfigSchemaError("schema must be an object")
    _bounded_json_size(
        schema,
        limit=limits.max_schema_bytes,
        error_type=PluginConfigSchemaError,
        label="schema",
    )
    if not schema:
        return
    counter = [0]
    _validate_schema_node(schema, path="$", depth=0, counter=counter, limits=limits)
    if schema.get("type") != "object":
        raise PluginConfigSchemaError("the root schema type must be object", path="$/type")


def validate_config_payload_bounds(
    config: dict[str, Any],
    *,
    limits: PluginConfigLimits = DEFAULT_PLUGIN_CONFIG_LIMITS,
) -> None:
    """Apply transport-independent size and shape limits to a config object."""

    if not isinstance(config, dict):
        raise PluginConfigValidationError("config must be an object")
    _bounded_json_size(
        config,
        limit=limits.max_config_bytes,
        error_type=PluginConfigValidationError,
        label="config",
    )
    counter = [0]
    _validate_value_bounds(config, path="$", depth=0, counter=counter, limits=limits)


def validate_plugin_config(
    config: dict[str, Any],
    schema: dict[str, Any],
    *,
    limits: PluginConfigLimits = DEFAULT_PLUGIN_CONFIG_LIMITS,
) -> None:
    """Validate a plugin config against a previously untrusted local schema."""

    validate_config_payload_bounds(config, limits=limits)
    validate_config_schema(schema, limits=limits)
    if not schema:
        if config:
            raise PluginConfigValidationError(
                "this plugin does not declare configurable values",
                code="plugin_config_not_supported",
            )
        return
    _validate_instance(config, schema, path="$", limits=limits)


def _validate_schema_node(
    schema: dict[str, Any],
    *,
    path: str,
    depth: int,
    counter: list[int],
    limits: PluginConfigLimits,
) -> None:
    _bump_schema_counter(path=path, depth=depth, counter=counter, limits=limits)
    schema_type = schema.get("type")
    if schema_type is not None and (
        not isinstance(schema_type, str) or schema_type not in _JSON_TYPES
    ):
        raise PluginConfigSchemaError("type must name one supported JSON type", path=f"{path}/type")

    allowed = set(_COMMON_KEYS)
    if isinstance(schema_type, str):
        allowed.update(_TYPE_KEYS[schema_type])
    unknown = sorted(set(schema) - allowed)
    if unknown:
        raise PluginConfigSchemaError(
            f"unsupported schema keyword: {unknown[0]}",
            path=f"{path}/{_escape_pointer(unknown[0])}",
        )

    structural_keys = set(schema) - _COMMON_KEYS
    if structural_keys and schema_type is None:
        raise PluginConfigSchemaError(
            "type is required when type-specific constraints are used",
            path=f"{path}/type",
        )

    for key in ("title", "description"):
        if key in schema:
            value = schema[key]
            if not isinstance(value, str) or len(value) > limits.max_string_length:
                raise PluginConfigSchemaError(
                    f"{key} must be a bounded string", path=f"{path}/{key}"
                )

    if "examples" in schema and not isinstance(schema["examples"], list):
        raise PluginConfigSchemaError("examples must be an array", path=f"{path}/examples")
    if "enum" in schema:
        enum = schema["enum"]
        if not isinstance(enum, list) or not enum or len(enum) > limits.max_array_items:
            raise PluginConfigSchemaError(
                "enum must be a non-empty bounded array", path=f"{path}/enum"
            )
        serialized = [_json_identity(value, path=f"{path}/enum") for value in enum]
        if len(serialized) != len(set(serialized)):
            raise PluginConfigSchemaError("enum values must be unique", path=f"{path}/enum")

    if schema_type == "object":
        _validate_object_schema(schema, path=path, depth=depth, counter=counter, limits=limits)
    elif schema_type == "array":
        _validate_array_schema(schema, path=path, depth=depth, counter=counter, limits=limits)
    elif schema_type == "string":
        _validate_string_schema(schema, path=path, limits=limits)
    elif schema_type in {"number", "integer"}:
        _validate_number_schema(schema, path=path)

    if "const" in schema:
        _json_identity(schema["const"], path=f"{path}/const")
    if "default" in schema:
        _json_identity(schema["default"], path=f"{path}/default")


def _validate_object_schema(
    schema: dict[str, Any],
    *,
    path: str,
    depth: int,
    counter: list[int],
    limits: PluginConfigLimits,
) -> None:
    properties = schema.get("properties", {})
    if not isinstance(properties, dict) or len(properties) > limits.max_object_properties:
        raise PluginConfigSchemaError(
            "properties must be a bounded object", path=f"{path}/properties"
        )
    for key, child in properties.items():
        property_path = f"{path}/properties/{_escape_pointer(key) if isinstance(key, str) else '?'}"
        _validate_key(key, path=property_path, limits=limits, schema=True)
        if not isinstance(child, dict):
            raise PluginConfigSchemaError("property schema must be an object", path=property_path)
        _validate_schema_node(
            child,
            path=property_path,
            depth=depth + 1,
            counter=counter,
            limits=limits,
        )

    required = schema.get("required", [])
    if not isinstance(required, list) or len(required) > limits.max_object_properties:
        raise PluginConfigSchemaError("required must be a bounded array", path=f"{path}/required")
    if any(not isinstance(key, str) for key in required) or len(required) != len(set(required)):
        raise PluginConfigSchemaError(
            "required entries must be unique strings", path=f"{path}/required"
        )
    undeclared = sorted(set(required) - set(properties))
    if undeclared:
        raise PluginConfigSchemaError(
            f"required property is not declared: {undeclared[0]}", path=f"{path}/required"
        )

    additional = schema.get("additionalProperties", False)
    if not isinstance(additional, (bool, dict)):
        raise PluginConfigSchemaError(
            "additionalProperties must be a boolean or schema",
            path=f"{path}/additionalProperties",
        )
    if isinstance(additional, dict):
        _validate_schema_node(
            additional,
            path=f"{path}/additionalProperties",
            depth=depth + 1,
            counter=counter,
            limits=limits,
        )
    _validate_non_negative_limit_pair(
        schema,
        minimum_key="minProperties",
        maximum_key="maxProperties",
        path=path,
        hard_max=limits.max_object_properties,
    )


def _validate_array_schema(
    schema: dict[str, Any],
    *,
    path: str,
    depth: int,
    counter: list[int],
    limits: PluginConfigLimits,
) -> None:
    items = schema.get("items")
    if items is not None:
        if not isinstance(items, dict):
            raise PluginConfigSchemaError("items must be a schema object", path=f"{path}/items")
        _validate_schema_node(
            items,
            path=f"{path}/items",
            depth=depth + 1,
            counter=counter,
            limits=limits,
        )
    if "uniqueItems" in schema and not isinstance(schema["uniqueItems"], bool):
        raise PluginConfigSchemaError(
            "uniqueItems must be a boolean", path=f"{path}/uniqueItems"
        )
    _validate_non_negative_limit_pair(
        schema,
        minimum_key="minItems",
        maximum_key="maxItems",
        path=path,
        hard_max=limits.max_array_items,
    )


def _validate_string_schema(
    schema: dict[str, Any], *, path: str, limits: PluginConfigLimits
) -> None:
    _validate_non_negative_limit_pair(
        schema,
        minimum_key="minLength",
        maximum_key="maxLength",
        path=path,
        hard_max=limits.max_string_length,
    )
    if "pattern" in schema:
        pattern = schema["pattern"]
        if not isinstance(pattern, str) or len(pattern) > limits.max_pattern_length:
            raise PluginConfigSchemaError("pattern must be a bounded string", path=f"{path}/pattern")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise PluginConfigSchemaError("pattern is not valid", path=f"{path}/pattern") from exc


def _validate_number_schema(schema: dict[str, Any], *, path: str) -> None:
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"):
        if key not in schema:
            continue
        value = schema[key]
        if not _is_number(value) or not math.isfinite(float(value)):
            raise PluginConfigSchemaError(f"{key} must be a finite number", path=f"{path}/{key}")
        if key == "multipleOf" and value <= 0:
            raise PluginConfigSchemaError("multipleOf must be positive", path=f"{path}/{key}")
    for minimum_key, maximum_key in (
        ("minimum", "maximum"),
        ("exclusiveMinimum", "exclusiveMaximum"),
    ):
        if minimum_key in schema and maximum_key in schema:
            if schema[minimum_key] > schema[maximum_key]:
                raise PluginConfigSchemaError(
                    f"{minimum_key} cannot exceed {maximum_key}", path=path
                )


def _validate_instance(
    value: Any,
    schema: dict[str, Any],
    *,
    path: str,
    limits: PluginConfigLimits,
) -> None:
    schema_type = schema.get("type")
    if schema_type is not None and not _matches_type(value, schema_type):
        raise PluginConfigValidationError(f"expected {schema_type}", path=path)
    if "enum" in schema:
        identity = _json_identity(value, path=path, validation=True)
        identities = {_json_identity(item, path=path) for item in schema["enum"]}
        if identity not in identities:
            raise PluginConfigValidationError("value is not in enum", path=path)
    if "const" in schema:
        if _json_identity(value, path=path, validation=True) != _json_identity(
            schema["const"], path=path
        ):
            raise PluginConfigValidationError("value does not match const", path=path)

    if schema_type == "object":
        _validate_object_instance(value, schema, path=path, limits=limits)
    elif schema_type == "array":
        _validate_array_instance(value, schema, path=path, limits=limits)
    elif schema_type == "string":
        _validate_string_instance(value, schema, path=path)
    elif schema_type in {"number", "integer"}:
        _validate_number_instance(value, schema, path=path)


def _validate_object_instance(
    value: dict[str, Any],
    schema: dict[str, Any],
    *,
    path: str,
    limits: PluginConfigLimits,
) -> None:
    properties = schema.get("properties", {})
    missing = [key for key in schema.get("required", []) if key not in value]
    if missing:
        raise PluginConfigValidationError(
            f"required property is missing: {missing[0]}",
            path=f"{path}/{_escape_pointer(missing[0])}",
        )
    minimum = schema.get("minProperties")
    maximum = schema.get("maxProperties")
    if minimum is not None and len(value) < minimum:
        raise PluginConfigValidationError("object has too few properties", path=path)
    if maximum is not None and len(value) > maximum:
        raise PluginConfigValidationError("object has too many properties", path=path)

    additional = schema.get("additionalProperties", False)
    for key, item in value.items():
        child_path = f"{path}/{_escape_pointer(key)}"
        child_schema = properties.get(key)
        if child_schema is not None:
            _validate_instance(item, child_schema, path=child_path, limits=limits)
        elif additional is False:
            raise PluginConfigValidationError("additional property is not allowed", path=child_path)
        elif isinstance(additional, dict):
            _validate_instance(item, additional, path=child_path, limits=limits)


def _validate_array_instance(
    value: list[Any],
    schema: dict[str, Any],
    *,
    path: str,
    limits: PluginConfigLimits,
) -> None:
    minimum = schema.get("minItems")
    maximum = schema.get("maxItems")
    if minimum is not None and len(value) < minimum:
        raise PluginConfigValidationError("array has too few items", path=path)
    if maximum is not None and len(value) > maximum:
        raise PluginConfigValidationError("array has too many items", path=path)
    if schema.get("uniqueItems"):
        identities = [_json_identity(item, path=path, validation=True) for item in value]
        if len(identities) != len(set(identities)):
            raise PluginConfigValidationError("array items must be unique", path=path)
    items = schema.get("items")
    if isinstance(items, dict):
        for index, item in enumerate(value):
            _validate_instance(item, items, path=f"{path}/{index}", limits=limits)


def _validate_string_instance(value: str, schema: dict[str, Any], *, path: str) -> None:
    minimum = schema.get("minLength")
    maximum = schema.get("maxLength")
    if minimum is not None and len(value) < minimum:
        raise PluginConfigValidationError("string is too short", path=path)
    if maximum is not None and len(value) > maximum:
        raise PluginConfigValidationError("string is too long", path=path)
    pattern = schema.get("pattern")
    if pattern is not None and re.search(pattern, value) is None:
        raise PluginConfigValidationError("string does not match pattern", path=path)


def _validate_number_instance(value: int | float, schema: dict[str, Any], *, path: str) -> None:
    checks = (
        ("minimum", lambda boundary: value >= boundary, "number is below minimum"),
        ("maximum", lambda boundary: value <= boundary, "number is above maximum"),
        ("exclusiveMinimum", lambda boundary: value > boundary, "number is not above minimum"),
        ("exclusiveMaximum", lambda boundary: value < boundary, "number is not below maximum"),
    )
    for key, predicate, message in checks:
        if key in schema and not predicate(schema[key]):
            raise PluginConfigValidationError(message, path=path)
    multiple = schema.get("multipleOf")
    if multiple is not None:
        quotient = float(value) / float(multiple)
        if not math.isclose(quotient, round(quotient), rel_tol=1e-9, abs_tol=1e-12):
            raise PluginConfigValidationError("number is not a multipleOf value", path=path)


def _validate_value_bounds(
    value: Any,
    *,
    path: str,
    depth: int,
    counter: list[int],
    limits: PluginConfigLimits,
) -> None:
    if depth > limits.max_depth:
        raise PluginConfigValidationError("config exceeds maximum depth", path=path)
    counter[0] += 1
    if counter[0] > limits.max_config_nodes:
        raise PluginConfigValidationError("config has too many values", path=path)
    if isinstance(value, dict):
        if len(value) > limits.max_object_properties:
            raise PluginConfigValidationError("object has too many properties", path=path)
        for key, child in value.items():
            child_path = f"{path}/{_escape_pointer(key) if isinstance(key, str) else '?'}"
            _validate_key(key, path=child_path, limits=limits, schema=False)
            _validate_value_bounds(
                child,
                path=child_path,
                depth=depth + 1,
                counter=counter,
                limits=limits,
            )
    elif isinstance(value, list):
        if len(value) > limits.max_array_items:
            raise PluginConfigValidationError("array has too many items", path=path)
        for index, child in enumerate(value):
            _validate_value_bounds(
                child,
                path=f"{path}/{index}",
                depth=depth + 1,
                counter=counter,
                limits=limits,
            )
    elif isinstance(value, str):
        if len(value) > limits.max_string_length:
            raise PluginConfigValidationError("string is too long", path=path)
    elif value is None or isinstance(value, bool):
        return
    elif _is_number(value):
        if not math.isfinite(float(value)):
            raise PluginConfigValidationError("number must be finite", path=path)
    else:
        raise PluginConfigValidationError("config contains a non-JSON value", path=path)


def _validate_non_negative_limit_pair(
    schema: dict[str, Any],
    *,
    minimum_key: str,
    maximum_key: str,
    path: str,
    hard_max: int,
) -> None:
    for key in (minimum_key, maximum_key):
        if key not in schema:
            continue
        value = schema[key]
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= hard_max:
            raise PluginConfigSchemaError(
                f"{key} must be an integer between 0 and {hard_max}", path=f"{path}/{key}"
            )
    if minimum_key in schema and maximum_key in schema:
        if schema[minimum_key] > schema[maximum_key]:
            raise PluginConfigSchemaError(
                f"{minimum_key} cannot exceed {maximum_key}", path=path
            )


def _bump_schema_counter(
    *, path: str, depth: int, counter: list[int], limits: PluginConfigLimits
) -> None:
    if depth > limits.max_depth:
        raise PluginConfigSchemaError("schema exceeds maximum depth", path=path)
    counter[0] += 1
    if counter[0] > limits.max_schema_nodes:
        raise PluginConfigSchemaError("schema has too many nodes", path=path)


def _validate_key(
    key: Any,
    *,
    path: str,
    limits: PluginConfigLimits,
    schema: bool,
) -> None:
    error_type = PluginConfigSchemaError if schema else PluginConfigValidationError
    if not isinstance(key, str) or not key or len(key) > limits.max_key_length:
        raise error_type("object keys must be non-empty bounded strings", path=path)


def _bounded_json_size(value: Any, *, limit: int, error_type: type, label: str) -> None:
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError) as exc:
        raise error_type(f"{label} must contain only finite JSON values") from exc
    if len(serialized) > limit:
        raise error_type(f"{label} exceeds {limit} bytes")


def _json_identity(value: Any, *, path: str, validation: bool = False) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (RecursionError, TypeError, ValueError) as exc:
        if validation:
            raise PluginConfigValidationError("value is not valid JSON", path=path) from exc
        raise PluginConfigSchemaError("schema value is not valid JSON", path=path) from exc


def _matches_type(value: Any, schema_type: str) -> bool:
    if schema_type == "object":
        return isinstance(value, dict)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "number":
        return _is_number(value) and math.isfinite(float(value))
    if schema_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if schema_type == "boolean":
        return isinstance(value, bool)
    if schema_type == "null":
        return value is None
    return False


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


__all__ = [
    "DEFAULT_PLUGIN_CONFIG_LIMITS",
    "PluginConfigContractError",
    "PluginConfigLimits",
    "PluginConfigSchemaError",
    "PluginConfigValidationError",
    "validate_config_payload_bounds",
    "validate_config_schema",
    "validate_plugin_config",
]
