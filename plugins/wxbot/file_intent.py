"""Fail-closed file intent mapping for wxbot conversations.

A file must not become a side effect of an ordinary answer.  The operation
comes from a semantic decision; this module only validates and projects it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.common.intent import IntentDecision, IntentDomain
from app.common.intent_runtime import is_confident, slot_bool, slot_int, slot_text

FileOperation = Literal[
    "none",
    "inspect_incoming",
    "generate",
    "send_existing",
    "convert",
    "export_history",
]
FileSource = Literal["none", "incoming_attachment", "conversation", "user_path"]

MAX_RECENT_MESSAGE_EXPORT_MINUTES = 24 * 60
_FILE_ACTIONS = {
    "inspect_incoming",
    "generate",
    "send_existing",
    "convert",
    "export_history",
}


@dataclass(frozen=True, slots=True)
class FileIntent:
    """The bounded, auditable result of file intent detection."""

    operation: FileOperation = "none"
    delivery_required: bool = False
    requested_format: str = ""
    source: FileSource = "none"
    confidence: float = 0.0
    has_attachment: bool = False
    needs_confirmation: bool = False
    recent_minutes: int | None = None
    recent_minutes_invalid: bool = False
    cues: tuple[str, ...] = ()

    @property
    def file_requested(self) -> bool:
        return self.operation != "none"

    def as_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "delivery_required": self.delivery_required,
            "requested_format": self.requested_format,
            "source": self.source,
            "confidence": self.confidence,
            "has_attachment": self.has_attachment,
            "needs_confirmation": self.needs_confirmation,
            "recent_minutes": self.recent_minutes,
            "recent_minutes_invalid": self.recent_minutes_invalid,
            "cues": list(self.cues),
        }


def parse_recent_message_minutes(text: str) -> int | None:
    """Minutes are supplied by the semantic decision, not parsed from wording."""

    _ = text
    return None


def classify_file_intent(
    text: str = "",
    *,
    has_attachment: bool = False,
    decision: IntentDecision | None = None,
) -> FileIntent:
    """Project a semantic decision onto the file-delivery contract."""

    _ = text
    attachment = bool(has_attachment)
    if decision is None or decision.domain is not IntentDomain.FILE:
        return FileIntent(has_attachment=attachment)
    if not is_confident(decision):
        return FileIntent(has_attachment=attachment, confidence=decision.confidence)
    action = str(decision.action or "").strip()
    if action not in _FILE_ACTIONS:
        return FileIntent(has_attachment=attachment, confidence=decision.confidence)

    requested_format = slot_text(decision, "format", "requested_format").lower()
    delivery_required = slot_bool(
        decision,
        "delivery_required",
        default=action in {"generate", "send_existing", "convert", "export_history"},
    )
    recent_minutes = slot_int(decision, "recent_minutes")
    recent_minutes_invalid = slot_bool(decision, "recent_minutes_invalid")
    if recent_minutes is not None and (
        recent_minutes <= 0 or recent_minutes > MAX_RECENT_MESSAGE_EXPORT_MINUTES
    ):
        recent_minutes_invalid = True
        recent_minutes = None
    if recent_minutes_invalid:
        recent_minutes = None

    source: FileSource = "none"
    source_slot = slot_text(decision, "source")
    if source_slot in {"incoming_attachment", "conversation", "user_path"}:
        source = source_slot  # type: ignore[assignment]
    elif attachment and action in {"inspect_incoming", "convert", "generate"}:
        source = "incoming_attachment"
    elif action == "export_history":
        source = "conversation"
    elif action == "send_existing":
        source = "user_path"

    if action == "inspect_incoming" and not attachment:
        return FileIntent(has_attachment=False, confidence=decision.confidence)

    return FileIntent(
        operation=action,  # type: ignore[arg-type]
        delivery_required=delivery_required,
        requested_format=requested_format,
        source=source,
        confidence=decision.confidence,
        has_attachment=attachment,
        needs_confirmation=not delivery_required
        and action in {"generate", "send_existing", "convert", "export_history"},
        recent_minutes=recent_minutes,
        recent_minutes_invalid=recent_minutes_invalid,
        cues=(action,),
    )


__all__ = [
    "MAX_RECENT_MESSAGE_EXPORT_MINUTES",
    "FileIntent",
    "classify_file_intent",
    "parse_recent_message_minutes",
]
