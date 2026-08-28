"""Small, serializable semantic-intent contract.

The contract is deliberately independent from a classifier implementation.  A
provider can project a native tool call into :class:`IntentDecision` without
having to expose the provider's response shape to the rest of the application.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from enum import Enum
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator


class IntentOperation(str, Enum):
    """The user-visible operation implied by a message."""

    CONVERSE = "converse"
    RETRIEVE = "retrieve"
    CREATE = "create"
    UPDATE = "update"
    EXECUTE = "execute"
    HANDOFF = "handoff"
    UNKNOWN = "unknown"


class IntentSource(str, Enum):
    """Where information or an external capability should come from."""

    NONE = "none"
    WEB = "web"
    X = "x"
    KNOWLEDGE_BASE = "knowledge_base"
    LOCAL_HISTORY = "local_history"
    USER_ATTACHMENT = "user_attachment"
    UNKNOWN = "unknown"


class IntentArtifact(str, Enum):
    """The kind of result the user is asking for."""

    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    FILE = "file"
    STRUCTURED_DATA = "structured_data"
    UNKNOWN = "unknown"


class IntentDomain(str, Enum):
    """Application domain that should consume the decision."""

    NONE = "none"
    IDENTITY = "identity"
    HANDOFF = "handoff"
    COMPLAINT = "complaint"
    FAQ = "faq"
    BUSINESS = "business"
    CHITCHAT = "chitchat"
    CREDITS = "credits"
    FILE = "file"
    MAP = "map"
    DRAW = "draw"
    VIDEO = "video"
    MEMORY = "memory"
    TIBO_RESET = "tibo_reset"
    AVATAR = "avatar"
    GROUP_INFO = "group_info"
    GROUP_PLUGIN_STATUS = "group_plugin_status"
    WEB_SEARCH = "web_search"
    UNKNOWN = "unknown"


_EnumT = TypeVar("_EnumT", bound=Enum)
_DEFAULT_OPERATION = IntentOperation.UNKNOWN
_DEFAULT_SOURCE = IntentSource.NONE
_DEFAULT_ARTIFACT = IntentArtifact.TEXT
_DEFAULT_DOMAIN = IntentDomain.NONE


def _enum_value(value: _EnumT | str) -> str:
    return str(getattr(value, "value", value))


def _coerce_enum(value: Any, enum_type: type[_EnumT], default: _EnumT) -> _EnumT:
    """Normalize an enum-like value and downgrade unknown values safely."""

    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        return default
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    try:
        return enum_type(normalized)
    except ValueError:
        return default


def _coerce_text(value: Any, *, limit: int, default: str = "") -> str:
    if not isinstance(value, str):
        return default
    return value.strip()[:limit]


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "on"}:
            return True
        if normalized in {"false", "0", "no", "n", "off"}:
            return False
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    return default


def _coerce_slots(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    slots: dict[str, Any] = {}
    for raw_key, raw_item in list(value.items())[:16]:
        key = _coerce_text(raw_key, limit=64)
        if not key:
            continue
        if isinstance(raw_item, bool) or raw_item is None:
            slots[key] = raw_item
        elif isinstance(raw_item, int):
            slots[key] = raw_item
        elif isinstance(raw_item, float):
            if math.isfinite(raw_item):
                slots[key] = raw_item
        elif isinstance(raw_item, str):
            slots[key] = raw_item.strip()[:400]
    return slots


def _coerce_confidence(value: Any, *, default: float = 0.0) -> float:
    """Return a finite probability in [0, 1], or ``default`` otherwise."""

    if isinstance(value, bool):
        return default
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        return default
    return confidence


class IntentDecision(BaseModel):
    """Provider-neutral semantic intent.

    ``from_dict`` and ``from_json`` are deliberately fail-soft because model
    output is untrusted input.  Direct model construction remains strict for
    the confidence boundary, which makes programming errors visible in tests
    and in code that already owns validation.
    """

    model_config = ConfigDict(
        extra="ignore",
        frozen=False,
        populate_by_name=True,
    )

    operation: IntentOperation = _DEFAULT_OPERATION
    source: IntentSource = _DEFAULT_SOURCE
    artifact: IntentArtifact = _DEFAULT_ARTIFACT
    domain: IntentDomain = _DEFAULT_DOMAIN
    action: str = Field(default="", max_length=64)
    query: str = Field(default="", max_length=4000)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    needs_tool: bool = False
    tool_name: str | None = Field(default=None, max_length=128)
    slots: dict[str, Any] = Field(default_factory=dict)

    @field_validator("confidence", mode="before")
    @classmethod
    def validate_confidence(cls, value: Any) -> float:
        if isinstance(value, bool):
            raise ValueError("confidence must be a finite number between 0 and 1")
        try:
            confidence = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("confidence must be a finite number between 0 and 1") from exc
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be a finite number between 0 and 1")
        return confidence

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | Any) -> IntentDecision:
        """Safely parse a mapping, downgrading malformed values to defaults."""

        if not isinstance(value, Mapping):
            return cls()

        payload = dict(value)
        payload["operation"] = _coerce_enum(
            payload.get("operation"), IntentOperation, _DEFAULT_OPERATION
        )
        source_default = (
            _DEFAULT_SOURCE
            if "source" not in payload or payload.get("source") is None
            else IntentSource.UNKNOWN
        )
        payload["source"] = _coerce_enum(payload.get("source"), IntentSource, source_default)
        artifact_default = (
            _DEFAULT_ARTIFACT
            if "artifact" not in payload or payload.get("artifact") is None
            else IntentArtifact.UNKNOWN
        )
        payload["artifact"] = _coerce_enum(
            payload.get("artifact"), IntentArtifact, artifact_default
        )
        domain_default = (
            _DEFAULT_DOMAIN
            if "domain" not in payload or payload.get("domain") is None
            else IntentDomain.UNKNOWN
        )
        payload["domain"] = _coerce_enum(payload.get("domain"), IntentDomain, domain_default)
        payload["action"] = _coerce_text(payload.get("action"), limit=64)
        payload["query"] = _coerce_text(payload.get("query"), limit=4000)
        payload["confidence"] = _coerce_confidence(payload.get("confidence"))
        payload["needs_tool"] = _coerce_bool(payload.get("needs_tool"))
        if payload.get("tool_name") is not None:
            payload["tool_name"] = _coerce_text(payload.get("tool_name"), limit=128) or None
        payload["slots"] = _coerce_slots(payload.get("slots"))

        try:
            return cls.model_validate(payload)
        except Exception:
            # A model response must never break the message path.  Preserve
            # only the fields that have already passed the cheap normalizers.
            return cls(
                operation=payload["operation"],
                source=payload["source"],
                artifact=payload["artifact"],
                domain=payload["domain"],
                action=payload["action"],
                query=payload["query"],
                confidence=payload["confidence"],
                needs_tool=payload["needs_tool"],
                tool_name=payload.get("tool_name"),
                slots=payload["slots"],
            )

    @classmethod
    def from_json(cls, value: str | bytes | bytearray | Any) -> IntentDecision:
        """Safely parse a JSON object; invalid/non-object JSON becomes unknown."""

        try:
            payload = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return cls()
        return cls.from_dict(payload)

    def to_minimal_dict(self) -> dict[str, Any]:
        """Return the compact audit/tool-projection representation."""

        result: dict[str, Any] = {
            "operation": _enum_value(self.operation),
            "source": _enum_value(self.source),
            "artifact": _enum_value(self.artifact),
            "domain": _enum_value(self.domain),
            "confidence": self.confidence,
            "needs_tool": self.needs_tool,
        }
        if self.action:
            result["action"] = self.action
        if self.query:
            result["query"] = self.query
        if self.tool_name:
            result["tool_name"] = self.tool_name
        if self.slots:
            result["slots"] = dict(self.slots)
        return result

    def to_minimal_json(self) -> str:
        """Serialize :meth:`to_minimal_dict` without provider-specific fields."""

        return json.dumps(self.to_minimal_dict(), ensure_ascii=False, separators=(",", ":"))
