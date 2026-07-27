"""Typed result contract between orchestration and inbound consumption."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ProcessingStatus(StrEnum):
    COMPLETED = "completed"
    INTENTIONALLY_SUPPRESSED = "intentionally_suppressed"
    RETRYABLE_FAILURE = "retryable_failure"
    PERMANENT_FAILURE = "permanent_failure"


@dataclass(frozen=True)
class ProcessingOutcome:
    """Final disposition of one inbound message.

    Only ``completed`` and ``intentionally_suppressed`` are safe for the bus
    to acknowledge directly. A permanent failure must first be durably moved
    to the DLQ; a retryable failure must remain pending or enter durable retry.
    """

    status: ProcessingStatus
    route_label: str = "unknown"
    reason: str = ""
    error_type: str = ""

    @property
    def ackable(self) -> bool:
        return self.status in {
            ProcessingStatus.COMPLETED,
            ProcessingStatus.INTENTIONALLY_SUPPRESSED,
        }

    @classmethod
    def completed(
        cls,
        *,
        route_label: str = "unknown",
        reason: str = "",
    ) -> ProcessingOutcome:
        return cls(
            status=ProcessingStatus.COMPLETED,
            route_label=route_label,
            reason=reason,
        )

    @classmethod
    def intentionally_suppressed(
        cls,
        *,
        route_label: str = "unknown",
        reason: str = "",
    ) -> ProcessingOutcome:
        return cls(
            status=ProcessingStatus.INTENTIONALLY_SUPPRESSED,
            route_label=route_label,
            reason=reason,
        )

    @classmethod
    def retryable_failure(
        cls,
        *,
        route_label: str = "unknown",
        reason: str = "",
        error_type: str = "",
    ) -> ProcessingOutcome:
        return cls(
            status=ProcessingStatus.RETRYABLE_FAILURE,
            route_label=route_label,
            reason=reason,
            error_type=error_type,
        )

    @classmethod
    def permanent_failure(
        cls,
        *,
        route_label: str = "unknown",
        reason: str = "",
        error_type: str = "",
    ) -> ProcessingOutcome:
        return cls(
            status=ProcessingStatus.PERMANENT_FAILURE,
            route_label=route_label,
            reason=reason,
            error_type=error_type,
        )


class ProcessingFailure(RuntimeError):
    """Base exception for collaborators that already know failure class."""

    def __init__(self, reason: str, *, error_type: str = "") -> None:
        normalized = str(reason or "processing_failure").strip() or "processing_failure"
        super().__init__(normalized)
        self.reason = normalized
        self.error_type = error_type or self.__class__.__name__


class RetryableProcessingError(ProcessingFailure):
    @classmethod
    def from_outcome(cls, outcome: ProcessingOutcome) -> RetryableProcessingError:
        return cls(
            outcome.reason or "retryable_processing_failure",
            error_type=outcome.error_type or cls.__name__,
        )


class PermanentProcessingError(ProcessingFailure):
    pass


def normalize_processing_outcome(value: Any) -> ProcessingOutcome:
    """Adapt legacy orchestrators while rejecting ambiguous new results.

    Existing orchestrators historically returned ``None`` or a route label.
    Those values remain successful. Flow-like objects with a known ``status``
    are also adapted. Unknown structured values are retryable instead of being
    silently acknowledged.
    """

    if isinstance(value, ProcessingOutcome):
        return value
    if value is None:
        return ProcessingOutcome.completed(reason="legacy_none_result")
    if isinstance(value, str):
        normalized = value.strip()
        if normalized in {item.value for item in ProcessingStatus}:
            return ProcessingOutcome(status=ProcessingStatus(normalized))
        return ProcessingOutcome.completed(
            route_label=normalized or "unknown",
            reason="legacy_route_result",
        )

    status = str(getattr(value, "status", "") or "").strip().lower()
    if status == "completed":
        return ProcessingOutcome.completed(reason="legacy_flow_completed")
    if status in {"stopped", "deferred"}:
        return ProcessingOutcome.intentionally_suppressed(
            reason=str(getattr(value, "stop_reason", "") or status),
        )
    if status == "failed":
        return ProcessingOutcome.retryable_failure(
            reason=str(getattr(value, "error", "") or "legacy_flow_failed"),
        )

    return ProcessingOutcome.retryable_failure(
        reason=f"invalid_processing_outcome:{value.__class__.__name__}",
        error_type="InvalidProcessingOutcome",
    )
