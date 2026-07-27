"""Extensible adapter catalog and provider factory SPI."""

from __future__ import annotations

import asyncio
import inspect
import math
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from app.channel.connections import ChannelConnectionDocument
    from app.channel.registry import ChannelOutbound


_ADAPTER_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
CHANNEL_ADAPTER_SPI_VERSION = "v1"
CHANNEL_ADAPTER_PROBE_TIMEOUT_MAX_SECONDS = 60.0
CHANNEL_CONFIG_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_SUPPORTED_CONFIG_SCHEMA_KEYS = frozenset(
    {
        "$schema",
        "additionalProperties",
        "description",
        "properties",
        "required",
        "title",
        "type",
    }
)
_SUPPORTED_CONFIG_PROPERTY_KEYS = frozenset(
    {
        "default",
        "description",
        "enum",
        "format",
        "maxLength",
        "maximum",
        "minLength",
        "minimum",
        "title",
        "type",
    }
)
_SUPPORTED_JSON_TYPES = frozenset(
    {"array", "boolean", "integer", "null", "number", "object", "string"}
)


class SecretFieldDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=512)
    required: bool = True
    accepted_ref_schemes: tuple[str, ...] = ("env", "vault", "secret-manager")
    environment_variable: str = ""


class ChannelAdapterDescriptor(BaseModel):
    """Safe metadata used by admin APIs to render connection configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    spi_version: Literal["v1"] = CHANNEL_ADAPTER_SPI_VERSION
    adapter_id: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=512)
    channel: str = Field(min_length=1, max_length=64)
    version: str = Field(default="1", max_length=32)
    capabilities: tuple[str, ...] = ()
    runtime_modes: tuple[str, ...] = ()
    supports_multiple_connections: bool = False
    config_schema: dict[str, Any] = Field(default_factory=dict)
    ui_schema: dict[str, Any] = Field(default_factory=dict)
    secret_fields: tuple[SecretFieldDescriptor, ...] = ()

    @field_validator("adapter_id")
    @classmethod
    def validate_adapter_id(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if not _ADAPTER_ID.fullmatch(normalized):
            raise ValueError("adapter_id must be a lowercase stable identifier")
        return normalized

    @field_validator("channel")
    @classmethod
    def normalize_channel(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if not _ADAPTER_ID.fullmatch(normalized):
            raise ValueError("channel must be a lowercase stable identifier")
        return normalized

    @field_validator("capabilities", "runtime_modes")
    @classmethod
    def normalize_string_set(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                normalized for value in values if (normalized := str(value or "").strip().lower())
            )
        )


class ChannelProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    status: str = Field(default="ok", min_length=1, max_length=64)
    error_code: str = Field(default="", max_length=96)


class ChannelProviderFactory(Protocol):
    def __call__(
        self,
        connection: ChannelConnectionDocument,
    ) -> ChannelOutbound | Awaitable[ChannelOutbound]: ...


class ChannelConnectionProbe(Protocol):
    def __call__(
        self,
        connection: ChannelConnectionDocument,
    ) -> ChannelProbeResult | Awaitable[ChannelProbeResult]: ...


@dataclass(frozen=True, slots=True)
class ChannelAdapterRegistration:
    """Plugin-contributed descriptor plus connection-scoped runtime factory."""

    descriptor: ChannelAdapterDescriptor
    provider_factory: ChannelProviderFactory | None = None
    probe: ChannelConnectionProbe | None = None

    async def create_provider(
        self,
        connection: ChannelConnectionDocument,
    ) -> ChannelOutbound:
        if self.provider_factory is None:
            raise RuntimeError(
                f"channel adapter provider factory is not registered: {self.descriptor.adapter_id}"
            )
        value = self.provider_factory(connection)
        if inspect.isawaitable(value):
            value = await value
        return value

    async def probe_connection(
        self,
        connection: ChannelConnectionDocument,
        *,
        timeout_seconds: float = 10.0,
    ) -> ChannelProbeResult:
        if self.probe is None:
            return ChannelProbeResult(
                ok=False,
                status="unavailable",
                error_code="adapter_probe_unavailable",
            )
        timeout = float(timeout_seconds)
        if (
            not math.isfinite(timeout)
            or timeout <= 0
            or timeout > CHANNEL_ADAPTER_PROBE_TIMEOUT_MAX_SECONDS
        ):
            raise ValueError(
                "probe timeout must be positive and no greater than "
                f"{CHANNEL_ADAPTER_PROBE_TIMEOUT_MAX_SECONDS:g} seconds"
            )

        probe = self.probe
        is_async_callable = inspect.iscoroutinefunction(probe) or inspect.iscoroutinefunction(
            type(probe).__call__
        )

        async def invoke() -> ChannelProbeResult:
            # A plugin is allowed to expose a synchronous probe. Run it on a
            # worker thread so a slow SDK call cannot stall the event loop.
            value = (
                probe(connection)
                if is_async_callable
                else await asyncio.to_thread(probe, connection)
            )
            if inspect.isawaitable(value):
                value = await value
            return ChannelProbeResult.model_validate(value)

        async with asyncio.timeout(timeout):
            return await invoke()


class ChannelAdapterCatalog:
    """Mutable startup catalog; plugins may contribute arbitrary adapter IDs."""

    def __init__(
        self,
        registrations: Iterable[ChannelAdapterRegistration] = (),
        *,
        live_registrations_provider: Callable[[], Iterable[ChannelAdapterRegistration]]
        | None = None,
    ) -> None:
        initial_registrations = tuple(registrations)
        if live_registrations_provider is not None and initial_registrations:
            raise ValueError(
                "live channel adapter catalogs cannot also contain static registrations"
            )
        self._registrations: dict[str, ChannelAdapterRegistration] = {}
        self._live_registrations_provider = live_registrations_provider
        for registration in initial_registrations:
            self.register(registration)

    def register(
        self,
        registration: ChannelAdapterRegistration,
        *,
        replace: bool = False,
    ) -> None:
        if self._live_registrations_provider is not None:
            raise RuntimeError("cannot mutate a live channel adapter catalog")
        adapter_id = registration.descriptor.adapter_id
        _validate_registration_contract(registration)
        if adapter_id in self._registrations and not replace:
            raise ValueError(f"channel adapter already registered: {adapter_id}")
        self._registrations[adapter_id] = registration

    def unregister(self, adapter_id: str) -> bool:
        if self._live_registrations_provider is not None:
            raise RuntimeError("cannot mutate a live channel adapter catalog")
        return self._registrations.pop(_normalize_adapter_id(adapter_id), None) is not None

    def get(self, adapter_id: str) -> ChannelAdapterRegistration | None:
        return self._current_registrations().get(_normalize_adapter_id(adapter_id))

    def require(self, adapter_id: str) -> ChannelAdapterRegistration:
        registration = self.get(adapter_id)
        if registration is None:
            raise KeyError(f"channel adapter is not registered: {adapter_id}")
        return registration

    def list_descriptors(self) -> tuple[ChannelAdapterDescriptor, ...]:
        return tuple(
            registration.descriptor
            for _, registration in sorted(self._current_registrations().items())
        )

    def list_registrations(self) -> tuple[ChannelAdapterRegistration, ...]:
        return tuple(
            registration for _, registration in sorted(self._current_registrations().items())
        )

    def _current_registrations(self) -> dict[str, ChannelAdapterRegistration]:
        provider = self._live_registrations_provider
        if provider is None:
            return self._registrations
        registrations: dict[str, ChannelAdapterRegistration] = {}
        for registration in provider():
            _validate_registration_contract(registration)
            adapter_id = registration.descriptor.adapter_id
            if adapter_id in registrations:
                raise ValueError(
                    f"channel adapter already registered by live provider: {adapter_id}"
                )
            registrations[adapter_id] = registration
        return registrations


WECHAT_SDK_ADAPTER_ID = "wechat-sdk"

WECHAT_SDK_DESCRIPTOR = ChannelAdapterDescriptor(
    adapter_id=WECHAT_SDK_ADAPTER_ID,
    display_name="WeChat SDK",
    description="WeChat bot connection backed by the local SDK bridge.",
    channel="wechat",
    version="1",
    capabilities=(
        "inbound_text",
        "outbound_text",
        "outbound_image",
        "group_mentions",
        "session_roster",
        "media_proxy",
        "health_probe",
    ),
    runtime_modes=("bridge_worker",),
    supports_multiple_connections=True,
    config_schema={
        "$schema": CHANNEL_CONFIG_SCHEMA_DIALECT,
        "type": "object",
        "additionalProperties": False,
        "required": ["sdk_url"],
        "properties": {
            "sdk_url": {
                "type": "string",
                "format": "uri",
                "minLength": 1,
                "title": "微信 SDK 地址",
                "description": "连接器可访问的 wxbot_client HTTP 地址，不要包含凭据。",
            },
            "media_base_url": {
                "type": "string",
                "format": "uri",
                "title": "媒体访问基址",
                "description": "可选；仅在媒体地址与 SDK 地址不同时填写。",
            },
            "poll_interval_seconds": {
                "type": "number",
                "minimum": 0.1,
                "default": 3,
                "title": "入站轮询间隔（秒）",
            },
            "send_interval_seconds": {
                "type": "number",
                "minimum": 0.1,
                "default": 2,
                "title": "出站发送间隔（秒）",
            },
        },
    },
    ui_schema={
        "order": [
            "sdk_url",
            "media_base_url",
            "poll_interval_seconds",
            "send_interval_seconds",
        ],
        "widgets": {
            "sdk_url": "url",
            "media_base_url": "url",
            "poll_interval_seconds": "number",
            "send_interval_seconds": "number",
        },
    },
    # The deployed WeChat SDK exposes its HTTP interface without a platform
    # credential. Keep connection setup limited to the SDK's actual runtime
    # parameters instead of inventing a required token contract.
    secret_fields=(),
)


def build_default_channel_adapter_catalog(
    registrations: Iterable[ChannelAdapterRegistration] = (),
) -> ChannelAdapterCatalog:
    catalog = ChannelAdapterCatalog((ChannelAdapterRegistration(descriptor=WECHAT_SDK_DESCRIPTOR),))
    for registration in registrations:
        catalog.register(registration, replace=True)
    return catalog


def _normalize_adapter_id(value: str) -> str:
    return str(value or "").strip().lower()


def _validate_registration_contract(
    registration: ChannelAdapterRegistration,
) -> None:
    adapter_id = registration.descriptor.adapter_id
    if len(registration.descriptor.secret_fields) > 1:
        raise ValueError(f"channel adapter SPI v1 supports at most one secret field: {adapter_id}")
    _validate_config_schema_contract(registration.descriptor)


def _validate_config_schema_contract(descriptor: ChannelAdapterDescriptor) -> None:
    """Fail fast when an adapter advertises validation we do not implement."""

    schema = descriptor.config_schema
    if not schema:
        return
    unknown = sorted(set(schema).difference(_SUPPORTED_CONFIG_SCHEMA_KEYS))
    if unknown:
        raise ValueError(
            f"unsupported config schema keyword for {descriptor.adapter_id}: {unknown[0]}"
        )
    dialect = str(schema.get("$schema") or "")
    if dialect and dialect != CHANNEL_CONFIG_SCHEMA_DIALECT:
        raise ValueError(
            f"unsupported config schema dialect for {descriptor.adapter_id}: {dialect}"
        )
    if schema.get("type", "object") != "object":
        raise ValueError("channel adapter config schema root type must be object")
    additional = schema.get("additionalProperties", True)
    if not isinstance(additional, bool):
        raise ValueError("config schema additionalProperties must be boolean")
    required = schema.get("required", [])
    if not isinstance(required, (list, tuple)) or not all(
        isinstance(item, str) and item for item in required
    ):
        raise ValueError("config schema required must be an array of field names")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise ValueError("config schema properties must be an object")
    if any(field_name not in properties for field_name in required):
        raise ValueError("config schema required fields must exist in properties")
    for field_name, raw_field_schema in properties.items():
        if not isinstance(field_name, str) or not field_name:
            raise ValueError("config schema property names must be non-empty strings")
        if not isinstance(raw_field_schema, dict):
            raise ValueError(f"config schema property must be an object: {field_name}")
        unknown = sorted(set(raw_field_schema).difference(_SUPPORTED_CONFIG_PROPERTY_KEYS))
        if unknown:
            raise ValueError(
                f"unsupported config schema keyword for {descriptor.adapter_id}."
                f"{field_name}: {unknown[0]}"
            )
        expected_type = str(raw_field_schema.get("type") or "")
        if expected_type and expected_type not in _SUPPORTED_JSON_TYPES:
            raise ValueError(
                f"unsupported config schema type for {descriptor.adapter_id}."
                f"{field_name}: {expected_type}"
            )
        expected_format = str(raw_field_schema.get("format") or "")
        if expected_format and expected_format != "uri":
            raise ValueError(
                f"unsupported config schema format for {descriptor.adapter_id}."
                f"{field_name}: {expected_format}"
            )
        if expected_format and expected_type != "string":
            raise ValueError(f"config schema uri format requires string: {field_name}")

        minimum_length = raw_field_schema.get("minLength")
        maximum_length = raw_field_schema.get("maxLength")
        for keyword, constraint in (
            ("minLength", minimum_length),
            ("maxLength", maximum_length),
        ):
            if constraint is not None and (
                not isinstance(constraint, int) or isinstance(constraint, bool) or constraint < 0
            ):
                raise ValueError(
                    f"config schema {keyword} must be a non-negative integer: {field_name}"
                )
        if (minimum_length is not None or maximum_length is not None) and expected_type != "string":
            raise ValueError(f"config schema length constraints require string: {field_name}")
        if (
            minimum_length is not None
            and maximum_length is not None
            and minimum_length > maximum_length
        ):
            raise ValueError(f"config schema minLength exceeds maxLength: {field_name}")

        minimum = raw_field_schema.get("minimum")
        maximum = raw_field_schema.get("maximum")
        for keyword, constraint in (("minimum", minimum), ("maximum", maximum)):
            if constraint is not None and not _is_finite_json_number(constraint):
                raise ValueError(f"config schema {keyword} must be a finite number: {field_name}")
        if (minimum is not None or maximum is not None) and expected_type not in {
            "integer",
            "number",
        }:
            raise ValueError(f"config schema numeric constraints require number: {field_name}")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError(f"config schema minimum exceeds maximum: {field_name}")

        enum_values = raw_field_schema.get("enum")
        if "enum" in raw_field_schema and (
            not isinstance(enum_values, (list, tuple)) or not enum_values
        ):
            raise ValueError(f"config schema enum must be a non-empty array: {field_name}")
        for value in enum_values or ():
            error = _config_schema_value_error(value, raw_field_schema)
            if error:
                raise ValueError(f"config schema enum value violates {error}: {field_name}")
        if "default" in raw_field_schema:
            default = raw_field_schema["default"]
            error = _config_schema_value_error(default, raw_field_schema)
            if error:
                raise ValueError(f"config schema default violates {error}: {field_name}")
            if enum_values and default not in enum_values:
                raise ValueError(f"config schema default is not present in enum: {field_name}")


def _is_finite_json_number(value: Any) -> bool:
    return bool(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (not isinstance(value, float) or math.isfinite(value))
    )


def _config_schema_value_error(
    value: Any,
    field_schema: dict[str, Any],
) -> str:
    expected_type = str(field_schema.get("type") or "")
    matches_type = {
        "array": lambda item: isinstance(item, list),
        "boolean": lambda item: isinstance(item, bool),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "null": lambda item: item is None,
        "number": _is_finite_json_number,
        "object": lambda item: isinstance(item, dict),
        "string": lambda item: isinstance(item, str),
    }.get(expected_type, lambda _item: True)
    if not matches_type(value):
        return "type"
    if isinstance(value, float) and not math.isfinite(value):
        return "finite-number"
    if isinstance(value, str):
        minimum_length = field_schema.get("minLength")
        maximum_length = field_schema.get("maxLength")
        if minimum_length is not None and len(value) < minimum_length:
            return "minLength"
        if maximum_length is not None and len(value) > maximum_length:
            return "maxLength"
        if field_schema.get("format") == "uri" and not _is_strict_http_uri(value):
            return "uri-format"
    if _is_finite_json_number(value):
        minimum = field_schema.get("minimum")
        maximum = field_schema.get("maximum")
        if minimum is not None and value < minimum:
            return "minimum"
        if maximum is not None and value > maximum:
            return "maximum"
    return ""


def _is_strict_http_uri(value: str) -> bool:
    normalized = str(value or "")
    if not normalized or normalized != normalized.strip():
        return False
    if any(character.isspace() for character in normalized):
        return False
    if "?" in normalized or "#" in normalized:
        return False
    try:
        parsed = urlsplit(normalized)
        _ = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme.lower() in {"http", "https"}
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
    )


__all__ = [
    "CHANNEL_ADAPTER_PROBE_TIMEOUT_MAX_SECONDS",
    "CHANNEL_ADAPTER_SPI_VERSION",
    "CHANNEL_CONFIG_SCHEMA_DIALECT",
    "WECHAT_SDK_ADAPTER_ID",
    "WECHAT_SDK_DESCRIPTOR",
    "ChannelAdapterCatalog",
    "ChannelAdapterDescriptor",
    "ChannelAdapterRegistration",
    "ChannelConnectionProbe",
    "ChannelProbeResult",
    "ChannelProviderFactory",
    "SecretFieldDescriptor",
    "build_default_channel_adapter_catalog",
]
