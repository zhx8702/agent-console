from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import Field
from sqlalchemy import text

from app.admin.authorization import (
    AdminPermission,
    RoutePermission,
    build_admin_authorization_dependency,
)
from app.admin.mutation_ledger import (
    MutationAudit,
    MutationChange,
    MutationIdempotencyConflictError,
    MutationIdentity,
    run_idempotent_mutation,
)
from app.admin.route_permissions import declare_route_permission
from app.common.request_models import StrictRequestModel
from app.infra.db import get_engine
from app.jev.models import JevPolicy


class PolicyWrite(StrictRequestModel):
    version: int = Field(ge=0)
    policy: JevPolicy


def build_jev_router(service, settings) -> APIRouter:
    router = APIRouter(prefix="/v1/admin/jev", tags=["jev"],
                       dependencies=[Depends(build_admin_authorization_dependency(settings))])

    def principal_for(request: Request, tenant_id: str):
        principal = request.state.admin_principal
        if not principal.allows_tenant(tenant_id) or principal.requires_explicit_group_scope:
            raise HTTPException(403, "tenant_admin_scope_required")
        return principal

    @router.get("")
    @declare_route_permission(RoutePermission("GET", "/v1/admin/jev", AdminPermission.READ))
    async def dashboard(request: Request, response: Response,
                        tenant_id: str = Query(min_length=1, max_length=64),
                        domain: Literal["", "relationship", "memory", "intent", "moderation", "participation"] = "",
                        status: Literal["", "pending", "running", "completed", "failed", "skipped"] = "",
                        limit: int = Query(default=50, ge=1, le=100)):
        principal_for(request, tenant_id)
        policy, version = await service.store.policy(tenant_id, service.defaults())
        payload = await service.store.dashboard(tenant_id, domain=domain, status=status, limit=limit)
        response.headers["ETag"] = f'"{version}"'
        return {**payload, "policy": policy.model_dump(), "version": version,
                "runtime": {"enabled": bool(settings.typesafe_enabled),
                            "key_configured": bool(settings.typesafe_api_key),
                            "model": settings.typesafe_model,
                            "worker_concurrency": settings.typesafe_worker_concurrency,
                            "online_timeout": settings.typesafe_online_timeout,
                            "policy_refresh_seconds": 5}}

    @router.put("/policy")
    @declare_route_permission(RoutePermission("PUT", "/v1/admin/jev/policy", AdminPermission.DANGER))
    async def save_policy(body: PolicyWrite, request: Request, response: Response,
                          tenant_id: str = Query(min_length=1, max_length=64),
                          idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128)):
        principal = principal_for(request, tenant_id)
        async with get_engine().begin() as conn:
            async def mutate():
                await conn.execute(text("INSERT INTO jev_policy (tenant_id,version,policy) VALUES (:tid,0,CAST(:policy AS JSON)) ON CONFLICT DO NOTHING"),
                                   {"tid": tenant_id, "policy": service.defaults().model_dump_json()})
                row = (await conn.execute(text("SELECT version,policy FROM jev_policy WHERE tenant_id=:tid FOR UPDATE"), {"tid": tenant_id})).mappings().one()
                if row["version"] != body.version:
                    raise HTTPException(409, "jev_policy_version_conflict")
                version = body.version + 1
                await conn.execute(text("UPDATE jev_policy SET version=:v,policy=CAST(:policy AS JSON) WHERE tenant_id=:tid"),
                                   {"v": version, "policy": body.policy.model_dump_json(), "tid": tenant_id})
                return MutationChange(response={"version": version, "policy": body.policy.model_dump()},
                    before_state={"version": body.version}, after_state={"version": version,
                        "enabled": body.policy.enabled, "shadow_only": body.policy.shadow_only}, resource_version=str(version))
            try:
                outcome = await run_idempotent_mutation(conn,
                    identity=MutationIdentity(tenant_id=tenant_id, plugin_name="jev", operation="policy.update",
                        resource_key=tenant_id, idempotency_key=idempotency_key, request_payload=body.model_dump()),
                    audit=MutationAudit(actor=principal.subject, roles=principal.roles, reason_code="jev_policy_update"), mutate=mutate)
            except MutationIdempotencyConflictError as exc:
                raise HTTPException(409, "idempotency_key_conflict") from exc
        service._policies.pop(tenant_id, None)
        response.headers["ETag"] = f'"{outcome.response["version"]}"'
        return outcome.response

    @router.post("/jobs/{job_id}/retry")
    @declare_route_permission(RoutePermission("POST", "/v1/admin/jev/jobs/{job_id}/retry", AdminPermission.DANGER))
    async def retry(job_id: str, request: Request,
                    tenant_id: str = Query(min_length=1, max_length=64),
                    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128)):
        principal = principal_for(request, tenant_id)
        async with get_engine().begin() as conn:
            async def mutate():
                rows = (await conn.execute(text("SELECT id,status,target_id,state FROM jev_evaluation WHERE id=:id AND tenant_id=:tid FOR UPDATE"),
                                         {"id": job_id, "tid": tenant_id})).mappings().all()
                if not rows:
                    raise HTTPException(404, "evaluation_not_found")
                row = rows[0]
                if row["status"] != "failed" or (row["target_id"] is None and not row["state"]):
                    raise HTTPException(409, "evaluation_not_retryable")
                await conn.execute(text("UPDATE jev_evaluation SET status='pending',attempts=0,error_type='',next_run_at=NOW(),updated_at=NOW() WHERE id=:id"), {"id": job_id})
                return MutationChange(response={"id": job_id, "status": "pending"}, before_state={"status": "failed"}, after_state={"status": "pending"})
            try:
                outcome = await run_idempotent_mutation(conn,
                    identity=MutationIdentity(tenant_id=tenant_id, plugin_name="jev", operation="job.retry",
                        resource_key=job_id, idempotency_key=idempotency_key, request_payload={"job_id": job_id}),
                    audit=MutationAudit(actor=principal.subject, roles=principal.roles, reason_code="jev_job_retry"), mutate=mutate)
            except MutationIdempotencyConflictError as exc:
                raise HTTPException(409, "idempotency_key_conflict") from exc
        return outcome.response

    return router
