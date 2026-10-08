"""Editable, evidence-reviewed group knowledge revisions with retained baselines."""
from __future__ import annotations

import json
from uuid import uuid4

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
from app.jev.models import redact


class RevisionWrite(StrictRequestModel):
    version: int = Field(ge=1)
    target_doc_id: int = Field(gt=0)
    base_hash: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]+$")
    title: str = Field(min_length=1, max_length=180)
    content: str = Field(min_length=1, max_length=24000)
    reason: str = Field(min_length=1, max_length=500)


def register_revision_routes(router, service, principal_for):
    @router.post("/knowledge/candidates/{candidate_id}/revision")
    @declare_route_permission(RoutePermission("POST", "/v1/admin/jev/knowledge/candidates/{candidate_id}/revision", AdminPermission.DANGER))
    async def edit(candidate_id: str, body: RevisionWrite, request: Request,
                   tenant_id: str = Query(min_length=1, max_length=64),
                   idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128)):
        principal = principal_for(request, tenant_id)
        svc = service()
        async with get_engine().begin() as conn:
            async def mutate():
                initial = (await conn.execute(text("SELECT session_id,source_members FROM jev_knowledge_candidate WHERE tenant_id=:tid AND id=:id"),
                           {"tid": tenant_id, "id": candidate_id})).mappings().first()
                if not initial:
                    raise HTTPException(404, "candidate_not_found")
                sid = initial["session_id"]
                doc = await svc.kb.get_document(tenant_id, body.target_doc_id, session_id=sid)
                if doc is None:
                    raise HTTPException(404, "group_knowledge_not_found")
                members = sorted(set(initial["source_members"]) | set((doc.meta or {}).get("source_members") or []))
                for member in members:
                    await conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                                       {"key": f"memory-member-v1:{tenant_id}:{member}"})
                row = (await conn.execute(text("SELECT * FROM jev_knowledge_candidate WHERE tenant_id=:tid AND id=:id FOR UPDATE"),
                       {"tid": tenant_id, "id": candidate_id})).mappings().first()
                if not row:
                    raise HTTPException(404, "candidate_not_found")
                if row["version"] != body.version or row["status"] not in {"needs_review", "revision_ready", "ready", "duplicate"}:
                    raise HTTPException(409, "candidate_version_or_status_changed")
                async with svc.kb.mutation_scope(tenant_id, [sid]):
                    doc = await svc.kb.get_document(tenant_id, body.target_doc_id, session_id=sid)
                    if doc is None or doc.content_hash != body.base_hash:
                        raise HTTPException(409, "revision_base_changed")
                    if len(doc.content) > 24000:
                        raise HTTPException(409, "revision_baseline_too_large")
                    if not set((doc.meta or {}).get("source_members") or []).issubset(members):
                        raise HTTPException(409, "revision_provenance_changed")
                    try:
                        await svc.require_scope(tenant_id, sid)
                        await svc.candidate_evidence(dict(row))
                        if await svc.store.blocked_members(tenant_id, members):
                            raise KnowledgeEvidenceChanged("revision_member_blocked")
                    except (KnowledgeScopeDisabled, KnowledgeEvidenceChanged) as exc:
                        raise HTTPException(409, str(exc)) from exc
                    if not any(c["doc_id"] == doc.id and c.get("session_id", sid) == sid for c in row["comparisons"]):
                        raise HTTPException(409, "revision_target_not_compared")
                    revision = {"id": str(uuid4()), "target_doc_id": doc.id, "base_hash": doc.content_hash,
                        "title": redact(body.title, limit=180), "content": redact(body.content, limit=24000),
                        "before": {"title": doc.title, "content": doc.content, "source": doc.source, "url": doc.url, "metadata": doc.meta},
                        "origin": "operator", "edited_by": principal.subject, "edit_reason": body.reason,
                        "status": "pending", "source_members": members}
                    await conn.execute(text("UPDATE jev_knowledge_candidate SET revision=CAST(:revision AS JSON),"
                        "source_members=CAST(:members AS JSON),status='pending',version=version+1,attempts=0,"
                        "reason='operator_revision',error_type='',next_run_at=NOW(),updated_at=NOW() WHERE id=:id"),
                        {"id": candidate_id, "revision": json.dumps(revision), "members": json.dumps(members)})
                return MutationChange(response={"id": candidate_id, "version": body.version+1, "status": "pending"},
                    before_state={"version": body.version}, after_state={"version": body.version+1, "status": "pending"})
            try:
                outcome = await run_idempotent_mutation(conn, identity=MutationIdentity(tenant_id=tenant_id,
                    plugin_name="jev", operation="knowledge.revision.edit", resource_key=candidate_id,
                    idempotency_key=idempotency_key, request_payload=body.model_dump()),
                    audit=MutationAudit(actor=principal.subject, roles=principal.roles, reason_code="jev_revision_edit"), mutate=mutate)
            except MutationIdempotencyConflictError as exc:
                raise HTTPException(409, "idempotency_key_conflict") from exc
        return outcome.response

    @router.get("/knowledge/documents/{doc_id}/history")
    @declare_route_permission(RoutePermission("GET", "/v1/admin/jev/knowledge/documents/{doc_id}/history", AdminPermission.READ))
    async def history(doc_id: int, request: Request, tenant_id: str = Query(min_length=1, max_length=64),
                      session_id: str = Query(min_length=1, max_length=256)):
        principal_for(request, tenant_id)
        from app.jev.store import execute
        rows = await execute("SELECT id,revision,updated_at FROM jev_knowledge_candidate WHERE tenant_id=:tid "
            "AND session_id=:sid AND kb_doc_id=:doc AND status='published' AND CAST(revision AS JSONB)<>'{}'::jsonb ORDER BY updated_at DESC",
            {"tid": tenant_id, "sid": session_id, "doc": doc_id})
        return {"items": rows}
