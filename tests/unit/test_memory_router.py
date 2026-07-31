from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI

from app.admin.authorization import AdminRole, Principal, build_admin_authorization_dependency
from app.admin.mutation_ledger import MutationIdempotencyConflictError, MutationOutcome
from app.common.config import Settings
from plugins.memory.router import build_memory_router
from plugins.memory.store import MemoryMutationError, MemoryProfileConflictError


class _FakeStore:
    def __init__(self) -> None:
        self.saved_identity: dict[str, object] | None = None
        self.saved_session: dict[str, object] | None = None
        self.saved_item: dict[str, object] | None = None
        self.updated_item: tuple[int, dict[str, object]] | None = None
        self.deleted_item: tuple[int, bool] | None = None
        self.forgotten: dict[str, object] | None = None
        self.backfill_calls: list[dict[str, object]] = []
        self.rebuild_calls: list[dict[str, object]] = []
        self.job_list_calls: list[dict[str, object]] = []
        self.job_stats_calls: list[dict[str, object]] = []
        self.job_maintenance_calls: list[dict[str, object]] = []
        self.acceptance_review_calls: list[dict[str, object]] = []
        self.acceptance_stats_calls: list[dict[str, object]] = []
        self.acceptance_audit_calls: list[dict[str, object]] = []
        self.acceptance_backfill_calls: list[dict[str, object]] = []
        self.group_graph_calls: list[dict[str, object]] = []
        self.daily_relationship_calls: list[dict[str, object]] = []
        self.window_relationship_calls: list[dict[str, object]] = []
        self.window_catchup_calls: list[dict[str, object]] = []
        self.window_stats_calls: list[dict[str, object]] = []
        self.edge_review_calls: list[dict[str, object]] = []
        self.profile_candidate_create_calls: list[dict[str, object]] = []
        self.profile_candidate_list_calls: list[dict[str, object]] = []
        self.profile_candidate_review_calls: list[dict[str, object]] = []
        self.history_date_calls: list[dict[str, object]] = []
        self.retrieved: dict[str, object] | None = None
        self.settings = type(
            "_Settings",
            (),
            {
                "admin_bearer_token": "admin_token",
                "admin_principal_tokens_json": "",
                "wxbot_default_tenant_id": "demo",
                "memory_llm_extraction_enabled": False,
                "memory_llm_extraction_job_enabled": True,
                "memory_llm_extraction_job_drain_enabled": False,
                "memory_retrieval_enabled": True,
                "memory_group_identity_memory_enabled": False,
                "memory_hybrid_retrieval_enabled": False,
                "memory_vector_index_enabled": False,
                "memory_graph_retrieval_enabled": False,
                "memory_graph_llm_extraction_enabled": False,
                "memory_governance_auto_cleanup_enabled": True,
                "memory_needs_review_retention_days": 30,
                "memory_rejected_retention_days": 7,
                "memory_auto_expire_days": 180,
                "memory_governance_batch_size": 500,
            },
        )()

    async def list_profiles(self, **kwargs):
        return [{
            "tenant_id": kwargs["tenant_id"],
            "channel": kwargs.get("channel") or "wechat",
            "source_key": kwargs.get("source_key") or "wxbot",
            "user_id": kwargs.get("user_id") or "wxid_a",
            "message_count": 8,
            "imported_message_count": 5,
        }]

    async def get_identity_profile(self, **kwargs):
        return {
            "tenant_id": kwargs["tenant_id"],
            "channel": kwargs["channel"],
            "source_key": kwargs["source_key"],
            "user_id": kwargs["user_id"],
            "long_term_memory": "已知用户事实与偏好：\n- 偏好微信联系",
            "manual_notes": "VIP",
            "message_count": 8,
            "imported_message_count": 5,
        }

    async def upsert_identity_profile(self, **kwargs):
        self.saved_identity = kwargs
        return {
            "tenant_id": kwargs["tenant_id"],
            "channel": kwargs["channel"],
            "source_key": kwargs["source_key"],
            "user_id": kwargs["user_id"],
            "long_term_memory": kwargs.get("long_term_memory") or "",
            "manual_notes": kwargs.get("manual_notes") or "",
        }

    async def list_session_profiles(self, **kwargs):
        return [{
            "tenant_id": kwargs["tenant_id"],
            "channel": kwargs.get("channel") or "wechat",
            "source_key": kwargs.get("source_key") or "wxbot",
            "session_id": kwargs.get("session_id") or "group-1@chatroom",
            "user_id": kwargs.get("user_id") or "wxid_a",
            "short_term_memory": "用户最近说：查物流",
            "message_count": 2,
            "imported_message_count": 1,
        }]

    async def get_session_profile(self, **kwargs):
        return {
            "tenant_id": kwargs["tenant_id"],
            "channel": kwargs["channel"],
            "source_key": kwargs["source_key"],
            "session_id": kwargs["session_id"],
            "user_id": kwargs["user_id"],
            "short_term_memory": "用户最近说：查物流",
            "manual_notes": "该群里关注物流",
            "message_count": 2,
            "imported_message_count": 1,
        }

    async def upsert_session_profile(self, **kwargs):
        self.saved_session = kwargs
        return {
            "tenant_id": kwargs["tenant_id"],
            "channel": kwargs["channel"],
            "source_key": kwargs["source_key"],
            "session_id": kwargs["session_id"],
            "user_id": kwargs["user_id"],
            "short_term_memory": kwargs.get("short_term_memory") or "",
            "manual_notes": kwargs.get("manual_notes") or "",
        }

    async def get_runtime_profile(self, **kwargs):
        return {
            "tenant_id": kwargs["tenant_id"],
            "channel": kwargs["channel"],
            "source_key": kwargs["source_key"],
            "session_id": kwargs["session_id"],
            "user_id": kwargs["user_id"],
            "short_term_memory": "用户最近说：查物流",
            "long_term_memory": "已知用户事实与偏好：\n- 偏好微信联系",
            "manual_notes": "全局记忆备注：\nVIP\n\n当前会话备注：\n该群里关注物流",
            "identity_manual_notes": "VIP",
            "session_manual_notes": "该群里关注物流",
            "message_count": 8,
            "identity_message_count": 8,
            "session_message_count": 2,
        }

    async def list_events(self, **kwargs):
        return [{
            "id": 1,
            "tenant_id": kwargs["tenant_id"],
            "channel": kwargs.get("channel") or "wechat",
            "source_key": kwargs.get("source_key") or "wxbot",
            "user_id": kwargs.get("user_id") or "wxid_a",
            "session_id": kwargs.get("session_id") or "group-1@chatroom",
            "user_text": "查物流",
            "assistant_text": "好的",
            "raw_text": "raw private event text",
        }]

    async def get_group_relationship_graph(self, **kwargs):
        self.group_graph_calls.append(kwargs)
        return {
            "scope": {
                "tenant_id": kwargs.get("tenant_id"),
                "channel": kwargs.get("channel"),
                "source_key": kwargs.get("source_key"),
                "session_id": kwargs.get("session_id"),
            },
            "filters": {
                "tenant_id": kwargs.get("tenant_id"),
                "channel": kwargs.get("channel"),
                "source_key": kwargs.get("source_key"),
                "session_id": kwargs.get("session_id"),
                "from": kwargs.get("from_"),
                "to": kwargs.get("to"),
                "node_type": kwargs.get("node_type"),
                "edge_type": kwargs.get("edge_type"),
                "acceptance_status": kwargs.get("acceptance_status"),
                "min_confidence": kwargs.get("min_confidence"),
                "limit": kwargs.get("limit"),
            },
            "schema": {"version": "legacy"},
            "nodes": [{
                "id": "entity:11",
                "type": "person",
                "label": "Alice",
                "status": "active",
                "acceptance_status": "accepted",
                "confidence": 0.9,
                "evidence_count": 1,
                "first_seen": "2026-05-01T00:00:00",
                "last_seen": "2026-05-02T00:00:00",
                "source_ref_count": 1,
                "original_text": "private profile text",
            }],
            "edges": [{
                "id": "fact:12",
                "source": "entity:11",
                "target": "entity:12",
                "type": "knows",
                "label": "knows",
                "confidence": 0.8,
                "acceptance_status": "accepted",
                "evidence_count": 1,
                "first_seen": "2026-05-01T00:00:00",
                "last_seen": "2026-05-02T00:00:00",
                "source_event_ids": [101],
                "memory_item_ids": [201],
                "extraction_method": "auto",
                "content": "private raw relation evidence",
            }],
            "counts": {"nodes": 1, "edges": 1},
            "generated_from": [
                "plugin_memory_entity",
                "plugin_memory_fact",
                "plugin_memory_episode",
            ],
        }

    async def get_group_relationship_edge_evidence(self, **kwargs):
        return {
            "schema": {"version": "legacy"},
            "edge": {
                "id": kwargs["edge_id"],
                "type": "knows",
                "content": "private raw relation evidence",
            },
            "evidence_ids": {
                "memory_item_ids": [201],
                "event_ids": [101],
                "episode_ids": [301],
            },
            "evidence_counts": {"memory_items": 1, "events": 1, "episodes": 1},
            "memory_items": [{
                "id": 201,
                "tenant_id": kwargs["tenant_id"],
                "channel": kwargs.get("channel") or "wechat",
                "source_key": kwargs.get("source_key") or "wxbot",
                "user_id": "wxid_a",
                "session_id": kwargs.get("session_id") or "group-1@chatroom",
                "content": "private raw memory content",
                "original_text": "private original text",
                "acceptance_status": "accepted",
            }],
            "events": [{
                "id": 101,
                "tenant_id": kwargs["tenant_id"],
                "channel": kwargs.get("channel") or "wechat",
                "source_key": kwargs.get("source_key") or "wxbot",
                "user_id": "wxid_a",
                "session_id": kwargs.get("session_id") or "group-1@chatroom",
                "user_text": "private user text",
                "assistant_text": "private assistant text",
            }],
            "episodes": [{
                "id": 301,
                "event_ids": [101],
                "memory_item_ids": [201],
                "summary": "private episode summary",
            }],
            **(
                {
                    "raw": {
                        "fact": {"object_value": "private raw graph value"},
                        "memory_items": [{"content": "private raw memory content"}],
                        "events": [{"user_text": "private user text"}],
                        "episodes": [{"summary": "private episode summary"}],
                    }
                }
                if kwargs.get("include_raw")
                else {}
            ),
        }

    async def run_daily_group_relationship_extraction(self, **kwargs):
        self.daily_relationship_calls.append(kwargs)
        return {
            "ok": True,
            "status": "rule_only",
            "result_status": "rule_only",
            "skipped_reason": "no_llm",
            "run_key": "group-rel-daily:test",
            "idempotency_key": "group-rel-daily:test",
            "scope": {
                "tenant_id": kwargs["tenant_id"],
                "channel": kwargs["channel"],
                "source_key": kwargs["source_key"],
                "session_id": kwargs["session_id"],
                "user_id": kwargs.get("user_id") or "__group__",
                "user_id_scope": kwargs.get("user_id") or "__group__",
                "user_id_auto": not bool(kwargs.get("user_id")),
            },
            "date": kwargs["date"],
            "window": {"start": f"{kwargs['date']}T00:00:00", "end": f"{kwargs['date']}T00:00:00"},
            "counts": {
                "raw_messages": 3,
                "imported_messages": 2,
                "senders": 2,
                "source_events": 2,
                "created": 1,
                "updated": 0,
                "memory_items": 1,
                "facts": 0,
                "episodes": 0,
                "jobs": 0,
            },
            "job_counts_before": {"pending": 1, "running": 0, "succeeded": 2, "failed": 0, "dead": 0},
            "job_counts_after": {"pending": 0, "running": 0, "succeeded": 3, "failed": 0, "dead": 0},
            "job_counts": {"pending": 0, "running": 0, "succeeded": 3, "failed": 0, "dead": 0},
            "jobs": {"claimed": 1, "succeeded": 1, "failed": 0, "dead": 0, "batches": 1},
            "controls": {
                "batch_limit": kwargs.get("batch_limit") or kwargs.get("limit") or 5,
                "max_jobs": kwargs.get("max_jobs") or kwargs.get("batch_limit") or kwargs.get("limit") or 5,
                "continuous": bool(kwargs.get("continuous")),
                "time_budget_seconds": kwargs.get("time_budget_seconds") or 60,
                "stop_reason": "single_batch_complete",
            },
            "limit": kwargs.get("batch_limit") or kwargs.get("limit") or 5,
            "more_remain": False,
            "source_event_ids": [101, 102],
            "sender_ids": ["wxid_a", "wxid_b"],
            "memory_item_ids": [201],
            "content": "RAW_FIELD_SENTINEL_CONTENT",
            "user_text": "RAW_FIELD_SENTINEL_USER_TEXT",
        }

    async def run_group_relationship_window_extraction(self, **kwargs):
        self.window_relationship_calls.append(kwargs)
        return {
            "ok": True,
            "status": "completed",
            "scope": {
                "tenant_id": kwargs["tenant_id"],
                "channel": kwargs["channel"],
                "source_key": kwargs["source_key"],
                "session_id": kwargs["session_id"],
                "user_id_scope": kwargs.get("user_id") or "__group__",
            },
            "date": kwargs["date"],
            "controls": {
                "window_size": kwargs["window_size"],
                "max_windows": kwargs["max_windows"],
                "cursor_event_id": kwargs["cursor_event_id"],
                "dry_run": kwargs["dry_run"],
            },
            "windows": [{
                "index": 1,
                "event_count": 2,
                "first_event_id": 101,
                "last_event_id": 102,
                "sender_count": 2,
                "candidate_count": 1,
                "applied_count": 1,
                "skipped_count": 0,
                "user_text": "RAW_FIELD_SENTINEL_WINDOW",
            }],
            "totals": {"events": 2, "windows": 1, "candidates": 1, "applied": 1, "skipped": 0},
            "next_cursor_event_id": 102,
            "more_remain": False,
            "generated_from": ["plugin_memory_event", "llm_window_extractor"],
            "content": "RAW_FIELD_SENTINEL_CONTENT",
            "value_json": {"raw": "RAW_FIELD_SENTINEL_VALUE"},
        }

    async def run_group_relationship_window_catchup(self, **kwargs):
        self.window_catchup_calls.append(kwargs)
        return {
            "ok": True,
            "status": "completed",
            "scope": {
                "tenant_id": kwargs["tenant_id"],
                "channel": kwargs["channel"],
                "source_key": kwargs["source_key"],
                "session_id": kwargs["session_id"],
            },
            "date": kwargs["date"],
            "controls": {
                "window_size": kwargs["window_size"],
                "max_windows_per_run": kwargs["max_windows_per_run"],
                "cursor_event_id": kwargs["cursor_event_id"],
                "dry_run": kwargs["dry_run"],
                "time_budget_seconds": kwargs["time_budget_seconds"],
            },
            "totals": {"events": 2, "windows": 1, "candidates": 1, "applied": 1, "skipped": 0},
            "windows_processed": 1,
            "next_cursor_event_id": 102,
            "more_remain": False,
            "stop_reason": "no_more_events",
            "content": "RAW_FIELD_SENTINEL_CONTENT",
        }

    async def get_group_relationship_window_stats(self, **kwargs):
        self.window_stats_calls.append(kwargs)
        return {
            "ok": True,
            "scope": kwargs,
            "totals": {"items": 1, "events": 2, "windows": 1, "needs_review": 1},
            "status_counts": {"pending": 1},
            "acceptance_counts": {"needs_review": 1},
            "predicate_counts": {"asked": 1},
            "content": "RAW_FIELD_SENTINEL_CONTENT",
        }

    async def review_group_relationship_edge(self, **kwargs):
        self.edge_review_calls.append(kwargs)
        return {
            "ok": True,
            "edge_id": kwargs["edge_id"],
            "edge": {"id": kwargs["edge_id"], "type": "knows", "content": "RAW_FIELD_SENTINEL_EDGE"},
            "action": kwargs["action"],
            "reviewed_by": kwargs.get("reviewed_by") or "admin/api",
            "review_reason": kwargs.get("review_reason") or "",
            "result": {
                "reviewed_item_count": 1,
                "memory_item_ids": [201],
                "item_statuses": [{
                    "id": 201,
                    "status": "active" if kwargs["action"] == "accept" else "archived",
                    "acceptance_status": "accepted" if kwargs["action"] == "accept" else "rejected",
                    "content": "RAW_FIELD_SENTINEL_MEMORY",
                }],
                "evidence_counts": {"memory_items": 1, "events": 1, "episodes": 0},
            },
            "evidence_ids": {
                "backing_memory_item_id": 201,
                "memory_item_ids": [201],
                "event_ids": [101],
                "episode_ids": [],
            },
            "user_text": "RAW_FIELD_SENTINEL_USER_TEXT",
        }

    async def get_group_graph_history_dates(self, **kwargs):
        self.history_date_calls.append(kwargs)
        return {
            "ok": True,
            "tenant_id": kwargs["tenant_id"],
            "channel": kwargs["channel"],
            "source_key": kwargs["source_key"],
            "session_id": kwargs["session_id"],
            "user_id": kwargs["user_id"],
            "user_id_scope": kwargs["user_id"] or "__group__",
            "user_id_auto": not bool(kwargs["user_id"]),
            "recent_days": kwargs["recent_days"],
            "items": [
                {
                    "date": "2026-05-15",
                    "raw_message_count": 10,
                    "imported_count": 4,
                    "job_counts": {
                        "pending": 1,
                        "running": 0,
                        "succeeded": 2,
                        "failed": 1,
                        "dead": 0,
                    },
                    "status": "partial",
                }
            ],
        }

    async def list_memory_items(self, **kwargs):
        return [{
            "id": 1,
            "tenant_id": kwargs["tenant_id"],
            "channel": kwargs.get("channel") or "wechat",
            "source_key": kwargs.get("source_key") or "wxbot",
            "user_id": kwargs.get("user_id") or "wxid_a",
            "session_id": kwargs.get("session_id") or "",
            "scope_type": kwargs.get("scope_type") or "identity",
            "source_type": kwargs.get("source_type") or "manual",
            "memory_type": "note",
            "content": "重点客户",
            "original_text": "raw original note",
            "value_json": {"raw": "private value"},
            "raw_text": "raw private item text",
            "message_text": "raw private message text",
            "status": kwargs.get("status") or "active",
            "confidence": 1.0,
            "pinned": True,
        }]

    async def list_llm_extraction_jobs(self, **kwargs):
        self.job_list_calls.append(kwargs)
        return [{
            "id": 3,
            "tenant_id": kwargs.get("tenant_id") or "demo",
            "channel": kwargs.get("channel") or "wechat",
            "source_key": kwargs.get("source_key") or "wxbot",
            "user_id": kwargs.get("user_id") or "wxid_a",
            "session_id": kwargs.get("session_id") or "group-1@chatroom",
            "status": kwargs.get("status") or "pending",
            "error_type": kwargs.get("error_type") or "TimeoutError",
            "attempts": 0,
            "max_attempts": 3,
            "last_error": "",
            "result_json": {"error_type": "TimeoutError", "raw": "hidden"},
            "source_trace_id": "trace-1",
        }]

    async def get_llm_extraction_job_status_counts(self, **kwargs):
        self.job_stats_calls.append(kwargs)
        return {
            "counts": {"pending": 2, "running": 1, "succeeded": 3, "failed": 4, "dead": 5},
            "status_counts": {"pending": 2, "running": 1, "succeeded": 3, "failed": 4, "dead": 5},
            "retry_counts": {
                "retryable_failed": 3,
                "exhausted_failed": 1,
                "ready": 5,
                "delayed": 2,
            },
            "error_type_counts": {"TimeoutError": 4},
            "scope_counts": [{
                "tenant_id": kwargs.get("tenant_id") or "demo",
                "channel": kwargs.get("channel") or "wechat",
                "source_key": kwargs.get("source_key") or "wxbot",
                "user_id": kwargs.get("user_id") or "wxid_a",
                "session_id": kwargs.get("session_id") or "group-1@chatroom",
                "status": kwargs.get("status") or "failed",
                "error_type": kwargs.get("error_type") or "TimeoutError",
                "count": 4,
            }],
            "dead_scope_counts": [{
                "tenant_id": kwargs.get("tenant_id") or "demo",
                "channel": kwargs.get("channel") or "wechat",
                "source_key": kwargs.get("source_key") or "wxbot",
                "user_id": kwargs.get("user_id") or "wxid_a",
                "session_id": kwargs.get("session_id") or "group-1@chatroom",
                "status": "dead",
                "error_type": kwargs.get("error_type") or "TimeoutError",
                "count": 1,
            }],
            "retry_scope_counts": [{
                "tenant_id": kwargs.get("tenant_id") or "demo",
                "channel": kwargs.get("channel") or "wechat",
                "source_key": kwargs.get("source_key") or "wxbot",
                "user_id": kwargs.get("user_id") or "wxid_a",
                "session_id": kwargs.get("session_id") or "group-1@chatroom",
                "status": "failed",
                "error_type": kwargs.get("error_type") or "TimeoutError",
                "retryable": True,
                "count": 3,
            }],
            "latency_seconds": {"avg": 1.25, "max": 4.5, "pending_avg_age": 12.0},
            "graph_result_counts": {"error": 1, "facts": 2, "episodes": 1, "entities": 2, "skipped": 1},
        }

    async def maintain_llm_extraction_jobs(self, **kwargs):
        self.job_maintenance_calls.append(kwargs)
        return {
            "dry_run": kwargs.get("dry_run"),
            "limit": kwargs.get("limit"),
            "would_affect": 1,
            "affected": 0 if kwargs.get("dry_run") else 1,
            "ids": [3],
            "results": [{
                "action": kwargs.get("actions", [""])[0],
                "dry_run": kwargs.get("dry_run"),
                "would_affect": 1,
                "affected": 0 if kwargs.get("dry_run") else 1,
                "ids": [3],
            }],
        }

    async def maintain_llm_extraction_jobs_idempotent(self, **kwargs):
        result = await self.maintain_llm_extraction_jobs(**kwargs["params"])
        return MutationOutcome(response=result, status_code=200, replayed=False, mutation_id="fake")

    async def get_memory_acceptance_stats(self, **kwargs):
        self.acceptance_stats_calls.append(kwargs)
        return {
            "total": 3,
            "counts": {"accepted": 1, "missing_acceptance": 2},
            "sensitivity_counts": {"normal": 2, "private": 1, "sensitive": 0},
            "ids_preview": [1, 2, 3],
        }

    async def audit_legacy_acceptance(self, **kwargs):
        self.acceptance_audit_calls.append(kwargs)
        return {
            "dry_run": True,
            "missing_acceptance": 2,
            "suggested_action": "needs_review",
            "groups": [{
                "scope_type": "identity",
                "status": "active",
                "memory_type": "note",
                "source_type": "manual",
                "count": 2,
                "ids_preview": [1, 2],
                "suggested_action": "needs_review",
            }],
            "ids_preview": [1, 2],
        }

    async def backfill_legacy_acceptance(self, **kwargs):
        self.acceptance_backfill_calls.append(kwargs)
        return {
            "dry_run": kwargs.get("dry_run"),
            "mark_missing_as": kwargs.get("mark_missing_as"),
            "would_affect": 2,
            "affected": 0 if kwargs.get("dry_run") else min(int(kwargs.get("max_items") or 0), 2),
            "ids_preview": [1, 2],
            "ids": [] if kwargs.get("dry_run") else [1],
        }

    async def backfill_legacy_acceptance_idempotent(self, **kwargs):
        result = await self.backfill_legacy_acceptance(**kwargs["params"])
        return MutationOutcome(response=result, status_code=200, replayed=False, mutation_id="fake")

    async def list_memory_graph_entities(self, **kwargs):
        return [{
            "id": 11,
            "tenant_id": kwargs["tenant_id"],
            "channel": kwargs.get("channel") or "wechat",
            "source_key": kwargs.get("source_key") or "wxbot",
            "user_id": kwargs.get("user_id") or "wxid_a",
            "entity_type": "brand",
            "name": "Adidas",
            "status": kwargs.get("status") or "active",
        }]

    async def list_memory_graph_facts(self, **kwargs):
        return [{
            "id": 12,
            "tenant_id": kwargs["tenant_id"],
            "channel": kwargs.get("channel") or "wechat",
            "source_key": kwargs.get("source_key") or "wxbot",
            "user_id": kwargs.get("user_id") or "wxid_a",
            "predicate": "likes",
            "object_name": "Adidas",
            "object_value": "raw graph value",
            "status": kwargs.get("status") or "active",
        }]

    async def list_memory_graph_episodes(self, **kwargs):
        return [{
            "id": 13,
            "tenant_id": kwargs["tenant_id"],
            "channel": kwargs.get("channel") or "wechat",
            "source_key": kwargs.get("source_key") or "wxbot",
            "user_id": kwargs.get("user_id") or "wxid_a",
            "session_id": kwargs.get("session_id") or "",
            "title": "用户询问过物流进度",
            "summary": "raw episode summary",
            "status": kwargs.get("status") or "active",
        }]

    async def create_memory_item(self, **kwargs):
        self.saved_item = kwargs
        return {
            "id": 2,
            **kwargs,
            "status": kwargs.get("status") or "active",
            "confidence": kwargs.get("confidence") or 1.0,
        }

    async def create_memory_item_idempotent(self, **kwargs):
        result = await self.create_memory_item(**kwargs["item_fields"])
        return MutationOutcome(response=result, status_code=200, replayed=False, mutation_id="fake")

    async def create_profile_enrichment_candidate(self, **kwargs):
        self.profile_candidate_create_calls.append(kwargs)
        return {
            "id": 20,
            "tenant_id": kwargs["tenant_id"],
            "channel": kwargs["channel"],
            "source_key": kwargs["source_key"],
            "session_id": kwargs["session_id"],
            "user_id": kwargs["user_id"],
            "scope_type": "session",
            "source_type": "profile_enrichment",
            "memory_type": "profile_enrichment_candidate",
            "status": "pending",
            "acceptance_status": "needs_review",
            "value": {
                "report": kwargs["report_payload"],
                "review": {"state": "needs_review", "created_by": kwargs.get("created_by")},
                "acceptance": {"status": "needs_review"},
            },
        }

    async def member_memory_write_blocked(self, **_kwargs):
        return False

    async def list_profile_enrichment_candidates(self, **kwargs):
        self.profile_candidate_list_calls.append(kwargs)
        return [{
            "id": 20,
            "tenant_id": kwargs["tenant_id"],
            "channel": kwargs.get("channel") or "wechat",
            "source_key": kwargs.get("source_key") or "wxbot",
            "session_id": kwargs.get("session_id") or "group-1@chatroom",
            "user_id": kwargs.get("user_id") or "wxid_member",
            "source_type": "profile_enrichment",
            "memory_type": "profile_enrichment_candidate",
            "status": "pending",
            "acceptance_status": kwargs.get("review_state") or "needs_review",
            "value": {"review": {"state": kwargs.get("review_state") or "needs_review"}},
        }]

    async def get_profile_enrichment_candidate(self, candidate_id: int):
        if candidate_id != 20:
            return None
        return {
            "id": 20,
            "source_type": "profile_enrichment",
            "memory_type": "profile_enrichment_candidate",
            "status": "pending",
            "acceptance_status": "needs_review",
            "value": {"review": {"state": "needs_review"}},
        }

    async def review_profile_enrichment_candidate(self, candidate_id: int, **kwargs):
        self.profile_candidate_review_calls.append({
            "candidate_id": candidate_id,
            "action": kwargs.get("action"),
            "notes": kwargs.get("notes"),
            "reviewed_by": kwargs.get("reviewed_by"),
        })
        state = {"accept": "accepted", "reject": "rejected", "hide": "hidden"}[kwargs["action"]]
        return {
            "id": candidate_id,
            "status": "active" if state == "accepted" else "pending",
            "acceptance_status": state,
            "value": {"review": {"state": state, "notes": kwargs.get("notes")}},
        }

    async def review_profile_enrichment_candidate_idempotent(self, candidate_id: int, **kwargs):
        response = await self.review_profile_enrichment_candidate(candidate_id, **kwargs)
        return MutationOutcome(response=response, status_code=200, replayed=False, mutation_id="fake")

    async def update_memory_item(self, item_id: int, **kwargs):
        self.updated_item = (item_id, kwargs)
        return {"id": item_id, "content": kwargs.get("content") or "重点客户", "status": "active"}

    async def update_memory_item_idempotent(self, item_id: int, **kwargs):
        result = await self.update_memory_item(item_id, **kwargs["updates"])
        return MutationOutcome(response=result, status_code=200, replayed=False, mutation_id="fake")

    async def update_memory_item_scoped(self, item_id: int, **kwargs):
        self.updated_item = (item_id, kwargs)
        if kwargs.get("user_id") == "other":
            return None
        return {"id": item_id, "content": kwargs.get("content") or "重点客户", "status": "active"}

    async def review_memory_item_acceptance(self, item_id: int, **kwargs):
        self.acceptance_review_calls.append({
            "item_id": item_id,
            "action": kwargs.get("action"),
            "reason": kwargs.get("review_reason"),
            "actor": kwargs.get("reviewed_by"),
            "superseded_by_item_id": kwargs.get("superseded_by_item_id"),
            "supersedes_item_id": kwargs.get("supersedes_item_id"),
        })
        return {
            "id": item_id,
            "content": "重点客户",
            "status": "pending" if kwargs.get("action") != "accept" else "active",
            "acceptance_status": "rejected" if kwargs.get("action") == "mark_joke" else kwargs.get("action"),
        }

    async def review_memory_item_acceptance_idempotent(self, item_id: int, **kwargs):
        response = await self.review_memory_item_acceptance(item_id, **kwargs)
        payload = {"ok": True, "ids": [item_id], "count": 1, "item": response}
        return MutationOutcome(response=payload, status_code=200, replayed=False, mutation_id="fake")

    async def soft_delete_memory_item(self, item_id: int, *, allow_pinned: bool = False):
        self.deleted_item = (item_id, allow_pinned)
        return {"id": item_id, "status": "deleted"}

    async def soft_delete_memory_item_idempotent(self, item_id: int, **kwargs):
        item = await self.soft_delete_memory_item(
            item_id,
            allow_pinned=bool(kwargs.get("allow_pinned")),
        )
        payload = {"ok": True, "ids": [item_id], "count": 1, "item": item}
        return MutationOutcome(response=payload, status_code=200, replayed=False, mutation_id="fake")

    async def retrieve_memory_items(self, **kwargs):
        self.retrieved = kwargs
        return [{
            "id": 4,
            "tenant_id": kwargs["tenant_id"],
            "channel": kwargs["channel"],
            "source_key": kwargs["source_key"],
            "user_id": kwargs["user_id"],
            "scope_type": "identity",
            "source_type": "manual",
            "memory_type": "note",
            "content": "重点客户",
            "status": "active",
            "sensitivity": "normal",
            "deleted_at": None,
        }]

    async def forget_memory_items(self, **kwargs):
        self.forgotten = kwargs
        return {"ids": [kwargs.get("item_id") or 4], "count": 1}

    async def forget_memory_items_idempotent(self, **kwargs):
        for key in ("idempotency_key", "actor", "actor_kind", "roles", "trace_id"):
            kwargs.pop(key, None)
        result = await self.forget_memory_items(**kwargs)
        return MutationOutcome(
            response={"ok": True, **result},
            status_code=200,
            replayed=False,
            mutation_id="fake",
        )

    async def backfill_from_sdk(self, **kwargs):
        self.backfill_calls.append(kwargs)
        return {
            "ok": True,
            "tenant_id": kwargs["tenant_id"],
            "user_id": kwargs["user_id"] or "__group__",
            "user_id_scope": kwargs["user_id"] or "__group__",
            "user_id_auto": not bool(kwargs["user_id"]),
            "session_count": len(kwargs["session_ids"]),
            "imported_count": 12,
        }

    async def backfill_from_sdk_idempotent(self, **kwargs):
        result = await self.backfill_from_sdk(**kwargs["params"])
        return MutationOutcome(response=result, status_code=200, replayed=False, mutation_id="fake")

    async def rebuild_memory_item_vector_index(self, **kwargs):
        self.rebuild_calls.append(kwargs)
        return {"enabled": True, "scanned": 2, "indexed": 2, "deleted": 0, "skipped": 0, "errors": 0}

    async def rebuild_memory_item_vector_index_idempotent(self, **kwargs):
        result = await self.rebuild_memory_item_vector_index(**kwargs["params"])
        return MutationOutcome(response=result, status_code=200, replayed=False, mutation_id="fake")

    async def rebuild_memory_graph_vector_index(self, **kwargs):
        self.rebuild_calls.append({"graph": True, **kwargs})
        return {
            "enabled": True,
            "dry_run": kwargs.get("dry_run"),
            "scanned": 3,
            "indexed": 0 if kwargs.get("dry_run") else 3,
            "deleted": 0,
            "skipped": 0,
            "errors": 0,
        }

    async def rebuild_memory_graph_vector_index_idempotent(self, **kwargs):
        result = await self.rebuild_memory_graph_vector_index(**kwargs["params"])
        return MutationOutcome(response=result, status_code=200, replayed=False, mutation_id="fake")

    async def smoke_memory_vector_enable(self, **kwargs):
        self.rebuild_calls.append({"smoke": True, **kwargs})
        return {
            "safe_to_enable": True,
            "preflight": {"safe_to_enable": True},
            "rebuild": {"dry_run": kwargs.get("dry_run"), "scanned": kwargs.get("limit")},
            "search": {"ok": True, "behavior": "fallback"},
            "reasons": [],
        }


