"""
REST API for the repeater plugin.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from pydantic import Field

from app.admin.audit import set_admin_audit_context
from app.admin.auth_router import authenticate_admin_request
from app.admin.mutation_ledger import (
    MutationAudit,
    MutationChange,
    MutationIdempotencyConflictError,
    MutationIdentity,
    MutationOutcome,
    hash_identifier,
)
from app.common.request_models import StrictRequestModel
from plugins.repeater.store import (
    RepeaterConfigVersionConflictError,
    RepeaterStore,
)


class RepeaterConfigUpdate(StrictRequestModel):
    enabled: bool | None = None
    cooldown_seconds: int | None = Field(default=None, ge=1, le=86_400)


def build_repeater_router(store: RepeaterStore) -> APIRouter:
    router = APIRouter()

    @router.get("/config/{tenant_id}/{session_id:path}")
    async def get_config(
        tenant_id: str,
        session_id: str,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        tenant, session = _require_scope(store, request, tenant_id, session_id)
        config = await store.get_config(tenant, session)
        _set_version_headers(response, int(config["version"]))
        return config

    @router.post("/config/{tenant_id}/{session_id:path}")
    async def set_config(
        tenant_id: str,
        session_id: str,
        body: RepeaterConfigUpdate,
        request: Request,
        response: Response,
        if_match: str | None = Header(default=None, alias="If-Match"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        tenant, session = _require_scope(store, request, tenant_id, session_id)
        if body.enabled is None and body.cooldown_seconds is None:
            raise HTTPException(status_code=400, detail="no_mutable_fields")
        expected_version = _required_if_match(if_match)
        operation_key = _required_idempotency_key(idempotency_key)
        mutation_holder = []

        async def mutate() -> MutationChange:
            mutation = await store.set_config(
                tenant,
                session,
                expected_version=expected_version,
                enabled=body.enabled,
                cooldown_seconds=body.cooldown_seconds,
            )
            mutation_holder.append(mutation)
            return MutationChange(
                response=dict(mutation.after),
                before_state=_audit_summary(mutation.before),
                after_state=_audit_summary(mutation.after),
                resource_version=str(mutation.after["version"]),
            )

        try:
            outcome = await store.run_admin_mutation(
                identity=MutationIdentity(
                    tenant_id=tenant,
                    plugin_name="repeater",
                    operation="repeater.config.set",
                    resource_key=session,
                    idempotency_key=operation_key,
                    request_payload={
                        "expected_version": expected_version,
                        "updates": body.model_dump(exclude_none=True),
                    },
                ),
                audit=_mutation_audit(
                    request,
                    scope={
                        "session_hash": hash_identifier(session),
                        "expected_version": expected_version,
                    },
                    reason_code="conditional_repeater_config_update",
                ),
                mutate=mutate,
            )
        except RepeaterConfigVersionConflictError as exc:
            raise _version_conflict(exc.expected, exc.current) from exc
        except MutationIdempotencyConflictError as exc:
            raise _idempotency_conflict() from exc
        after = dict(outcome.response)
        response.status_code = outcome.status_code
        _set_mutation_headers(response, outcome)
        _set_version_headers(response, int(after["version"]))
        before = mutation_holder[0].before if mutation_holder else {"version": expected_version}
        set_admin_audit_context(
            request,
            target_type="plugin_repeater_config",
            tenant_id=tenant,
            session_id=session,
            before_state=_audit_summary(before),
            after_state=_audit_summary(after),
            policy_version=int(after["version"]),
            trace_id=_trace_id(request),
            reason=(
                "conditional_config_update_idempotent_replay"
                if outcome.replayed
                else "conditional_config_update_sdk_gate_unchanged"
            ),
        )
        return after

    @router.get("/events/{tenant_id}")
    async def list_events(
        tenant_id: str,
        request: Request,
        session_id: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        tenant, session = _require_scope(
            store,
            request,
            tenant_id,
            session_id,
            allow_tenant_collection=True,
        )
        return {
            "items": await store.list_events(
                tenant,
                session_id=session or None,
                limit=limit,
            )
        }

    return router


def _require_scope(
    store: RepeaterStore,
    request: Request,
    tenant_id: str,
    session_id: str | None,
    *,
    allow_tenant_collection: bool = False,
) -> tuple[str, str]:
    principal = authenticate_admin_request(request, store.settings)
    tenant = str(tenant_id or "").strip()
    session = str(session_id or "").strip()
    if not tenant:
        raise HTTPException(status_code=400, detail="tenant_id_required")
    if len(tenant) > 64:
        raise HTTPException(status_code=400, detail="tenant_id_invalid")
    if not principal.allows_tenant(tenant):
        raise HTTPException(status_code=403, detail="tenant_scope_forbidden")
    if not session:
        if allow_tenant_collection and not principal.requires_explicit_group_scope:
            return tenant, ""
        raise HTTPException(status_code=400, detail="session_id_required")
    if len(session) > 256:
        raise HTTPException(status_code=400, detail="session_id_invalid")
    if not session.endswith("@chatroom"):
        raise HTTPException(status_code=400, detail="group_session_required")
    if not principal.allows_group(tenant, session):
        raise HTTPException(status_code=403, detail="group_scope_forbidden")
    return tenant, session


def _required_if_match(value: str | None) -> int:
    if value is None or not value.strip():
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="if_match_required",
        )
    normalized = value.strip()
    if normalized.startswith("W/"):
        normalized = normalized[2:].strip()
    if len(normalized) >= 2 and normalized[0] == '"' and normalized[-1] == '"':
        normalized = normalized[1:-1]
    if not normalized.isdigit():
        raise HTTPException(status_code=400, detail="invalid_if_match")
    return int(normalized)


def _etag(version: int) -> str:
    return f'"{max(0, int(version))}"'


def _set_version_headers(response: Response, version: int) -> None:
    response.headers["ETag"] = _etag(version)
    response.headers["Cache-Control"] = "no-store"


def _version_conflict(expected: int, current: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "version_conflict",
            "expected_version": expected,
            "current_version": current,
        },
        headers={"ETag": _etag(current), "Cache-Control": "no-store"},
    )


def _audit_summary(config: dict[str, Any]) -> dict[str, object]:
    return {
        "version": int(config.get("version") or 0),
        "enabled": bool(config.get("enabled")),
        "cooldown_seconds": int(config.get("cooldown_seconds") or 0),
    }


def _trace_id(request: Request) -> str:
    return str(
        request.headers.get("X-Trace-ID")
        or request.headers.get("X-Request-ID")
        or getattr(request.state, "admin_request_id", "")
    ).strip()[:128]


def _required_idempotency_key(value: str | None) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail={"code": "idempotency_key_required"},
        )
    if len(normalized) > 128:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_idempotency_key"},
        )
    return normalized


def _idempotency_conflict() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": "idempotency_key_conflict"},
    )


def _mutation_audit(
    request: Request,
    *,
    scope: dict[str, object],
    reason_code: str,
) -> MutationAudit:
    principal = getattr(request.state, "admin_principal", None)
    return MutationAudit(
        actor=str(getattr(principal, "subject", "") or "unknown")[:128],
        actor_kind=str(getattr(principal, "auth_kind", "") or "unknown")[:32],
        roles=tuple(str(role)[:64] for role in (getattr(principal, "roles", ()) or ())),
        scope=scope,
        reason_code=reason_code,
        trace_id=_trace_id(request),
    )


def _set_mutation_headers(response: Response, outcome: MutationOutcome) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Mutation-ID"] = outcome.mutation_id
    if outcome.replayed:
        response.headers["Idempotent-Replayed"] = "true"
