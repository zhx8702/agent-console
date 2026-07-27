"""Durable recovery ledger for development-only AMap dotenv mutations.

The dotenv file and PostgreSQL cannot participate in one transaction.  This
module therefore uses the generic plugin mutation tables as a small write-ahead
ledger: an intent is committed as ``prepared`` before the file is touched, and
the exact response plus a redacted semantic audit are committed afterwards.
The router can recover a prepared row by inspecting the mutation marker written
atomically with the dotenv update.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi.encoders import jsonable_encoder
from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.admin.mutation_ledger import (
    MutationAudit,
    MutationIdentity,
    fingerprint,
    hash_identifier,
    plugin_admin_mutation_audit,
    plugin_admin_mutation_idempotency,
)
from app.infra.db import get_engine

_PROTOCOL = "amap_dotenv_v1"
_SAFE_TRACE_ID = re.compile(r"^[a-zA-Z0-9_.:@/-]{0,128}$")


class AMapConfigMutationError(RuntimeError):
    """Base error for an unavailable or corrupt AMap mutation ledger."""


class AMapConfigIdempotencyConflictError(AMapConfigMutationError):
    """An idempotency key is already bound to a different AMap intent."""


class AMapConfigMutationIndeterminateError(AMapConfigMutationError):
    """A prepared file mutation can no longer be attributed safely."""

    def __init__(self, *, mutation_id: str) -> None:
        super().__init__(f"AMap config mutation {mutation_id} is indeterminate")
        self.mutation_id = mutation_id


@dataclass(frozen=True, slots=True)
class AMapConfigPreparation:
    expected_etag: str
    target_etag: str
    response: Any
    before_state: dict[str, object]
    after_state: dict[str, object]
    scope: dict[str, object] = field(default_factory=dict)
    status_code: int = 200


@dataclass(frozen=True, slots=True)
class AMapConfigMutationClaim:
    mutation_id: str
    is_new: bool
    preparation: AMapConfigPreparation | None = None
    completed: AMapConfigMutationResult | None = None


@dataclass(frozen=True, slots=True)
class AMapConfigMutationResult:
    mutation_id: str
    response: Any
    status_code: int
    resource_version: str
    replayed: bool = False
    before_state: dict[str, object] = field(default_factory=dict)
    after_state: dict[str, object] = field(default_factory=dict)


class AMapConfigMutationStore:
    """Persist AMap prepare/complete/recovery state in the generic ledger."""

    def __init__(self, engine: AsyncEngine | None = None) -> None:
        self._engine = engine

    @property
    def engine(self) -> AsyncEngine:
        return self._engine or get_engine()

    async def lookup(
        self,
        *,
        identity: MutationIdentity,
    ) -> AMapConfigMutationClaim | None:
        """Return a durable replay/recovery claim without creating one."""

        tenant_id, plugin_name, operation, key_hash, request_hash, resource_hash = (
            _identity_parts(identity)
        )
        async with self.engine.begin() as conn:
            existing = await _load_identity_row(
                conn,
                tenant_id=tenant_id,
                plugin_name=plugin_name,
                operation=operation,
                key_hash=key_hash,
            )
            if existing is None:
                return None
            return await _claim_from_existing(
                conn,
                existing,
                request_hash=request_hash,
                resource_hash=resource_hash,
            )

    async def claim(
        self,
        *,
        identity: MutationIdentity,
        audit: MutationAudit,
        preparation: AMapConfigPreparation,
    ) -> AMapConfigMutationClaim:
        tenant_id, plugin_name, operation, key_hash, request_hash, resource_hash = (
            _identity_parts(identity)
        )

        mutation_id = str(uuid4())
        now = datetime.now(UTC)
        envelope = _prepared_envelope(preparation, audit)
        values = {
            "id": mutation_id,
            "tenant_id": tenant_id,
            "plugin_name": plugin_name,
            "operation": operation,
            "resource_key_hash": resource_hash,
            "idempotency_key_hash": key_hash,
            "request_hash": request_hash,
            "response_status_code": None,
            "response_json": envelope,
            "created_at": now,
            "completed_at": None,
        }

        async with self.engine.begin() as conn:
            inserted = await _insert_claim(conn, values)
            if inserted is not None:
                return AMapConfigMutationClaim(
                    mutation_id=str(inserted),
                    is_new=True,
                    preparation=preparation,
                )

            existing = await _load_identity_row(
                conn,
                tenant_id=tenant_id,
                plugin_name=plugin_name,
                operation=operation,
                key_hash=key_hash,
            )
            if existing is None:
                raise AMapConfigMutationError("AMap mutation claim disappeared")
            return await _claim_from_existing(
                conn,
                existing,
                request_hash=request_hash,
                resource_hash=resource_hash,
            )

    async def complete_success(self, mutation_id: str) -> AMapConfigMutationResult:
        """Commit the exact replay response and one redacted semantic audit."""

        async with self.engine.begin() as conn:
            row = await _load_mutation_row(conn, mutation_id)
            if row is None:
                raise AMapConfigMutationError("AMap mutation claim not found")
            if row.get("completed_at") is not None:
                return await _completed_result(conn, row, replayed=True)

            envelope = _decode_envelope(row.get("response_json"))
            if str(envelope.get("state") or "") != "prepared":
                raise AMapConfigMutationError("AMap mutation is not prepared")
            preparation = _preparation_from_envelope(envelope)
            response_value = jsonable_encoder(preparation.response)
            completed_at = datetime.now(UTC)
            updated = (
                await conn.execute(
                    update(plugin_admin_mutation_idempotency)
                    .where(
                        plugin_admin_mutation_idempotency.c.id == mutation_id,
                        plugin_admin_mutation_idempotency.c.completed_at.is_(None),
                    )
                    .values(
                        response_status_code=int(preparation.status_code),
                        response_json=response_value,
                        completed_at=completed_at,
                    )
                    .returning(plugin_admin_mutation_idempotency.c.id)
                )
            ).scalar_one_or_none()
            if updated is None:
                current = await _load_mutation_row(conn, mutation_id)
                if current is None or current.get("completed_at") is None:
                    raise AMapConfigMutationError("AMap mutation completion lost")
                return await _completed_result(conn, current, replayed=True)

            audit = _audit_from_envelope(envelope)
            await _insert_audit(
                conn,
                mutation_id=mutation_id,
                row=row,
                preparation=preparation,
                audit=audit,
                created_at=completed_at,
            )
            return AMapConfigMutationResult(
                mutation_id=mutation_id,
                response=response_value,
                status_code=int(preparation.status_code),
                resource_version=preparation.target_etag,
                before_state=dict(preparation.before_state),
                after_state=dict(preparation.after_state),
            )

    async def complete_failure(
        self,
        mutation_id: str,
        *,
        status_code: int,
        response: Any,
        resource_version: str,
    ) -> AMapConfigMutationResult:
        """Durably replay a deterministic failure that touched no file state."""

        response_value = jsonable_encoder(response)
        async with self.engine.begin() as conn:
            row = await _load_mutation_row(conn, mutation_id)
            if row is None:
                raise AMapConfigMutationError("AMap mutation claim not found")
            if row.get("completed_at") is not None:
                return await _completed_result(conn, row, replayed=True)
            completed_at = datetime.now(UTC)
            updated = (
                await conn.execute(
                    update(plugin_admin_mutation_idempotency)
                    .where(
                        plugin_admin_mutation_idempotency.c.id == mutation_id,
                        plugin_admin_mutation_idempotency.c.completed_at.is_(None),
                    )
                    .values(
                        response_status_code=int(status_code),
                        response_json=response_value,
                        completed_at=completed_at,
                    )
                    .returning(plugin_admin_mutation_idempotency.c.id)
                )
            ).scalar_one_or_none()
            if updated is None:
                current = await _load_mutation_row(conn, mutation_id)
                if current is None or current.get("completed_at") is None:
                    raise AMapConfigMutationError("AMap mutation failure completion lost")
                return await _completed_result(conn, current, replayed=True)
            return AMapConfigMutationResult(
                mutation_id=mutation_id,
                response=response_value,
                status_code=int(status_code),
                resource_version=str(resource_version or ""),
            )

    async def mark_indeterminate(self, mutation_id: str) -> None:
        """Fence a prepared intent whose file outcome cannot be proven."""

        async with self.engine.begin() as conn:
            row = await _load_mutation_row(conn, mutation_id)
            if row is None or row.get("completed_at") is not None:
                return
            envelope = _decode_envelope(row.get("response_json"))
            if str(envelope.get("state") or "") == "indeterminate":
                return
            if str(envelope.get("state") or "") != "prepared":
                raise AMapConfigMutationError("invalid AMap mutation recovery state")
            envelope["state"] = "indeterminate"
            await conn.execute(
                update(plugin_admin_mutation_idempotency)
                .where(
                    plugin_admin_mutation_idempotency.c.id == mutation_id,
                    plugin_admin_mutation_idempotency.c.completed_at.is_(None),
                )
                .values(
                    response_status_code=409,
                    response_json=envelope,
                )
            )


async def _insert_claim(conn: AsyncConnection, values: dict[str, Any]) -> str | None:
    columns = [
        "tenant_id",
        "plugin_name",
        "operation",
        "idempotency_key_hash",
    ]
    dialect = str(conn.dialect.name or "").lower()
    if dialect == "postgresql":
        statement = (
            postgresql_insert(plugin_admin_mutation_idempotency)
            .values(**values)
            .on_conflict_do_nothing(index_elements=columns)
            .returning(plugin_admin_mutation_idempotency.c.id)
        )
    elif dialect == "sqlite":
        statement = (
            sqlite_insert(plugin_admin_mutation_idempotency)
            .values(**values)
            .on_conflict_do_nothing(index_elements=columns)
            .returning(plugin_admin_mutation_idempotency.c.id)
        )
    else:
        statement = insert(plugin_admin_mutation_idempotency).values(**values).returning(
            plugin_admin_mutation_idempotency.c.id
        )
    return (await conn.execute(statement)).scalar_one_or_none()


def _identity_parts(
    identity: MutationIdentity,
) -> tuple[str, str, str, str, str, str]:
    tenant_id = _bounded(identity.tenant_id, 64)
    plugin_name = _bounded(identity.plugin_name, 64)
    operation = _bounded(identity.operation, 96)
    idempotency_key = _bounded(identity.idempotency_key, 128)
    if not tenant_id or not plugin_name or not operation or not idempotency_key:
        raise AMapConfigMutationError("incomplete AMap mutation identity")
    return (
        tenant_id,
        plugin_name,
        operation,
        hash_identifier(idempotency_key),
        fingerprint(identity.request_payload),
        hash_identifier(_bounded(identity.resource_key, 512)),
    )


async def _claim_from_existing(
    conn: AsyncConnection,
    existing: dict[str, Any],
    *,
    request_hash: str,
    resource_hash: str,
) -> AMapConfigMutationClaim:
    if (
        str(existing.get("request_hash") or "") != request_hash
        or str(existing.get("resource_key_hash") or "") != resource_hash
    ):
        raise AMapConfigIdempotencyConflictError(
            "idempotency key was used for another AMap config intent"
        )
    if existing.get("completed_at") is not None:
        completed = await _completed_result(conn, existing, replayed=True)
        return AMapConfigMutationClaim(
            mutation_id=completed.mutation_id,
            is_new=False,
            completed=completed,
        )

    stored = _decode_envelope(existing.get("response_json"))
    state = str(stored.get("state") or "")
    if state == "indeterminate":
        raise AMapConfigMutationIndeterminateError(
            mutation_id=str(existing["id"]),
        )
    if state != "prepared":
        raise AMapConfigMutationError("invalid AMap mutation recovery state")
    return AMapConfigMutationClaim(
        mutation_id=str(existing["id"]),
        is_new=False,
        preparation=_preparation_from_envelope(stored),
    )


async def _load_identity_row(
    conn: AsyncConnection,
    *,
    tenant_id: str,
    plugin_name: str,
    operation: str,
    key_hash: str,
) -> dict[str, Any] | None:
    result = await conn.execute(
        select(plugin_admin_mutation_idempotency).where(
            plugin_admin_mutation_idempotency.c.tenant_id == tenant_id,
            plugin_admin_mutation_idempotency.c.plugin_name == plugin_name,
            plugin_admin_mutation_idempotency.c.operation == operation,
            plugin_admin_mutation_idempotency.c.idempotency_key_hash == key_hash,
        )
    )
    row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


async def _load_mutation_row(
    conn: AsyncConnection,
    mutation_id: str,
) -> dict[str, Any] | None:
    result = await conn.execute(
        select(plugin_admin_mutation_idempotency).where(
            plugin_admin_mutation_idempotency.c.id == str(mutation_id),
        )
    )
    row = result.mappings().one_or_none()
    return dict(row) if row is not None else None


async def _completed_result(
    conn: AsyncConnection,
    row: dict[str, Any],
    *,
    replayed: bool,
) -> AMapConfigMutationResult:
    audit_result = await conn.execute(
        select(
            plugin_admin_mutation_audit.c.resource_version,
            plugin_admin_mutation_audit.c.before_state_json,
            plugin_admin_mutation_audit.c.after_state_json,
        ).where(plugin_admin_mutation_audit.c.mutation_id == str(row["id"]))
    )
    audit = audit_result.mappings().one_or_none()
    response_value = _json_value(row.get("response_json"))
    resource_version = str(audit.get("resource_version") or "") if audit else ""
    if not resource_version and isinstance(response_value, dict):
        detail = response_value.get("detail")
        if isinstance(detail, dict):
            resource_version = str(detail.get("current_etag") or "")
    return AMapConfigMutationResult(
        mutation_id=str(row["id"]),
        response=response_value,
        status_code=int(row.get("response_status_code") or 200),
        resource_version=resource_version,
        replayed=replayed,
        before_state=_mapping(audit.get("before_state_json")) if audit else {},
        after_state=_mapping(audit.get("after_state_json")) if audit else {},
    )


async def _insert_audit(
    conn: AsyncConnection,
    *,
    mutation_id: str,
    row: dict[str, Any],
    preparation: AMapConfigPreparation,
    audit: MutationAudit,
    created_at: datetime,
) -> None:
    roles = sorted({_bounded(role, 64) for role in audit.roles if _bounded(role, 64)})
    await conn.execute(
        insert(plugin_admin_mutation_audit).values(
            id=str(uuid4()),
            mutation_id=mutation_id,
            tenant_id=str(row["tenant_id"]),
            plugin_name=str(row["plugin_name"]),
            operation=str(row["operation"]),
            actor=_bounded(audit.actor, 128) or "unknown",
            actor_kind=_bounded(audit.actor_kind, 32) or "unknown",
            roles_json=roles,
            scope_json=_safe_scope(preparation.scope),
            before_state_json=_safe_config_state(preparation.before_state),
            after_state_json=_safe_config_state(preparation.after_state),
            reason_code=_bounded(audit.reason_code, 96) or "amap_runtime_config_update",
            reason_hash="",
            trace_id=_safe_trace_id(audit.trace_id),
            resource_version=_bounded(preparation.target_etag, 128),
            created_at=created_at,
        )
    )


def _prepared_envelope(
    preparation: AMapConfigPreparation,
    audit: MutationAudit,
) -> dict[str, Any]:
    return {
        "protocol": _PROTOCOL,
        "state": "prepared",
        "expected_etag": str(preparation.expected_etag),
        "target_etag": str(preparation.target_etag),
        "response": jsonable_encoder(preparation.response),
        "status_code": int(preparation.status_code),
        "before_state": _safe_config_state(preparation.before_state),
        "after_state": _safe_config_state(preparation.after_state),
        "scope": _safe_scope(preparation.scope),
        "audit": {
            "actor": _bounded(audit.actor, 128) or "unknown",
            "actor_kind": _bounded(audit.actor_kind, 32) or "unknown",
            "roles": [
                _bounded(role, 64)
                for role in audit.roles
                if _bounded(role, 64)
            ],
            "reason_code": _bounded(audit.reason_code, 96)
            or "amap_runtime_config_update",
            "trace_id": _safe_trace_id(audit.trace_id),
        },
    }


def _preparation_from_envelope(value: dict[str, Any]) -> AMapConfigPreparation:
    if str(value.get("protocol") or "") != _PROTOCOL:
        raise AMapConfigMutationError("unsupported AMap mutation protocol")
    expected_etag = str(value.get("expected_etag") or "")
    target_etag = str(value.get("target_etag") or "")
    if not expected_etag or not target_etag:
        raise AMapConfigMutationError("incomplete AMap mutation recovery record")
    return AMapConfigPreparation(
        expected_etag=expected_etag,
        target_etag=target_etag,
        response=value.get("response"),
        before_state=_safe_config_state(_mapping(value.get("before_state"))),
        after_state=_safe_config_state(_mapping(value.get("after_state"))),
        scope=_safe_scope(_mapping(value.get("scope"))),
        status_code=int(value.get("status_code") or 200),
    )


def _audit_from_envelope(value: dict[str, Any]) -> MutationAudit:
    audit = _mapping(value.get("audit"))
    raw_roles = audit.get("roles")
    roles = tuple(str(role) for role in raw_roles) if isinstance(raw_roles, list) else ()
    return MutationAudit(
        actor=str(audit.get("actor") or "unknown"),
        actor_kind=str(audit.get("actor_kind") or "unknown"),
        roles=roles,
        reason_code=str(audit.get("reason_code") or "amap_runtime_config_update"),
        trace_id=str(audit.get("trace_id") or ""),
    )


def _decode_envelope(value: Any) -> dict[str, Any]:
    decoded = _json_value(value)
    if not isinstance(decoded, dict) or str(decoded.get("protocol") or "") != _PROTOCOL:
        raise AMapConfigMutationError("invalid AMap mutation ledger payload")
    return decoded


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    return value


def _mapping(value: Any) -> dict[str, Any]:
    decoded = _json_value(value)
    return dict(decoded) if isinstance(decoded, dict) else {}


def _safe_config_state(value: dict[str, Any]) -> dict[str, object]:
    """Whitelist the only state fields permitted in the semantic audit."""

    return {
        "api_key_configured": bool(value.get("api_key_configured")),
        "timeout_seconds": float(value.get("timeout_seconds") or 0.0),
        "storage_dir_configured": bool(value.get("storage_dir_configured")),
    }


def _safe_scope(value: dict[str, Any]) -> dict[str, object]:
    return {
        "timeout_changed": bool(value.get("timeout_changed")),
        "storage_dir_changed": bool(value.get("storage_dir_changed")),
    }


def _safe_trace_id(value: Any) -> str:
    normalized = _bounded(value, 128)
    if not normalized or _SAFE_TRACE_ID.fullmatch(normalized):
        return normalized
    return hash_identifier(normalized)


def _bounded(value: Any, maximum: int) -> str:
    return str(value or "").strip()[:maximum]


__all__ = [
    "AMapConfigIdempotencyConflictError",
    "AMapConfigMutationClaim",
    "AMapConfigMutationError",
    "AMapConfigMutationIndeterminateError",
    "AMapConfigMutationResult",
    "AMapConfigMutationStore",
    "AMapConfigPreparation",
]