class _SessionScopedControlStore:
    def __init__(self) -> None:
        self.forget_calls: list[dict[str, object]] = []
        self.update_calls: list[dict[str, object]] = []

    async def forget_memory_items(self, **kwargs):
        self.forget_calls.append(kwargs)
        if kwargs.get("item_id") == 8 and kwargs.get("session_id") == "session-a":
            return {"ids": [8], "count": 1}
        return {"ids": [], "count": 0}

    async def forget_memory_items_idempotent(self, **kwargs):
        for key in ("idempotency_key", "actor", "actor_kind", "roles", "trace_id"):
            kwargs.pop(key, None)
        result = await self.forget_memory_items(**kwargs)
        return MutationOutcome(
            response={"ok": True, **result},
            status_code=200,
            replayed=False,
            mutation_id="fake",
        )

    async def update_memory_item_scoped(self, item_id: int, **kwargs):
        self.update_calls.append({"item_id": item_id, **kwargs})
        if item_id == 8 and kwargs.get("session_id") == "session-a":
            return {"id": item_id, "session_id": "session-a", "content": kwargs.get("content")}
        return None


class _ProfileConflictStore(_FakeStore):
    async def upsert_identity_profile(self, **kwargs):
        raise MemoryProfileConflictError(
            expected_version=str(kwargs.get("expected_version") or ""),
            actual_version="2026-07-30T10:00:00+00:00",
        )

    async def upsert_session_profile(self, **kwargs):
        raise MemoryProfileConflictError(
            expected_version=str(kwargs.get("expected_version") or ""),
            actual_version="2026-07-30T11:00:00+00:00",
        )


