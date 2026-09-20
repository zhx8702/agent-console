from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from app.infra.db import get_engine
from app.jev.models import JevPolicy


async def execute(sql: str, params: dict | None = None) -> list[dict]:
    async with get_engine().begin() as conn:
        result = await conn.execute(text(sql), params or {})
        return [dict(row) for row in result.mappings()] if result.returns_rows else []


class JevStore:
    async def policy(self, tenant_id: str, default: JevPolicy) -> tuple[JevPolicy, int]:
        rows = await execute("SELECT policy, version FROM jev_policy WHERE tenant_id=:tid", {"tid": tenant_id})
        if not rows:
            return default, 0
        return JevPolicy.model_validate(rows[0]["policy"]), int(rows[0]["version"])

    async def enqueue(self, *, tenant_id: str, session_id: str, domain: str,
                      fingerprint: str, input_hash: str, target_id: int | None = None,
                      state: dict | None = None, run=execute) -> str | None:
        rows = await run(
            "INSERT INTO jev_evaluation (id,tenant_id,session_id,domain,fingerprint,input_hash,target_id,state) "
            "VALUES (:id,:tid,:sid,:domain,:fingerprint,:input_hash,:target_id,CAST(:state AS JSON)) "
            "ON CONFLICT (tenant_id,domain,fingerprint) DO NOTHING RETURNING id",
            {"id": str(uuid4()), "tid": tenant_id, "sid": session_id, "domain": domain,
             "fingerprint": fingerprint, "input_hash": input_hash, "target_id": target_id,
             "state": json.dumps(state or {}, ensure_ascii=False)},
        )
        return str(rows[0]["id"]) if rows else None

    async def claim(self, *, limit: int, lease_seconds: int, max_attempts: int) -> list[dict]:
        # Terminalize workers that repeatedly crashed after claiming a job.
        await execute("UPDATE jev_evaluation SET status='failed', error_type='LeaseExpired', "
                      "lease_token=NULL, locked_until=NULL, updated_at=NOW() "
                      "WHERE status='running' AND locked_until<NOW() AND attempts>=:attempts",
                      {"attempts": max_attempts})
        return await execute(
            "WITH due AS (SELECT id FROM jev_evaluation WHERE "
            "((status='pending' AND next_run_at<=NOW()) OR (status='running' AND locked_until<NOW())) "
            "AND attempts<:attempts ORDER BY next_run_at,created_at LIMIT :limit FOR UPDATE SKIP LOCKED) "
            "UPDATE jev_evaluation j SET status='running',attempts=attempts+1,lease_token=:token,"
            "locked_until=NOW()+(:lease * INTERVAL '1 second'),updated_at=NOW() FROM due "
            "WHERE j.id=due.id RETURNING j.*",
            {"limit": limit, "attempts": max_attempts, "token": str(uuid4()), "lease": lease_seconds},
        )

    async def finish(self, job: dict, *, status: str, result: dict | None = None,
                     error: str = "", applied: bool = False, duration_ms: int = 0,
                     retry_seconds: int = 0, run=execute) -> bool:
        usage = (result or {}).get("usage") or {}
        rows = await run(
            "UPDATE jev_evaluation SET status=:status,result=CAST(:result AS JSON),error_type=:error,"
            "duration_ms=duration_ms+:duration,input_tokens=input_tokens+:input,output_tokens=output_tokens+:output,"
            "applied=:applied,lease_token=NULL,locked_until=NULL,updated_at=NOW(),"
            "next_run_at=NOW()+(:retry * INTERVAL '1 second'),state=CASE WHEN :terminal THEN '{}'::json ELSE state END "
            "WHERE id=:id AND lease_token=:token AND status='running' AND locked_until>NOW() RETURNING id",
            {"status": status, "result": json.dumps(result), "error": error[:96], "duration": duration_ms,
             "input": max(0, int(usage.get("input_tokens") or 0)), "output": max(0, int(usage.get("output_tokens") or 0)),
             "applied": applied, "retry": retry_seconds, "id": job["id"], "token": job["lease_token"],
             "terminal": status in {"completed", "skipped"}},
        )
        return bool(rows)

    async def record_online(self, *, tenant_id: str, session_id: str, domain: str,
                            key: str, result: dict | None, duration_ms: int,
                            error: str = "", applied: bool = False) -> None:
        usage = (result or {}).get("usage") or {}
        await execute(
            "INSERT INTO jev_evaluation (id,tenant_id,session_id,domain,fingerprint,input_hash,state,"
            "status,attempts,result,error_type,duration_ms,input_tokens,output_tokens,applied) "
            "VALUES (:id,:tid,:sid,:domain,:key,:key,'{}',:status,1,CAST(:result AS JSON),:error,:duration,:input,:output,:applied) "
            "ON CONFLICT (tenant_id,domain,fingerprint) DO NOTHING",
            {"id": str(uuid4()), "tid": tenant_id, "sid": session_id, "domain": domain, "key": key,
             "status": "failed" if error else "completed", "result": json.dumps(result),
             "error": error[:96], "duration": duration_ms, "input": int(usage.get("input_tokens") or 0),
             "output": int(usage.get("output_tokens") or 0), "applied": applied},
        )

    async def dashboard(self, tenant_id: str, *, domain: str = "", status: str = "", limit: int = 50) -> dict:
        where = "tenant_id=:tid AND (:domain='' OR domain=:domain)"
        params: dict[str, Any] = {"tid": tenant_id, "domain": domain, "status": status, "limit": limit}
        summary = await execute(
            "SELECT domain,status,count(*) AS count,sum(attempts) AS attempts,sum(input_tokens) AS input_tokens,"
            "sum(output_tokens) AS output_tokens,sum(duration_ms) AS total_duration_ms,"
            "count(*) FILTER (WHERE applied) AS applied FROM jev_evaluation WHERE " + where + " GROUP BY domain,status", params)
        items = await execute(
            "SELECT id,domain,target_id,status,attempts,result,error_type,duration_ms,input_tokens,output_tokens,"
            "applied,created_at,updated_at,(status='failed' AND (target_id IS NOT NULL OR state::jsonb<>'{}'::jsonb)) AS retryable FROM jev_evaluation WHERE " + where + " AND (:status='' OR status=:status) "
            "ORDER BY CASE WHEN jsonb_typeof(result::jsonb #> '{answers,priority,score}')='number' THEN (result::jsonb #>> '{answers,priority,score}')::float ELSE 0 END DESC, created_at DESC LIMIT :limit", params)
        return {"summary": summary, "items": items}


    async def memory_changes(self, since, item_id: int) -> list[dict]:
        return await execute(
            "SELECT id,updated_at FROM plugin_memory_item WHERE deleted_at IS NULL "
            "AND source_type NOT IN ('manual','explicit_user') AND (updated_at,id)>(:since,:id) "
            "ORDER BY updated_at,id LIMIT 100", {"since": since, "id": item_id})

    async def maintain(self) -> None:
        # Pending source text is short-lived; terminal audits retain no message text.
        await execute("UPDATE jev_evaluation SET status='skipped',state='{}',error_type='Expired',updated_at=NOW() "
                      "WHERE status IN ('pending','failed') AND created_at<NOW()-INTERVAL '7 days'")
        await execute("DELETE FROM jev_evaluation WHERE status IN ('completed','skipped','failed') "
                      "AND updated_at<NOW()-INTERVAL '30 days'")
