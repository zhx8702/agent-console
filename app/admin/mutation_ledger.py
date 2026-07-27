"""Transactional idempotency and semantic audit for high-risk plugin writes.

The ledger deliberately accepts an already-open ``AsyncConnection``.  A
caller must execute its business mutation through that same connection so the
claim, mutation result, and secret-free semantic audit commit or roll back as
one unit.

Only the response needed for exact replay is retained in the operational
idempotency table.  The separate audit table is defensively restricted to
small state metadata and never receives request bodies, prompts, samples, or
secrets.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi.encoders import jsonable_encoder
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection

_metadata = MetaData()

plugin_admin_mutation_idempotency = Table(
    "plugin_admin_mutation_idempotency",
    _metadata,
    Column("id", String(36), primary_key=True),
    Column("tenant_id", String(64), nullable=False),
    Column("plugin_name", String(64), nullable=False),
    Column("operation", String(96), nullable=False),
    Column("resource_key_hash", String(64), nullable=False),
    Column("idempotency_key_hash", String(64), nullable=False),
    Column("request_hash", String(64), nullable=False),
    Column("response_status_code", Integer, nullable=True),
    Column("response_json", JSON, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint(
        "tenant_id",
        "plugin_name",
        "operation",
        "idempotency_key_hash",
        name="uq_plugin_admin_mutation_idempotency_key",
    ),
)
Index(
    "ix_plugin_admin_mutation_idempotency_created",
    plugin_admin_mutation_idempotency.c.tenant_id,
    plugin_admin_mutation_idempotency.c.plugin_name,
    plugin_admin_mutation_idempotency.c.created_at,
)

plugin_admin_mutation_audit = Table(
    "plugin_admin_mutation_audit",
    _metadata,
    Column("id", String(36), primary_key=True),
    Column(
        "mutation_id",
        String(36),
        ForeignKey("plugin_admin_mutation_idempotency.id"),
        nullable=False,
        unique=True,
    ),
    Column("tenant_id", String(64), nullable=False),
    Column("plugin_name", String(64), nullable=False),
    Column("operation", String(96), nullable=False),
    Column("actor", String(128), nullable=False),
    Column("actor_kind", String(32), nullable=False),
    Column("roles_json", JSON, nullable=False),
    Column("scope_json", JSON, nullable=False),
    Column("before_state_json", JSON, nullable=False),
    Column("after_state_json", JSON, nullable=False),
    Column("reason_code", String(96), nullable=False),
    Column("reason_hash", String(64), nullable=False),
    Column("trace_id", String(128), nullable=False),
    Column("resource_version", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
Index(
    "ix_plugin_admin_mutation_audit_scope_created",
    plugin_admin_mutation_audit.c.tenant_id,
    plugin_admin_mutation_audit.c.plugin_name,
    plugin_admin_mutation_audit.c.created_at,
)
Index(
    "ix_plugin_admin_mutation_audit_trace",
    plugin_admin_mutation_audit.c.trace_id,
)


class MutationLedgerError(RuntimeError):
    """Base error for the durable mutation ledger."""


class MutationIdempotencyConflictError(MutationLedgerError):
    """An idempotency key was reused for a different canonical request."""


class UnsafeMutationAuditError(MutationLedgerError):
    """Audit metadata contains a field that may carry sensitive prose."""


@dataclass(frozen=True)
class MutationIdentity:
    tenant_id: str
    plugin_name: str
    operation: str
    resource_key: str
    idempotency_key: str
    request_payload: Any


@dataclass(frozen=True)
class MutationAudit:
    actor: str
    actor_kind: str = "admin"
    roles: Sequence[str] = field(default_factory=tuple)
    scope: Mapping[str, Any] = field(default_factory=dict)
    reason_code: str = "manual_change"
    reason: str = ""
    trace_id: str = ""


@dataclass(frozen=True)
class MutationChange:
    response: Any
    before_state: Mapping[str, Any] = field(default_factory=dict)
    after_state: Mapping[str, Any] = field(default_factory=dict)
    resource_version: str = ""
    status_code: int = 200


@dataclass(frozen=True)
class MutationOutcome:
    response: Any
    status_code: int
    replayed: bool
    mutation_id: str


MutationCallback = Callable[[], Awaitable[MutationChange]]

_SAFE_AUDIT_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_CONTROLLED_VALUE = re.compile(r"^[a-zA-Z0-9_.:@/-]{0,128}$")
_SHA256_VALUE = re.compile(r"^[a-f0-9]{64}$")
_CONTROLLED_TEXT_AUDIT_KEYS = frozenset(
    {
        "acceptance_status",
        "sensitivity",
        "status",
    }
)
_BANNED_AUDIT_KEY_PARTS = frozenset(
    {
        "artifact",
        "body",
        "checkpoint",
        "content",
        "credential",
        "file",
        "markdown",
        "message",
        "notes",
        "password",
        "payload",
        "prompt",
        "raw",
        "result",
        "sample",
        "secret",
        "text",
        "token",
        "value",
    }
)


def canonical_json(value: Any) -> str:
    """Return the stable representation used to fingerprint a request."""

    encoded = jsonable_encoder(value)
    return json.dumps(
        encoded,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def hash_identifier(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _bounded(value: Any, maximum: int) -> str:
    return str(value or "").strip()[:maximum]


def _audit_metadata(value: Mapping[str, Any], *, field_name: str) -> dict[str, Any]:
    encoded = jsonable_encoder(dict(value))
    if not isinstance(encoded, dict):
        raise UnsafeMutationAuditError(f"{field_name} must be an object")
    return _validate_audit_object(encoded, field_name=field_name, depth=0)


def _validate_audit_object(value: dict[str, Any], *, field_name: str, depth: int) -> dict[str, Any]:
    if depth > 3 or len(value) > 32:
        raise UnsafeMutationAuditError(f"{field_name} is too large")
    result: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key or "").strip().lower()
        parts = {part for part in key.split("_") if part}
        if not _SAFE_AUDIT_KEY.fullmatch(key) or parts.intersection(_BANNED_AUDIT_KEY_PARTS):
            raise UnsafeMutationAuditError(f"unsafe audit field: {key or '<empty>'}")
        if isinstance(raw_value, dict):
            result[key] = _validate_audit_object(
                raw_value,
                field_name=f"{field_name}.{key}",
                depth=depth + 1,
            )
        elif isinstance(raw_value, list):
            if len(raw_value) > 32 or any(isinstance(item, (dict, list)) for item in raw_value):
                raise UnsafeMutationAuditError(f"unsafe audit list: {field_name}.{key}")
            result[key] = [
                _audit_scalar(item, field_name=f"{field_name}.{key}", key=key)
                for item in raw_value
            ]
        else:
            result[key] = _audit_scalar(
                raw_value,
                field_name=f"{field_name}.{key}",
                key=key,
            )
    return result


def _audit_scalar(
    value: Any,
    *,
    field_name: str,
    key: str,
) -> str | int | float | bool | None:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if key.endswith("_hash") and _SHA256_VALUE.fullmatch(value):
            return value
        if key in _CONTROLLED_TEXT_AUDIT_KEYS and _SAFE_CONTROLLED_VALUE.fullmatch(value):
            return value
    raise UnsafeMutationAuditError(f"unsafe audit value: {field_name}")


def _safe_trace_id(value: Any) -> str:
    normalized = _bounded(value, 128)
    if not normalized or _SAFE_CONTROLLED_VALUE.fullmatch(normalized):
        return normalized
    return hash_identifier(normalized)


async def run_idempotent_mutation(
    conn: AsyncConnection,
    *,
    identity: MutationIdentity,
    audit: MutationAudit,
    mutate: MutationCallback,
) -> MutationOutcome:
    """Execute one mutation with exact replay and same-transaction audit."""

    if not conn.in_transaction():
        raise MutationLedgerError("an active database transaction is required")

    tenant_id = _bounded(identity.tenant_id, 64)
    plugin_name = _bounded(identity.plugin_name, 64)
    operation = _bounded(identity.operation, 96)
    idempotency_key = _bounded(identity.idempotency_key, 128)
    if not tenant_id or not plugin_name or not operation:
        raise MutationLedgerError("tenant_id, plugin_name and operation are required")
    if not idempotency_key:
        raise MutationLedgerError("idempotency_key is required")

    request_hash = fingerprint(identity.request_payload)
    key_hash = hash_identifier(idempotency_key)
    resource_hash = hash_identifier(_bounded(identity.resource_key, 512))
    mutation_id = str(uuid4())
    now = datetime.now(UTC)
    values = {
        "id": mutation_id,
        "tenant_id": tenant_id,
        "plugin_name": plugin_name,
        "operation": operation,
        "resource_key_hash": resource_hash,
        "idempotency_key_hash": key_hash,
        "request_hash": request_hash,
        "response_status_code": None,
        "response_json": None,
        "created_at": now,
        "completed_at": None,
    }
    dialect = str(conn.dialect.name or "").lower()
    conflict_columns = [
        "tenant_id",
        "plugin_name",
        "operation",
        "idempotency_key_hash",
    ]
    if dialect == "postgresql":
        statement = (
            postgresql_insert(plugin_admin_mutation_idempotency)
            .values(**values)
            .on_conflict_do_nothing(index_elements=conflict_columns)
            .returning(plugin_admin_mutation_idempotency.c.id)
        )
    elif dialect == "sqlite":
        statement = (
            sqlite_insert(plugin_admin_mutation_idempotency)
            .values(**values)
            .on_conflict_do_nothing(index_elements=conflict_columns)
            .returning(plugin_admin_mutation_idempotency.c.id)
        )
    else:
        statement = insert(plugin_admin_mutation_idempotency).values(**values).returning(
            plugin_admin_mutation_idempotency.c.id
        )

    inserted = (await conn.execute(statement)).scalar_one_or_none()
    if inserted is None:
        existing = (
            await conn.execute(
                select(plugin_admin_mutation_idempotency).where(
                    plugin_admin_mutation_idempotency.c.tenant_id == tenant_id,
                    plugin_admin_mutation_idempotency.c.plugin_name == plugin_name,
                    plugin_admin_mutation_idempotency.c.operation == operation,
                    plugin_admin_mutation_idempotency.c.idempotency_key_hash == key_hash,
                )
            )
        ).mappings().one()
        if (
            str(existing["request_hash"]) != request_hash
            or str(existing["resource_key_hash"]) != resource_hash
        ):
            raise MutationIdempotencyConflictError(
                "idempotency key was used for another request"
            )
        if existing["completed_at"] is None:
            raise MutationLedgerError("idempotency mutation is incomplete")
        return MutationOutcome(
            response=existing["response_json"],
            status_code=int(existing["response_status_code"] or 200),
            replayed=True,
            mutation_id=str(existing["id"]),
        )

    change = await mutate()
    response = jsonable_encoder(change.response)
    scope = _audit_metadata(audit.scope, field_name="scope")
    before_state = _audit_metadata(change.before_state, field_name="before_state")
    after_state = _audit_metadata(change.after_state, field_name="after_state")
    reason = str(audit.reason or "").strip()
    roles = sorted({_bounded(role, 64) for role in audit.roles if _bounded(role, 64)})
    completed_at = datetime.now(UTC)
    await conn.execute(
        update(plugin_admin_mutation_idempotency)
        .where(plugin_admin_mutation_idempotency.c.id == mutation_id)
        .values(
            response_status_code=int(change.status_code),
            response_json=response,
            completed_at=completed_at,
        )
    )
    await conn.execute(
        insert(plugin_admin_mutation_audit).values(
            id=str(uuid4()),
            mutation_id=mutation_id,
            tenant_id=tenant_id,
            plugin_name=plugin_name,
            operation=operation,
            actor=_bounded(audit.actor, 128) or "unknown",
            actor_kind=_bounded(audit.actor_kind, 32) or "unknown",
            roles_json=roles,
            scope_json=scope,
            before_state_json=before_state,
            after_state_json=after_state,
            reason_code=_bounded(audit.reason_code, 96) or "manual_change",
            reason_hash=hash_identifier(reason) if reason else "",
            trace_id=_safe_trace_id(audit.trace_id),
            resource_version=_bounded(change.resource_version, 128),
            created_at=completed_at,
        )
    )
    return MutationOutcome(
        response=response,
        status_code=int(change.status_code),
        replayed=False,
        mutation_id=mutation_id,
    )


__all__ = [
    "MutationAudit",
    "MutationChange",
    "MutationIdempotencyConflictError",
    "MutationIdentity",
    "MutationLedgerError",
    "MutationOutcome",
    "UnsafeMutationAuditError",
    "canonical_json",
    "fingerprint",
    "hash_identifier",
    "plugin_admin_mutation_audit",
    "plugin_admin_mutation_idempotency",
    "run_idempotent_mutation",
]
