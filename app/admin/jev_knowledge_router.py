"""Tenant-scoped operator review of Jev knowledge candidates."""
from __future__ import annotations

import json
from typing import Literal

from fastapi import Header, HTTPException, Query, Request
from pydantic import Field
from sqlalchemy import text

from app.admin.authorization import AdminPermission, RoutePermission
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
from app.jev.knowledge import KnowledgeEvidenceChanged, KnowledgeScopeDisabled
from app.jev.knowledge_publish import publish_candidate


class KnowledgeAction(StrictRequestModel):
    version: int = Field(ge=1)
    action: Literal["publish", "apply_revision", "reject", "retry"]
    reason: str = Field(min_length=1, max_length=500)


def register_knowledge_routes(router, jev, principal_for):
    def service():
        if jev.knowledge_service is None:
            raise HTTPException(503, "knowledge_service_unavailable")
        return jev.knowledge_service

    from app.admin.jev_quality_router import register_quality_routes
    from app.admin.jev_revision_router import register_revision_routes
    register_revision_routes(router, service, principal_for)
    register_quality_routes(router, service, principal_for)

    @router.get("/knowledge")
    @declare_route_permission(RoutePermission("GET", "/v1/admin/jev/knowledge", AdminPermission.READ))
    async def dashboard(request: Request, tenant_id: str = Query(min_length=1, max_length=64),
                        session_id: str = Query(default="", max_length=256),
                        job_cursor: str = Query(default="", max_length=512),
                        candidate_cursor: str = Query(default="", max_length=512),
                        finding_cursor: str = Query(default="", max_length=512),
                        candidate_status: Literal["", "pending", "running", "failed", "skipped", "needs_review", "ready", "revision_ready", "unresolved", "resolved", "published", "rejected", "duplicate"] = "",
                        finding_status: Literal["", "needs_review", "supported", "rejected", "confirmed", "dismissed"] = "",
                        page_size: int = Query(default=100, ge=1, le=100)):
        principal_for(request, tenant_id)
        try:
            return await service().store.dashboard(tenant_id, session_id, job_cursor=job_cursor,
                candidate_cursor=candidate_cursor, finding_cursor=finding_cursor,
                candidate_status=candidate_status, finding_status=finding_status, page_size=page_size)
        except ValueError as exc:
            raise HTTPException(400, "invalid_knowledge_cursor") from exc

    @router.post("/knowledge/jobs/{job_id}/retry")
    @declare_route_permission(RoutePermission("POST", "/v1/admin/jev/knowledge/jobs/{job_id}/retry", AdminPermission.DANGER))
    async def retry_job(job_id: str, request: Request, tenant_id: str = Query(min_length=1, max_length=64),
                        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128)):
        principal = principal_for(request, tenant_id)
        async with get_engine().begin() as conn:
            async def mutate():
                row = (await conn.execute(text("SELECT status,session_id FROM jev_knowledge_job WHERE id=:id AND tenant_id=:tid FOR UPDATE"),
                                         {"id": job_id, "tid": tenant_id})).mappings().first()
                if not row:
                    raise HTTPException(404, "knowledge_job_not_found")
                if row["status"] not in {"failed", "skipped"}:
                    raise HTTPException(409, "knowledge_job_not_retryable")
                try:
                    await service().require_scope(tenant_id, row["session_id"])
                except KnowledgeScopeDisabled as exc:
                    raise HTTPException(409, str(exc)) from exc
                await conn.execute(text("UPDATE jev_knowledge_job SET status='pending',attempts=0,error_type='',"
                    "lease_token=NULL,locked_until=NULL,next_run_at=NOW(),updated_at=NOW() WHERE id=:id"), {"id": job_id})
                return MutationChange(response={"id": job_id, "status": "pending"},
                                      before_state={"status": row["status"]}, after_state={"status": "pending"})
            try:
                outcome = await run_idempotent_mutation(conn, identity=MutationIdentity(tenant_id=tenant_id,
                    plugin_name="jev", operation="knowledge.job.retry", resource_key=job_id,
                    idempotency_key=idempotency_key, request_payload={"job_id": job_id}),
                    audit=MutationAudit(actor=principal.subject, roles=principal.roles, reason_code="jev_knowledge_retry"), mutate=mutate)
            except MutationIdempotencyConflictError as exc:
                raise HTTPException(409, "idempotency_key_conflict") from exc
        return outcome.response

    @router.get("/knowledge/candidates/{candidate_id}/evidence")
    @declare_route_permission(RoutePermission("GET", "/v1/admin/jev/knowledge/candidates/{candidate_id}/evidence", AdminPermission.READ))
    async def evidence(candidate_id: str, request: Request, tenant_id: str = Query(min_length=1, max_length=64)):
        principal_for(request, tenant_id)
        from app.jev.store import execute
        rows = await execute("SELECT * FROM jev_knowledge_candidate WHERE id=:id AND tenant_id=:tid",
                             {"id": candidate_id, "tid": tenant_id})
        if not rows:
            raise HTTPException(404, "candidate_not_found")
        try:
            await service().require_scope(tenant_id, rows[0]["session_id"])
            _, messages = await service().candidate_evidence(rows[0])
        except (KnowledgeScopeDisabled, KnowledgeEvidenceChanged) as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"messages": service().message_payload(messages)}

    @router.get("/knowledge/findings/{finding_id}/evidence")
    @declare_route_permission(RoutePermission("GET", "/v1/admin/jev/knowledge/findings/{finding_id}/evidence", AdminPermission.READ))
    async def finding_evidence(finding_id: str, request: Request, tenant_id: str = Query(min_length=1, max_length=64)):
        principal_for(request, tenant_id)
        from app.jev.store import execute
        rows = await execute("SELECT * FROM jev_quality_finding WHERE id=:id AND tenant_id=:tid", {"id": finding_id, "tid": tenant_id})
        if not rows:
            raise HTTPException(404, "finding_not_found")
        svc = service()
        row = rows[0]
        try:
            await svc.require_scope(tenant_id, row["session_id"])
            ids = row["finding"]["evidence_ids"]
            messages = await svc.store.evidence(tenant_id, row["session_id"], ids)
            messages = await svc.permitted_messages(tenant_id, messages)
            if {r["id"] for r in messages} != set(ids):
                raise KnowledgeEvidenceChanged("source_removed_or_blocked")
        except (KnowledgeScopeDisabled, KnowledgeEvidenceChanged) as exc:
            raise HTTPException(409, str(exc)) from exc
        payload = svc.message_payload(messages)
        runtime = await svc.store.runtime_evidence(tenant_id, row["session_id"], ids)
        safe_runtime = svc.runtime_payload(runtime)
        return {"messages": payload, "evidence_hash": svc.quality_evidence_fingerprint(messages, runtime), "runtime": safe_runtime}

    @router.post("/knowledge/candidates/{candidate_id}")
    @declare_route_permission(RoutePermission("POST", "/v1/admin/jev/knowledge/candidates/{candidate_id}", AdminPermission.DANGER))
    async def act(candidate_id: str, body: KnowledgeAction, request: Request,
                  tenant_id: str = Query(min_length=1, max_length=64),
                  idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128)):
        principal = principal_for(request, tenant_id)
        svc = service()
        async with get_engine().begin() as conn:
            async def mutate():
                # Member locks precede candidate locks, matching the erasure path.
                if body.action in {"publish", "apply_revision"}:
                    initial = (await conn.execute(text("SELECT source_members FROM jev_knowledge_candidate WHERE id=:id AND tenant_id=:tid"),
                               {"id": candidate_id, "tid": tenant_id})).mappings().first()
                    if initial:
                        for member in sorted(initial["source_members"]):
                            await conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                                {"key": f"memory-member-v1:{tenant_id}:{member}"})
                row = (await conn.execute(text("SELECT * FROM jev_knowledge_candidate WHERE id=:id AND tenant_id=:tid FOR UPDATE"),
                       {"id": candidate_id, "tid": tenant_id})).mappings().first()
                if not row:
                    raise HTTPException(404, "candidate_not_found")
                item = dict(row)
                if item["version"] != body.version:
                    raise HTTPException(409, "candidate_version_conflict")
                if item["status"] in {"running", "published"}:
                    raise HTTPException(409, "candidate_not_editable")
                doc_id = None
                status = "rejected" if body.action == "reject" else "pending"
                revision = dict(item.get("revision") or {})
                if body.action in {"publish", "apply_revision"}:
                    expected_status = "revision_ready" if body.action == "apply_revision" else "ready"
                    if item["status"] != expected_status:
                        raise HTTPException(409, "candidate_requires_supported_review")
                    try:
                        doc_id, revision = await publish_candidate(svc, item, actor=principal.subject,
                            reason=body.reason, apply_revision=body.action == "apply_revision")
                    except (KnowledgeScopeDisabled, KnowledgeEvidenceChanged) as exc:
                        raise HTTPException(409, str(exc)) from exc
                    status = "published"
                elif body.action == "retry" and item["status"] not in {"failed", "skipped", "needs_review", "unresolved", "duplicate", "ready", "revision_ready"}:
                    raise HTTPException(409, "candidate_not_retryable")
                await conn.execute(text("UPDATE jev_knowledge_candidate SET status=:status,version=version+1,"
                    "reviewed_by=:actor,reason=:reason,kb_doc_id=:doc,review=CAST(:review AS JSON),revision=CAST(:revision AS JSON),attempts=0,error_type='',"
                    "next_run_at=NOW(),updated_at=NOW() WHERE id=:id"),
                    {"status": status, "actor": principal.subject, "reason": "operator_"+body.action,
                     "doc": doc_id, "id": candidate_id, "revision": json.dumps(revision), "review": json.dumps({**item["review"], "_operator": {"action": body.action, "reason": body.reason, "actor": principal.subject}})})
                return MutationChange(response={"id": candidate_id, "status": status, "version": body.version+1, "kb_doc_id": doc_id},
                    before_state={"status": item["status"], "version": body.version},
                    after_state={"status": status, "kb_doc_id": doc_id}, resource_version=str(body.version+1))
            try:
                outcome = await run_idempotent_mutation(conn, identity=MutationIdentity(
                    tenant_id=tenant_id, plugin_name="jev", operation="knowledge.candidate.review",
                    resource_key=candidate_id, idempotency_key=idempotency_key, request_payload=body.model_dump()),
                    audit=MutationAudit(actor=principal.subject, roles=principal.roles, reason_code="jev_knowledge_review"), mutate=mutate)
            except MutationIdempotencyConflictError as exc:
                raise HTTPException(409, "idempotency_key_conflict") from exc
        return outcome.response