@pytest.mark.asyncio
async def test_memory_router_supports_layered_memory_endpoints() -> None:
    app = FastAPI()
    store = _FakeStore()
    app.include_router(build_memory_router(store))

    transport = httpx.ASGITransport(app=app)
    admin_headers = {"Authorization": "Bearer admin_token"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        list_resp = await client.get(
            "/profiles?tenant_id=demo&channel=wechat&source_key=wxbot&user_id=wxid_a&limit=10",
            headers=admin_headers,
        )
        get_identity_resp = await client.get("/profiles/demo/wechat/wxbot/wxid_a", headers=admin_headers)
        save_identity_resp = await client.post(
            "/profiles",
            headers=admin_headers,
            json={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "user_id": "wxid_a",
                "manual_notes": "重点客户",
            },
        )
        list_session_resp = await client.get(
            "/session-profiles?tenant_id=demo&channel=wechat&source_key=wxbot&session_id=group-1@chatroom&user_id=wxid_a&limit=10",
            headers=admin_headers,
        )
        get_session_resp = await client.get(
            "/session-profiles/demo/wechat/wxbot/group-1@chatroom",
            params={"user_id": "wxid_a"},
            headers=admin_headers,
        )
        save_session_resp = await client.post(
            "/session-profiles",
            headers=admin_headers,
            json={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "session_id": "group-1@chatroom",
                "user_id": "wxid_a",
                "manual_notes": "这个群更关注发货",
            },
        )
        runtime_resp = await client.get(
            "/runtime-profile/demo/wechat/wxbot/group-1@chatroom",
            params={"user_id": "wxid_a"},
            headers=admin_headers,
        )
        events_resp = await client.get(
            "/events?tenant_id=demo&channel=wechat&source_key=wxbot&user_id=wxid_a&session_id=group-1@chatroom&limit=10",
            headers=admin_headers,
        )
        group_graph_resp = await client.get(
            "/group-graph?tenant_id=demo&channel=wechat&source_key=wxbot&session_id=group-1@chatroom"
            "&from=2026-05-01T00:00:00&to=2026-05-15T00:00:00&node_type=person&relation_type=knows"
            "&acceptance_status=accepted,needs_review&min_confidence=0.45&limit=25",
            headers=admin_headers,
        )
        history_dates_resp = await client.get(
            "/group-graph/history-dates?tenant_id=demo&channel=wechat&source_key=wxbot"
            "&session_id=group-1@chatroom&recent_days=7",
            headers=admin_headers,
        )
        list_items_resp = await client.get(
            "/items?tenant_id=demo&channel=wechat&source_key=wxbot&user_id=wxid_a&scope_type=identity",
            headers=admin_headers,
        )
        acceptance_stats_resp = await client.get(
            "/items/acceptance-stats?tenant_id=demo&channel=wechat&source_key=wxbot&user_id=wxid_a"
            "&scope_type=identity&memory_type=note&status=active",
            headers=admin_headers,
        )
        acceptance_stats_invalid_resp = await client.get(
            "/items/acceptance-stats?tenant_id=demo&acceptance_status=accepted;DROP",
            headers=admin_headers,
        )
        acceptance_audit_resp = await client.get(
            "/items/acceptance-legacy-audit?tenant_id=demo&channel=wechat&source_key=wxbot&user_id=wxid_a",
            headers=admin_headers,
        )
        acceptance_backfill_forbidden_resp = await client.post(
            "/items/acceptance-legacy-backfill",
            json={"tenant_id": "demo", "dry_run": True, "max_items": 10},
        )
        acceptance_backfill_invalid_status_resp = await client.post(
            "/items/acceptance-legacy-backfill",
            headers={"Authorization": "Bearer admin_token"},
            json={"tenant_id": "demo", "dry_run": True, "max_items": 10, "mark_missing_as": "accepted"},
        )
        acceptance_backfill_resp = await client.post(
            "/items/acceptance-legacy-backfill",
            headers={
                "Authorization": "Bearer admin_token",
                "X-Actor-ID": "admin_backfill",
                "Idempotency-Key": "acceptance-backfill-preview",
            },
            json={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "user_id": "wxid_a",
                "dry_run": True,
                "max_items": 10,
                "mark_missing_as": "needs_review",
            },
        )
        jobs_resp = await client.get(
            "/extraction-jobs?tenant_id=demo&channel=wechat&source_key=wxbot&user_id=wxid_a&status=pending",
            headers=admin_headers,
        )
        job_stats_resp = await client.get(
            "/extraction-jobs/stats?tenant_id=demo&channel=wechat&source_key=wxbot&user_id=wxid_a",
            headers=admin_headers,
        )
        graph_entities_resp = await client.get(
            "/graph/entities?tenant_id=demo&channel=wechat&source_key=wxbot&user_id=wxid_a",
            headers=admin_headers,
        )
        graph_facts_resp = await client.get(
            "/graph/facts?tenant_id=demo&channel=wechat&source_key=wxbot&user_id=wxid_a",
            headers=admin_headers,
        )
        graph_episodes_resp = await client.get(
            "/graph/episodes?tenant_id=demo&channel=wechat&source_key=wxbot&user_id=wxid_a",
            headers=admin_headers,
        )
        graph_preview_resp = await client.get(
            "/graph/preview?tenant_id=demo&channel=wechat&source_key=wxbot&user_id=wxid_a",
            headers=admin_headers,
        )
        create_item_resp = await client.post(
            "/items",
            headers={
                "Authorization": "Bearer admin_token",
                "Idempotency-Key": "create-item-2",
            },
            json={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "user_id": "wxid_a",
                "source_type": "manual",
                "content": "重点客户",
            },
        )
        update_item_resp = await client.patch(
            "/items/2?tenant_id=demo",
            headers={
                "Authorization": "Bearer admin_token",
                "Idempotency-Key": "update-item-2",
            },
            json={"content": "高价值客户"},
        )
        delete_item_resp = await client.delete(
            "/items/2?tenant_id=demo",
            headers={"Idempotency-Key": "delete-item-2"},
        )
        backfill_resp = await client.post(
            "/backfill",
            headers={
                "Authorization": "Bearer admin_token",
                "Idempotency-Key": "backfill-group-1",
            },
            json={
                "tenant_id": "demo",
                "connection_id": "legacy-wechat-default",
                "channel": "wechat",
                "source_key": "wxbot",
                "session_ids": ["group-1@chatroom"],
                "days_limit": 180,
                "max_messages_per_session": 200,
                "target_date": "2026-05-15",
            },
        )
        rebuild_forbidden_resp = await client.post(
            "/items/vector-rebuild",
            json={"tenant_id": "demo", "limit": 2},
        )
        rebuild_resp = await client.post(
            "/items/vector-rebuild",
            headers={
                "Authorization": "Bearer admin_token",
                "Idempotency-Key": "item-vector-preview",
            },
            json={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "limit": 2,
                "dry_run": True,
            },
        )
        smoke_forbidden_resp = await client.post(
            "/items/vector-smoke",
            json={"tenant_id": "demo", "limit": 2},
        )
        smoke_resp = await client.post(
            "/items/vector-smoke",
            headers={"Authorization": "Bearer admin_token"},
            json={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "user_id": "wxid_a",
                "session_id": "group-1@chatroom",
                "query": "重点客户",
                "limit": 2,
                "dry_run": False,
            },
        )

    assert list_resp.status_code == 200
    assert list_resp.json()["items"][0]["imported_message_count"] == 5
    assert get_identity_resp.status_code == 200
    assert get_identity_resp.json()["manual_notes"] == "VIP"
    assert save_identity_resp.status_code == 200
    assert save_identity_resp.json()["manual_notes"] == "重点客户"
    assert store.saved_identity is not None
    assert store.saved_identity["user_id"] == "wxid_a"

    assert list_session_resp.status_code == 200
    assert list_session_resp.json()["items"][0]["session_id"] == "group-1@chatroom"
    assert get_session_resp.status_code == 200
    assert get_session_resp.json()["manual_notes"] == "该群里关注物流"
    assert save_session_resp.status_code == 200
    assert store.saved_session is not None
    assert store.saved_session["session_id"] == "group-1@chatroom"

    assert runtime_resp.status_code == 200
    assert runtime_resp.json()["session_message_count"] == 2
    assert events_resp.status_code == 200
    assert events_resp.json()["items"][0]["user_text"] == "查物流"
    assert group_graph_resp.status_code == 200
    group_graph = group_graph_resp.json()
    assert group_graph["schema"]["version"] == "group-graph.v1"
    assert "person" in group_graph["schema"]["node_types"]
    assert "works_on" in group_graph["schema"]["edge_types"]
    assert group_graph["scope"]["session_id"] == "group-1@chatroom"
    assert group_graph["filters"]["edge_type"] == "knows"
    assert group_graph["filters"]["relation_type"] == "knows"
    assert group_graph["nodes"][0]["label"] == "Alice"
    assert group_graph["edges"][0]["source_event_ids"] == [101]
    assert "content" not in str(group_graph)
    assert "original_text" not in str(group_graph)
    assert store.group_graph_calls[0]["node_type"] == "person"
    assert store.group_graph_calls[0]["edge_type"] == "knows"
    assert store.group_graph_calls[0]["acceptance_status"] == "accepted,needs_review"
    assert store.group_graph_calls[0]["min_confidence"] == 0.45
    assert store.group_graph_calls[0]["limit"] == 25
    assert history_dates_resp.status_code == 200
    assert history_dates_resp.json()["user_id_scope"] == "__group__"
    assert history_dates_resp.json()["items"][0] == {
        "date": "2026-05-15",
        "raw_message_count": 10,
        "imported_count": 4,
        "job_counts": {
            "pending": 1,
            "running": 0,
            "succeeded": 2,
            "failed": 1,
            "dead": 0,
        },
        "status": "partial",
    }
    assert history_dates_resp.json()["user_id_auto"] is True
    assert store.history_date_calls[0]["user_id"] is None
    assert "private user text" not in str(history_dates_resp.json())
    assert list_items_resp.status_code == 200
    assert list_items_resp.json()["items"][0]["content"] == "重点客户"
    assert acceptance_stats_resp.status_code == 200
    assert acceptance_stats_resp.json()["counts"]["missing_acceptance"] == 2
    assert store.acceptance_stats_calls[0]["memory_type"] == "note"
    assert acceptance_stats_invalid_resp.status_code == 400
    assert acceptance_audit_resp.status_code == 200
    assert acceptance_audit_resp.json()["groups"][0]["ids_preview"] == [1, 2]
    assert "content" not in acceptance_audit_resp.json()["groups"][0]
    assert acceptance_backfill_forbidden_resp.status_code == 401
    assert acceptance_backfill_invalid_status_resp.status_code == 422
    assert acceptance_backfill_resp.status_code == 200
    assert acceptance_backfill_resp.json()["dry_run"] is True
    assert store.acceptance_backfill_calls[0]["reviewed_by"] == "admin_backfill"
    assert store.acceptance_backfill_calls[0]["mark_missing_as"] == "needs_review"
    assert jobs_resp.status_code == 200
    assert jobs_resp.json()["items"][0]["status"] == "pending"
    assert "last_error" not in jobs_resp.json()["items"][0]
    assert "result_json" not in jobs_resp.json()["items"][0]
    assert job_stats_resp.status_code == 200
    assert job_stats_resp.json()["counts"] == {
        "pending": 2,
        "running": 1,
        "succeeded": 3,
        "failed": 4,
        "dead": 5,
    }
    assert job_stats_resp.json()["retry_counts"]["retryable_failed"] == 3
    assert job_stats_resp.json()["dead_scope_counts"][0]["status"] == "dead"
    assert job_stats_resp.json()["retry_scope_counts"][0]["retryable"] is True
    assert job_stats_resp.json()["latency_seconds"]["avg"] == 1.25
    assert job_stats_resp.json()["graph_result_counts"]["error"] == 1
    assert graph_entities_resp.status_code == 200
    assert graph_entities_resp.json()["items"][0]["name"] == "Adidas"
    assert graph_facts_resp.status_code == 200
    assert graph_facts_resp.json()["items"][0]["predicate"] == "likes"
    assert graph_episodes_resp.status_code == 200
    assert graph_episodes_resp.json()["items"][0]["title"] == "用户询问过物流进度"
    assert graph_preview_resp.status_code == 200
    assert graph_preview_resp.json()["counts"] == {"entities": 1, "facts": 1, "episodes": 1}
    assert create_item_resp.status_code == 200
    assert store.saved_item is not None
    assert store.saved_item["source_type"] == "manual"
    assert update_item_resp.status_code == 200
    assert store.updated_item == (2, {"content": "高价值客户"})
    assert delete_item_resp.status_code == 200
    assert store.deleted_item == (2, False)

    assert backfill_resp.status_code == 200
    assert backfill_resp.json()["imported_count"] == 12
    assert backfill_resp.json()["user_id_scope"] == "__group__"
    assert backfill_resp.json()["user_id_auto"] is True
    assert store.backfill_calls[0]["user_id"] is None
    assert store.backfill_calls[0]["session_ids"] == ["group-1@chatroom"]
    assert store.backfill_calls[0]["target_date"] == "2026-05-15"
    assert rebuild_forbidden_resp.status_code == 401
    assert rebuild_resp.status_code == 200
    assert rebuild_resp.json()["indexed"] == 2
    assert store.rebuild_calls[0]["tenant_id"] == "demo"
    assert store.rebuild_calls[0]["limit"] == 2
    assert store.rebuild_calls[0]["dry_run"] is True
    assert smoke_forbidden_resp.status_code == 401
    assert smoke_resp.status_code == 200
    assert smoke_resp.json()["safe_to_enable"] is True
    assert store.rebuild_calls[1]["smoke"] is True
    assert store.rebuild_calls[1]["query"] == "重点客户"
    assert store.rebuild_calls[1]["dry_run"] is False


@pytest.mark.asyncio
async def test_memory_router_p2a_scoped_control_endpoints() -> None:
    app = FastAPI()
    store = _FakeStore()
    app.include_router(build_memory_router(store))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        remember_resp = await client.post(
            "/remember",
            headers={"X-User-Id": "wxid_a", "Idempotency-Key": "remember-item-4"},
            json={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "content": "重点客户",
                "memory_type": "note",
            },
        )
        search_resp = await client.post(
            "/search",
            headers={"X-User-Id": "wxid_a"},
            json={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "query": "重点",
            },
        )
        forget_resp = await client.post(
            "/forget",
            headers={"X-User-Id": "wxid_a", "Idempotency-Key": "forget-item-4"},
            json={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "item_id": 4,
                "allow_pinned": True,
            },
        )
        update_resp = await client.post(
            "/update",
            headers={"X-User-Id": "wxid_a"},
            json={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "item_id": 4,
                "content": "高价值客户",
                "priority": 90,
            },
        )
        cross_user_resp = await client.post(
            "/update",
            headers={"X-User-Id": "wxid_a"},
            json={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "user_id": "other",
                "item_id": 4,
                "content": "不该改",
            },
        )

    assert remember_resp.status_code == 200
    assert remember_resp.json()["ids"] == [2]
    assert store.saved_item is not None
    assert store.saved_item["user_id"] == "wxid_a"
    assert store.saved_item["source_type"] == "manual"

    assert search_resp.status_code == 200
    assert search_resp.json()["count"] == 1
    assert store.retrieved is not None
    assert store.retrieved["user_id"] == "wxid_a"

    assert forget_resp.status_code == 200
    assert forget_resp.json() == {"ok": True, "ids": [4], "count": 1}
    assert store.forgotten is not None
    assert store.forgotten["user_id"] == "wxid_a"
    assert store.forgotten["allow_pinned"] is True

    assert update_resp.status_code == 200
    assert store.updated_item is not None
    assert store.updated_item[1]["user_id"] == "wxid_a"
    assert store.updated_item[1]["content"] == "高价值客户"
    assert cross_user_resp.status_code == 403


@pytest.mark.asyncio
async def test_manual_memory_creation_enforces_group_audience_and_retention_contract() -> None:
    app = FastAPI()
    store = _FakeStore()
    app.include_router(build_memory_router(store))
    admin_headers = {
        "Authorization": "Bearer admin_token",
        "Idempotency-Key": "group-memory-create",
    }
    started_at = datetime.now(UTC)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        group_resp = await client.post(
            "/items",
            headers=admin_headers,
            json={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "user_id": "wxid_a",
                "session_id": "group-1@chatroom",
                "scope_type": "identity",
                "source_type": "manual",
                "memory_type": "note",
                "content": "群内物流偏好",
                "audience_scope": "private",
                "origin_session_kind": "private",
                "allowed_session_ids": [],
                "retention_days": 90,
            },
        )
        group_saved = dict(store.saved_item or {})
        private_resp = await client.post(
            "/items",
            headers={
                "Authorization": "Bearer admin_token",
                "Idempotency-Key": "private-memory-create",
            },
            json={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "user_id": "wxid_a",
                "source_type": "manual",
                "memory_type": "note",
                "content": "私聊偏好",
            },
        )
        private_saved = dict(store.saved_item or {})
        retention_conflict_resp = await client.post(
            "/items",
            headers={
                "Authorization": "Bearer admin_token",
                "Idempotency-Key": "retention-conflict",
            },
            json={
                "tenant_id": "demo",
                "user_id": "wxid_a",
                "content": "冲突保留策略",
                "retention_days": 30,
                "expires_at": "2026-12-31T00:00:00Z",
            },
        )

    assert group_resp.status_code == 200
    assert group_saved["scope_type"] == "session"
    assert group_saved["session_id"] == "group-1@chatroom"
    assert group_saved["origin_session_kind"] == "group"
    assert group_saved["audience_scope"] == "session"
    assert group_saved["allowed_session_ids"] == ["group-1@chatroom"]
    assert group_saved["source_kind"] == "manual"
    expires_at = group_saved["expires_at"]
    assert isinstance(expires_at, datetime)
    assert started_at + timedelta(days=89) < expires_at < started_at + timedelta(days=91)

    assert private_resp.status_code == 200
    assert private_saved["scope_type"] == "identity"
    assert private_saved["session_id"] == ""
    assert private_saved["origin_session_kind"] == "private"
    assert private_saved["audience_scope"] == "private"
    assert private_saved["allowed_session_ids"] == []
    assert private_saved["expires_at"] is None

    assert retention_conflict_resp.status_code == 400
    assert retention_conflict_resp.json()["detail"]["code"] == "memory_retention_conflict"


@pytest.mark.asyncio
async def test_remember_requires_idempotency_and_group_writes_are_recallable_in_that_group() -> None:
    async def group_member(_tenant_id: str, _session_id: str, _user_id: str) -> bool:
        return True

    app = FastAPI()
    store = _FakeStore()
    app.include_router(
        build_memory_router(store, group_membership_authorizer=group_member)
    )
    body = {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "session_id": "group-1@chatroom",
        "content": "只在当前群召回",
        "memory_type": "note",
    }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        missing_key_resp = await client.post(
            "/remember",
            headers={"X-User-Id": "wxid_a"},
            json=body,
        )
        remember_resp = await client.post(
            "/remember",
            headers={
                "X-User-Id": "wxid_a",
                "Idempotency-Key": "remember-group-item",
            },
            json=body,
        )

    assert missing_key_resp.status_code == 428
    assert remember_resp.status_code == 200
    assert remember_resp.json()["count"] == 1
    assert store.saved_item is not None
    assert store.saved_item["scope_type"] == "session"
    assert store.saved_item["origin_session_kind"] == "group"
    assert store.saved_item["audience_scope"] == "session"
    assert store.saved_item["allowed_session_ids"] == ["group-1@chatroom"]


@pytest.mark.asyncio
async def test_memory_management_status_is_admin_only_and_explains_runtime_blockers() -> None:
    async def scope_disabled(_tenant_id: str, _session_id: str) -> bool:
        return False

    app = FastAPI()
    store = _FakeStore()
    app.include_router(
        build_memory_router(store, scope_execution_allowed=scope_disabled)
    )
    params = {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "session_id": "group-1@chatroom",
        "user_id": "wxid_a",
    }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unauthenticated_resp = await client.get("/management-status", params=params)
        invalid_bearer_resp = await client.get(
            "/management-status",
            params=params,
            headers={"Authorization": "Bearer not-an-admin"},
        )
        admin_resp = await client.get(
            "/management-status",
            params=params,
            headers={"Authorization": "Bearer admin_token"},
        )

    assert unauthenticated_resp.status_code == 401
    assert invalid_bearer_resp.status_code == 403
    assert admin_resp.status_code == 200
    payload = admin_resp.json()
    assert payload["config"]["source"] == "effective_process_settings"
    assert payload["config"]["values"]["memory_retrieval_enabled"] is True
    assert payload["runtime_scope"]["status"] == "disabled"
    assert payload["jobs"]["stats"]["status_counts"]["failed"] == 4
    assert "last_error" not in payload["jobs"]["recent"][0]
    assert "result_json" not in payload["jobs"]["recent"][0]
    assert payload["governance"]["auto_expire_days"] == 180
    diagnostic_codes = {item["code"] for item in payload["diagnostics"]}
    assert {
        "runtime_scope_disabled",
        "automatic_extraction_disabled",
        "extraction_jobs_failed",
        "items_waiting_for_review",
        "group_audience_mismatch",
    }.issubset(diagnostic_codes)
    assert "job_drain_disabled" not in diagnostic_codes


@pytest.mark.asyncio
async def test_sensitive_retrieval_and_job_reads_reject_missing_or_invalid_admin_auth() -> None:
    app = FastAPI()
    store = _FakeStore()
    app.include_router(build_memory_router(store))
    retrieve_params = {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
    }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        retrieve_unauthenticated = await client.get(
            "/items/retrieve",
            params=retrieve_params,
        )
        retrieve_invalid_bearer = await client.get(
            "/items/retrieve",
            params=retrieve_params,
            headers={"Authorization": "Bearer invalid"},
        )
        retrieve_admin = await client.get(
            "/items/retrieve",
            params=retrieve_params,
            headers={"Authorization": "Bearer admin_token"},
        )
        jobs_unauthenticated = await client.get("/extraction-jobs")
        stats_invalid_bearer = await client.get(
            "/extraction-jobs/stats",
            headers={"Authorization": "Bearer invalid"},
        )

    assert retrieve_unauthenticated.status_code == 401
    assert retrieve_invalid_bearer.status_code == 403
    assert retrieve_admin.status_code == 200
    assert retrieve_admin.json()["items"][0]["content"] == "重点客户"
    assert jobs_unauthenticated.status_code == 401
    assert stats_invalid_bearer.status_code == 403


@pytest.mark.asyncio
async def test_retrieval_mount_guard_rejects_cross_tenant_scope() -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="admin_token",
        outbound_hmac_secret="test-secret",
        tenant_demo_secret="test-tenant-secret",
    )
    principal = Principal(
        subject="tenant-admin",
        roles=(AdminRole.TENANT_ADMIN.value,),
        tenant_ids=("demo",),
        auth_kind="test",
    )

    async def authenticate() -> Principal:
        return principal

    store = _FakeStore()
    store.settings = settings
    guard = build_admin_authorization_dependency(
        settings,
        authentication_dependency=authenticate,
    )
    app = FastAPI()
    mounted = APIRouter(
        prefix="/plugins/memory",
        dependencies=[Depends(guard)],
    )
    mounted.include_router(build_memory_router(store))
    app.include_router(mounted)
    params = {
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
    }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        allowed_resp = await client.get(
            "/plugins/memory/items/retrieve",
            params={"tenant_id": "demo", **params},
            headers={"Authorization": "Bearer admin_token"},
        )
        cross_tenant_resp = await client.get(
            "/plugins/memory/items/retrieve",
            params={"tenant_id": "other", **params},
            headers={"Authorization": "Bearer admin_token"},
        )

    assert allowed_resp.status_code == 200
    assert cross_tenant_resp.status_code == 403
    assert cross_tenant_resp.json()["detail"] == "tenant_scope_forbidden"


