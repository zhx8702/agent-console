from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from app.channel.session_aliases import session_policy_aliases
from app.infra.db import get_engine
from app.jev.desk import attach_turns, funnel_from_rows
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

    async def dashboard(self, tenant_id: str, *, domain: str = "", status: str = "", session_id: str = "", limit: int = 50) -> dict:
        where = "tenant_id=:tid AND (:domain='' OR domain=:domain) AND (:sid='' OR session_id=:sid)"
        params: dict[str, Any] = {"tid": tenant_id, "domain": domain, "status": status, "sid": session_id, "limit": limit}
        summary = await execute(
            "SELECT domain,status,count(*) AS count,sum(attempts) AS attempts,sum(input_tokens) AS input_tokens,"
            "sum(output_tokens) AS output_tokens,sum(duration_ms) AS total_duration_ms,"
            "count(*) FILTER (WHERE applied) AS applied FROM jev_evaluation WHERE " + where + " GROUP BY domain,status", params)
        items = await execute(
            "SELECT id,session_id,domain,target_id,status,attempts,result,error_type,duration_ms,input_tokens,output_tokens,"
            "applied,created_at,updated_at,(status='failed' AND (target_id IS NOT NULL OR state::jsonb<>'{}'::jsonb)) AS retryable FROM jev_evaluation WHERE " + where + " AND (:status='' OR status=:status) "
            "ORDER BY CASE WHEN jsonb_typeof(result::jsonb #> '{answers,priority,score}')='number' THEN (result::jsonb #>> '{answers,priority,score}')::float ELSE 0 END DESC, created_at DESC LIMIT :limit", params)
        participation = await execute(
            "SELECT status,applied,error_type,result FROM jev_evaluation WHERE tenant_id=:tid AND domain='participation' "
            "AND (:sid='' OR session_id=:sid)", {"tid": tenant_id, "sid": session_id})
        aliases = await session_policy_aliases(tenant_id, session_id) if session_id else []
        channel = await self.channel_overlay(tenant_id, session_id, aliases=aliases) if session_id else None
        social = await self.social_overlay(tenant_id, aliases) if session_id else None
        attach_turns(
            items,
            await self.participation_runtime(tenant_id, items),
            keywords=list((channel or {}).get("keywords") or []),
            reply_mode=str((channel or {}).get("reply_mode") or ""),
        )
        return {"summary": summary, "items": items, "funnel": funnel_from_rows(participation),
                "channel": channel, "aliases": aliases, "social": social}

    async def desk_overlay(self, tenant_id: str, session_id: str) -> tuple[list[str], dict | None, dict | None]:
        aliases = await session_policy_aliases(tenant_id, session_id)
        channel = await self.channel_overlay(tenant_id, session_id, aliases=aliases)
        social = await self.social_overlay(tenant_id, aliases)
        return aliases, channel, social

    async def social_overlay(self, tenant_id: str, aliases: list[str]) -> dict:
        for sid in aliases:
            rows = await execute(
                "SELECT group_enabled, global_enabled, tenant_enabled, policy_json "
                "FROM social_group_policy WHERE tenant_id=:tid AND session_id=:sid",
                {"tid": tenant_id, "sid": sid},
            )
            if not rows:
                continue
            policy = rows[0].get("policy_json") or {}
            if isinstance(policy, str):
                policy = json.loads(policy)
            if not isinstance(policy, dict):
                policy = {}
            return {
                "effective_enabled": bool(
                    rows[0].get("global_enabled") and rows[0].get("tenant_enabled") and rows[0].get("group_enabled")
                ),
                "group_enabled": bool(rows[0].get("group_enabled")),
                "proactive_enabled": bool(policy.get("proactive_enabled")),
            }
        return {"effective_enabled": True, "group_enabled": True, "proactive_enabled": False}

    async def channel_overlay(self, tenant_id: str, session_id: str, *, aliases: list[str] | None = None) -> dict:
        from plugins.wxbot.store import (
            _GLOBAL_POLICY_COLUMNS,
            _SESSION_POLICY_COLUMNS,
            _normalize_global_policy,
            _session_policy_document,
        )
        global_rows = await execute(
            f"SELECT {_GLOBAL_POLICY_COLUMNS} FROM plugin_wxbot_tenant_policy WHERE tenant_id=:tid",
            {"tid": tenant_id})
        lookup_ids = aliases or [session_id]
        session_rows: list[dict] = []
        matched_session_id = session_id
        for sid in lookup_ids:
            session_rows = await execute(
                f"SELECT {_SESSION_POLICY_COLUMNS} FROM plugin_wxbot_session_policy "
                "WHERE tenant_id=:tid AND session_id=:sid",
                {"tid": tenant_id, "sid": sid})
            if session_rows:
                matched_session_id = sid
                break
        document = _session_policy_document(
            session_rows[0] if session_rows else None,
            tenant_id,
            matched_session_id,
            _normalize_global_policy(global_rows[0] if global_rows else None, tenant_id),
        )
        keywords = [str(item) for item in (document.get("trigger_keywords") or []) if str(item).strip()]
        return {
            "reply_mode": str(document.get("effective_mode") or "off"),
            "configured_reply_mode": str(document.get("reply_mode") or "inherit"),
            "inherits_global_keywords": bool(document.get("inherits_global_keywords")),
            "keyword_count": len(keywords),
            "keywords": keywords,
            "mention_sender": bool(document.get("effective_mention_sender")),
            "has_session_row": bool(session_rows),
        }

    async def participation_runtime(self, tenant_id: str, items: list[dict]) -> dict[str, dict]:
        traces = []
        for item in items:
            if item.get("domain") != "participation":
                continue
            result = item.get("result") if isinstance(item.get("result"), dict) else {}
            audit = result.get("_audit") if isinstance(result.get("_audit"), dict) else {}
            trace = str(audit.get("trace_id") or "").strip()
            if trace:
                traces.append(trace)
        if not traces:
            return {}
        rows = await execute(
            "SELECT p.trace_id,p.status AS processing_status,p.reason AS processing_reason,p.message_id,"
            "o.content,o.is_self_sent,"
            "COALESCE(c.memory_opt_out,FALSE) OR COALESCE(c.deletion_state,'none') IN ('requested','failed') AS hidden, "
            "COALESCE((SELECT json_agg(json_build_object('status',q.status) ORDER BY q.id) "
            "FROM plugin_wxbot_reply_queue q WHERE q.tenant_id=p.tenant_id AND q.trace_id=p.trace_id "
            "AND p.trace_id<>''), '[]') AS deliveries, "
            "COALESCE((SELECT json_agg(json_build_object('status',s.status,'reasons',s.reason_codes_json) ORDER BY s.id) "
            "FROM social_participation_event s WHERE s.tenant_id=p.tenant_id AND s.session_id=p.session_id "
            "AND s.trace_id=p.trace_id AND p.trace_id<>'' AND s.event_kind='runtime'), '[]') AS decisions "
            "FROM processed_messages p "
            "LEFT JOIN plugin_wxbot_group_observations o ON o.tenant_id=p.tenant_id "
            "AND o.session_id=p.session_id AND o.message_id=p.message_id "
            "LEFT JOIN social_tenant_member_control c ON c.tenant_id=o.tenant_id AND c.user_id=o.sender_wxid "
            "WHERE p.tenant_id=:tid AND p.trace_id=ANY(:traces)",
            {"tid": tenant_id, "traces": traces})
        indexed: dict[str, dict] = {}
        for row in rows:
            trace = str(row.get("trace_id") or "")
            if trace and trace not in indexed:
                indexed[trace] = row
        return indexed

    async def recent_participation_messages(self, tenant_id: str, session_id: str) -> list[dict]:
        return await execute(
            "SELECT o.id,o.message_id,o.sender_wxid,o.sender_name,o.content,o.is_self_sent,o.occurred_ts "
            "FROM plugin_wxbot_group_observations o LEFT JOIN social_tenant_member_control c "
            "ON c.tenant_id=o.tenant_id AND c.user_id=o.sender_wxid "
            "WHERE o.tenant_id=:tid AND o.session_id=:sid "
            "AND o.occurred_ts>=EXTRACT(EPOCH FROM NOW())-1200 "
            "AND NOT COALESCE(c.memory_opt_out,FALSE) AND COALESCE(c.deletion_state,'none') NOT IN ('requested','failed') "
            "ORDER BY o.id DESC LIMIT 8", {"tid": tenant_id, "sid": session_id})

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
