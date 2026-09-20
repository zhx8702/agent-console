"""Human disposition of daily quality findings, independent of knowledge publication."""
from __future__ import annotations

import json
from datetime import UTC, datetime
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
from app.jev.knowledge import KnowledgeScopeDisabled
from app.jev.models import fingerprint, redact


class FindingAction(StrictRequestModel):
    action: Literal['confirm', 'dismiss']
    expected_status: Literal['needs_review', 'supported', 'rejected']
    reason: str = Field(min_length=1, max_length=500)
    evidence_hash: str = Field(default='', max_length=64)


def register_quality_routes(router, service, principal_for):
    @router.post('/knowledge/findings/{finding_id}')
    @declare_route_permission(RoutePermission('POST', '/v1/admin/jev/knowledge/findings/{finding_id}', AdminPermission.DANGER))
    async def review(finding_id: str, body: FindingAction, request: Request,
                     tenant_id: str = Query(min_length=1, max_length=64),
                     idempotency_key: str = Header(alias='Idempotency-Key', min_length=1, max_length=128)):
        principal = principal_for(request, tenant_id)
        svc = service()
        async with get_engine().begin() as conn:
            async def mutate():
                params = {'id': finding_id, 'tid': tenant_id}
                initial = (await conn.execute(text('SELECT source_members FROM jev_quality_finding WHERE id=:id AND tenant_id=:tid'), params)).mappings().first()
                if not initial:
                    raise HTTPException(404, 'finding_not_found')
                for member in sorted(initial['source_members']):
                    await conn.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,0))'),
                        {'key': f'memory-member-v1:{tenant_id}:{member}'})
                row = (await conn.execute(text('SELECT * FROM jev_quality_finding WHERE id=:id AND tenant_id=:tid FOR UPDATE'), params)).mappings().first()
                if row is None:
                    raise HTTPException(404, 'finding_not_found')
                if row['status'] != body.expected_status:
                    raise HTTPException(409, 'finding_status_changed')
                if body.action == 'confirm':
                    try:
                        await svc.require_scope(tenant_id, row['session_id'])
                    except KnowledgeScopeDisabled as exc:
                        raise HTTPException(409, str(exc)) from exc
                    ids = row['finding']['evidence_ids']
                    messages = await svc.store.evidence(tenant_id, row['session_id'], ids)
                    messages = await svc.permitted_messages(tenant_id, messages)
                    if set(ids) != {r['id'] for r in messages}:
                        raise HTTPException(409, 'source_removed_or_blocked')
                    runtime = await svc.store.runtime_evidence(tenant_id, row['session_id'], ids)
                    current_hash = fingerprint({'messages': svc.message_payload(messages), 'runtime': svc.runtime_payload(runtime)})
                    if not body.evidence_hash or body.evidence_hash != current_hash:
                        raise HTTPException(409, 'finding_evidence_changed_reload')
                status = 'confirmed' if body.action == 'confirm' else 'dismissed'
                reviewed = {**row['review'], '_operator': {'actor': principal.subject, 'action': body.action,
                    'reason': redact(body.reason, limit=500), 'at': datetime.now(UTC).isoformat(),
                    'previous_status': row['status'], 'evidence_hash': body.evidence_hash}}
                await conn.execute(text('UPDATE jev_quality_finding SET status=:status,review=CAST(:review AS JSON) WHERE id=:id AND tenant_id=:tid'),
                    {**params, 'status': status, 'review': json.dumps(reviewed)})
                return MutationChange(response={'id': finding_id, 'status': status},
                    before_state={'status': row['status']}, after_state={'status': status})
            try:
                result = await run_idempotent_mutation(conn, identity=MutationIdentity(tenant_id=tenant_id,
                    plugin_name='jev', operation='quality.finding.review', resource_key=finding_id,
                    idempotency_key=idempotency_key, request_payload=body.model_dump()),
                    audit=MutationAudit(actor=principal.subject, roles=principal.roles, reason_code='jev_quality_review'), mutate=mutate)
            except MutationIdempotencyConflictError as exc:
                raise HTTPException(409, 'idempotency_key_conflict') from exc
        return result.response