@pytest.mark.asyncio
async def test_profile_expected_version_is_forwarded_and_conflicts_return_actionable_409() -> None:
    app = FastAPI()
    store = _FakeStore()
    app.include_router(build_memory_router(store))
    admin_headers = {"Authorization": "Bearer admin_token"}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        saved_resp = await client.post(
            "/profiles",
            headers=admin_headers,
            json={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "user_id": "wxid_a",
                "manual_notes": "新备注",
                "expected_version": "2026-07-30T09:00:00+00:00",
            },
        )

    assert saved_resp.status_code == 200
    assert store.saved_identity is not None
    assert store.saved_identity["expected_version"] == "2026-07-30T09:00:00+00:00"

    conflict_app = FastAPI()
    conflict_app.include_router(build_memory_router(_ProfileConflictStore()))
    conflict_transport = httpx.ASGITransport(app=conflict_app)
    async with httpx.AsyncClient(
        transport=conflict_transport,
        base_url="http://test",
    ) as client:
        identity_conflict = await client.post(
            "/profiles",
            headers=admin_headers,
            json={
                "tenant_id": "demo",
                "user_id": "wxid_a",
                "manual_notes": "过期页面写入",
                "expected_version": "2026-07-30T08:00:00+00:00",
            },
        )
        session_conflict = await client.post(
            "/session-profiles",
            headers=admin_headers,
            json={
                "tenant_id": "demo",
                "session_id": "group-1@chatroom",
                "user_id": "wxid_a",
                "manual_notes": "过期页面写入",
                "expected_version": "2026-07-30T08:30:00+00:00",
            },
        )

    assert identity_conflict.status_code == 409
    assert identity_conflict.json()["detail"]["code"] == "memory_profile_version_conflict"
    assert identity_conflict.json()["detail"]["actual_version"] == "2026-07-30T10:00:00+00:00"
    assert session_conflict.status_code == 409
    assert session_conflict.json()["detail"]["code"] == "memory_profile_version_conflict"
    assert session_conflict.json()["detail"]["actual_version"] == "2026-07-30T11:00:00+00:00"


