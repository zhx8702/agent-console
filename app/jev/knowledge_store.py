"""PostgreSQL queue, progress and immutable candidate evidence references."""
from __future__ import annotations

import json
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

    async def save_page(self, job: dict, page: list[dict], drafts: list[dict]) -> bool:
        async with get_engine().begin() as conn:
            locked = (await conn.execute(text("SELECT id FROM jev_knowledge_job WHERE id=:id "
                "AND status='running' AND lease_token=:token AND locked_until>NOW() FOR UPDATE"),
                {"id": job["id"], "token": job["lease_token"]})).first()
            if not locked:
                return False
            # Lock all contributing members in one deterministic order before any candidate write.
            all_ids = sorted({i for draft in drafts for i in draft["evidence_ids"]})
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
            await conn.execute(text("UPDATE jev_knowledge_job SET cursor_id=:cursor,scanned=scanned+:scanned,"
                "candidate_count=candidate_count+:count,status=:status,attempts=0,lease_token=NULL,locked_until=NULL,"
                "error_type='',updated_at=NOW() WHERE id=:id"),
                {"id": job["id"], "cursor": max([int(r["id"]) for r in page], default=job["cursor_id"]),
                 "scanned": len(page), "count": inserted, "status": "pending" if page else "completed"})
        return True

    async def save_review(self, row: dict, *, status: str, reason: str, review: dict, comparisons: list[dict]) -> bool:
        async with get_engine().begin() as conn:
            updated = (await conn.execute(text("UPDATE jev_knowledge_candidate SET status=:status,reason=:reason,review=CAST(:review AS JSON),"
                "comparisons=CAST(:comparisons AS JSON),version=version+1,lease_token=NULL,locked_until=NULL,"
                "error_type='',updated_at=NOW() WHERE id=:id AND status='running' AND lease_token=:token "
                "AND locked_until>NOW() RETURNING id"), {"id": row["id"], "token": row["lease_token"], "status": status,
                "reason": reason, "review": json.dumps(review), "comparisons": json.dumps(comparisons)})).first()
            prior_id = (row.get("draft") or {}).get("resolves_candidate_id")
            if updated and prior_id and status in {"ready", "duplicate"}:
                await conn.execute(text("UPDATE jev_knowledge_candidate SET status='resolved',reason='followup_resolved',"
                    "version=version+1,review=CAST(CAST(review AS JSONB) || CAST(:link AS JSONB) AS JSON),updated_at=NOW() "
                    "WHERE id=:prior AND tenant_id=:tid AND session_id=:sid AND status='unresolved'"),
                    {"prior": prior_id, "tid": row["tenant_id"], "sid": row["session_id"],
                     "link": json.dumps({"resolution_candidate_id": row["id"]})})
        return bool(updated)

    async def open_candidates(self, tenant_id: str, session_id: str) -> list[dict]:
        return await execute("SELECT id,draft FROM jev_knowledge_candidate WHERE tenant_id=:tid AND session_id=:sid "
                             "AND status='unresolved' AND created_at>NOW()-INTERVAL '14 days' ORDER BY created_at DESC LIMIT 5",
                             {"tid": tenant_id, "sid": session_id})

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

    async def dashboard(self, tenant_id: str, session_id: str = "") -> dict:
        params = {"tid": tenant_id, "sid": session_id}
        where = "tenant_id=:tid AND (:sid='' OR session_id=:sid)"
        jobs = await execute("SELECT * FROM jev_knowledge_job WHERE " + where + " ORDER BY period DESC,created_at DESC LIMIT 50", params)
        candidates = await execute("SELECT * FROM jev_knowledge_candidate WHERE " + where + " ORDER BY created_at DESC LIMIT 100", params)
        for row in [*jobs, *candidates]:
            row.pop("lease_token", None)
        return {"jobs": jobs, "candidates": candidates}
