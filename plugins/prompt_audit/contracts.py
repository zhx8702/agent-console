"""Stable contracts for the standalone prompt-audit engine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any


class AuditMode(str, Enum):
    OFF = "off"
    OBSERVE = "observe"
    BLOCKING = "blocking"


class AuditDecisionKind(str, Enum):
    ALLOW = "allow"
    FLAG = "flag"
    BLOCK = "block"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


class AuditRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SafetyLabel(str, Enum):
    SAFE = "Safe"
    CONTROVERSIAL = "Controversial"
    UNSAFE = "Unsafe"


class RiskCategory(str, Enum):
    VIOLENT = "violent"
    NON_VIOLENT_ILLEGAL_ACTS = "non_violent_illegal_acts"
    SEXUAL_CONTENT = "sexual_content"
    PII = "pii"
    SELF_HARM = "self_harm"
    UNETHICAL_ACTS = "unethical_acts"
    POLITICALLY_SENSITIVE = "politically_sensitive"
    COPYRIGHT = "copyright"
    JAILBREAK = "jailbreak"


@dataclass(frozen=True, slots=True)
class AuditRequest:
    """Provider-neutral content supplied by a future inbound adapter.

    ``text`` is the newest user content. ``prior_text`` is ordered newest to
    oldest so truncation and chunking can preserve that priority.
    """

    request_id: str
    text: str
    tenant_id: str = ""
    session_id: str = ""
    user_id: str = ""
    channel: str = ""
    message_type: str = "text"
    prior_text: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "request_id",
            "text",
            "tenant_id",
            "session_id",
            "user_id",
            "channel",
            "message_type",
        ):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"audit {name} must be a string")
        if not str(self.request_id or "").strip():
            raise ValueError("audit request_id cannot be empty")
        if any(not isinstance(value, str) for value in self.prior_text):
            raise TypeError("audit prior_text values must be strings")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("audit metadata must be a mapping")
        object.__setattr__(self, "prior_text", tuple(self.prior_text))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class ScanResult:
    kind: AuditDecisionKind
    risk: AuditRisk
    safety: SafetyLabel
    categories: tuple[RiskCategory, ...] = ()
    unknown_categories: tuple[str, ...] = ()
    endpoint_id: str = ""
    latency_ms: int = 0
    chunk_count: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.kind, AuditDecisionKind):
            raise TypeError("scan result kind must be an AuditDecisionKind")
        if not isinstance(self.risk, AuditRisk):
            raise TypeError("scan result risk must be an AuditRisk")
        if not isinstance(self.safety, SafetyLabel):
            raise TypeError("scan result safety must be a SafetyLabel")
        if self.kind not in {
            AuditDecisionKind.ALLOW,
            AuditDecisionKind.FLAG,
            AuditDecisionKind.BLOCK,
        }:
            raise ValueError("scan result must be allow, flag, or block")
        if self.latency_ms < 0:
            raise ValueError("scan result latency_ms cannot be negative")
        if self.chunk_count < 1:
            raise ValueError("scan result chunk_count must be positive")
        expected = {
            AuditDecisionKind.ALLOW: (SafetyLabel.SAFE, AuditRisk.LOW),
            AuditDecisionKind.FLAG: (SafetyLabel.CONTROVERSIAL, AuditRisk.MEDIUM),
            AuditDecisionKind.BLOCK: (SafetyLabel.UNSAFE, AuditRisk.HIGH),
        }[self.kind]
        if (self.safety, self.risk) != expected:
            raise ValueError("scan result safety/risk is inconsistent with its decision")
        if self.kind == AuditDecisionKind.ALLOW and (
            self.categories or self.unknown_categories
        ):
            raise ValueError("safe scan result cannot contain risk categories")
        if self.kind != AuditDecisionKind.ALLOW and not self.categories:
            raise ValueError("non-safe scan result requires at least one category")


@dataclass(frozen=True, slots=True)
class AuditDecision:
    kind: AuditDecisionKind
    mode: AuditMode
    risk: AuditRisk = AuditRisk.LOW
    categories: tuple[RiskCategory, ...] = ()
    unknown_categories: tuple[str, ...] = ()
    error_code: str = ""
    endpoint_id: str = ""
    prompt_hash: str = ""
    redacted_preview: str = ""
    chunk_count: int = 0
    latency_ms: int = 0

    @property
    def allow_next_stage(self) -> bool:
        return self.kind in {AuditDecisionKind.ALLOW, AuditDecisionKind.FLAG}
