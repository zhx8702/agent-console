"""Stable, connection-scoped identities for normalized channel traffic.

External providers routinely reuse conversation, participant, and message IDs
across bot accounts.  Runtime persistence must therefore namespace those IDs
by the configured connection before using them as session or idempotency keys.
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal

LEGACY_WXBOT_CONNECTION_ID = "legacy-wechat-default"

ExternalIdentityKind = Literal["conversation", "participant", "message"]


def require_legacy_wxbot_history_scope(
    settings: Any,
    *,
    tenant_id: str,
    connection_id: str,
) -> str:
    """Authorize access to the process-wide legacy wxbot history endpoint.

    The legacy SDK exposes one account-wide database and cannot select a
    managed channel connection.  Callers must therefore carry explicit
    identity and fail closed instead of silently reading another account.
    """

    tenant = _required(tenant_id, "tenant_id")
    connection = _required(connection_id, "connection_id")
    default_tenant = _required(
        str(getattr(settings, "wxbot_default_tenant_id", "") or ""),
        "wxbot_default_tenant_id",
    )
    if tenant != default_tenant:
        raise ValueError("legacy_wxbot_history_tenant_unavailable")
    if connection != LEGACY_WXBOT_CONNECTION_ID:
        raise ValueError("connection_scoped_history_unavailable")
    return connection


def canonical_session_namespace(connection_id: str) -> str:
    """Return the stable namespace owned by a channel connection.

    The legacy wxbot connection deliberately returns an empty namespace so its
    established session IDs remain byte-for-byte compatible.
    """

    connection = _required(connection_id, "connection_id")
    if connection == LEGACY_WXBOT_CONNECTION_ID:
        return ""
    digest = hashlib.sha256(connection.encode("utf-8")).hexdigest()[:20]
    return f"cx1:{digest}"


def canonical_external_id(
    connection_id: str,
    external_id: str,
    *,
    kind: ExternalIdentityKind,
) -> str:
    """Namespace an external ID without exposing provider-controlled content."""

    connection = _required(connection_id, "connection_id")
    external = _required(external_id, "external_id")
    if connection == LEGACY_WXBOT_CONNECTION_ID:
        return external
    digest = hashlib.sha256(
        f"channel-identity-v1\0{kind}\0{connection}\0{external}".encode()
    ).hexdigest()[:48]
    # Preserve only the non-sensitive group-kind marker so existing group
    # policy code can classify a canonical conversation without seeing the
    # provider's original identifier.
    suffix = (
        "@chatroom"
        if kind == "conversation" and external.lower().endswith("@chatroom")
        else ""
    )
    return f"cx1:{kind[0]}:{digest}{suffix}"


def canonical_conversation_id(connection_id: str, external_conversation_id: str) -> str:
    return canonical_external_id(
        connection_id,
        external_conversation_id,
        kind="conversation",
    )


def canonical_participant_id(connection_id: str, external_participant_id: str) -> str:
    return canonical_external_id(
        connection_id,
        external_participant_id,
        kind="participant",
    )


def canonical_message_id(connection_id: str, external_message_id: str) -> str:
    return canonical_external_id(
        connection_id,
        external_message_id,
        kind="message",
    )


def _required(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} cannot be empty")
    return normalized


__all__ = [
    "LEGACY_WXBOT_CONNECTION_ID",
    "canonical_conversation_id",
    "canonical_external_id",
    "canonical_message_id",
    "canonical_participant_id",
    "canonical_session_namespace",
    "require_legacy_wxbot_history_scope",
]
