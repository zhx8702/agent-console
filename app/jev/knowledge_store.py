"""PostgreSQL queue, progress and immutable candidate evidence references."""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date
from uuid import uuid4

from sqlalchemy import text

from app.infra.db import get_engine
from app.jev.models import JevPolicy, fingerprint
from app.jev.store import execute

_TABLES = {"job": "jev_knowledge_job", "candidate": "jev_knowledge_candidate"}


class KnowledgeStore:
    async def policies(self) -> list[tuple[str, JevPolicy]]:
        rows = await execute("SELECT tenant_id,policy FROM jev_policy ORDER BY tenant_id")
        return [(r["tenant_id"], JevPolicy.model_validate(r["policy"])) for r in rows]

    async def schedule(self, tenant_id: str, session_id: str, period: date, start: int, end: int) -> None:
        await execute("INSERT INTO jev_knowledge_job (id,tenant_id,session_id,period,start_ts,end_ts) "
                      "VALUES (:id,:tid,:sid,:day,:start,:end) ON CONFLICT (tenant_id,session_id,period) DO UPDATE "
                      "SET status='pending',attempts=0,next_run_at=NOW() WHERE jev_knowledge_job.status='completed' "
                      "AND EXISTS (SELECT 1 FROM plugin_wxbot_group_observations o WHERE o.tenant_id=:tid "
                      "AND o.session_id=:sid AND o.occurred_ts>=:start AND o.occurred_ts<:end AND o.id>jev_knowledge_job.cursor_id)",
                      {"id": str(uuid4()), "tid": tenant_id, "sid": session_id, "day": period, "start": start, "end": end})

    async def claim(self, kind: str) -> dict | None:
        table = _TABLES[kind]
        await execute(f"UPDATE {table} SET status='failed',error_type='LeaseExpired',lease_token=NULL,locked_until=NULL "
                      "WHERE status='running' AND locked_until<NOW() AND attempts>=3")
        rows = await execute(
            f"WITH due AS (SELECT id FROM {table} WHERE attempts<3 AND "
            "((status='pending' AND next_run_at<=NOW()) OR (status='running' AND locked_until<NOW())) "
            "ORDER BY next_run_at,created_at LIMIT 1 FOR UPDATE SKIP LOCKED) "
            f"UPDATE {table} j SET status='running',attempts=attempts+1,lease_token=:token,"
            "locked_until=NOW()+INTERVAL '15 minutes',updated_at=NOW() FROM due WHERE j.id=due.id RETURNING j.*",
            {"token": str(uuid4())})
        return rows[0] if rows else None

    async def finish(self, kind: str, row: dict, *, status: str, error: str = "", retry: bool = False) -> bool:
        rows = await execute(
            f"UPDATE {_TABLES[kind]} SET status=:status,error_type=:error,lease_token=NULL,locked_until=NULL,"
            "next_run_at=NOW()+(:delay * INTERVAL '1 second'),updated_at=NOW() "
            "WHERE id=:id AND status='running' AND lease_token=:token AND locked_until>NOW() RETURNING id",
            {"id": row["id"], "token": row["lease_token"], "status": status, "error": error[:96],
             "delay": min(300, 10 * 2 ** row["attempts"]) if retry else 0})
        return bool(rows)

    async def page(self, job: dict, limit: int = 50) -> list[dict]:
        return await execute(
            "SELECT id,message_id,content,sender_wxid,sender_name,is_self_sent,occurred_ts "
            "FROM plugin_wxbot_group_observations WHERE tenant_id=:tid AND session_id=:sid "
            "AND occurred_ts>=:start AND occurred_ts<:end AND id>:cursor ORDER BY id LIMIT :lim",
            {"tid": job["tenant_id"], "sid": job["session_id"], "start": job["start_ts"],
             "end": job["end_ts"], "cursor": job["cursor_id"], "lim": limit})

    async def context(self, job: dict, limit: int = 15) -> list[dict]:
        return list(reversed(await execute(
            "SELECT id,message_id,content,sender_wxid,sender_name,is_self_sent,occurred_ts "
            "FROM plugin_wxbot_group_observations WHERE tenant_id=:tid AND session_id=:sid "
            "AND occurred_ts>=:start AND occurred_ts<:end AND ((:cursor>0 AND id<=:cursor) OR (:cursor=0 AND occurred_ts<:day_start)) ORDER BY id DESC LIMIT :lim",
            {"tid": job["tenant_id"], "sid": job["session_id"], "start": job["start_ts"] - 86400,
             "end": job["end_ts"], "day_start": job["start_ts"], "cursor": job["cursor_id"], "lim": limit})))

    async def evidence(self, tenant_id: str, session_id: str, ids: list[int]) -> list[dict]:
        return await execute(
            "SELECT id,message_id,content,sender_wxid,sender_name,is_self_sent,occurred_ts "
            "FROM plugin_wxbot_group_observations WHERE tenant_id=:tid AND session_id=:sid AND id=ANY(:ids) ORDER BY id",
            {"tid": tenant_id, "sid": session_id, "ids": ids})

    async def blocked_members(self, tenant_id: str, members: list[str]) -> set[str]:
        if not members:
            return set()
        rows = await execute("SELECT user_id FROM social_tenant_member_control WHERE tenant_id=:tid "
                             "AND user_id=ANY(:ids) AND (memory_opt_out OR deletion_state IN ('requested','failed'))",
                             {"tid": tenant_id, "ids": members})
        return {r["user_id"] for r in rows}

    async def runtime_evidence(self, tenant_id: str, session_id: str, ids: list[int]) -> list[dict]:
        return await execute(
            "SELECT o.id,p.trace_id,p.status AS processing_status,p.reason AS processing_reason,"
            "COALESCE((SELECT json_agg(json_build_object('status',q.status,'reply_text',q.reply_text,'error',q.error) ORDER BY q.id) "
            "FROM plugin_wxbot_reply_queue q WHERE q.tenant_id=p.tenant_id AND q.trace_id=p.trace_id "
            "AND p.trace_id<>''), '[]') AS deliveries, "
            "COALESCE((SELECT json_agg(json_build_object('status',s.status,'reasons',s.reason_codes_json,'stage',s.runtime_stage) ORDER BY s.id) "
            "FROM social_participation_event s WHERE s.tenant_id=p.tenant_id AND s.session_id=p.session_id "
            "AND s.trace_id=p.trace_id AND p.trace_id<>'' AND s.event_kind='runtime'), '[]') AS decisions "
            "FROM plugin_wxbot_group_observations o LEFT JOIN processed_messages p ON p.tenant_id=o.tenant_id "
            "AND p.session_id=o.session_id AND p.message_id=o.message_id "
            "WHERE o.tenant_id=:tid AND o.session_id=:sid AND o.id=ANY(:ids) ORDER BY o.id",
            {"tid": tenant_id, "sid": session_id, "ids": ids})

    async def save_page(self, job: dict, page: list[dict], drafts: list[dict], findings: list[dict] | None = None) -> bool:
        findings = findings or []
        async with get_engine().begin() as conn:
            locked = (await conn.execute(text("SELECT id FROM jev_knowledge_job WHERE id=:id "
                "AND status='running' AND lease_token=:token AND locked_until>NOW() FOR UPDATE"),
                {"id": job["id"], "token": job["lease_token"]})).first()
            if not locked:
                return False
            # Lock all contributing members in one deterministic order before any candidate write.
            all_ids = sorted({i for draft in [*drafts, *[f["finding"] for f in findings]] for i in draft["evidence_ids"]})
            evidence_query = text("SELECT id,sender_wxid,is_self_sent FROM plugin_wxbot_group_observations "
                                  "WHERE tenant_id=:tid AND session_id=:sid AND id=ANY(:ids)")
            evidence_params = {"tid": job["tenant_id"], "sid": job["session_id"], "ids": all_ids}
            initial_evidence = (await conn.execute(evidence_query, evidence_params)).mappings().all() if all_ids else []
            all_members = sorted({r["sender_wxid"] for r in initial_evidence if not r["is_self_sent"]})
            for member in all_members:
                await conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                                   {"key": f"memory-member-v1:{job['tenant_id']}:{member}"})
            # Erasure can complete while locks are acquired, so re-read original messages now.
            current_evidence = (await conn.execute(evidence_query, evidence_params)).mappings().all() if all_ids else []
            indexed = {r["id"]: r for r in current_evidence}
            blocked_rows = (await conn.execute(text("SELECT user_id FROM social_tenant_member_control WHERE tenant_id=:tid "
                "AND user_id=ANY(:members) AND (memory_opt_out OR deletion_state IN ('requested','failed'))"),
                {"tid": job["tenant_id"], "members": all_members})).mappings().all() if all_members else []
            blocked = {r["user_id"] for r in blocked_rows}
            inserted = 0
            for draft in drafts:
                if not set(draft["evidence_ids"]).issubset(indexed):
                    continue
                evidence = [indexed[i] for i in draft["evidence_ids"]]
                members = sorted({r["sender_wxid"] for r in evidence if not r["is_self_sent"]})
                if blocked.intersection(members):
                    continue
                row = await conn.execute(text(
                    "INSERT INTO jev_knowledge_candidate (id,job_id,tenant_id,session_id,fingerprint,draft,source_members) "
                    "VALUES (:id,:job,:tid,:sid,:hash,CAST(:draft AS JSON),CAST(:members AS JSON)) ON CONFLICT DO NOTHING RETURNING id"),
                    {"id": str(uuid4()), "job": job["id"], "tid": job["tenant_id"], "sid": job["session_id"],
                     "hash": fingerprint(draft), "draft": json.dumps(draft, ensure_ascii=False), "members": json.dumps(members)})
                inserted += int(row.first() is not None)
            quality_inserted = 0
            for finding in findings:
                value = finding["finding"]
                if not set(value["evidence_ids"]).issubset(indexed):
                    continue
                members = sorted({indexed[i]["sender_wxid"] for i in value["evidence_ids"] if not indexed[i]["is_self_sent"]})
                if blocked.intersection(members):
                    continue
                inserted_finding = await conn.execute(text("INSERT INTO jev_quality_finding "
                    "(id,job_id,tenant_id,session_id,fingerprint,finding,source_members,review,status,reason) "
                    "VALUES (:id,:job,:tid,:sid,:fingerprint,CAST(:finding AS JSON),CAST(:members AS JSON),CAST(:review AS JSON),:status,:reason) "
                    "ON CONFLICT DO NOTHING RETURNING id"), {"id": str(uuid4()), "job": job["id"], "tid": job["tenant_id"],
                    "sid": job["session_id"], "fingerprint": fingerprint([value["kind"], sorted(value["evidence_ids"])]),
                    "finding": json.dumps(value), "members": json.dumps(members), "review": json.dumps(finding["review"]),
                    "status": finding["status"], "reason": finding["reason"]})
                quality_inserted += int(inserted_finding.first() is not None)
            await conn.execute(text("UPDATE jev_knowledge_job SET cursor_id=:cursor,scanned=scanned+:scanned,"
                "candidate_count=candidate_count+:count,quality_count=quality_count+:quality_count,status=:status,attempts=0,lease_token=NULL,locked_until=NULL,"
                "error_type='',updated_at=NOW() WHERE id=:id"),
                {"id": job["id"], "cursor": max([int(r["id"]) for r in page], default=job["cursor_id"]),
                 "scanned": len(page), "count": inserted, "quality_count": quality_inserted, "status": "pending" if page else "completed"})
        return True

    async def save_review(self, row: dict, *, status: str, reason: str, review: dict, comparisons: list[dict], revision: dict | None = None) -> bool:
        revision = revision or {}
        members = sorted(set(row.get("source_members") or []) | set(revision.get("source_members") or []))
        async with get_engine().begin() as conn:
            for member in members:
                await conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                                   {"key": f"memory-member-v1:{row['tenant_id']}:{member}"})
            blocked = (await conn.execute(text("SELECT 1 FROM social_tenant_member_control WHERE tenant_id=:tid "
                "AND user_id=ANY(:members) AND (memory_opt_out OR deletion_state IN ('requested','failed')) LIMIT 1"),
                {"tid": row["tenant_id"], "members": members})).first() if members else None
            if blocked:
                await conn.execute(text("UPDATE jev_knowledge_candidate SET status='skipped',reason='member_policy_changed',"
                    "lease_token=NULL,locked_until=NULL,version=version+1,updated_at=NOW() "
                    "WHERE id=:id AND status='running' AND lease_token=:token AND locked_until>NOW()"),
                    {"id": row["id"], "token": row["lease_token"]})
                return False
            updated = (await conn.execute(text("UPDATE jev_knowledge_candidate SET status=:status,reason=:reason,review=CAST(:review AS JSON),"
                "comparisons=CAST(:comparisons AS JSON),revision=CAST(:revision AS JSON),source_members=CAST(:members AS JSON),version=version+1,lease_token=NULL,locked_until=NULL,"
                "error_type='',updated_at=NOW() WHERE id=:id AND status='running' AND lease_token=:token "
                "AND locked_until>NOW() RETURNING id"), {"id": row["id"], "token": row["lease_token"], "status": status,
                "reason": reason, "review": json.dumps(review), "comparisons": json.dumps(comparisons),
                "revision": json.dumps(revision), "members": json.dumps(members)})).first()
            prior_id = (row.get("draft") or {}).get("resolves_candidate_id")
            if updated and prior_id and status in {"ready", "duplicate", "revision_ready"}:
                await conn.execute(text("UPDATE jev_knowledge_candidate SET status='resolved',reason='followup_resolved',"
                    "version=version+1,review=CAST(CAST(review AS JSONB) || CAST(:link AS JSONB) AS JSON),updated_at=NOW() "
                    "WHERE id=:prior AND tenant_id=:tid AND session_id=:sid AND status='unresolved'"),
                    {"prior": prior_id, "tid": row["tenant_id"], "sid": row["session_id"],
                     "link": json.dumps({"resolution_candidate_id": row["id"]})})
        return bool(updated)

    async def open_candidates(self, tenant_id: str, session_id: str, *, messages: list[dict] | None = None) -> list[dict]:
        # Rank the entire two-week unresolved set, not only the newest questions.
        content = " ".join(str(r.get("content") or "") for r in messages or [])[:20000].lower()
        terms = re.findall(r"[a-z0-9_][a-z0-9_.-]{2,}", content)
        for word in re.findall(r"[\u4e00-\u9fff]+", content):
            terms.extend(word[i:i+2] for i in range(len(word)-1))
        tokens = [token for token, _ in Counter(terms).most_common(100)]
        return await execute("SELECT id,draft FROM jev_knowledge_candidate WHERE tenant_id=:tid AND session_id=:sid "
                             "AND status='unresolved' AND created_at>NOW()-INTERVAL '14 days' "
                             "ORDER BY (SELECT COUNT(*) FROM unnest(CAST(:tokens AS TEXT[])) token "
                             "WHERE strpos(lower(draft->>'question'),token)>0) DESC,created_at DESC LIMIT 8",
                             {"tid": tenant_id, "sid": session_id, "tokens": tokens})

    async def candidate(self, tenant_id: str, session_id: str, candidate_id: str) -> dict | None:
        rows = await execute("SELECT * FROM jev_knowledge_candidate WHERE tenant_id=:tid AND session_id=:sid AND id=:id",
                             {"tid": tenant_id, "sid": session_id, "id": candidate_id})
        return rows[0] if rows else None

    async def knowledge_snapshot(self, tenant_id: str, session_id: str, candidate_id: str = "") -> str:
        rows = await execute(
            "SELECT id,content_hash FROM kb_documents WHERE tenant_id=:tid AND session_id IN ('',:sid) "
            "AND (:candidate='' OR COALESCE(metadata->>'candidate_id','')<>:candidate) ORDER BY id",
            {"tid": tenant_id, "sid": session_id, "candidate": candidate_id})
        return fingerprint(rows)

    async def dashboard(self, tenant_id: str, session_id: str = "", *, job_cursor: str = "",
                        candidate_cursor: str = "", finding_cursor: str = "", candidate_status: str = "",
                        finding_status: str = "", page_size: int = 100) -> dict:
        from app.jev.knowledge_pages import read_page
        page_size = max(1, min(100, page_size))
        jobs, job_next = await read_page("jobs", tenant_id, session_id, cursor=job_cursor, limit=min(50, page_size))
        candidates, candidate_next = await read_page("candidates", tenant_id, session_id,
            status=candidate_status, cursor=candidate_cursor, limit=page_size)
        findings, finding_next = await read_page("findings", tenant_id, session_id,
            status=finding_status, cursor=finding_cursor, limit=page_size)
        quality_summary = await execute("SELECT finding->>'kind' AS kind,status,count(*) AS count FROM jev_quality_finding WHERE "
            "tenant_id=:tid AND (:sid='' OR session_id=:sid) GROUP BY finding->>'kind',status",
            {"tid": tenant_id, "sid": session_id})
        return {"jobs": jobs, "candidates": candidates, "findings": findings, "quality_summary": quality_summary,
            "pagination": {"jobs": job_next, "candidates": candidate_next, "findings": finding_next}}
