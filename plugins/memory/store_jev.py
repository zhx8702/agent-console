"""Durable post-persistence Jev reviews for window and legacy memory paths."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.common.logging import get_logger
from app.jev.models import answer, confidence, memory_fingerprint, probability, redact
from plugins.memory import store as runtime

log = get_logger(__name__)


class MemoryJevStoreMixin:
    async def _queue_jev_item(self, item: dict | None) -> dict | None:
        service = getattr(self, "jev_service", None)
        if not item or service is None or not service.enabled:
            return item
        try:
            # A queue outage cannot poison the surrounding memory transaction.
            conn = runtime._ACTIVE_MUTATION_CONNECTION.get()
            if conn is not None:
                async with conn.begin_nested():
                    item["_jev_queued"] = await service.enqueue_memory(item, run=runtime._exec)
            else:
                item["_jev_queued"] = await service.enqueue_memory(item, run=runtime._exec)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("jev.memory_enqueue_failed", error_type=type(exc).__name__)
        return item

    async def _jev_current(self, job: dict, *, lock: bool = False) -> dict | None:
        item = await self.get_memory_item(int(job["target_id"]), for_update=lock)
        if not item or item.get("tenant_id") != job["tenant_id"] or item.get("deleted_at"):
            return None
        if item.get("status") in {"deleted", "invalidated", "expired"}:
            return None
        expires = runtime._coerce_datetime(item.get("expires_at"))
        if expires and expires <= datetime.now(UTC).replace(tzinfo=None):
            return None
        if memory_fingerprint(item) != job["input_hash"]:
            return None
        if not await self.jev_service.allowed(item["tenant_id"], item.get("session_id") or "", job["domain"]):
            return None
        member = str(item.get("user_id") or "")
        if member and member != runtime.GROUP_HISTORY_USER_ID_SCOPE:
            if await self._member_memory_write_blocked(tenant_id=item["tenant_id"], user_id=member):
                return None
        return item

    async def jev_state(self, job: dict) -> dict | None:
        item = await self._jev_current(job)
        if item is None:
            return None
        value = item.get("value") or {}
        relation = value.get("relation") or {}
        event_ids = runtime._memory_evidence_event_ids(value, source_event_id=item.get("source_event_id"))[:8]
        observation_ids = sorted(runtime._coerce_int_set(
            relation.get("evidence_observation_ids") or value.get("source_observation_ids") or []))[:8]
        params = {"tid": item["tenant_id"], "sid": item.get("session_id") or "",
                  "channel": item.get("channel"), "source_key": item.get("source_key")}
        evidence = []
        if event_ids:
            rows = await runtime._exec(
                "SELECT id, user_text, source_member_id FROM plugin_memory_event "
                "WHERE tenant_id=:tid AND channel=:channel AND source_key=:source_key "
                "AND (:sid='' OR session_id=:sid) AND id=ANY(:ids)", {**params, "ids": event_ids})
            if len(rows) != len(event_ids):
                return None
            for row in rows:
                evidence.append({"text": row["user_text"], "member": row.get("source_member_id") or "", "name": ""})
        if observation_ids:
            sessions = await self._resolve_group_graph_session_ids(tenant_id=item["tenant_id"], session_id=item["session_id"])
            rows = await runtime._exec(
                "SELECT id, content, sender_wxid, sender_name FROM plugin_wxbot_group_observations "
                "WHERE tenant_id=:tid AND session_id=ANY(:sids) AND id=ANY(:ids) AND COALESCE(is_self_sent,FALSE)=FALSE",
                {"tid": item["tenant_id"], "sids": sessions or [item["session_id"]], "ids": observation_ids})
            if len(rows) != len(observation_ids):
                return None
            for row in rows:
                evidence.append({"text": row["content"], "member": row["sender_wxid"], "name": row.get("sender_name") or ""})
        identities: dict[str, str] = {}
        for field in ("subject", "object"):
            name = str(relation.get(field) or "")
            if name and relation.get(field + "_type", "person") == "person":
                identities[name] = "person_" + str(len(identities) + 1)
        member = str(item.get("user_id") or "")
        if member and member != runtime.GROUP_HISTORY_USER_ID_SCOPE:
            identities.setdefault(member, "speaker")
        for row in evidence:
            member = str(row["member"])
            if member:
                if await self._member_memory_write_blocked(tenant_id=item["tenant_id"], user_id=member):
                    return None
                token = identities.setdefault(member, "person_" + str(len(identities) + 1))
                if len(str(row["name"])) >= 2:
                    identities[str(row["name"])] = token
        snippets = [{"speaker": identities.get(str(row["member"]), "unknown"),
                     "text": redact(row["text"], identities, 600)} for row in evidence[:8]]
        if not snippets and item.get("original_text"):
            snippets = [{"speaker": "speaker", "text": redact(item["original_text"], identities, 1200)}]
        return {"kind": job["domain"], "candidate": redact(item.get("content"), identities, 600),
                "predicate": str(relation.get("predicate") or ""),
                "signals": relation.get("signals") or {}, "evidence": snippets,
                "evidence_count": len(event_ids) + len(observation_ids),
                "sensitivity": item.get("sensitivity", "normal")}

    async def apply_jev_result(self, job: dict, result: dict, *, policy, duration_ms: int) -> bool:
        service = self.jev_service
        async with self._mutation_transaction():
            claimed = await runtime._exec(
                "SELECT id FROM jev_evaluation WHERE id=:id AND lease_token=:token "
                "AND status='running' AND locked_until>NOW() FOR UPDATE",
                {"id": job["id"], "token": job["lease_token"]})
            if not claimed:
                return False
            item = await self._jev_current(job, lock=True)
            if item is None or await self.jev_state(job) is None:
                await service.store.finish(job, status="skipped", result=result, error="stale_or_protected",
                                           duration_ms=duration_ms, run=runtime._exec)
                return False
            value = dict(item.get("value") or {})
            audit = {"status": "completed", "evaluation_id": job["id"], "model": result.get("model"),
                     "usage": result.get("usage"), **(result.get("answers") or {})}
            value["jev"] = audit
            if isinstance(value.get("relation"), dict):
                value["relation"] = {**value["relation"], "typesafe_shadow": audit}
            previous = value.get("acceptance") or {}
            actor = str(previous.get("reviewed_by") or "")
            human_reviewed = bool(actor and actor not in {"system/auto", "system/jev", "system/auto-review"})
            can_apply = (policy.enabled and getattr(policy, job["domain"]) and not policy.shadow_only
                         and confidence(result) >= policy.min_confidence
                         and not human_reviewed and not item.get("pinned")
                         and item.get("source_type") not in {"manual", "explicit_user"})
            decision = answer(result, "decision")
            if decision not in {"accepted", "needs_review", "rejected"}:
                can_apply = False
            # Neither an evaluator nor a high score can clear the sensitivity gate.
            if decision == "accepted" and (item.get("sensitivity") != "normal" or
                    probability(result, "sensitive", fallback=1) >= 0.5 or
                    probability(result, "supported", fallback=0) < max(0.5, policy.min_confidence)):
                decision = "needs_review"
            audit["applied"] = can_apply
            await self.update_memory_item(int(item["id"]), value_json=value)
            if can_apply:
                action = {"accepted": "accept", "rejected": "reject", "needs_review": "needs_review"}[decision]
                await self.review_memory_item_acceptance(int(item["id"]), action=action,
                    reviewed_by="system/jev", review_reason="jev:" + str(result.get("model") or "unknown"))
            current = await self.get_memory_item(int(item["id"]))
            current_value = dict(current.get("value") or {})
            current_value["jev"] = {**current_value["jev"], "content_hash": memory_fingerprint(current)}
            await runtime._exec("UPDATE plugin_memory_item SET value_json=:value WHERE id=:id",
                                {"value": runtime._to_json(current_value), "id": item["id"]})
            await service.store.finish(job, status="completed", result=result, applied=can_apply,
                                       duration_ms=duration_ms, run=runtime._exec)
        return can_apply
