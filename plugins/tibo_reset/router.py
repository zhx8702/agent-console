from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status

from app.admin.auth_router import authenticate_admin_request
from app.admin.mutation_ledger import (
    MutationAudit,
    MutationChange,
    MutationIdempotencyConflictError,
    MutationIdentity,
    MutationOutcome,
)
from plugins.tibo_reset.service import TiboResetService
from plugins.tibo_reset.store import TiboResetStore


def _require_admin(request: Request, settings: Any) -> None:
    authenticate_admin_request(request, settings)


def build_tibo_reset_router(
    store: TiboResetStore,
    service: TiboResetService,
    settings: Any,
) -> APIRouter:
    router = APIRouter()

    @router.get("/status")
    async def status(request: Request):
        _require_admin(request, settings)
        return await service.status()

    @router.get("/stats")
    async def stats(
        request: Request,
        timezone: str = Query(default=""),
    ):
        _require_admin(request, settings)
        return await service.stats(timezone_name=timezone or None)

    @router.post("/poll/run-once")
    async def run_once(
        request: Request,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        _require_admin(request, settings)
        operation_key = _required_idempotency_key(idempotency_key)

        async def mutate() -> MutationChange:
            result = _safe_poll_result(await service.poll_once())
            return MutationChange(
                response=result,
                before_state={"execution_requested": True},
                after_state=_poll_audit_state(result),
                resource_version=str(result.get("latest_tweet_id") or result.get("fetched") or 0),
            )

        try:
            outcome = await store.run_admin_mutation(
                identity=MutationIdentity(
                    tenant_id="__platform__",
                    plugin_name="tibo_reset",
                    operation="tibo_reset.poll.run_once",
                    resource_key="reset_feed",
                    idempotency_key=operation_key,
                    request_payload={"operation": "poll_once"},
                ),
                audit=_mutation_audit(request),
                mutate=mutate,
            )
        except MutationIdempotencyConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "idempotency_key_conflict"},
            ) from exc
        _set_mutation_headers(response, outcome)
        return outcome.response

    @router.get("/feed")
    async def feed(
        request: Request,
        limit: int = Query(default=50, ge=1, le=500),
    ):
        _require_admin(request, settings)
        return {"items": await store.list_feed(limit=limit)}

    @router.get("/deliveries")
    async def deliveries(
        request: Request,
        tenant_id: str = Query(default=""),
        session_id: str = Query(default=""),
        status: str = Query(default=""),
        limit: int = Query(default=100, ge=1, le=500),
    ):
        _require_admin(request, settings)
        return {
            "items": await store.list_deliveries(
                tenant_id=tenant_id,
                session_id=session_id,
                status=status,
                limit=limit,
            )
        }

    return router


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


def _mutation_audit(request: Request) -> MutationAudit:
    principal = getattr(request.state, "admin_principal", None)
    trace_id = str(
        request.headers.get("X-Trace-ID")
        or request.headers.get("X-Request-ID")
        or getattr(request.state, "admin_request_id", "")
    ).strip()[:128]
    return MutationAudit(
        actor=str(getattr(principal, "subject", "") or "unknown")[:128],
        actor_kind=str(getattr(principal, "auth_kind", "") or "unknown")[:32],
        roles=tuple(str(role)[:64] for role in (getattr(principal, "roles", ()) or ())),
        scope={"manual_poll": True},
        reason_code="manual_tibo_reset_poll",
        trace_id=trace_id,
    )


def _safe_poll_result(payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in (
        "status",
        "fetched",
        "groups",
        "claimed",
        "queued",
        "failed",
        "deferred",
        "scope_denied",
    ):
        if key in payload:
            result[key] = payload[key]
    ingest = payload.get("ingest")
    if isinstance(ingest, dict):
        for key in (
            "baseline",
            "inserted",
            "eligible_inserted",
            "latest_tweet_id",
        ):
            if key in ingest:
                result[key] = ingest[key]
    return result


def _poll_audit_state(payload: dict[str, Any]) -> dict[str, object]:
    return {
        "status": str(payload.get("status") or "")[:32],
        "fetched": int(payload.get("fetched") or 0),
        "groups": int(payload.get("groups") or 0),
        "claimed": int(payload.get("claimed") or 0),
        "queued": int(payload.get("queued") or 0),
        "failed": int(payload.get("failed") or 0),
        "deferred": int(payload.get("deferred") or 0),
        "scope_denied": int(payload.get("scope_denied") or 0),
    }


def _set_mutation_headers(response: Response, outcome: MutationOutcome) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Mutation-ID"] = outcome.mutation_id
    if outcome.replayed:
        response.headers["Idempotent-Replayed"] = "true"
