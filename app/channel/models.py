from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.common.types import Channel, InboundEvent, Session


def _channel_value(channel: Channel | str) -> str:
    return str(getattr(channel, "value", channel) or "").strip()


def _first_metadata_str(metadata: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(metadata.get(key) or "").strip()
        if value:
            return value
    return ""


def configuration_session_id(
    event: InboundEvent,
    session: Session | None = None,
) -> str:
    """Return the operator-facing conversation ID used by plugin configuration.

    Managed channel traffic uses a connection-scoped ``cx1:`` conversation ID
    for runtime state isolation.  Plugin configuration, however, is created
    from the external conversation IDs exposed by channel rosters and the
    admin UI.  Keep a legacy/non-canonical event ID authoritative, and only
    translate connection-scoped events back to their verified external ID.

    This helper is for configuration and configuration-owned data only.  It
    must not be used as the key for runtime session state.
    """

    canonical_session_id = str(event.session_id or "").strip()
    if canonical_session_id and not canonical_session_id.startswith("cx1:"):
        return canonical_session_id

    event_metadata = dict(event.metadata or {})
    session_metadata = dict(session.metadata or {}) if session is not None else {}
    candidates = (
        event.external_conversation_id,
        event_metadata.get("external_conversation_id"),
        event_metadata.get("external_session_id"),
        getattr(session, "external_conversation_id", "") if session is not None else "",
        session_metadata.get("external_conversation_id"),
        session_metadata.get("external_session_id"),
    )
    for value in candidates:
        session_id = str(value or "").strip()
        if session_id and session_id != canonical_session_id:
            return session_id
    return canonical_session_id


def apply_event_scope_to_session(session: Session, event: InboundEvent) -> Session:
    """Copy normalized connection identity from an event into its session."""

    if session.tenant_id != event.tenant_id:
        raise ValueError("event and session tenant_id must match")
    session.channel = event.channel
    session.adapter_id = event.adapter_id
    session.connection_id = event.connection_id
    session.conversation_id = event.conversation_id or event.session_id
    session.external_conversation_id = event.external_conversation_id or event.session_id
    session.canonical_conversation_id = (
        event.canonical_conversation_id or event.conversation_id or event.session_id
    )
    session.external_user_id = event.external_user_id or event.user_id
    session.external_participant_id = (
        event.external_participant_id or event.external_user_id or event.user_id
    )
    session.canonical_participant_id = event.canonical_participant_id or event.user_id
    metadata = dict(session.metadata or {})
    scoped_values = {
        "adapter_id": event.adapter_id,
        "connection_id": event.connection_id,
        "conversation_id": session.conversation_id,
        "external_conversation_id": session.external_conversation_id,
        "canonical_conversation_id": session.canonical_conversation_id,
        "external_user_id": session.external_user_id,
        "external_participant_id": session.external_participant_id,
        "canonical_participant_id": session.canonical_participant_id,
        "external_message_id": event.external_message_id or event.message_id,
        "canonical_message_id": event.canonical_message_id or event.message_id,
    }
    metadata.update({key: value for key, value in scoped_values.items() if value})
    session.metadata = metadata
    return session


@dataclass(frozen=True)
class ChannelTarget:
    tenant_id: str
    channel: str
    session_id: str
    adapter_id: str = ""
    connection_id: str = ""
    external_conversation_id: str = ""
    canonical_conversation_id: str = ""
    external_participant_id: str = ""
    canonical_participant_id: str = ""
    session_name: str = ""
    session_kind: str = ""
    user_id: str = ""
    sender_id: str = ""
    sender_name: str = ""
    reply_to_message_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_event(cls, event: InboundEvent) -> ChannelTarget:
        session_id = str(event.session_id or "")
        metadata = dict(event.metadata or {})
        return cls(
            tenant_id=str(event.tenant_id or ""),
            channel=_channel_value(event.channel),
            session_id=session_id,
            adapter_id=str(event.adapter_id or metadata.get("adapter_id") or ""),
            connection_id=str(event.connection_id or metadata.get("connection_id") or ""),
            external_conversation_id=str(
                event.external_conversation_id
                or metadata.get("external_conversation_id")
                or session_id
            ),
            canonical_conversation_id=str(
                event.canonical_conversation_id
                or event.conversation_id
                or metadata.get("canonical_conversation_id")
                or session_id
            ),
            external_participant_id=str(
                event.external_participant_id
                or event.external_user_id
                or metadata.get("external_participant_id")
                or event.user_id
            ),
            canonical_participant_id=str(
                event.canonical_participant_id
                or metadata.get("canonical_participant_id")
                or event.user_id
            ),
            session_name=str(metadata.get("session_name") or ""),
            session_kind=str(
                metadata.get("session_kind")
                or ("group" if session_id.endswith("@chatroom") else "private")
            ),
            user_id=str(event.user_id or ""),
            sender_id=_first_metadata_str(metadata, "sender_id", "sender_wxid")
            or str(event.user_id or ""),
            sender_name=str(metadata.get("sender_name") or ""),
            reply_to_message_id=_first_metadata_str(
                metadata,
                "reply_to_message_id",
                "msg_svr_id",
                "message_id",
            )
            or str(event.message_id or ""),
            metadata=metadata,
        )

    @classmethod
    def from_session(cls, session: Session) -> ChannelTarget:
        session_id = str(getattr(session, "session_id", "") or "")
        metadata = dict(getattr(session, "metadata", {}) or {})
        return cls(
            tenant_id=str(getattr(session, "tenant_id", "") or ""),
            channel=_channel_value(getattr(session, "channel", "")),
            session_id=session_id,
            adapter_id=str(getattr(session, "adapter_id", "") or metadata.get("adapter_id") or ""),
            connection_id=str(
                getattr(session, "connection_id", "") or metadata.get("connection_id") or ""
            ),
            external_conversation_id=str(
                getattr(session, "external_conversation_id", "")
                or metadata.get("external_conversation_id")
                or session_id
            ),
            canonical_conversation_id=str(
                getattr(session, "canonical_conversation_id", "")
                or getattr(session, "conversation_id", "")
                or metadata.get("canonical_conversation_id")
                or session_id
            ),
            external_participant_id=str(
                getattr(session, "external_participant_id", "")
                or getattr(session, "external_user_id", "")
                or metadata.get("external_participant_id")
                or getattr(session, "user_id", "")
                or ""
            ),
            canonical_participant_id=str(
                getattr(session, "canonical_participant_id", "")
                or metadata.get("canonical_participant_id")
                or getattr(session, "user_id", "")
                or ""
            ),
            session_name=str(metadata.get("session_name") or ""),
            session_kind=str(
                metadata.get("session_kind")
                or ("group" if session_id.endswith("@chatroom") else "private")
            ),
            user_id=str(getattr(session, "user_id", "") or ""),
            sender_id=_first_metadata_str(metadata, "sender_id", "sender_wxid")
            or str(getattr(session, "user_id", "") or ""),
            sender_name=str(metadata.get("sender_name") or ""),
            reply_to_message_id=_first_metadata_str(
                metadata,
                "reply_to_message_id",
                "msg_svr_id",
                "message_id",
            ),
            metadata=metadata,
        )


@dataclass(frozen=True)
class ChannelMedia:
    image_path: str = ""
    image_url: str = ""
    video_path: str = ""
    video_url: str = ""


@dataclass(frozen=True)
class ChannelFile:
    """A file already present on the outbound provider's local filesystem.

    ``file_size`` intentionally distinguishes an omitted assertion (``None``)
    from an explicit zero-byte file (``0``).
    """

    file_path: str
    file_name: str = ""
    file_size: int | None = None
    file_md5: str = ""
    file_sha256: str = ""


@dataclass(frozen=True)
class ChannelSendOptions:
    trace_id: str = ""
    mention_sender: bool | None = None
    reply_to_message_id: str = ""
    source_message: dict[str, Any] = field(default_factory=dict)
    delivery_metadata: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""


@dataclass(frozen=True)
class ChannelSendResult:
    message_id: str = ""
    provider: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
