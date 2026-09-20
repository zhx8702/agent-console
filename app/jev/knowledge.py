"""Daily, resumable group knowledge extraction and independent Jev review.

No conversation sends. Publication remains a separately audited operator action.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime

from app.common.logging import get_logger
from app.common.types import ChatMessage, ChatRequest, Role
from app.jev.knowledge_models import (
    EXTRACTION_PROMPT,
    KnowledgeDraft,
    check_evidence,
    comparison_questions,
    day_bounds,
    due_days,
    knowledge_questions,
    parse_extraction,
    review_disposition,
)
from app.jev.knowledge_store import KnowledgeStore
from app.jev.models import answer, confidence, fingerprint, probability, redact
from app.llm.activity import wait_for_llm_activity

log = get_logger(__name__)


class KnowledgeScopeDisabled(RuntimeError):
    pass


class KnowledgeEvidenceChanged(RuntimeError):
    pass


class JevKnowledgeService:
    def __init__(self, jev, *, llm, kb, store=None):
        self.jev = jev
        self.llm = llm
        self.kb = kb
        self.store = store or KnowledgeStore()
        self._task = None
        self._schedule_at = 0.0

    def start(self):
        if self.jev.enabled and self.kb is not None and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="jev-daily-knowledge")

    async def close(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("jev.knowledge_tick_failed", error_type=type(exc).__name__)
            await asyncio.sleep(5)

    async def require_scope(self, tenant_id: str, session_id: str):
        policy, _ = await self.jev.store.policy(tenant_id, self.jev.defaults())
        if not self.jev.enabled or self.kb is None or not policy.enabled or not policy.knowledge or session_id not in policy.knowledge_sessions:
            raise KnowledgeScopeDisabled("knowledge_scope_disabled")
        registry = self.jev.registry
        if registry is None:
            raise KnowledgeScopeDisabled("owner_unavailable")
        for owner in ("wxbot", "memory"):
            if await registry.scope_execution_allowed(owner, tenant_id=tenant_id, session_id=session_id) is not True:
                raise KnowledgeScopeDisabled("owner_disabled")
        return policy

    async def permitted_messages(self, tenant_id: str, messages: list[dict]) -> list[dict]:
        members = sorted({str(row.get("sender_wxid") or "") for row in messages if not row.get("is_self_sent")})
        blocked = await self.store.blocked_members(tenant_id, members)
        return [row for row in messages if row.get("is_self_sent") or (row.get("sender_wxid") and row["sender_wxid"] not in blocked)]

    @staticmethod
    def message_payload(messages: list[dict]) -> list[dict]:
        identities = {}
        for row in messages:
            member = str(row.get("sender_wxid") or "")
            token = "bot" if row.get("is_self_sent") else identities.setdefault(member, f"member_{len(identities)+1}")
            if member:
                identities[member] = token
            if len(str(row.get("sender_name") or "")) >= 2:
                identities[str(row["sender_name"])] = token
        return [{"id": int(row["id"]), "speaker": "bot" if row.get("is_self_sent") else identities.get(row.get("sender_wxid"), "unknown"),
                 "is_bot": bool(row.get("is_self_sent")), "occurred_ts": row["occurred_ts"],
                 "text": redact(row["content"], identities, 3000)} for row in messages]

    async def schedule(self, now: datetime):
        for tenant_id, policy in await self.store.policies():
            if not policy.enabled or not policy.knowledge:
                continue
            for session_id in policy.knowledge_sessions:
                try:
                    await self.require_scope(tenant_id, session_id)
                except KnowledgeScopeDisabled:
                    continue
                for period in due_days(now, policy.knowledge_timezone, policy.knowledge_daily_hour):
                    start, end = day_bounds(period, policy.knowledge_timezone)
                    await self.store.schedule(tenant_id, session_id, period, start, end)

    async def tick(self):
        if not self.jev.enabled or self.kb is None:
            return
        if time.monotonic() >= self._schedule_at:
            await self.schedule(datetime.now(UTC))
            self._schedule_at = time.monotonic() + 60
        # One bounded page and one candidate per tick. Separate task from online decisions.
        for kind, handler in (("job", self.extract_page), ("candidate", self.review_candidate)):
            row = await self.store.claim(kind)
            if not row:
                continue
            try:
                await handler(row)
            except asyncio.CancelledError:
                raise
            except (KnowledgeScopeDisabled, KnowledgeEvidenceChanged) as exc:
                await self.store.finish(kind, row, status="skipped", error=type(exc).__name__)
            except Exception as exc:
                retry = row["attempts"] < 3
                await self.store.finish(kind, row, status="pending" if retry else "failed", error=type(exc).__name__, retry=retry)
                log.warning("jev.knowledge_job_failed", kind=kind, job_id=row["id"], error_type=type(exc).__name__)

    async def extract_page(self, job: dict):
        tenant_id, session_id = job["tenant_id"], job["session_id"]
        await self.require_scope(tenant_id, session_id)
        page = await self.store.page(job)
        if not page:
            await self.store.save_page(job, [], [])
            return
        opened = await self.store.open_candidates(tenant_id, session_id)
        prior_ids = sorted({i for candidate in opened for i in candidate["draft"]["evidence_ids"]})
        prior_messages = await self.store.evidence(tenant_id, session_id, prior_ids) if prior_ids else []
        combined = {r["id"]: r for r in [*prior_messages, *await self.store.context(job), *page]}
        messages = await self.permitted_messages(tenant_id, sorted(combined.values(), key=lambda r: r["id"]))
        available_ids = {r["id"] for r in messages}
        opened = [c for c in opened if set(c["draft"]["evidence_ids"]).issubset(available_ids)]
        if not any(not row.get("is_self_sent") for row in messages):
            await self.store.save_page(job, page, [])
            return
        payload = self.message_payload(messages)
        response = await wait_for_llm_activity(self.llm.chat(ChatRequest(
            tenant_id=tenant_id, trace_id=f"knowledge_{job['id']}_{job['cursor_id']}",
            model_tier="tier-2", system=EXTRACTION_PROMPT,
            messages=[ChatMessage(role=Role.USER, content=json.dumps({"messages": payload, "open_questions": [{"id": c["id"], "question": c["draft"]["question"], "evidence_ids": c["draft"]["evidence_ids"]} for c in opened]}, ensure_ascii=False))],
            max_tokens=6000, temperature=0.1,
            metadata={"purpose": "jev_knowledge_extraction"})), timeout=120)
        extraction = parse_extraction(response.content)
        # Reject the whole malformed batch so the checkpoint never skips unprocessed messages.
        drafts = []
        page_ids = {int(row["id"]) for row in page}
        for draft in extraction.candidates:
            check_evidence(draft, messages)
            if draft.resolves_candidate_id:
                prior = next((c for c in opened if c["id"] == draft.resolves_candidate_id), None)
                if prior is None or not set(prior["draft"]["evidence_ids"]).intersection(draft.evidence_ids):
                    raise ValueError("unverified_prior_question")
            if not page_ids.intersection(draft.evidence_ids):
                continue  # Context-only discoveries already belonged to a previous page.
            # Generated text may reintroduce secrets; redact again before storage/evaluation.
            clean = draft.model_dump()
            for field in ("title", "question", "environment", "solution", "outcome"):
                clean[field] = redact(clean[field], limit=4000)
            drafts.append(KnowledgeDraft.model_validate(clean).model_dump())
        await self.require_scope(tenant_id, session_id)
        current = await self.permitted_messages(tenant_id, messages)
        if {r["id"] for r in current} != {r["id"] for r in messages}:
            raise KnowledgeEvidenceChanged("member_policy_changed")
        await self.store.save_page(job, page, drafts)

    async def candidate_evidence(self, row: dict):
        draft = KnowledgeDraft.model_validate(row["draft"])
        evidence = await self.store.evidence(row["tenant_id"], row["session_id"], draft.evidence_ids)
        evidence = await self.permitted_messages(row["tenant_id"], evidence)
        try:
            check_evidence(draft, evidence)
        except ValueError as exc:
            raise KnowledgeEvidenceChanged("source_removed_or_blocked") from exc
        return draft, evidence

    async def erase_member(self, *, tenant_id: str, user_id: str, run):
        """Called under the existing member erasure lock/transaction.

        Delete vector projections before reporting erasure complete. Failure propagates so
        the existing durable deletion job retries; this is not best-effort cleanup.
        """
        docs = await run("SELECT id,session_id FROM kb_documents WHERE tenant_id=:tid AND source='jev_group_knowledge' "
                         "AND CAST(metadata->'source_members' AS JSONB) @> CAST(:members AS JSONB)",
                         {"tid": tenant_id, "members": json.dumps([user_id])})
        if docs and self.kb is None:
            raise RuntimeError("knowledge_erasure_index_unavailable")
        for doc in docs:
            await self.kb.delete_document(tenant_id, int(doc["id"]), session_id=doc["session_id"])
        await run("DELETE FROM jev_knowledge_candidate WHERE tenant_id=:tid "
                  "AND CAST(source_members AS JSONB) @> CAST(:members AS JSONB)",
                  {"tid": tenant_id, "members": json.dumps([user_id])})

    async def evaluate(self, row: dict, *, questions: dict, state: dict, purpose: str):
        await self.require_scope(row["tenant_id"], row["session_id"])
        started = time.monotonic()
        result = await self.jev.evaluate_questions(questions, state, timeout=self.jev.settings.typesafe_timeout)
        await self.jev.store.record_online(tenant_id=row["tenant_id"], session_id=row["session_id"], domain="knowledge",
            key=fingerprint([row["id"], purpose, state, result.get("model")]), result=result,
            duration_ms=int((time.monotonic()-started)*1000), applied=False)
        return result

    async def review_candidate(self, row: dict):
        policy = await self.require_scope(row["tenant_id"], row["session_id"])
        draft, evidence = await self.candidate_evidence(row)
        prior = None
        if draft.resolves_candidate_id:
            prior = await self.store.candidate(row["tenant_id"], row["session_id"], draft.resolves_candidate_id)
            if prior is None or prior["status"] != "unresolved":
                raise KnowledgeEvidenceChanged("prior_question_changed")
            await self.candidate_evidence(prior)
        result = await self.evaluate(row, questions=knowledge_questions(),
            state={"candidate": draft.model_dump(), "evidence": self.message_payload(evidence),
                   "prior_question": prior["draft"]["question"] if prior else None}, purpose="review")
        disposition, reason = review_disposition(draft, result, policy.knowledge_min_confidence)
        if prior and probability(result, "resolves_prior", fallback=0) < policy.knowledge_min_confidence:
            disposition, reason = "needs_review", "prior_resolution_uncertain"
        comparisons = []
        snapshot = await self.store.knowledge_snapshot(row["tenant_id"], row["session_id"], row["id"])
        if disposition == "ready":
            # Compare authorized group knowledge and tenant-global documents, never another group.
            docs = {}
            for scope in (row["session_id"], ""):
                if not await self.kb.list_documents(row["tenant_id"], session_id=scope, limit=1):
                    continue  # Empty scope has no vector collection yet.
                hits = await self.kb.search_documents(row["tenant_id"], draft.question,
                    session_id=scope, top_k=5, raise_on_error=True)
                for hit in hits:
                    if hit.doc_id not in docs:
                        doc = await self.kb.get_document(row["tenant_id"], hit.doc_id, session_id=scope)
                        if doc:
                            docs[doc.id] = doc
            for doc in docs.values():
                compared = await self.evaluate(row, questions=comparison_questions(), state={
                    "candidate": draft.model_dump(), "existing": {"title": redact(doc.title), "content": redact(doc.content, limit=6000)}},
                    purpose=f"compare:{doc.id}:{doc.content_hash}")
                relation = answer(compared, "decision")
                comparisons.append({"doc_id": doc.id, "session_id": doc.session_id, "content_hash": doc.content_hash,
                    "relation": relation, "confidence": confidence(compared), "result": compared})
            if any(c["relation"] not in {"duplicate", "unrelated"} or c["confidence"] < policy.knowledge_min_confidence for c in comparisons):
                disposition, reason = "needs_review", "existing_knowledge_requires_review"
            elif any(c["relation"] == "duplicate" for c in comparisons):
                disposition, reason = "duplicate", "existing_knowledge_duplicate"
        if snapshot != await self.store.knowledge_snapshot(row["tenant_id"], row["session_id"], row["id"]):
            raise RuntimeError("knowledge_changed_during_review")
        result["_knowledge_snapshot"] = snapshot
        # No cached policy or stale member permission can authorize the saved recommendation.
        current_policy = await self.require_scope(row["tenant_id"], row["session_id"])
        _, fresh = await self.candidate_evidence(row)
        if fingerprint(self.message_payload(fresh)) != fingerprint(self.message_payload(evidence)):
            raise KnowledgeEvidenceChanged("source_changed")
        if current_policy.knowledge_min_confidence > policy.knowledge_min_confidence:
            disposition, reason = "needs_review", "threshold_changed"
        await self.store.save_review(row, status=disposition, reason=reason, review=result, comparisons=comparisons)
