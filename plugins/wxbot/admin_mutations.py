"""Durable concurrency and idempotency state for wxbot admin mutations.

Kept separate from the broad wxbot persistence facade so configuration writes
have a small, independently testable crash-recovery boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from app.admin.mutation_ledger import canonical_json, fingerprint, hash_identifier
from app.infra.db import get_engine


class WxbotAdminIdempotencyConflictError(RuntimeError):
    """An admin idempotency key was already bound to another intent."""


class WxbotAdminMutationBusyError(RuntimeError):
    """The original effect attempt is still running or has an unknown outcome."""

    def __init__(self, *, mutation_id: str, status: str) -> None:
        super().__init__(f"wxbot admin mutation {mutation_id} is {status}")
        self.mutation_id = mutation_id
        self.status = status


class WxbotAdminVersionConflictError(RuntimeError):
    def __init__(self, *, expected: int, current: int) -> None:
        super().__init__(f"expected version {expected}, current version {current}")
        self.expected = expected
        self.current = current


@dataclass(frozen=True, slots=True)
class WxbotAdminMutationClaim:
    mutation_id: str
    operation: str
    status: str
    is_new: bool
    response: Any = None
    response_status_code: int = 200
    resource_version: int | None = None


@dataclass(frozen=True, slots=True)
class WxbotAdminMutationResult:
    mutation_id: str
    response: Any
    response_status_code: int
    resource_version: int | None
    replayed: bool = False


def _json_value(value: Any) -> Any:
    if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
        if isinstance(value, str) and value.strip():
            try:
                return json.loads(value)
            except (TypeError, ValueError):
                return value
        return value
    return json.loads(canonical_json(value))


async def observe_admin_resource(
    _store: Any,
    tenant_id: str,
    resource_key: str,
    *,
    resource_kind: str,
    state_payload: Any,
) -> int:
    """Observe a configuration resource and return its monotonic ETag version.

    Out-of-band changes advance the version.  A pending, dispatched admin
    mutation can be recovered safely when the observed canonical state is
    exactly the desired state persisted before dispatch.
    """

    tenant = str(tenant_id or "").strip()[:64]
    if not tenant:
        raise ValueError("tenant_id required")
    resource_hash = hash_identifier(str(resource_key or "")[:512])
    kind = str(resource_kind or "wxbot_admin_resource").strip()[:96]
    observed_hash = fingerprint(state_payload)
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO plugin_wxbot_admin_resource_version "
                "(tenant_id, resource_key_hash, resource_kind, version, state_hash, "
                "pending_mutation_id, created_at, updated_at) "
                "VALUES (:tid, :resource_hash, :kind, 0, :state_hash, NULL, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
                "ON CONFLICT (tenant_id, resource_key_hash) DO NOTHING"
            ),
            {
                "tid": tenant,
                "resource_hash": resource_hash,
                "kind": kind,
                "state_hash": observed_hash,
            },
        )
        row_result = await conn.execute(
            text(
                "SELECT version, state_hash, pending_mutation_id "
                "FROM plugin_wxbot_admin_resource_version "
                "WHERE tenant_id = :tid AND resource_key_hash = :resource_hash"
            ),
            {"tid": tenant, "resource_hash": resource_hash},
        )
        row = row_result.mappings().one()
        current_version = max(0, int(row.get("version") or 0))
        pending_id = str(row.get("pending_mutation_id") or "").strip()
        if pending_id:
            mutation_result = await conn.execute(
                text(
                    "SELECT status, desired_state_hash, recovery_response_json "
                    "FROM plugin_wxbot_admin_mutation_state WHERE id = :id"
                ),
                {"id": pending_id},
            )
            pending = mutation_result.mappings().one_or_none()
            if (
                pending is not None
                and str(pending.get("status") or "") in {"dispatching", "indeterminate"}
                and str(pending.get("desired_state_hash") or "") == observed_hash
            ):
                next_version = current_version + 1
                response_value = _json_value(pending.get("recovery_response_json"))
                dialect = str(conn.dialect.name or "").lower()
                response_expr = (
                    "CAST(:response_json AS JSONB)"
                    if dialect == "postgresql"
                    else ":response_json"
                )
                await conn.execute(
                    text(
                        "UPDATE plugin_wxbot_admin_resource_version SET "
                        "version = :version, state_hash = :state_hash, "
                        "pending_mutation_id = NULL, updated_at = CURRENT_TIMESTAMP "
                        "WHERE tenant_id = :tid AND resource_key_hash = :resource_hash "
                        "AND pending_mutation_id = :mutation_id"
                    ),
                    {
                        "version": next_version,
                        "state_hash": observed_hash,
                        "tid": tenant,
                        "resource_hash": resource_hash,
                        "mutation_id": pending_id,
                    },
                )
                await conn.execute(
                    text(
                        "UPDATE plugin_wxbot_admin_mutation_state SET "
                        "status = 'completed', response_status_code = 200, "
                        f"response_json = {response_expr}, resource_version = :version, "
                        "error_code = '', completed_at = CURRENT_TIMESTAMP, "
                        "updated_at = CURRENT_TIMESTAMP WHERE id = :mutation_id"
                    ),
                    {
                        "response_json": canonical_json(response_value or {}),
                        "version": next_version,
                        "mutation_id": pending_id,
                    },
                )
                return next_version
            return current_version

        if str(row.get("state_hash") or "") != observed_hash:
            current_version += 1
            await conn.execute(
                text(
                    "UPDATE plugin_wxbot_admin_resource_version SET "
                    "version = :version, state_hash = :state_hash, resource_kind = :kind, "
                    "updated_at = CURRENT_TIMESTAMP "
                    "WHERE tenant_id = :tid AND resource_key_hash = :resource_hash "
                    "AND pending_mutation_id IS NULL"
                ),
                {
                    "version": current_version,
                    "state_hash": observed_hash,
                    "kind": kind,
                    "tid": tenant,
                    "resource_hash": resource_hash,
                },
            )
        return current_version

async def claim_admin_mutation(
    _store: Any,
    tenant_id: str,
    *,
    operation: str,
    resource_key: str,
    idempotency_key: str,
    request_payload: Any,
    trace_id: str = "",
    expected_version: int | None = None,
    desired_state: Any = None,
    recovery_response: Any = None,
) -> WxbotAdminMutationClaim:
    """Persist an intent and dispatch fence before any external side effect."""

    tenant = str(tenant_id or "").strip()[:64]
    key = str(idempotency_key or "").strip()
    action = str(operation or "").strip()[:96]
    if not tenant or not action or len(key) < 8 or len(key) > 255:
        raise ValueError("valid tenant, operation and idempotency key required")
    resource_hash = hash_identifier(str(resource_key or "")[:512])
    key_hash = hash_identifier(key)
    intent_hash = fingerprint(
        {
            "operation": action,
            "resource_key_hash": resource_hash,
            "request": request_payload,
        }
    )
    desired_hash = fingerprint(desired_state) if desired_state is not None else ""
    mutation_id = str(uuid4())
    recovery_json = canonical_json(recovery_response if recovery_response is not None else {})
    engine = get_engine()
    async with engine.begin() as conn:
        dialect = str(conn.dialect.name or "").lower()
        recovery_expr = (
            "CAST(:recovery_response_json AS JSONB)"
            if dialect == "postgresql"
            else ":recovery_response_json"
        )
        inserted_result = await conn.execute(
            text(
                "INSERT INTO plugin_wxbot_admin_mutation_state "
                "(id, tenant_id, operation, resource_key_hash, idempotency_key_hash, "
                "request_hash, expected_version, desired_state_hash, status, "
                "recovery_response_json, trace_id, created_at, updated_at, "
                "dispatch_started_at) "
                "VALUES (:id, :tid, :operation, :resource_hash, :key_hash, "
                f":request_hash, :expected_version, :desired_hash, 'dispatching', {recovery_expr}, "
                ":trace_id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
                "ON CONFLICT (tenant_id, idempotency_key_hash) DO NOTHING "
                "RETURNING id"
            ),
            {
                "id": mutation_id,
                "tid": tenant,
                "operation": action,
                "resource_hash": resource_hash,
                "key_hash": key_hash,
                "request_hash": intent_hash,
                "expected_version": expected_version,
                "desired_hash": desired_hash,
                "recovery_response_json": recovery_json,
                "trace_id": str(trace_id or "")[:128],
            },
        )
        inserted = inserted_result.scalar_one_or_none()
        if inserted is None:
            existing_result = await conn.execute(
                text(
                    "SELECT id, operation, resource_key_hash, request_hash, status, "
                    "response_status_code, response_json, resource_version "
                    "FROM plugin_wxbot_admin_mutation_state "
                    "WHERE tenant_id = :tid AND idempotency_key_hash = :key_hash"
                ),
                {"tid": tenant, "key_hash": key_hash},
            )
            existing = existing_result.mappings().one()
            if (
                str(existing.get("operation") or "") != action
                or str(existing.get("resource_key_hash") or "") != resource_hash
                or str(existing.get("request_hash") or "") != intent_hash
            ):
                raise WxbotAdminIdempotencyConflictError(
                    "idempotency key was used for another wxbot admin intent"
                )
            status = str(existing.get("status") or "")
            if status != "completed":
                raise WxbotAdminMutationBusyError(
                    mutation_id=str(existing["id"]),
                    status=status or "indeterminate",
                )
            return WxbotAdminMutationClaim(
                mutation_id=str(existing["id"]),
                operation=action,
                status=status,
                is_new=False,
                response=_json_value(existing.get("response_json")),
                response_status_code=int(existing.get("response_status_code") or 200),
                resource_version=(
                    int(existing["resource_version"])
                    if existing.get("resource_version") is not None
                    else None
                ),
            )

        if expected_version is not None:
            reserved = await conn.execute(
                text(
                    "UPDATE plugin_wxbot_admin_resource_version SET "
                    "pending_mutation_id = :mutation_id, updated_at = CURRENT_TIMESTAMP "
                    "WHERE tenant_id = :tid AND resource_key_hash = :resource_hash "
                    "AND version = :expected_version AND pending_mutation_id IS NULL "
                    "RETURNING version"
                ),
                {
                    "mutation_id": mutation_id,
                    "tid": tenant,
                    "resource_hash": resource_hash,
                    "expected_version": int(expected_version),
                },
            )
            if reserved.scalar_one_or_none() is None:
                current_result = await conn.execute(
                    text(
                        "SELECT version FROM plugin_wxbot_admin_resource_version "
                        "WHERE tenant_id = :tid AND resource_key_hash = :resource_hash"
                    ),
                    {"tid": tenant, "resource_hash": resource_hash},
                )
                current = current_result.scalar_one_or_none()
                raise WxbotAdminVersionConflictError(
                    expected=int(expected_version),
                    current=max(0, int(current or 0)),
                )
        return WxbotAdminMutationClaim(
            mutation_id=mutation_id,
            operation=action,
            status="dispatching",
            is_new=True,
        )

async def complete_admin_mutation(
    _store: Any,
    mutation_id: str,
    *,
    response: Any,
    response_status_code: int = 200,
    state_payload: Any = None,
) -> WxbotAdminMutationResult:
    """Commit the exact replay response and release a held CAS resource."""

    response_json = canonical_json(response)
    engine = get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "SELECT tenant_id, resource_key_hash, expected_version, status, "
                "response_status_code, response_json, resource_version "
                "FROM plugin_wxbot_admin_mutation_state WHERE id = :id"
            ),
            {"id": mutation_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise ValueError("wxbot admin mutation not found")
        if str(row.get("status") or "") == "completed":
            return WxbotAdminMutationResult(
                mutation_id=mutation_id,
                response=_json_value(row.get("response_json")),
                response_status_code=int(row.get("response_status_code") or 200),
                resource_version=(
                    int(row["resource_version"])
                    if row.get("resource_version") is not None
                    else None
                ),
                replayed=True,
            )

        resource_version: int | None = None
        expected = row.get("expected_version")
        if expected is not None:
            resource_version = int(expected) + 1
            state_hash = fingerprint(state_payload)
            updated = await conn.execute(
                text(
                    "UPDATE plugin_wxbot_admin_resource_version SET "
                    "version = :version, state_hash = :state_hash, "
                    "pending_mutation_id = NULL, updated_at = CURRENT_TIMESTAMP "
                    "WHERE tenant_id = :tid AND resource_key_hash = :resource_hash "
                    "AND version = :expected_version AND pending_mutation_id = :mutation_id "
                    "RETURNING version"
                ),
                {
                    "version": resource_version,
                    "state_hash": state_hash,
                    "tid": str(row["tenant_id"]),
                    "resource_hash": str(row["resource_key_hash"]),
                    "expected_version": int(expected),
                    "mutation_id": mutation_id,
                },
            )
            committed_version = updated.scalar_one_or_none()
            if committed_version is None:
                raise WxbotAdminVersionConflictError(
                    expected=int(expected),
                    current=int(expected),
                )
            resource_version = int(committed_version)
        dialect = str(conn.dialect.name or "").lower()
        response_expr = (
            "CAST(:response_json AS JSONB)" if dialect == "postgresql" else ":response_json"
        )
        await conn.execute(
            text(
                "UPDATE plugin_wxbot_admin_mutation_state SET "
                "status = 'completed', response_status_code = :response_status_code, "
                f"response_json = {response_expr}, resource_version = :resource_version, "
                "error_code = '', completed_at = CURRENT_TIMESTAMP, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = :id"
            ),
            {
                "response_status_code": int(response_status_code),
                "response_json": response_json,
                "resource_version": resource_version,
                "id": mutation_id,
            },
        )
        return WxbotAdminMutationResult(
            mutation_id=mutation_id,
            response=_json_value(response),
            response_status_code=int(response_status_code),
            resource_version=resource_version,
        )

async def fail_admin_mutation(
    _store: Any,
    mutation_id: str,
    *,
    status_code: int,
    response: Any,
    error_code: str,
    indeterminate: bool = False,
) -> None:
    """Record a deterministic failure, or fence an unknown external result."""

    engine = get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "SELECT tenant_id, resource_key_hash, expected_version "
                "FROM plugin_wxbot_admin_mutation_state WHERE id = :id"
            ),
            {"id": mutation_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            return
        dialect = str(conn.dialect.name or "").lower()
        response_expr = (
            "CAST(:response_json AS JSONB)" if dialect == "postgresql" else ":response_json"
        )
        status = "indeterminate" if indeterminate else "completed"
        completed_expr = "NULL" if indeterminate else "CURRENT_TIMESTAMP"
        await conn.execute(
            text(
                "UPDATE plugin_wxbot_admin_mutation_state SET status = :status, "
                "response_status_code = :status_code, "
                f"response_json = {response_expr}, error_code = :error_code, "
                f"completed_at = {completed_expr}, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = :id"
            ),
            {
                "status": status,
                "status_code": int(status_code),
                "response_json": canonical_json(response),
                "error_code": str(error_code or "mutation_failed")[:96],
                "id": mutation_id,
            },
        )
        if not indeterminate and row.get("expected_version") is not None:
            await conn.execute(
                text(
                    "UPDATE plugin_wxbot_admin_resource_version SET "
                    "pending_mutation_id = NULL, updated_at = CURRENT_TIMESTAMP "
                    "WHERE tenant_id = :tid AND resource_key_hash = :resource_hash "
                    "AND pending_mutation_id = :mutation_id"
                ),
                {
                    "tid": str(row["tenant_id"]),
                    "resource_hash": str(row["resource_key_hash"]),
                    "mutation_id": mutation_id,
                },
            )
