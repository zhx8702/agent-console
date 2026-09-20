"""Publication under the same scope locks used by every normal KB writer."""
from __future__ import annotations

from app.jev.knowledge import KnowledgeEvidenceChanged
from app.jev.knowledge_models import review_disposition
from app.jev.models import answer, confidence, probability


async def publish_candidate(svc, item: dict, *, actor: str, reason: str, apply_revision: bool = False) -> tuple[int, dict]:
    tenant_id, session_id = item["tenant_id"], item["session_id"]
    async with svc.kb.mutation_scope(tenant_id, ["", session_id]):
        policy = await svc.require_scope(tenant_id, session_id)
        draft, evidence = await svc.candidate_evidence(item)
        disposition, _ = review_disposition(draft, item["review"], policy.knowledge_min_confidence)
        if item["review"].get("_evidence_hash") != svc.evidence_fingerprint(evidence):
            raise KnowledgeEvidenceChanged("source_changed_reevaluate")
        if disposition != "ready":
            raise KnowledgeEvidenceChanged("candidate_threshold_or_evidence_changed")
        revision = dict(item.get("revision") or {})
        target = None
        if apply_revision:
            evaluation = revision.get("evaluation") or {}
            if (revision.get("status") != "ready" or answer(evaluation, "decision") != "accept"
                    or confidence(evaluation) < policy.knowledge_min_confidence
                    or probability(evaluation, "supported", fallback=0) < policy.knowledge_min_confidence
                    or probability(evaluation, "sensitive", fallback=1) >= .5
                    or revision.get("evidence_hash") != svc.evidence_fingerprint(evidence)):
                raise KnowledgeEvidenceChanged("revision_not_supported")
            target = await svc.kb.get_document(tenant_id, revision["target_doc_id"], session_id=session_id)
            if target is None:
                raise KnowledgeEvidenceChanged("revision_target_missing")
            if await svc.store.blocked_members(tenant_id, revision.get("source_members") or []):
                raise KnowledgeEvidenceChanged("revision_member_blocked")
            # Recover a crash after KB commit but before candidate/audit commit without applying twice.
            if ((target.meta or {}).get("jev_revision_id") == revision["id"]
                    and target.title == revision["title"] and target.content == revision["content"]):
                return target.id, {**revision, "status": "published", "result_hash": target.content_hash}
            if target.content_hash != revision["base_hash"]:
                raise KnowledgeEvidenceChanged("revision_base_changed")
        if item["review"].get("_knowledge_snapshot") != await svc.store.knowledge_snapshot(tenant_id, session_id, item["id"]):
            raise KnowledgeEvidenceChanged("knowledge_changed_reevaluate")
        for comparison in item["comparisons"]:
            doc = await svc.kb.get_document(tenant_id, comparison["doc_id"], session_id=comparison.get("session_id", session_id))
            if doc is None or doc.content_hash != comparison["content_hash"]:
                raise KnowledgeEvidenceChanged("knowledge_changed_reevaluate")
        metadata = {"candidate_id": item["id"], "reviewed_by": actor, "review_reason": reason,
            "evidence_ids": draft.evidence_ids, "resolution_ids": draft.resolution_ids,
            "source_members": sorted(set(item.get("source_members") or []) | {r['sender_wxid'] for r in evidence if not r.get('is_self_sent')}),
            "verification": "human_reported_outcome", "jev": item["review"]}
        if apply_revision:
            metadata = {**(target.meta or {}), **metadata, "jev_revision_id": revision["id"], "previous_content_hash": revision["base_hash"]}
            doc_id = await svc.kb.update_document(tenant_id=tenant_id, session_id=session_id, doc_id=target.id,
                title=revision["title"], content=revision["content"], source=target.source or "manual", url=target.url, metadata=metadata)
            if doc_id is None:
                raise KnowledgeEvidenceChanged("revision_target_missing")
            current = await svc.kb.get_document(tenant_id, doc_id, session_id=session_id)
            return doc_id, {**revision, "status": "published", "result_hash": current.content_hash}
        content = (f"问题：{draft.question}\n\n适用环境：{draft.environment or '未注明'}\n\n"
                   f"处理方法：{draft.solution}\n\n观察结果及限制：{draft.outcome}\n\n"
                   "来源：群成员对话中的解决经验，仅适用于所述条件；未经独立外部核验。")
        doc_id = await svc.kb.add_document(tenant_id=tenant_id, session_id=session_id, title=draft.title,
            content=content, source="jev_group_knowledge", metadata=metadata)
        return doc_id, revision
