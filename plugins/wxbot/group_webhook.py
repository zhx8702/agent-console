"""Per-group webhook tokens and outbound push for third-party platforms."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.channel import ChannelSendOptions, ChannelTarget
from app.channel.identity import LEGACY_WXBOT_CONNECTION_ID, canonical_conversation_id
from app.common.ids import new_trace_id
from app.common.logging import get_logger
from plugins.wxbot.store import _exec

logger = get_logger(__name__)

_TOKEN_PREFIX = "whg_"
_WEBHOOK_ID_PREFIX = "gwh_"


@dataclass(frozen=True)
class GroupWebhookRecord:
    webhook_id: str
    tenant_id: str
    connection_id: str
    external_session_id: str
    canonical_session_id: str
    session_name: str
    token_hash: str
    token_hint: str
    enabled: bool
    created_by: str
    created_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None

    @property
    def active(self) -> bool:
        return self.enabled and self.revoked_at is None


def hash_webhook_token(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def new_webhook_token() -> str:
    return f"{_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


def token_hint(token: str) -> str:
    clean = str(token or "").strip()
    return clean[-4:] if len(clean) >= 4 else ""


_BROWSER_API_PREFIX = "/api"


def webhook_public_path(token: str, *, api_prefix: str = _BROWSER_API_PREFIX) -> str:
    prefix = str(api_prefix or "").strip()
    if prefix and not prefix.startswith("/"):
        prefix = f"/{prefix}"
    prefix = prefix.rstrip("/")
    return f"{prefix}/v1/webhook/groups/{token}"


def webhook_public_url(
    base_url: str,
    token: str,
    *,
    api_prefix: str = _BROWSER_API_PREFIX,
) -> str:
    origin = str(base_url or "").strip().rstrip("/")
    if origin.endswith("/api"):
        origin = origin[: -len("/api")]
    return f"{origin}{webhook_public_path(token, api_prefix=api_prefix)}"


def normalize_group_session_ids(
    session_id: str,
    connection_id: str,
) -> tuple[str, str]:
    session = str(session_id or "").strip()
    if not session.endswith("@chatroom"):
        raise ValueError("group_session_required")
    connection = str(connection_id or "").strip() or LEGACY_WXBOT_CONNECTION_ID
    if session.startswith("cx1:"):
        return session, session
    return session, canonical_conversation_id(connection, session)


def _row_to_record(row: dict[str, Any]) -> GroupWebhookRecord:
    return GroupWebhookRecord(
        webhook_id=str(row.get("webhook_id") or ""),
        tenant_id=str(row.get("tenant_id") or ""),
        connection_id=str(row.get("connection_id") or ""),
        external_session_id=str(row.get("external_session_id") or ""),
        canonical_session_id=str(row.get("canonical_session_id") or ""),
        session_name=str(row.get("session_name") or ""),
        token_hash=str(row.get("token_hash") or ""),
        token_hint=str(row.get("token_hint") or ""),
        enabled=bool(row.get("enabled")),
        created_by=str(row.get("created_by") or ""),
        created_at=row.get("created_at"),
        last_used_at=row.get("last_used_at"),
        revoked_at=row.get("revoked_at"),
    )


def public_webhook_document(record: GroupWebhookRecord) -> dict[str, Any]:
    return {
        "webhook_id": record.webhook_id,
        "tenant_id": record.tenant_id,
        "connection_id": record.connection_id,
        "session_id": record.external_session_id,
        "canonical_session_id": record.canonical_session_id,
        "session_name": record.session_name,
        "token_hint": record.token_hint,
        "enabled": record.active,
        "created_at": record.created_at,
        "last_used_at": record.last_used_at,
        "path_template": "/api/v1/webhook/groups/{token}",
    }


async def get_group_webhook(webhook_id: str) -> GroupWebhookRecord | None:
    rows = await _exec(
        "SELECT webhook_id, tenant_id, connection_id, external_session_id, "
        "canonical_session_id, session_name, token_hash, token_hint, enabled, "
        "created_by, created_at, last_used_at, revoked_at "
        "FROM plugin_wxbot_group_webhook WHERE webhook_id = :wid LIMIT 1",
        {"wid": str(webhook_id or "").strip()},
    )
    return _row_to_record(rows[0]) if rows else None


async def get_group_webhook_by_token(token: str) -> GroupWebhookRecord | None:
    clean = str(token or "").strip()
    if not clean.startswith(_TOKEN_PREFIX) or len(clean) < 12:
        return None
    digest = hash_webhook_token(clean)
    if not digest:
        return None
    rows = await _exec(
        "SELECT webhook_id, tenant_id, connection_id, external_session_id, "
        "canonical_session_id, session_name, token_hash, token_hint, enabled, "
        "created_by, created_at, last_used_at, revoked_at "
        "FROM plugin_wxbot_group_webhook WHERE token_hash = :digest LIMIT 1",
        {"digest": digest},
    )
    return _row_to_record(rows[0]) if rows else None


async def list_group_webhooks(tenant_id: str) -> list[GroupWebhookRecord]:
    rows = await _exec(
        "SELECT webhook_id, tenant_id, connection_id, external_session_id, "
        "canonical_session_id, session_name, token_hash, token_hint, enabled, "
        "created_by, created_at, last_used_at, revoked_at "
        "FROM plugin_wxbot_group_webhook WHERE tenant_id = :tid "
        "ORDER BY created_at DESC, webhook_id DESC",
        {"tid": str(tenant_id or "").strip()},
    )
    return [_row_to_record(row) for row in rows]


async def find_active_group_webhook(
    *,
    tenant_id: str,
    connection_id: str,
    external_session_id: str,
) -> GroupWebhookRecord | None:
    rows = await _exec(
        "SELECT webhook_id, tenant_id, connection_id, external_session_id, "
        "canonical_session_id, session_name, token_hash, token_hint, enabled, "
        "created_by, created_at, last_used_at, revoked_at "
        "FROM plugin_wxbot_group_webhook "
        "WHERE tenant_id = :tid AND connection_id = :cid "
        "AND external_session_id = :sid AND revoked_at IS NULL "
        "LIMIT 1",
        {
            "tid": tenant_id,
            "cid": connection_id,
            "sid": external_session_id,
        },
    )
    return _row_to_record(rows[0]) if rows else None


async def create_group_webhook(
    *,
    tenant_id: str,
    connection_id: str,
    session_id: str,
    session_name: str = "",
    created_by: str = "",
) -> tuple[GroupWebhookRecord, str]:
    external_session_id, canonical_session_id = normalize_group_session_ids(
        session_id,
        connection_id,
    )
    existing = await find_active_group_webhook(
        tenant_id=tenant_id,
        connection_id=connection_id or LEGACY_WXBOT_CONNECTION_ID,
        external_session_id=external_session_id,
    )
    if existing is not None:
        raise ValueError("group_webhook_exists")
    token = new_webhook_token()
    webhook_id = f"{_WEBHOOK_ID_PREFIX}{secrets.token_hex(12)}"
    await _exec(
        "INSERT INTO plugin_wxbot_group_webhook ("
        "webhook_id, tenant_id, connection_id, external_session_id, "
        "canonical_session_id, session_name, token_hash, token_hint, enabled, "
        "created_by"
        ") VALUES ("
        ":wid, :tid, :cid, :ext, :can, :name, :hash, :hint, TRUE, :by"
        ")",
        {
            "wid": webhook_id,
            "tid": tenant_id,
            "cid": connection_id or LEGACY_WXBOT_CONNECTION_ID,
            "ext": external_session_id,
            "can": canonical_session_id,
            "name": str(session_name or "").strip()[:256],
            "hash": hash_webhook_token(token),
            "hint": token_hint(token),
            "by": str(created_by or "").strip()[:128],
        },
    )
    record = await get_group_webhook(webhook_id)
    if record is None:
        raise RuntimeError("group_webhook_create_failed")
    return record, token


async def rotate_group_webhook(webhook_id: str) -> tuple[GroupWebhookRecord, str]:
    record = await get_group_webhook(webhook_id)
    if record is None or not record.active:
        raise ValueError("group_webhook_not_found")
    token = new_webhook_token()
    await _exec(
        "UPDATE plugin_wxbot_group_webhook SET token_hash = :hash, token_hint = :hint "
        "WHERE webhook_id = :wid AND revoked_at IS NULL",
        {
            "hash": hash_webhook_token(token),
            "hint": token_hint(token),
            "wid": webhook_id,
        },
    )
    updated = await get_group_webhook(webhook_id)
    if updated is None:
        raise RuntimeError("group_webhook_rotate_failed")
    return updated, token


async def revoke_group_webhook(webhook_id: str) -> GroupWebhookRecord:
    record = await get_group_webhook(webhook_id)
    if record is None or not record.active:
        raise ValueError("group_webhook_not_found")
    await _exec(
        "UPDATE plugin_wxbot_group_webhook SET enabled = FALSE, revoked_at = :now "
        "WHERE webhook_id = :wid AND revoked_at IS NULL",
        {"now": datetime.now(UTC), "wid": webhook_id},
    )
    updated = await get_group_webhook(webhook_id)
    if updated is None:
        raise RuntimeError("group_webhook_revoke_failed")
    return updated


async def delete_group_webhook(webhook_id: str) -> GroupWebhookRecord:
    record = await get_group_webhook(webhook_id)
    if record is None:
        raise ValueError("group_webhook_not_found")
    await _exec(
        "DELETE FROM plugin_wxbot_group_webhook WHERE webhook_id = :wid",
        {"wid": webhook_id},
    )
    return record


async def touch_group_webhook(webhook_id: str) -> None:
    await _exec(
        "UPDATE plugin_wxbot_group_webhook SET last_used_at = :now "
        "WHERE webhook_id = :wid",
        {"now": datetime.now(UTC), "wid": webhook_id},
    )


async def send_group_webhook_text(
    outbound: Any,
    record: GroupWebhookRecord,
    *,
    text: str,
    message_id: str,
    trace_id: str = "",
) -> Any:
    target = ChannelTarget(
        tenant_id=record.tenant_id,
        channel="wechat",
        session_id=record.canonical_session_id or record.external_session_id,
        adapter_id="wechat-sdk",
        connection_id=record.connection_id,
        external_conversation_id=record.external_session_id,
        canonical_conversation_id=record.canonical_session_id
        or record.external_session_id,
        session_name=record.session_name,
        session_kind="group",
    )
    command_id = (
        f"group-webhook:{record.webhook_id}:{message_id}"
        if message_id
        else f"group-webhook:{record.webhook_id}:{new_trace_id()}"
    )
    return await outbound.send_text(
        target,
        text,
        ChannelSendOptions(
            trace_id=trace_id or new_trace_id(),
            idempotency_key=command_id,
            delivery_metadata={
                "command_id": command_id,
                "idempotency_key": command_id,
                "force_send": True,
                "speech_budget_enabled": False,
                "duplicate_guard_enabled": False,
                "source": "external_group_webhook",
                "speech_class": "scheduled",
                "speech_output_kind": "ordinary",
            },
        ),
    )