@pytest.mark.asyncio
async def test_profile_upserts_map_member_memory_write_block_to_stable_409() -> None:
    app = FastAPI()
    store = _FakeStore()

    async def raise_member_write_blocked(**_kwargs):
        raise MemoryMutationError("member_memory_write_blocked", status_code=409)

    store.upsert_identity_profile = raise_member_write_blocked
    store.upsert_session_profile = raise_member_write_blocked
    app.include_router(build_memory_router(store))

    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer admin_token"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        identity_resp = await client.post(
            "/profiles",
            headers=headers,
            json={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "user_id": "wxid_a",
                "manual_notes": "must not be saved",
            },
        )
        session_resp = await client.post(
            "/session-profiles",
            headers=headers,
            json={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "session_id": "group-1@chatroom",
                "user_id": "wxid_a",
                "manual_notes": "must not be saved",
            },
        )

    assert identity_resp.status_code == 409
    assert identity_resp.json() == {"detail": "member_memory_write_blocked"}
    assert session_resp.status_code == 409
    assert session_resp.json() == {"detail": "member_memory_write_blocked"}


@pytest.mark.asyncio
async def test_memory_router_safe_read_endpoints_scope_and_scrub_current_user_payloads() -> None:
    app = FastAPI()
    store = _FakeStore()
    app.include_router(build_memory_router(store))

    headers = {"X-User-Id": "wxid_a", "Idempotency-Key": "forget-session"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        events_resp = await client.get(
            "/events",
            headers=headers,
            params={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "user_id": "wxid_a",
                "session_id": "group-1@chatroom",
            },
        )
        items_resp = await client.get(
            "/items",
            headers=headers,
            params={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "user_id": "wxid_a",
            },
        )
        graph_facts_resp = await client.get(
            "/graph/facts",
            headers=headers,
            params={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "user_id": "wxid_a",
            },
        )
        graph_episodes_resp = await client.get(
            "/graph/episodes",
            headers=headers,
            params={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "user_id": "wxid_a",
            },
        )
        cross_user_resp = await client.get(
            "/items",
            headers=headers,
            params={"tenant_id": "demo", "user_id": "wxid_b"},
        )
        broad_resp = await client.get("/items", params={"tenant_id": "demo"})
        group_graph_review_resp = await client.get(
            "/group-graph",
            params={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "session_id": "group-1@chatroom",
                "acceptance_status": "needs_review",
            },
        )
        group_graph_default_resp = await client.get(
            "/group-graph",
            params={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "session_id": "group-1@chatroom",
            },
        )

    assert events_resp.status_code == 200
    assert "user_text" not in events_resp.json()["items"][0]
    assert "assistant_text" not in events_resp.json()["items"][0]
    assert items_resp.status_code == 200
    assert "content" not in items_resp.json()["items"][0]
    assert "original_text" not in items_resp.json()["items"][0]
    assert graph_facts_resp.status_code == 200
    assert "object_value" not in graph_facts_resp.json()["items"][0]
    assert graph_episodes_resp.status_code == 200
    assert "summary" not in graph_episodes_resp.json()["items"][0]
    assert cross_user_resp.status_code == 403
    assert broad_resp.status_code == 403
    assert group_graph_review_resp.status_code == 401
    assert group_graph_default_resp.status_code == 401


@pytest.mark.asyncio
async def test_memory_router_non_admin_events_and_items_use_safe_dtos() -> None:
    app = FastAPI()
    store = _FakeStore()
    app.include_router(build_memory_router(store))

    headers = {"X-User-Id": "wxid_a"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        events_resp = await client.get(
            "/events",
            headers=headers,
            params={"tenant_id": "demo", "user_id": "wxid_a"},
        )
        items_resp = await client.get(
            "/items",
            headers=headers,
            params={"tenant_id": "demo", "user_id": "wxid_a"},
        )
        admin_events_resp = await client.get(
            "/events",
            headers={"Authorization": "Bearer admin_token"},
            params={"tenant_id": "demo", "user_id": "wxid_a"},
        )
        admin_items_resp = await client.get(
            "/items",
            headers={"Authorization": "Bearer admin_token"},
            params={"tenant_id": "demo", "user_id": "wxid_a"},
        )

    assert events_resp.status_code == 200
    safe_event = events_resp.json()["items"][0]
    assert safe_event["id"] == 1
    assert safe_event["user_id"] == "wxid_a"
    for raw_key in {"user_text", "assistant_text", "raw_text", "message_text", "content"}:
        assert raw_key not in safe_event

    assert items_resp.status_code == 200
    safe_item = items_resp.json()["items"][0]
    assert safe_item["id"] == 1
    for raw_key in {
        "content",
        "original_text",
        "value_json",
        "raw",
        "raw_text",
        "message_text",
        "user_text",
        "assistant_text",
    }:
        assert raw_key not in safe_item

    assert admin_events_resp.status_code == 200
    admin_event = admin_events_resp.json()["items"][0]
    assert admin_event["user_text"] == "查物流"
    assert admin_event["assistant_text"] == "好的"
    assert admin_event["raw_text"] == "raw private event text"

    assert admin_items_resp.status_code == 200
    admin_item = admin_items_resp.json()["items"][0]
    assert admin_item["content"] == "重点客户"
    assert admin_item["original_text"] == "raw original note"
    assert admin_item["value_json"] == {"raw": "private value"}
    assert admin_item["raw_text"] == "raw private item text"


@pytest.mark.asyncio
@pytest.mark.parametrize("acceptance_status", ["candidate", "needs_review", "rejected"])
async def test_memory_router_non_admin_group_graph_review_statuses_are_denied(acceptance_status: str) -> None:
    app = FastAPI()
    store = _FakeStore()
    app.include_router(build_memory_router(store))

    transport = httpx.ASGITransport(app=app)
    params = {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "session_id": "group-1@chatroom",
        "acceptance_status": acceptance_status,
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        forbidden_resp = await client.get("/group-graph", params=params)
        admin_resp = await client.get(
            "/group-graph",
            headers={"Authorization": "Bearer admin_token"},
            params=params,
        )

    assert forbidden_resp.status_code == 401
    assert admin_resp.status_code == 200
    assert store.group_graph_calls[-1]["acceptance_status"] == acceptance_status


@pytest.mark.asyncio
async def test_memory_router_allows_active_group_member_safe_graph_read() -> None:
    app = FastAPI()
    store = _FakeStore()

    async def is_member(tenant_id: str, session_id: str, user_id: str) -> bool:
        return (tenant_id, session_id, user_id) == (
            "demo",
            "group-1@chatroom",
            "wxid_a",
        )

    app.include_router(
        build_memory_router(store, group_membership_authorizer=is_member)
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        allowed = await client.get(
            "/group-graph",
            headers={"X-User-Id": "wxid_a"},
            params={
                "tenant_id": "demo",
                "session_id": "group-1@chatroom",
                "acceptance_status": "accepted",
            },
        )
        denied = await client.get(
            "/group-graph",
            headers={"X-User-Id": "wxid_b"},
            params={
                "tenant_id": "demo",
                "session_id": "group-1@chatroom",
                "acceptance_status": "accepted",
            },
        )

    assert allowed.status_code == 200
    assert "private raw relation evidence" not in str(allowed.json())
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_memory_router_control_endpoints_require_matching_session_id() -> None:
    app = FastAPI()
    store = _SessionScopedControlStore()
    app.include_router(build_memory_router(store))

    base = {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "item_id": 8,
    }
    headers = {"X-User-Id": "wxid_a"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        forget_no_session = await client.post(
            "/forget",
            headers={**headers, "Idempotency-Key": "forget-no-session"},
            json={**base, "allow_pinned": True},
        )
        forget_wrong_session = await client.post(
            "/forget",
            headers={**headers, "Idempotency-Key": "forget-wrong-session"},
            json={**base, "session_id": "session-b", "allow_pinned": True},
        )
        forget_matching_session = await client.post(
            "/forget",
            headers={**headers, "Idempotency-Key": "forget-matching-session"},
            json={**base, "session_id": "session-a", "allow_pinned": True},
        )
        update_no_session = await client.post(
            "/update",
            headers=headers,
            json={**base, "content": "不该改"},
        )
        update_wrong_session = await client.post(
            "/update",
            headers=headers,
            json={**base, "session_id": "session-b", "content": "不该改"},
        )
        update_matching_session = await client.post(
            "/update",
            headers=headers,
            json={**base, "session_id": "session-a", "content": "可以改"},
        )

    assert forget_no_session.status_code == 200
    assert forget_no_session.json()["count"] == 0
    assert forget_wrong_session.status_code == 200
    assert forget_wrong_session.json()["count"] == 0
    assert forget_matching_session.status_code == 200
    assert forget_matching_session.json()["ids"] == [8]

    assert update_no_session.status_code == 404
    assert update_wrong_session.status_code == 404
    assert update_matching_session.status_code == 200
    assert update_matching_session.json()["item"]["content"] == "可以改"

    assert [call["session_id"] for call in store.forget_calls] == ["", "session-b", "session-a"]
    assert [call["session_id"] for call in store.update_calls] == ["", "session-b", "session-a"]


@pytest.mark.asyncio
async def test_memory_router_extraction_job_filters_and_stats_are_safe() -> None:
    app = FastAPI()
    store = _FakeStore()
    app.include_router(build_memory_router(store))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        jobs_resp = await client.get(
            "/extraction-jobs",
            headers={"Authorization": "Bearer admin_token"},
            params={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "user_id": "wxid_a",
                "session_id": "s1",
                "status": "failed",
                "error_type": "TimeoutError",
                "created_after": "2026-05-01T00:00:00",
                "updated_before": "2026-05-12T00:00:00",
                "limit": 25,
            },
        )
        stats_resp = await client.get(
            "/extraction-jobs/stats",
            headers={"Authorization": "Bearer admin_token"},
            params={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "user_id": "wxid_a",
                "session_id": "s1",
                "status": "failed",
                "error_type": "TimeoutError",
                "created_after": "2026-05-01T00:00:00",
                "updated_before": "2026-05-12T00:00:00",
                "limit": 10,
            },
        )

    assert jobs_resp.status_code == 200
    job = jobs_resp.json()["items"][0]
    assert job["error_type"] == "TimeoutError"
    assert "last_error" not in job
    assert "result_json" not in job
    assert store.job_list_calls[0]["error_type"] == "TimeoutError"
    assert store.job_list_calls[0]["created_after"] is not None
    assert store.job_list_calls[0]["updated_before"] is not None

    assert stats_resp.status_code == 200
    stats = stats_resp.json()
    assert stats["error_type_counts"] == {"TimeoutError": 4}
    assert stats["scope_counts"][0]["count"] == 4
    assert stats["retry_counts"]["retryable_failed"] == 3
    assert stats["dead_scope_counts"][0]["status"] == "dead"
    assert stats["retry_scope_counts"][0]["retryable"] is True
    assert stats["latency_seconds"]["pending_avg_age"] == 12.0
    assert stats["graph_result_counts"]["error"] == 1
    assert store.job_stats_calls[0]["limit"] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["mark_joke", "reject"])
async def test_memory_router_acceptance_review_requires_admin_and_calls_store(action: str) -> None:
    app = FastAPI()
    store = _FakeStore()
    app.include_router(build_memory_router(store))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        forbidden_resp = await client.post(
            "/items/7/acceptance-review?tenant_id=demo",
            json={"action": action, "review_reason": "bad memory", "reviewed_by": "ignored"},
        )
        resp = await client.post(
            "/items/7/acceptance-review?tenant_id=demo",
            headers={
                "Authorization": "Bearer admin_token",
                "Idempotency-Key": f"acceptance-{action}",
            },
            json={"action": action, "review_reason": "bad memory", "reviewed_by": "admin-test"},
        )

    assert forbidden_resp.status_code in {401, 403}
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["item"]["id"] == 7
    assert store.acceptance_review_calls == [{
        "item_id": 7,
        "action": action,
        "reason": "bad memory",
        "actor": "admin",
        "superseded_by_item_id": None,
        "supersedes_item_id": None,
    }]


@pytest.mark.asyncio
async def test_memory_router_acceptance_review_passes_supersede_ids() -> None:
    app = FastAPI()
    store = _FakeStore()
    app.include_router(build_memory_router(store))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/items/7/acceptance-review?tenant_id=demo",
            headers={
                "Authorization": "Bearer admin_token",
                "Idempotency-Key": "acceptance-supersede-7",
            },
            json={
                "action": "supersede",
                "review_reason": "newer memory",
                "reviewed_by": "admin-test",
                "superseded_by_item_id": 8,
            },
        )

    assert resp.status_code == 200
    assert store.acceptance_review_calls == [{
        "item_id": 7,
        "action": "supersede",
        "reason": "newer memory",
        "actor": "admin",
        "superseded_by_item_id": 8,
        "supersedes_item_id": None,
    }]


@pytest.mark.asyncio
async def test_memory_high_risk_mutations_require_idempotency_key() -> None:
    app = FastAPI()
    store = _FakeStore()
    app.include_router(build_memory_router(store), prefix="/plugins/memory")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        forget = await client.post(
            "/plugins/memory/forget",
            headers={"X-User-Id": "wxid_a"},
            json={"tenant_id": "demo", "item_id": 4, "allow_pinned": True},
        )
        acceptance = await client.post(
            "/plugins/memory/items/7/acceptance-review?tenant_id=demo",
            headers={"Authorization": "Bearer admin_token"},
            json={"action": "accept"},
        )
        profile = await client.post(
            "/plugins/memory/profile-enrichment/candidates/20/review?tenant_id=demo",
            headers={"Authorization": "Bearer admin_token"},
            json={"action": "accept"},
        )
        delete = await client.delete("/plugins/memory/items/7?tenant_id=demo")

        acceptance_backfill = await client.post(
            "/plugins/memory/items/acceptance-legacy-backfill",
            headers={"Authorization": "Bearer admin_token"},
            json={"tenant_id": "demo", "dry_run": True, "max_items": 10},
        )
        maintenance = await client.post(
            "/plugins/memory/extraction-jobs/maintenance",
            headers={"Authorization": "Bearer admin_token"},
            json={"action": "retry", "tenant_id": "demo", "status": "failed"},
        )
        create = await client.post(
            "/plugins/memory/items",
            headers={"Authorization": "Bearer admin_token"},
            json={"tenant_id": "demo", "user_id": "wxid_a", "content": "safe note"},
        )
        update = await client.patch(
            "/plugins/memory/items/7?tenant_id=demo",
            headers={"Authorization": "Bearer admin_token"},
            json={"content": "updated safe note"},
        )
        backfill = await client.post(
            "/plugins/memory/backfill",
            headers={"Authorization": "Bearer admin_token"},
            json={
                "tenant_id": "demo",
                "connection_id": "legacy-wechat-default",
                "session_ids": ["group-1@chatroom"],
                "user_id": "wxid_a",
            },
        )
        item_vectors = await client.post(
            "/plugins/memory/items/vector-rebuild",
            headers={"Authorization": "Bearer admin_token"},
            json={"tenant_id": "demo", "dry_run": True},
        )
        graph_vectors = await client.post(
            "/plugins/memory/graph/vector-rebuild",
            headers={"Authorization": "Bearer admin_token"},
            json={"tenant_id": "demo", "dry_run": True},
        )

    for response in (
        forget,
        acceptance,
        profile,
        delete,
        acceptance_backfill,
        maintenance,
        create,
        update,
        backfill,
        item_vectors,
        graph_vectors,
    ):
        assert response.status_code == 428
        assert response.json()["detail"]["code"] == "idempotency_key_required"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        (
            "POST",
            "/plugins/memory/items",
            {"tenant_id": "demo", "user_id": "wxid_a", "content": "safe note"},
        ),
        (
            "PATCH",
            "/plugins/memory/items/7?tenant_id=demo",
            {"content": "updated safe note"},
        ),
        (
            "POST",
            "/plugins/memory/backfill",
            {
                "tenant_id": "demo",
                "connection_id": "legacy-wechat-default",
                "session_ids": ["group-1@chatroom"],
            },
        ),
        (
            "POST",
            "/plugins/memory/extraction-jobs/maintenance",
            {"action": "retry", "tenant_id": "demo", "status": "failed"},
        ),
        (
            "POST",
            "/plugins/memory/items/vector-rebuild",
            {"tenant_id": "demo", "dry_run": True},
        ),
        (
            "POST",
            "/plugins/memory/graph/vector-rebuild",
            {"tenant_id": "demo", "dry_run": True},
        ),
        (
            "POST",
            "/plugins/memory/items/acceptance-legacy-backfill",
            {"tenant_id": "demo", "dry_run": True, "max_items": 10},
        ),
    ],
)
async def test_memory_high_risk_mutations_reject_unknown_fields(
    method: str,
    path: str,
    payload: dict[str, object],
) -> None:
    app = FastAPI()
    store = _FakeStore()
    app.include_router(build_memory_router(store), prefix="/plugins/memory")
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.request(
            method,
            path,
            headers={
                "Authorization": "Bearer admin_token",
                "Idempotency-Key": f"unknown-field-{method.lower()}-{len(path)}",
            },
            json={**payload, "unexpected_field": "must-not-be-ignored"},
        )

    assert response.status_code == 422
    assert any(error.get("loc", [])[-1:] == ["unexpected_field"] for error in response.json()["detail"])


@pytest.mark.asyncio
async def test_memory_backfill_maps_idempotency_conflict_to_409() -> None:
    app = FastAPI()
    store = _FakeStore()

    async def raise_conflict(**_kwargs):
        raise MutationIdempotencyConflictError("same key, different request")

    store.backfill_from_sdk_idempotent = raise_conflict
    app.include_router(build_memory_router(store), prefix="/plugins/memory")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/plugins/memory/backfill",
            headers={
                "Authorization": "Bearer admin_token",
                "Idempotency-Key": "backfill-conflict",
            },
            json={
                "tenant_id": "demo",
                "connection_id": "legacy-wechat-default",
                "session_ids": ["group-1@chatroom"],
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "idempotency_key_conflict"


@pytest.mark.asyncio
async def test_memory_item_idempotent_mutations_map_member_write_block_to_stable_409() -> None:
    app = FastAPI()
    store = _FakeStore()

    async def raise_member_write_blocked(*_args, **_kwargs):
        raise MemoryMutationError("member_memory_write_blocked", status_code=409)

    store.create_memory_item_idempotent = raise_member_write_blocked
    store.update_memory_item_idempotent = raise_member_write_blocked
    app.include_router(build_memory_router(store), prefix="/plugins/memory")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/plugins/memory/items",
            headers={
                "Authorization": "Bearer admin_token",
                "Idempotency-Key": "blocked-create-item",
            },
            json={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "user_id": "wxid_member",
                "content": "must not be saved",
            },
        )
        update_resp = await client.patch(
            "/plugins/memory/items/20?tenant_id=demo",
            headers={
                "Authorization": "Bearer admin_token",
                "Idempotency-Key": "blocked-update-item",
            },
            json={"content": "must not be saved"},
        )

    assert create_resp.status_code == 409
    assert create_resp.json() == {"detail": "member_memory_write_blocked"}
    assert update_resp.status_code == 409
    assert update_resp.json() == {"detail": "member_memory_write_blocked"}


@pytest.mark.asyncio
async def test_memory_router_profile_enrichment_candidate_crud_requires_admin() -> None:
    app = FastAPI()
    store = _FakeStore()
    app.include_router(build_memory_router(store), prefix="/plugins/memory")

    body = {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "session_id": "group-1@chatroom",
        "user_id": "wxid_member",
        "report_payload": {
            "profile": {"display_names": ["Synthetic Member"], "summary": "Candidate only"},
            "review": {"state": "accepted"},
            "external_candidates": [{"binding_status": "candidate"}],
        },
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        forbidden_resp = await client.post("/plugins/memory/profile-enrichment/candidates", json=body)
        create_resp = await client.post(
            "/plugins/memory/profile-enrichment/candidates",
            headers={"Authorization": "Bearer admin_token", "X-Actor-ID": "admin-test"},
            json=body,
        )
        list_resp = await client.get(
            "/plugins/memory/profile-enrichment/candidates",
            headers={"Authorization": "Bearer admin_token"},
            params={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "session_id": "group-1@chatroom",
                "user_id": "wxid_member",
                "review_state": "needs_review",
            },
        )
        get_resp = await client.get(
            "/plugins/memory/profile-enrichment/candidates/20",
            headers={"Authorization": "Bearer admin_token"},
        )
        review_resp = await client.post(
            "/plugins/memory/profile-enrichment/candidates/20/review?tenant_id=demo",
            headers={
                "Authorization": "Bearer admin_token",
                "Idempotency-Key": "profile-review-20",
            },
            json={"action": "accept", "notes": "explicit synthetic review", "reviewed_by": "admin-test"},
        )

    assert forbidden_resp.status_code == 401
    assert create_resp.status_code == 200
    assert create_resp.json()["acceptance_status"] == "needs_review"
    assert list_resp.status_code == 200
    assert list_resp.json()["items"][0]["acceptance_status"] == "needs_review"
    assert get_resp.status_code == 200
    assert review_resp.status_code == 200
    assert review_resp.json()["acceptance_status"] == "accepted"
    assert store.profile_candidate_create_calls[0]["created_by"] == "admin-test"
    assert store.profile_candidate_create_calls[0]["report_payload"]["review"]["state"] == "accepted"
    assert store.profile_candidate_list_calls[0]["review_state"] == "needs_review"
    assert store.profile_candidate_review_calls == [{
        "candidate_id": 20,
        "action": "accept",
        "notes": "explicit synthetic review",
        "reviewed_by": "admin",
    }]


@pytest.mark.asyncio
async def test_profile_enrichment_mutations_map_member_write_block_to_stable_409() -> None:
    app = FastAPI()
    store = _FakeStore()

    async def raise_member_write_blocked(*_args, **_kwargs):
        raise MemoryMutationError("member_memory_write_blocked", status_code=409)

    store.create_profile_enrichment_candidate = raise_member_write_blocked
    store.review_profile_enrichment_candidate_idempotent = raise_member_write_blocked
    app.include_router(build_memory_router(store), prefix="/plugins/memory")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        create_resp = await client.post(
            "/plugins/memory/profile-enrichment/candidates",
            headers={"Authorization": "Bearer admin_token"},
            json={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "session_id": "group-1@chatroom",
                "user_id": "wxid_member",
                "report_payload": {"profile": {"summary": "must not be saved"}},
            },
        )
        review_resp = await client.post(
            "/plugins/memory/profile-enrichment/candidates/20/review?tenant_id=demo",
            headers={
                "Authorization": "Bearer admin_token",
                "Idempotency-Key": "blocked-profile-review",
            },
            json={"action": "accept"},
        )

    assert create_resp.status_code == 409
    assert create_resp.json() == {"detail": "member_memory_write_blocked"}
    assert review_resp.status_code == 409
    assert review_resp.json() == {"detail": "member_memory_write_blocked"}


@pytest.mark.asyncio
async def test_memory_router_profile_enrichment_from_report_generates_and_saves_candidate() -> None:
    app = FastAPI()
    store = _FakeStore()
    builder_calls: list[dict[str, object]] = []

    async def build_report(session, arguments):
        builder_calls.append({
            "tenant_id": session.tenant_id,
            "channel": session.channel,
            "source_key": session.source_key,
            "session_id": session.session_id,
            "user_id": session.user_id,
            "arguments": arguments,
        })
        return {
            "profile": {
                "display_names": ["Synthetic Member"],
                "summary": "Candidate only; contact synthetic@example.com",
            },
            "review": {"state": "accepted"},
            "external_candidates": [{"binding_status": "matched"}],
        }

    app.include_router(
        build_memory_router(store, profile_report_builder=build_report),
        prefix="/plugins/memory",
    )
    body = {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "session_id": "group-1@chatroom",
        "user_id": "wxid_member",
        "query": "Synthetic Member",
        "hours": 168,
        "limit": 8,
        "external_candidates": [{"platform": "github", "display_name": "Synthetic Member"}],
    }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        forbidden_resp = await client.post("/plugins/memory/profile-enrichment/candidates/from-report", json=body)
        create_resp = await client.post(
            "/plugins/memory/profile-enrichment/candidates/from-report",
            headers={"Authorization": "Bearer admin_token", "X-Actor-ID": "admin-test"},
            json=body,
        )

    assert forbidden_resp.status_code == 401
    assert create_resp.status_code == 200
    assert create_resp.json()["acceptance_status"] == "needs_review"
    assert builder_calls == [{
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "session_id": "group-1@chatroom",
        "user_id": "wxid_member",
        "arguments": {
            "query": "Synthetic Member",
            "user_id": "wxid_member",
            "hours": 168,
            "limit": 8,
            "external_candidates": [{"platform": "github", "display_name": "Synthetic Member"}],
        },
    }]
    assert store.profile_candidate_create_calls[0]["created_by"] == "admin-test"
    assert store.profile_candidate_create_calls[0]["report_payload"]["review"]["state"] == "accepted"


@pytest.mark.asyncio
async def test_profile_enrichment_from_report_blocks_before_report_builder() -> None:
    app = FastAPI()
    store = _FakeStore()
    preflight_calls: list[dict[str, object]] = []
    builder_calls = 0

    async def member_memory_write_blocked(**kwargs):
        preflight_calls.append(kwargs)
        return True

    async def build_report(_session, _arguments):
        nonlocal builder_calls
        builder_calls += 1
        return {"profile": {"summary": "must not be built"}}

    store.member_memory_write_blocked = member_memory_write_blocked
    app.include_router(
        build_memory_router(store, profile_report_builder=build_report),
        prefix="/plugins/memory",
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/plugins/memory/profile-enrichment/candidates/from-report",
            headers={"Authorization": "Bearer admin_token"},
            json={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "session_id": "group-1@chatroom",
                "user_id": "wxid_member",
                "query": "Synthetic Member",
            },
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "member_memory_write_blocked"}
    assert preflight_calls == [
        {
            "tenant_id": "demo",
            "user_id": "wxid_member",
            "channel": "wechat",
        }
    ]
    assert builder_calls == 0
    assert store.profile_candidate_create_calls == []


@pytest.mark.asyncio
async def test_profile_enrichment_from_report_maps_post_builder_write_race_to_409() -> None:
    app = FastAPI()
    store = _FakeStore()
    builder_calls = 0

    async def build_report(_session, _arguments):
        nonlocal builder_calls
        builder_calls += 1
        return {"profile": {"summary": "candidate"}}

    async def raise_member_write_blocked(**_kwargs):
        raise MemoryMutationError("member_memory_write_blocked", status_code=409)

    store.create_profile_enrichment_candidate = raise_member_write_blocked
    app.include_router(
        build_memory_router(store, profile_report_builder=build_report),
        prefix="/plugins/memory",
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/plugins/memory/profile-enrichment/candidates/from-report",
            headers={"Authorization": "Bearer admin_token"},
            json={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "session_id": "group-1@chatroom",
                "user_id": "wxid_member",
                "query": "Synthetic Member",
            },
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "member_memory_write_blocked"}
    assert builder_calls == 1


@pytest.mark.asyncio
async def test_memory_router_profile_enrichment_from_report_requires_builder() -> None:
    app = FastAPI()
    store = _FakeStore()
    app.include_router(build_memory_router(store), prefix="/plugins/memory")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/plugins/memory/profile-enrichment/candidates/from-report",
            headers={"Authorization": "Bearer admin_token"},
            json={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "session_id": "group-1@chatroom",
                "user_id": "wxid_member",
                "query": "Synthetic Member",
            },
        )

    assert resp.status_code == 503
    assert store.profile_candidate_create_calls == []


@pytest.mark.asyncio
async def test_memory_router_profile_enrichment_review_rejects_invalid_state_and_action() -> None:
    app = FastAPI()
    store = _FakeStore()
    app.include_router(build_memory_router(store), prefix="/plugins/memory")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        list_resp = await client.get(
            "/plugins/memory/profile-enrichment/candidates",
            headers={"Authorization": "Bearer admin_token"},
            params={"tenant_id": "demo", "review_state": "active"},
        )
        review_resp = await client.post(
            "/plugins/memory/profile-enrichment/candidates/20/review?tenant_id=demo",
            headers={"Authorization": "Bearer admin_token"},
            json={"action": "needs_review"},
        )

    assert list_resp.status_code == 400
    assert review_resp.status_code == 400


@pytest.mark.asyncio
async def test_memory_router_group_graph_evidence_safe_default_and_admin_raw() -> None:
    app = FastAPI()
    store = _FakeStore()
    app.include_router(build_memory_router(store), prefix="/plugins/memory")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        forbidden_resp = await client.get(
            "/plugins/memory/group-graph/evidence/fact:12",
            params={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "session_id": "group-1@chatroom",
            },
        )
        safe_resp = await client.get(
            "/plugins/memory/group-graph/evidence/fact:12",
            headers={"Authorization": "Bearer admin_token"},
            params={
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "session_id": "group-1@chatroom",
            },
        )
        raw_forbidden_resp = await client.get(
            "/plugins/memory/group-graph/evidence/fact:12",
            params={"tenant_id": "demo", "raw": True},
        )
        raw_resp = await client.get(
            "/plugins/memory/group-graph/evidence/fact:12",
            headers={"Authorization": "Bearer admin_token"},
            params={"tenant_id": "demo", "raw": True},
        )

    assert forbidden_resp.status_code == 401
    assert safe_resp.status_code == 200
    safe_payload = safe_resp.json()
    assert safe_payload["schema"]["version"] == "group-graph.v1"
    assert safe_payload["evidence_ids"]["memory_item_ids"] == [201]
    assert safe_payload["evidence_counts"] == {"memory_items": 1, "events": 1, "episodes": 1}
    serialized_safe = str(safe_payload)
    assert "content" not in serialized_safe
    assert "original_text" not in serialized_safe
    assert "user_text" not in serialized_safe
    assert "assistant_text" not in serialized_safe
    assert "summary" not in serialized_safe
    assert "private raw" not in serialized_safe

    assert raw_forbidden_resp.status_code == 401
    assert raw_resp.status_code == 200
    raw_payload = raw_resp.json()
    assert raw_payload["raw"]["events"][0]["user_text"] == "private user text"
    assert raw_payload["raw"]["fact"]["object_value"] == "private raw graph value"


@pytest.mark.asyncio
async def test_memory_router_daily_group_relationship_extraction_requires_admin_and_is_safe() -> None:
    app = FastAPI()
    store = _FakeStore()
    app.include_router(build_memory_router(store), prefix="/plugins/memory")

    transport = httpx.ASGITransport(app=app)
    body = {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "session_id": "room-a@chatroom",
        "date": "2026-05-15",
        "limit": 7,
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        forbidden_resp = await client.post("/plugins/memory/group-graph/extract-daily", json=body)
        resp = await client.post(
            "/plugins/memory/group-graph/extract-daily",
            headers={"Authorization": "Bearer admin_token"},
            json=body,
        )

    assert forbidden_resp.status_code == 401
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "rule_only"
    assert payload["skipped_reason"] == "no_llm"
    assert payload["counts"]["imported_messages"] == 2
    assert payload["job_counts_before"]["pending"] == 1
    assert payload["job_counts_after"]["succeeded"] == 3
    assert payload["jobs"]["claimed"] == 1
    assert payload["jobs"]["batches"] == 1
    assert payload["controls"]["batch_limit"] == 7
    assert payload["controls"]["max_jobs"] == 7
    assert payload["controls"]["continuous"] is False
    assert payload["controls"]["time_budget_seconds"] == 60
    assert payload["controls"]["stop_reason"] == "single_batch_complete"
    assert payload["more_remain"] is False
    assert payload["source_event_ids"] == [101, 102]
    assert store.daily_relationship_calls == [{
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "session_id": "room-a@chatroom",
        "user_id": None,
        "date": "2026-05-15",
        "batch_limit": 7,
        "max_jobs": 7,
        "continuous": False,
        "time_budget_seconds": 60,
    }]
    serialized = str(payload)
    assert "content" not in serialized
    assert "user_text" not in serialized
    assert "RAW_FIELD_SENTINEL" not in serialized


@pytest.mark.asyncio
async def test_memory_router_daily_group_relationship_extraction_passes_batch_controls() -> None:
    app = FastAPI()
    store = _FakeStore()
    app.include_router(build_memory_router(store), prefix="/plugins/memory")

    transport = httpx.ASGITransport(app=app)
    body = {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "session_id": "room-a@chatroom",
        "date": "2026-05-15",
        "batch_limit": 500,
        "continuous": True,
        "max_jobs": 900,
        "time_budget_seconds": 500,
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/plugins/memory/group-graph/extract-daily",
            headers={"Authorization": "Bearer admin_token"},
            json=body,
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["controls"]["batch_limit"] == 100
    assert payload["controls"]["max_jobs"] == 500
    assert payload["controls"]["continuous"] is True
    assert payload["controls"]["time_budget_seconds"] == 180
    assert store.daily_relationship_calls == [{
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "session_id": "room-a@chatroom",
        "user_id": None,
        "date": "2026-05-15",
        "batch_limit": 100,
        "max_jobs": 500,
        "continuous": True,
        "time_budget_seconds": 180,
    }]


@pytest.mark.asyncio
async def test_memory_router_window_group_relationship_extraction_requires_admin_scrubs_and_passes_controls() -> None:
    app = FastAPI()
    store = _FakeStore()
    app.include_router(build_memory_router(store), prefix="/plugins/memory")

    transport = httpx.ASGITransport(app=app)
    body = {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "session_id": "room-a@chatroom",
        "date": "2026-05-15",
        "window_size": 500,
        "max_windows": 50,
        "cursor_event_id": -10,
        "dry_run": True,
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        forbidden_resp = await client.post("/plugins/memory/group-graph/extract-window", json=body)
        resp = await client.post(
            "/plugins/memory/group-graph/extract-window",
            headers={"Authorization": "Bearer admin_token"},
            json=body,
        )

    assert forbidden_resp.status_code == 401
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "completed"
    assert payload["controls"] == {
        "window_size": 100,
        "max_windows": 10,
        "cursor_event_id": 0,
        "dry_run": True,
    }
    assert payload["totals"]["applied"] == 1
    assert store.window_relationship_calls == [{
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "session_id": "room-a@chatroom",
        "user_id": None,
        "date": "2026-05-15",
        "window_size": 100,
        "max_windows": 10,
        "cursor_event_id": 0,
        "dry_run": True,
    }]
    serialized = str(payload)
    assert "content" not in serialized
    assert "value_json" not in serialized
    assert "user_text" not in serialized
    assert "RAW_FIELD_SENTINEL" not in serialized


@pytest.mark.asyncio
async def test_memory_router_window_catchup_requires_admin_scrubs_and_passes_controls() -> None:
    app = FastAPI()
    store = _FakeStore()
    app.include_router(build_memory_router(store), prefix="/plugins/memory")

    transport = httpx.ASGITransport(app=app)
    body = {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "session_id": "room-a@chatroom",
        "date": "2026-05-15",
        "window_size": 500,
        "max_windows_per_run": 500,
        "cursor_event_id": -10,
        "dry_run": True,
        "time_budget_seconds": 999,
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        forbidden_resp = await client.post("/plugins/memory/group-graph/extract-window-catchup", json=body)
        resp = await client.post(
            "/plugins/memory/group-graph/extract-window-catchup",
            headers={"Authorization": "Bearer admin_token"},
            json=body,
        )

    assert forbidden_resp.status_code == 401
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["controls"] == {
        "window_size": 100,
        "max_windows_per_run": 100,
        "cursor_event_id": 0,
        "dry_run": True,
        "time_budget_seconds": 180,
    }
    assert payload["stop_reason"] == "no_more_events"
    assert store.window_catchup_calls == [{
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "session_id": "room-a@chatroom",
        "user_id": None,
        "date": "2026-05-15",
        "window_size": 100,
        "max_windows_per_run": 100,
        "cursor_event_id": 0,
        "dry_run": True,
        "time_budget_seconds": 180,
    }]
    serialized = str(payload)
    assert "content" not in serialized
    assert "RAW_FIELD_SENTINEL" not in serialized


@pytest.mark.asyncio
async def test_memory_router_window_stats_requires_admin_and_scrubs() -> None:
    app = FastAPI()
    store = _FakeStore()
    app.include_router(build_memory_router(store), prefix="/plugins/memory")

    transport = httpx.ASGITransport(app=app)
    params = {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "session_id": "room-a@chatroom",
        "date": "2026-05-15",
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        forbidden_resp = await client.get("/plugins/memory/group-graph/window-stats", params=params)
        resp = await client.get(
            "/plugins/memory/group-graph/window-stats",
            headers={"Authorization": "Bearer admin_token"},
            params=params,
        )

    assert forbidden_resp.status_code == 401
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["totals"]["items"] == 1
    assert store.window_stats_calls == [{
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "session_id": "room-a@chatroom",
        "user_id": None,
        "date": "2026-05-15",
    }]
    serialized = str(payload)
    assert "content" not in serialized
    assert "RAW_FIELD_SENTINEL" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "expected_acceptance"),
    [("accept", "accepted"), ("reject", "rejected")],
)
async def test_memory_router_edge_review_requires_admin_and_returns_safe_dto(
    action: str,
    expected_acceptance: str,
) -> None:
    app = FastAPI()
    store = _FakeStore()
    app.include_router(build_memory_router(store), prefix="/plugins/memory")

    transport = httpx.ASGITransport(app=app)
    body = {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "session_id": "group-1@chatroom",
        "action": action,
        "review_reason": "edge review",
        "reviewed_by": "admin-test",
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        forbidden_resp = await client.post(
            "/plugins/memory/group-graph/edges/fact:12/acceptance-review",
            json=body,
        )
        resp = await client.post(
            "/plugins/memory/group-graph/edges/fact:12/acceptance-review",
            headers={"Authorization": "Bearer admin_token"},
            json=body,
        )

    assert forbidden_resp.status_code == 401
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["edge_id"] == "fact:12"
    assert payload["result"]["memory_item_ids"] == [201]
    assert payload["result"]["item_statuses"][0]["acceptance_status"] == expected_acceptance
    assert store.edge_review_calls == [{
        "edge_id": "fact:12",
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "session_id": "group-1@chatroom",
        "action": action,
        "review_reason": "edge review",
        "reviewed_by": "admin-test",
        "superseded_by_item_id": None,
        "supersedes_item_id": None,
    }]
    serialized = str(payload)
    assert "content" not in serialized
    assert "user_text" not in serialized
    assert "RAW_FIELD_SENTINEL" not in serialized


@pytest.mark.asyncio
async def test_memory_router_extraction_job_admin_paths_with_plugin_prefix_are_registered() -> None:
    app = FastAPI()
    store = _FakeStore()
    app.include_router(build_memory_router(store), prefix="/plugins/memory")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        stats_resp = await client.get(
            "/plugins/memory/extraction-jobs/stats",
            headers={"Authorization": "Bearer admin_token"},
        )
        maintenance_forbidden_resp = await client.post(
            "/plugins/memory/extraction-jobs/maintenance",
            json={"action": "retry", "tenant_id": "demo"},
        )
        maintenance_dry_run_resp = await client.post(
            "/plugins/memory/extraction-jobs/maintenance",
            headers={
                "Authorization": "Bearer admin_token",
                "Idempotency-Key": "maintenance-preview",
            },
            json={"action": "retry", "tenant_id": "demo", "status": "failed"},
        )

    assert stats_resp.status_code == 200
    assert maintenance_forbidden_resp.status_code == 401
    assert maintenance_forbidden_resp.status_code != 404
    assert maintenance_dry_run_resp.status_code == 200
    assert maintenance_dry_run_resp.json()["dry_run"] is True


@pytest.mark.asyncio
async def test_memory_router_extraction_job_maintenance_requires_admin_and_filters() -> None:
    app = FastAPI()
    store = _FakeStore()
    app.include_router(build_memory_router(store))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        forbidden_resp = await client.post(
            "/extraction-jobs/maintenance",
            json={"action": "retry", "tenant_id": "demo"},
        )
        no_filter_resp = await client.post(
            "/extraction-jobs/maintenance",
            headers={"Authorization": "Bearer admin_token"},
            json={"action": "retry"},
        )
        write_no_filter_resp = await client.post(
            "/extraction-jobs/maintenance",
            headers={"Authorization": "Bearer admin_token"},
            json={"action": "reset_stale", "dry_run": False},
        )
        dry_run_resp = await client.post(
            "/extraction-jobs/maintenance",
            headers={
                "Authorization": "Bearer admin_token",
                "Idempotency-Key": "maintenance-filtered-preview",
            },
            json={"action": "retry", "tenant_id": "demo", "status": "failed", "limit": 100},
        )

    assert forbidden_resp.status_code == 401
    assert no_filter_resp.status_code == 400
    assert write_no_filter_resp.status_code == 400
    assert "filter" in write_no_filter_resp.json()["detail"]
    assert dry_run_resp.status_code == 200
    assert dry_run_resp.json()["dry_run"] is True
    assert store.job_maintenance_calls == [{
        "actions": ["retry"],
        "dry_run": True,
        "limit": 100,
        "tenant_id": "demo",
        "status": "failed",
    }]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "payload"),
    [
        ("retry", {"status": "not-a-status"}),
        ("retry", {"tenant_id": ""}),
        ("mark_dead", {"status": "not-a-status"}),
        ("mark_dead", {"tenant_id": ""}),
    ],
)
async def test_memory_router_extraction_job_maintenance_rejects_ineffective_filters(
    action: str,
    payload: dict[str, str],
) -> None:
    app = FastAPI()
    store = _FakeStore()
    app.include_router(build_memory_router(store))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/extraction-jobs/maintenance",
            headers={"Authorization": "Bearer admin_token"},
            json={"action": action, "dry_run": False, **payload},
        )

    assert resp.status_code == 422
    assert store.job_maintenance_calls == []


@pytest.mark.asyncio
async def test_memory_router_extraction_job_maintenance_cleanup_requires_smoke_scope() -> None:
    app = FastAPI()
    store = _FakeStore()
    app.include_router(build_memory_router(store))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unsafe_cleanup_resp = await client.post(
            "/extraction-jobs/maintenance",
            headers={"Authorization": "Bearer admin_token"},
            json={"action": "cleanup_smoke", "tenant_id": "demo", "dry_run": False},
        )
        smoke_cleanup_resp = await client.post(
            "/extraction-jobs/maintenance",
            headers={
                "Authorization": "Bearer admin_token",
                "Idempotency-Key": "cleanup-demo-smoke",
            },
            json={"action": "cleanup_smoke", "tenant_id": "demo-smoke", "dry_run": False},
        )

    assert unsafe_cleanup_resp.status_code == 400
    assert smoke_cleanup_resp.status_code == 200
    assert store.job_maintenance_calls[0]["actions"] == ["cleanup_smoke"]
    assert store.job_maintenance_calls[0]["dry_run"] is False
    assert store.job_maintenance_calls[0]["tenant_id"] == "demo-smoke"
