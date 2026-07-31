"""Durable LLM extraction job queue, worker, retry, and maintenance operations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime
from typing import Any
from uuid import uuid4

from plugins.memory import store as _store_runtime
from plugins.memory.graph_extractor import is_safe_graph_llm_context_item
from plugins.memory.store import (
    _LLM_JOB_SCOPE_GROUP_KEYS,
    GRAPH_LLM_BACKING_SOURCE_TYPE,
    GROUP_HISTORY_USER_ID_SCOPE,
    MEMORY_EXTRACTION_JOB_STATUSES,
    _clamp_int,
    _job_idempotency_key,
    _llm_job_error_type_expr,
    _llm_job_filter_sql,
    _llm_job_scope_filter_sql,
    _llm_job_scope_is_smoke_sql,
    _memory_item_matches_audience_contract,
    _memory_item_matches_scope,
    _normalize_memory_audience_contract,
    _safe_json_loads,
    _safe_llm_job_result_json,
    _sanitize_db_text,
    _semantic_key,
    _settings_bool,
    _settings_float,
    _settings_int,
    _to_json,
    _truncate_error,
    _worker_id,
    logger,
)


async def _exec(sql: str, params: dict | None = None) -> list[dict]:
    return await _store_runtime._exec(sql, params)


class _MemoryScopeExecutionDenied(RuntimeError):
    """Internal control-flow signal for a fail-closed execution gate."""


class _MemoryJobClaimLost(RuntimeError):
    """Internal signal raised before a stale extraction worker may write."""


async def _require_memory_scope_execution(
    scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None,
    *,
    tenant_id: str,
    session_id: str,
    job_id: int | None = None,
) -> None:
    if not callable(scope_execution_allowed):
        logger.error(
            "memory.llm_job_scope_gate_missing",
            job_id=job_id,
            tenant_id=tenant_id,
            session_id=session_id,
        )
        raise _MemoryScopeExecutionDenied("scope execution gate unavailable")
    try:
        allowed = await scope_execution_allowed(tenant_id, session_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "memory.llm_job_scope_gate_failed",
            job_id=job_id,
            tenant_id=tenant_id,
            session_id=session_id,
            error_type=exc.__class__.__name__,
        )
        raise _MemoryScopeExecutionDenied("scope execution gate failed") from exc
    if allowed is not True:
        raise _MemoryScopeExecutionDenied("scope execution denied")


async def _require_memory_job_execution(
    scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None,
    claim_lease_renew: Callable[[], Awaitable[bool]] | None,
    *,
    tenant_id: str,
    session_id: str,
    job_id: int | None = None,
) -> None:
    await _require_memory_scope_execution(
        scope_execution_allowed,
        tenant_id=tenant_id,
        session_id=session_id,
        job_id=job_id,
    )
    if job_id is None and claim_lease_renew is None:
        return
    if not callable(claim_lease_renew):
        raise _MemoryJobClaimLost("job claim gate unavailable")
    try:
        current = await claim_lease_renew()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "memory.llm_job_claim_renew_failed",
            job_id=job_id,
            error_type=exc.__class__.__name__,
        )
        raise _MemoryJobClaimLost("job claim renewal failed") from exc
    if current is not True:
        raise _MemoryJobClaimLost("job claim is no longer current")


async def _settle_under_repeated_cancellation(awaitable: Awaitable[Any]) -> Any:
    """Finish claim cleanup even if the caller is cancelled more than once."""

    task = asyncio.ensure_future(awaitable)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


_LLM_JOB_LOCK_OWNER_MAX_LENGTH = 128
_LLM_JOB_RESULT_CLAIM_TOKEN_KEY = "_claim_token"


def _new_llm_job_claim_token(settings: Any, worker_id: str | None) -> tuple[str, str]:
    """Return the display owner and a unique token stored in ``locked_by``.

    ``locked_by`` predates explicit claim-token columns and is bounded to 128
    characters.  Keeping the worker id as a prefix preserves operational
    provenance while the UUID suffix fences every new claim from stale workers.
    """

    owner = str(worker_id or _worker_id(settings) or "worker").strip() or "worker"
    nonce = uuid4().hex
    owner_limit = _LLM_JOB_LOCK_OWNER_MAX_LENGTH - len(nonce) - 1
    return owner, f"{owner[:owner_limit]}:{nonce}"


def _llm_job_claim_token(
    job: dict[str, Any],
    *,
    worker_id: str | None,
    settings: Any,
) -> str:
    """Resolve the fencing token, retaining support for pre-token claims."""

    token = str(job.get("claim_token") or job.get("locked_by") or "").strip()
    if token:
        return token[:_LLM_JOB_LOCK_OWNER_MAX_LENGTH]
    # Rolling deployments can still hand us a row claimed by the legacy
    # worker-id-only format.  New claims always include the UUID suffix.
    return str(worker_id or _worker_id(settings) or "").strip()[:_LLM_JOB_LOCK_OWNER_MAX_LENGTH]


def _llm_job_has_claim(job: dict[str, Any]) -> bool:
    """Return whether the claimed row itself carries ownership evidence."""

    return bool(str(job.get("claim_token") or job.get("locked_by") or "").strip())


async def _llm_job_transition_result(
    *,
    job_id: int,
    claim_token: str,
    intended_status: str,
    updated_rows: list[dict[str, Any]],
) -> str:
    """Resolve a fenced write as applied, idempotent replay, or stale."""

    if updated_rows:
        return intended_status
    observed = await _llm_job_observed_result(job_id, claim_token=claim_token)
    return intended_status if observed == intended_status else "stale"


async def _llm_job_observed_result(job_id: int, *, claim_token: str) -> str:
    """Return this claim's terminal state, otherwise report a stale claim."""

    claim_token = str(claim_token or "").strip()
    if not claim_token:
        return "stale"
    rows = await _exec(
        "SELECT status, locked_by, result_json FROM plugin_memory_extraction_job "
        "WHERE id = :id LIMIT 1",
        {"id": job_id},
    )
    if rows:
        current_status = str(rows[0].get("status") or "").strip().lower()
        current_token = str(rows[0].get("locked_by") or "").strip()
        result = _safe_json_loads(rows[0].get("result_json"), {})
        completed_token = (
            str(result.get(_LLM_JOB_RESULT_CLAIM_TOKEN_KEY) or "").strip()
            if isinstance(result, dict)
            else ""
        )
        if (
            current_status in {"succeeded", "failed", "dead"}
            and not current_token
            and completed_token == claim_token
        ):
            return current_status
    return "stale"


class MemoryExtractionJobStoreMixin:
    async def enqueue_llm_extraction_job(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        session_id: str,
        trace_id: str,
        source_event_id: int | None,
        idempotency_key: str | None = None,
        origin_session_kind: str = "private",
        audience_scope: str = "private",
        allowed_session_ids: Iterable[str] | None = None,
        sensitivity_category: str = "normal",
        expires_at: datetime | str | None = None,
        source_kind: str = "conversation",
    ) -> dict[str, Any] | None:
        if not _settings_bool(self.settings, "memory_llm_extraction_job_enabled", True):
            return None
        key = idempotency_key or _job_idempotency_key(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            trace_id=trace_id,
            source_event_id=source_event_id,
        )
        max_attempts = _settings_int(
            self.settings,
            "memory_llm_extraction_job_max_attempts",
            3,
            minimum=1,
        )
        audience = _normalize_memory_audience_contract(
            origin_session_kind=origin_session_kind,
            audience_scope=audience_scope,
            allowed_session_ids=allowed_session_ids,
            session_id=session_id,
            expires_at=expires_at,
        )
        audience_payload = {
            **audience,
            "expires_at": (
                audience["expires_at"].isoformat()
                if isinstance(audience.get("expires_at"), datetime)
                else None
            ),
            "sensitivity_category": str(sensitivity_category or "normal"),
            "source_kind": str(source_kind or "conversation"),
        }
        rows = await _exec(
            "INSERT INTO plugin_memory_extraction_job "
            "(tenant_id, channel, source_key, user_id, session_id, source_event_id, "
            "source_trace_id, status, attempts, max_attempts, next_run_at, result_json, idempotency_key, "
            "created_at, updated_at) "
            "VALUES (:tid, :channel, :source_key, :uid, :sid, :source_event_id, :trace, "
            "'pending', 0, :max_attempts, NOW(), :result_json, :idempotency_key, NOW(), NOW()) "
            "ON CONFLICT (idempotency_key) DO UPDATE SET updated_at = plugin_memory_extraction_job.updated_at "
            "RETURNING id, tenant_id, channel, source_key, user_id, session_id, source_event_id, "
            "source_trace_id, status, attempts, max_attempts, next_run_at, locked_until, "
            "locked_by, last_error, result_json, idempotency_key, created_at, updated_at",
            {
                "tid": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "uid": user_id,
                "sid": session_id,
                "source_event_id": source_event_id,
                "trace": trace_id,
                "max_attempts": max_attempts,
                "result_json": _to_json({"audience": audience_payload}),
                "idempotency_key": key,
            },
        )
        job = rows[0] if rows else None
        if job and str(job.get("status") or "") != "pending":
            return job
        if job:
            logger.info(
                "memory.llm_job_enqueued",
                job_id=job.get("id"),
                status=job.get("status"),
                attempts=job.get("attempts"),
                graph_enabled=self.graph_extractor.config.enabled,
            )
        return job

    async def _enhance_memory_with_llm(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        session_id: str,
        user_text: str,
        assistant_text: str,
        trace_id: str,
        source_event_id: int | None,
        origin_session_kind: str = "private",
        audience_scope: str = "private",
        allowed_session_ids: Iterable[str] | None = None,
        sensitivity_category: str = "normal",
        expires_at: datetime | str | None = None,
        source_kind: str = "conversation",
        scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None = None,
        claim_lease_renew: Callable[[], Awaitable[bool]] | None = None,
        job_id: int | None = None,
    ) -> int:
        user_text = _sanitize_db_text(user_text)
        assistant_text = _sanitize_db_text(assistant_text)
        existing_items = await self.list_memory_items(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id="",
            scope_type="identity",
            limit=20,
        )
        existing_summary = self.structured_extractor.summarize_existing_items(existing_items)
        actions = await self.structured_extractor.extract_actions(
            tenant_id=tenant_id,
            trace_id=trace_id,
            user_text=user_text,
            assistant_text=assistant_text,
            existing_items_summary=existing_summary,
            fallback_to_deterministic=False,
            raise_on_failure=True,
        )
        await _require_memory_job_execution(
            scope_execution_allowed,
            claim_lease_renew,
            tenant_id=tenant_id,
            session_id=session_id,
            job_id=job_id,
        )
        applied_count = 0
        for action in actions:
            if str(action.get("op") or "") == "ignore":
                continue
            await _require_memory_job_execution(
                scope_execution_allowed,
                claim_lease_renew,
                tenant_id=tenant_id,
                session_id=session_id,
                job_id=job_id,
            )
            item = await self._apply_structured_memory_action(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                action=action,
                source_event_id=source_event_id,
                source_trace_id=trace_id,
                original_text=user_text,
                origin_session_kind=origin_session_kind,
                audience_scope=audience_scope,
                allowed_session_ids=allowed_session_ids,
                sensitivity_category=sensitivity_category,
                expires_at=expires_at,
                source_kind=source_kind,
            )
            applied_count += 1 if item is not None else 0
        if applied_count:
            await _require_memory_job_execution(
                scope_execution_allowed,
                claim_lease_renew,
                tenant_id=tenant_id,
                session_id=session_id,
                job_id=job_id,
            )
            await self._refresh_legacy_cache_for_item_scope(
                {
                    "tenant_id": tenant_id,
                    "channel": channel,
                    "source_key": source_key,
                    "user_id": user_id,
                    "session_id": "",
                    "scope_type": "identity",
                }
            )
        return applied_count

    async def _enhance_memory_graph_with_llm(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        session_id: str,
        user_text: str,
        assistant_text: str,
        trace_id: str,
        source_event_id: int | None,
        origin_session_kind: str = "private",
        audience_scope: str = "private",
        allowed_session_ids: Iterable[str] | None = None,
        sensitivity_category: str = "normal",
        expires_at: datetime | str | None = None,
        source_kind: str = "conversation",
        scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None = None,
        claim_lease_renew: Callable[[], Awaitable[bool]] | None = None,
        job_id: int | None = None,
    ) -> dict[str, int]:
        if not self.graph_extractor.config.enabled or self.graph_extractor.llm_service is None:
            return {
                "entities": 0,
                "facts": 0,
                "episodes": 0,
                "invalidations": 0,
                "conflicts": 0,
                "skipped": 0,
            }
        user_text = _sanitize_db_text(user_text)
        assistant_text = _sanitize_db_text(assistant_text)
        existing_items = await self.list_memory_items(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id="",
            scope_type="identity",
            status="active",
            include_deleted=False,
            limit=20,
        )
        existing_items = [item for item in existing_items if is_safe_graph_llm_context_item(item)]
        session_profile = await self.get_session_profile(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            session_id=session_id,
            user_id=user_id,
        )
        graph = await self.graph_extractor.extract_graph(
            tenant_id=tenant_id,
            trace_id=trace_id,
            user_text=user_text,
            assistant_text=assistant_text,
            session_summary=str(session_profile.get("session_summary") or ""),
            memory_items_summary=self.graph_extractor.summarize_memory_items(existing_items),
            raise_on_failure=False,
        )
        await _require_memory_job_execution(
            scope_execution_allowed,
            claim_lease_renew,
            tenant_id=tenant_id,
            session_id=session_id,
            job_id=job_id,
        )
        counts = {
            "entities": 0,
            "facts": 0,
            "episodes": 0,
            "invalidations": 0,
            "conflicts": 0,
            "skipped": 0,
            "error": 1 if str(graph.get("reason") or "") == "error" else 0,
        }
        if counts["error"] and graph.get("error_type"):
            counts["error_type"] = str(graph.get("error_type") or "")[:80]
        entities_by_key = {
            str(entity.get("key") or ""): entity
            for entity in graph.get("entities") or []
            if str(entity.get("key") or "")
        }
        for entity in entities_by_key.values():
            await _require_memory_job_execution(
                scope_execution_allowed,
                claim_lease_renew,
                tenant_id=tenant_id,
                session_id=session_id,
                job_id=job_id,
            )
            await self._get_graph_entity_id_by_key(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                entity_key=str(entity.get("key") or ""),
                entities_by_key=entities_by_key,
                confidence=float(entity.get("confidence") or 0.0),
                status=str(entity.get("status") or "active"),
            )
            counts["entities"] += 1

        for invalidation in graph.get("invalidations") or []:
            await _require_memory_job_execution(
                scope_execution_allowed,
                claim_lease_renew,
                tenant_id=tenant_id,
                session_id=session_id,
                job_id=job_id,
            )
            applied = await self._apply_graph_invalidation(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                invalidation=invalidation,
                source_event_id=source_event_id,
                source_trace_id=trace_id,
                original_text=user_text,
                origin_session_kind=origin_session_kind,
                audience_scope=audience_scope,
                allowed_session_ids=allowed_session_ids,
                sensitivity_category=sensitivity_category,
                expires_at=expires_at,
                source_kind=source_kind,
            )
            counts["invalidations"] += applied

        for conflict in graph.get("conflicts") or []:
            await _require_memory_job_execution(
                scope_execution_allowed,
                claim_lease_renew,
                tenant_id=tenant_id,
                session_id=session_id,
                job_id=job_id,
            )
            applied = await self._apply_graph_conflict_marker(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                conflict=conflict,
                source_event_id=source_event_id,
                source_trace_id=trace_id,
                original_text=user_text,
                origin_session_kind=origin_session_kind,
                audience_scope=audience_scope,
                allowed_session_ids=allowed_session_ids,
                sensitivity_category=sensitivity_category,
                expires_at=expires_at,
                source_kind=source_kind,
            )
            counts["conflicts"] += applied

        for fact in graph.get("facts") or []:
            if str(fact.get("status") or "") == "skipped":
                counts["skipped"] += 1
                continue
            invalidates_id = fact.get("invalidates_memory_item_id")
            invalidates_key = str(fact.get("invalidates_normalized_key") or "")
            if invalidates_id is not None or invalidates_key:
                await _require_memory_job_execution(
                    scope_execution_allowed,
                    claim_lease_renew,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    job_id=job_id,
                )
                counts["invalidations"] += await self._apply_graph_invalidation(
                    tenant_id=tenant_id,
                    channel=channel,
                    source_key=source_key,
                    user_id=user_id,
                    invalidation={
                        "memory_item_id": invalidates_id,
                        "normalized_key": invalidates_key,
                        "reason": "llm_graph_fact_invalidation",
                    },
                    source_event_id=source_event_id,
                    source_trace_id=trace_id,
                    original_text=user_text,
                    origin_session_kind=origin_session_kind,
                    audience_scope=audience_scope,
                    allowed_session_ids=allowed_session_ids,
                    sensitivity_category=sensitivity_category,
                    expires_at=expires_at,
                    source_kind=source_kind,
                )
            await _require_memory_job_execution(
                scope_execution_allowed,
                claim_lease_renew,
                tenant_id=tenant_id,
                session_id=session_id,
                job_id=job_id,
            )
            backing_item = await self._ensure_graph_backing_memory_item(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                session_id=session_id,
                fact=fact,
                source_event_id=source_event_id,
                source_trace_id=trace_id,
                original_text=user_text,
                origin_session_kind=origin_session_kind,
                audience_scope=audience_scope,
                allowed_session_ids=allowed_session_ids,
                sensitivity_category=sensitivity_category,
                expires_at=expires_at,
                source_kind=source_kind,
            )
            if backing_item is None:
                counts["skipped"] += 1
                continue
            await _require_memory_job_execution(
                scope_execution_allowed,
                claim_lease_renew,
                tenant_id=tenant_id,
                session_id=session_id,
                job_id=job_id,
            )
            if await self._upsert_llm_graph_fact(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                fact=fact,
                entities_by_key=entities_by_key,
                backing_item=backing_item,
                source_event_id=source_event_id,
            ):
                counts["facts"] += 1

        for episode in graph.get("episodes") or []:
            await _require_memory_job_execution(
                scope_execution_allowed,
                claim_lease_renew,
                tenant_id=tenant_id,
                session_id=session_id,
                job_id=job_id,
            )
            if await self._upsert_llm_graph_episode(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                session_id=session_id,
                episode=episode,
                source_event_id=source_event_id,
                source_trace_id=trace_id,
                original_text=user_text,
                origin_session_kind=origin_session_kind,
                audience_scope=audience_scope,
                allowed_session_ids=allowed_session_ids,
                sensitivity_category=sensitivity_category,
                expires_at=expires_at,
                source_kind=source_kind,
            ):
                counts["episodes"] += 1
            else:
                counts["skipped"] += 1

        if counts["facts"] or counts["episodes"] or counts["invalidations"] or counts["conflicts"]:
            await _require_memory_job_execution(
                scope_execution_allowed,
                claim_lease_renew,
                tenant_id=tenant_id,
                session_id=session_id,
                job_id=job_id,
            )
            await self._refresh_legacy_cache_for_item_scope(
                {
                    "tenant_id": tenant_id,
                    "channel": channel,
                    "source_key": source_key,
                    "user_id": user_id,
                    "session_id": "",
                    "scope_type": "identity",
                }
            )
        return counts

    async def _apply_graph_invalidation(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        invalidation: dict[str, Any],
        source_event_id: int | None,
        source_trace_id: str,
        original_text: str,
        origin_session_kind: str = "private",
        audience_scope: str = "private",
        allowed_session_ids: Iterable[str] | None = None,
        sensitivity_category: str = "normal",
        expires_at: datetime | str | None = None,
        source_kind: str = "conversation",
    ) -> int:
        _ = sensitivity_category, source_kind
        audience_contract = _normalize_memory_audience_contract(
            origin_session_kind=origin_session_kind,
            audience_scope=audience_scope,
            allowed_session_ids=allowed_session_ids,
            session_id="",
            expires_at=expires_at,
        )
        target_ids: list[int] = []
        memory_item_id = invalidation.get("memory_item_id")
        if memory_item_id is not None:
            target_ids.append(int(memory_item_id))
        normalized_key = str(invalidation.get("normalized_key") or "")
        if normalized_key:
            for item in await self._find_memory_item_by_normalized_key(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                scope_type="identity",
                session_id="",
                normalized_key=normalized_key,
                statuses={"active", "pending"},
            ):
                if item.get("id") is not None:
                    target_ids.append(int(item["id"]))
        count = 0
        seen: set[int] = set()
        for target_id in target_ids:
            if target_id in seen:
                continue
            seen.add(target_id)
            item = await self.get_memory_item(target_id)
            if not item or not _memory_item_matches_scope(
                item,
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                session_id=str(item.get("session_id") or ""),
            ):
                continue
            if not _memory_item_matches_audience_contract(
                item,
                audience_contract,
                session_id="",
            ):
                continue
            if item.get("source_type") == "manual" or item.get("pinned"):
                continue
            if await self._mark_memory_item_invalidated(
                target_id,
                reason=str(invalidation.get("reason") or "llm_graph_invalidation"),
                source_event_id=source_event_id,
                source_trace_id=source_trace_id,
                original_text=original_text,
                include_original_text_metadata=False,
            ):
                count += 1
        return count

    async def _apply_graph_conflict_marker(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        conflict: dict[str, Any],
        source_event_id: int | None,
        source_trace_id: str,
        original_text: str,
        origin_session_kind: str = "private",
        audience_scope: str = "private",
        allowed_session_ids: Iterable[str] | None = None,
        sensitivity_category: str = "normal",
        expires_at: datetime | str | None = None,
        source_kind: str = "conversation",
    ) -> int:
        normalized_key = str(conflict.get("normalized_key") or "")
        if not normalized_key and conflict.get("memory_item_id") is not None:
            item = await self.get_memory_item(int(conflict["memory_item_id"]))
            normalized_key = str(item.get("normalized_key") or "") if item else ""
        if not normalized_key:
            return 0
        item = await self._insert_or_touch_memory_item(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            scope_type="identity",
            source_type=GRAPH_LLM_BACKING_SOURCE_TYPE,
            memory_type="note",
            content=f"Graph extraction conflict: {conflict.get('reason') or 'conflict'!s}"[:500],
            value_json={
                "op": "graph_llm_conflict",
                "target_memory_item_id": conflict.get("memory_item_id"),
                "target_normalized_key": normalized_key,
                "reason": conflict.get("reason"),
                "evidence": [
                    {
                        "source_event_id": source_event_id,
                        "source_trace_id": source_trace_id,
                        "reason": "llm_graph_conflict",
                    }
                ],
            },
            normalized_key=_semantic_key("graph_conflict", "target", normalized_key),
            confidence=0.0,
            status="pending",
            pinned=False,
            priority=0,
            sensitivity="normal",
            origin_session_kind=origin_session_kind,
            audience_scope=audience_scope,
            allowed_session_ids=allowed_session_ids,
            sensitivity_category=sensitivity_category,
            expires_at=expires_at,
            source_kind=source_kind,
            source_event_id=source_event_id,
            source_trace_id=source_trace_id,
            original_text=original_text,
        )
        return 1 if item else 0

    async def claim_llm_extraction_jobs(
        self,
        *,
        limit: int | None = None,
        worker_id: str | None = None,
        scope_allowlist: str | None = None,
    ) -> list[dict[str, Any]]:
        if not (self.structured_extractor.config.enabled or self.graph_extractor.config.enabled):
            return []
        if (
            self.structured_extractor.llm_service is None
            and self.graph_extractor.llm_service is None
        ):
            return []
        if not _settings_bool(self.settings, "memory_llm_extraction_job_enabled", True):
            return []

        if limit is not None:
            try:
                batch_size = int(limit)
            except (TypeError, ValueError):
                batch_size = 5
            batch_size = max(1, min(batch_size, 100))
        else:
            batch_size = _settings_int(
                self.settings,
                "memory_llm_extraction_job_drain_batch_size",
                5,
                minimum=1,
                maximum=100,
            )
        lock_ttl = _settings_float(
            self.settings,
            "memory_llm_extraction_job_lock_ttl_seconds",
            60.0,
            minimum=1.0,
        )
        owner, claim_token = _new_llm_job_claim_token(self.settings, worker_id)
        configured_allowlist = (
            scope_allowlist
            if scope_allowlist is not None
            else str(getattr(self.settings, "memory_llm_extraction_job_scope_allowlist", "") or "")
        )
        allowlist_sql, allowlist_params = _llm_job_scope_filter_sql(configured_allowlist)
        rows = await _exec(
            "WITH candidate AS ("
            "  SELECT id FROM plugin_memory_extraction_job "
            "  WHERE (status IN ('pending', 'failed') "
            "         OR (status = 'running' AND locked_until < NOW())) "
            "    AND next_run_at <= NOW() "
            "    AND (locked_until IS NULL OR locked_until < NOW()) "
            f"{allowlist_sql}"
            "  ORDER BY next_run_at ASC, created_at ASC "
            "  LIMIT :limit "
            "  FOR UPDATE SKIP LOCKED"
            ") "
            "UPDATE plugin_memory_extraction_job job SET "
            "status = 'running', locked_until = NOW() + (:lock_ttl * INTERVAL '1 second'), "
            "locked_by = :locked_by, updated_at = NOW() "
            "FROM candidate WHERE job.id = candidate.id "
            "RETURNING job.id, job.tenant_id, job.channel, job.source_key, job.user_id, "
            "job.session_id, job.source_event_id, job.source_trace_id, job.status, "
            "job.attempts, job.max_attempts, job.next_run_at, job.locked_until, "
            "job.locked_by, job.last_error, job.result_json, job.idempotency_key, "
            "job.created_at, job.updated_at",
            {
                "limit": batch_size,
                "lock_ttl": lock_ttl,
                "locked_by": claim_token,
                **allowlist_params,
            },
        )
        for job in rows:
            row_token = str(job.get("locked_by") or claim_token)
            job["locked_by"] = row_token
            # Additive aliases let callers distinguish worker provenance from
            # the token while preserving the historical locked_by field.
            job["claim_token"] = row_token
            job["worker_id"] = owner
            logger.info(
                "memory.llm_job_claimed",
                job_id=job.get("id"),
                status=job.get("status"),
                attempts=job.get("attempts"),
                worker_id=owner,
                locked_by=claim_token,
            )
        return rows

    async def get_llm_extraction_job_status_counts_for_day(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        session_id: str,
        start_at: datetime,
        end_at: datetime,
    ) -> dict[str, int]:
        job_rows = await _exec(
            "SELECT job.status, COUNT(*) AS count FROM plugin_memory_extraction_job job "
            "WHERE job.tenant_id = :tid AND job.channel = :channel "
            "AND job.source_key = :source_key "
            "AND job.user_id = :uid AND job.session_id = :sid "
            "AND ("
            "  job.source_event_id IN ("
            "    SELECT id FROM plugin_memory_event "
            "    WHERE tenant_id = :tid AND channel = :channel "
            "    AND source_key IN (:source_key, '*') "
            "    AND user_id = :uid AND session_id = :sid "
            "    AND created_at >= :start_at AND created_at < :end_at"
            "  ) "
            "  OR (job.source_event_id IS NULL "
            "      AND job.created_at >= :start_at AND job.created_at < :end_at)"
            ") "
            "GROUP BY job.status",
            {
                "tid": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "uid": user_id,
                "sid": session_id,
                "start_at": start_at,
                "end_at": end_at,
            },
        )
        counts = {status: 0 for status in MEMORY_EXTRACTION_JOB_STATUSES}
        for row in job_rows:
            status = str(row.get("status") or "")
            if status in counts:
                counts[status] = int(row.get("count") or 0)
        return counts

    async def claim_llm_extraction_jobs_for_day(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        session_id: str,
        start_at: datetime,
        end_at: datetime,
        limit: int,
        worker_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not (self.structured_extractor.config.enabled or self.graph_extractor.config.enabled):
            return []
        if (
            self.structured_extractor.llm_service is None
            and self.graph_extractor.llm_service is None
        ):
            return []
        if not _settings_bool(self.settings, "memory_llm_extraction_job_enabled", True):
            return []

        batch_size = _clamp_int(limit, 5, minimum=1, maximum=100)
        lock_ttl = _settings_float(
            self.settings,
            "memory_llm_extraction_job_lock_ttl_seconds",
            60.0,
            minimum=1.0,
        )
        owner, claim_token = _new_llm_job_claim_token(self.settings, worker_id)
        rows = await _exec(
            "WITH candidate AS ("
            "  SELECT job.id FROM plugin_memory_extraction_job job "
            "  WHERE job.tenant_id = :tid AND job.channel = :channel "
            "    AND job.source_key = :source_key "
            "    AND job.user_id = :uid AND job.session_id = :sid "
            "    AND (job.status IN ('pending', 'failed') "
            "         OR (job.status = 'running' AND job.locked_until < NOW())) "
            "    AND job.next_run_at <= NOW() "
            "    AND (job.locked_until IS NULL OR job.locked_until < NOW()) "
            "    AND ("
            "      job.source_event_id IN ("
            "        SELECT id FROM plugin_memory_event "
            "        WHERE tenant_id = :tid AND channel = :channel "
            "        AND source_key IN (:source_key, '*') "
            "        AND user_id = :uid AND session_id = :sid "
            "        AND created_at >= :start_at AND created_at < :end_at"
            "      ) "
            "      OR (job.source_event_id IS NULL "
            "          AND job.created_at >= :start_at AND job.created_at < :end_at)"
            "    ) "
            "  ORDER BY job.next_run_at ASC, job.created_at ASC "
            "  LIMIT :limit "
            "  FOR UPDATE SKIP LOCKED"
            ") "
            "UPDATE plugin_memory_extraction_job job SET "
            "status = 'running', locked_until = NOW() + (:lock_ttl * INTERVAL '1 second'), "
            "locked_by = :locked_by, updated_at = NOW() "
            "FROM candidate WHERE job.id = candidate.id "
            "RETURNING job.id, job.tenant_id, job.channel, job.source_key, job.user_id, "
            "job.session_id, job.source_event_id, job.source_trace_id, job.status, "
            "job.attempts, job.max_attempts, job.next_run_at, job.locked_until, "
            "job.locked_by, job.last_error, job.result_json, job.idempotency_key, "
            "job.created_at, job.updated_at",
            {
                "tid": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "uid": user_id,
                "sid": session_id,
                "start_at": start_at,
                "end_at": end_at,
                "limit": batch_size,
                "lock_ttl": lock_ttl,
                "locked_by": claim_token,
            },
        )
        for job in rows:
            row_token = str(job.get("locked_by") or claim_token)
            job["locked_by"] = row_token
            job["claim_token"] = row_token
            job["worker_id"] = owner
            logger.info(
                "memory.llm_job_claimed_for_day",
                job_id=job.get("id"),
                status=job.get("status"),
                attempts=job.get("attempts"),
                worker_id=owner,
                locked_by=claim_token,
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                session_id=session_id,
            )
        return rows

    async def drain_llm_extraction_jobs(
        self,
        *,
        limit: int | None = None,
        worker_id: str | None = None,
        scope_allowlist: str | None = None,
        scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None = None,
    ) -> dict[str, int]:
        jobs = await self.claim_llm_extraction_jobs(
            limit=limit,
            worker_id=worker_id,
            scope_allowlist=scope_allowlist,
        )
        result = {"claimed": 0, "succeeded": 0, "failed": 0, "dead": 0}
        for index, job in enumerate(jobs):
            try:
                tenant_id = str(job.get("tenant_id") or "")
                session_id = str(job.get("session_id") or "")
                try:
                    await _require_memory_scope_execution(
                        scope_execution_allowed,
                        tenant_id=tenant_id,
                        session_id=session_id,
                        job_id=int(job.get("id") or 0),
                    )
                except _MemoryScopeExecutionDenied:
                    await self.defer_llm_extraction_job(
                        job,
                        worker_id=worker_id,
                    )
                    logger.info(
                        "memory.llm_job_scope_deferred",
                        job_id=job.get("id"),
                        tenant_id=tenant_id,
                        session_id=session_id,
                    )
                    continue
                status = await self.process_llm_extraction_job(
                    job,
                    worker_id=worker_id,
                    scope_execution_allowed=scope_execution_allowed,
                )
                if status in {"deferred", "stale"}:
                    continue
                result["claimed"] += 1
                if status in result:
                    result[status] += 1
            except asyncio.CancelledError:
                # The current job may be between an LLM call and a durable
                # write, so its lease is deliberately left for token/TTL
                # recovery. Claims that have not started are unambiguous and
                # can be released immediately instead of stalling for a full
                # lock TTL.
                await _settle_under_repeated_cancellation(
                    self._defer_unvisited_llm_extraction_jobs(
                        jobs[index + 1 :],
                        worker_id=worker_id,
                    )
                )
                raise
        if jobs:
            logger.info("memory.llm_job_drain_completed", **result)
        return result

    async def _defer_unvisited_llm_extraction_jobs(
        self,
        jobs: list[dict[str, Any]],
        *,
        worker_id: str | None,
    ) -> None:
        if not jobs:
            return
        outcomes = await asyncio.gather(
            *(self.defer_llm_extraction_job(job, worker_id=worker_id) for job in jobs),
            return_exceptions=True,
        )
        for job, outcome in zip(jobs, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                logger.warning(
                    "memory.llm_job_unvisited_claim_release_failed",
                    job_id=job.get("id"),
                    error_type=outcome.__class__.__name__,
                )

    async def defer_llm_extraction_job(
        self,
        job: dict[str, Any],
        *,
        worker_id: str | None = None,
        defer_seconds: float = 30.0,
    ) -> bool:
        """Release a scope-denied claim without spending its retry budget."""

        job_id = int(job.get("id") or 0)
        if job_id <= 0:
            return False
        claim_token = _llm_job_claim_token(
            job,
            worker_id=worker_id,
            settings=self.settings,
        )
        if not claim_token:
            return False
        rows = await _exec(
            "UPDATE plugin_memory_extraction_job SET "
            "status = 'pending', locked_until = NULL, locked_by = '', "
            "next_run_at = GREATEST(next_run_at, "
            "NOW() + (:defer_seconds * INTERVAL '1 second')), updated_at = NOW() "
            "WHERE id = :id AND status = 'running' AND locked_by = :locked_by "
            "RETURNING id",
            {
                "id": job_id,
                "locked_by": claim_token,
                "defer_seconds": max(1.0, float(defer_seconds or 30.0)),
            },
        )
        return bool(rows)

    async def renew_llm_extraction_job_lease(
        self,
        job: dict[str, Any],
        *,
        worker_id: str | None = None,
        lease_seconds: float | None = None,
    ) -> bool:
        """Extend only the live lease represented by this job's claim token."""

        job_id = int(job.get("id") or 0)
        if job_id <= 0:
            return False
        claim_token = _llm_job_claim_token(
            job,
            worker_id=worker_id,
            settings=self.settings,
        )
        if not claim_token:
            return False
        ttl = (
            _settings_float(
                self.settings,
                "memory_llm_extraction_job_lock_ttl_seconds",
                60.0,
                minimum=1.0,
            )
            if lease_seconds is None
            else max(1.0, float(lease_seconds))
        )
        rows = await _exec(
            "UPDATE plugin_memory_extraction_job SET "
            "locked_until = NOW() + (:lock_ttl * INTERVAL '1 second'), updated_at = NOW() "
            "WHERE id = :id AND status = 'running' AND locked_by = :locked_by "
            "AND locked_until > NOW() RETURNING id",
            {
                "id": job_id,
                "locked_by": claim_token,
                "lock_ttl": ttl,
            },
        )
        return bool(rows)

    async def _guard_llm_job_member_mutation(
        self,
        job: dict[str, Any],
        *,
        event_hint: dict[str, Any] | None,
        scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None,
        claim_lease_renew: Callable[[], Awaitable[bool]] | None,
    ) -> dict[str, Any]:
        """Linearize one job's LLM call and writes with member erasure.

        The source event is read once before the transaction only to discover
        the member behind a group-history job.  Nothing is sent to an LLM until
        the transaction owns the same member advisory lock as ``forget_member``
        and has revalidated the claim, source event, and durable opt-out state.
        """

        job_id = int(job.get("id") or 0)
        tenant_id = str(job.get("tenant_id") or "").strip()
        channel = str(job.get("channel") or "").strip().lower()
        session_id = str(job.get("session_id") or "").strip()
        job_user_id = str(job.get("user_id") or "").strip()
        hinted_source_member_id = str((event_hint or {}).get("source_member_id") or "").strip()
        member_id = (
            hinted_source_member_id if job_user_id == GROUP_HISTORY_USER_ID_SCOPE else job_user_id
        )
        if not tenant_id or not member_id:
            raise _MemoryJobClaimLost("job source member unavailable")

        await self._lock_member_memory_mutation(
            tenant_id=tenant_id,
            user_id=member_id,
        )
        await _require_memory_job_execution(
            scope_execution_allowed,
            claim_lease_renew,
            tenant_id=tenant_id,
            session_id=session_id,
            job_id=job_id,
        )
        current_event = await self._get_memory_event_for_job(job)
        if current_event is None:
            raise _MemoryJobClaimLost("job source event no longer exists")
        if job_user_id == GROUP_HISTORY_USER_ID_SCOPE:
            current_source_member_id = str(current_event.get("source_member_id") or "").strip()
            if current_source_member_id != member_id:
                raise _MemoryJobClaimLost("job source member changed")
        if channel == "wechat" and await self._member_memory_write_blocked(
            tenant_id=tenant_id,
            user_id=member_id,
        ):
            logger.info(
                "memory.llm_job_suppressed_by_member_control",
                job_id=job_id,
                tenant_id=tenant_id,
                source_member_id=member_id,
            )
            raise _MemoryJobClaimLost("member memory writes are blocked")
        return current_event

    async def process_llm_extraction_job(
        self,
        job: dict[str, Any],
        *,
        worker_id: str | None = None,
        scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None = None,
    ) -> str:
        job_id = int(job["id"])
        claim_token = _llm_job_claim_token(
            job,
            worker_id=worker_id,
            settings=self.settings,
        )
        had_claim = _llm_job_has_claim(job)
        if not had_claim or not claim_token:
            logger.info(
                "memory.llm_job_unclaimed_process_rejected",
                job_id=job_id,
            )
            return "stale"

        async def renew_claim() -> bool:
            return await self.renew_llm_extraction_job_lease(
                job,
                worker_id=worker_id,
            )

        tenant_id = str(job.get("tenant_id") or "")
        session_id = str(job.get("session_id") or "")
        if scope_execution_allowed is None:
            scope_execution_allowed = getattr(self, "scope_execution_allowed", None)
        if _store_runtime._ACTIVE_MUTATION_CONNECTION.get() is None:
            event_hint = await self._get_memory_event_for_job(job)
            try:
                async with self._mutation_transaction():
                    await self._guard_llm_job_member_mutation(
                        job,
                        event_hint=event_hint,
                        scope_execution_allowed=scope_execution_allowed,
                        claim_lease_renew=renew_claim,
                    )
                    return await self.process_llm_extraction_job(
                        job,
                        worker_id=worker_id,
                        scope_execution_allowed=scope_execution_allowed,
                    )
            except _MemoryJobClaimLost:
                return await _llm_job_observed_result(
                    job_id,
                    claim_token=claim_token,
                )
            except _MemoryScopeExecutionDenied:
                deferred = await self.defer_llm_extraction_job(
                    job,
                    worker_id=worker_id,
                )
                logger.info(
                    "memory.llm_job_scope_deferred",
                    job_id=job_id,
                    tenant_id=tenant_id,
                    session_id=session_id,
                )
                return "deferred" if deferred else "stale"
        attempts = int(job.get("attempts") or 0)
        next_attempt = attempts + 1
        max_attempts = int(job.get("max_attempts") or 1)
        timeout_seconds = _settings_float(
            self.settings,
            "memory_llm_extraction_job_timeout_seconds",
            5.0,
            minimum=0.1,
        )
        previous_result = _safe_json_loads(job.get("result_json"), {})
        raw_audience = (
            previous_result.get("audience")
            if isinstance(previous_result, dict)
            and isinstance(previous_result.get("audience"), dict)
            else {}
        )
        job_session_id = str(job.get("session_id") or "")
        audience_contract = _normalize_memory_audience_contract(
            origin_session_kind=raw_audience.get("origin_session_kind") or "private",
            audience_scope=raw_audience.get("audience_scope") or "private",
            allowed_session_ids=raw_audience.get("allowed_session_ids"),
            session_id=job_session_id,
            expires_at=raw_audience.get("expires_at"),
        )
        audience_payload = {
            **audience_contract,
            "expires_at": (
                audience_contract["expires_at"].isoformat()
                if isinstance(audience_contract.get("expires_at"), datetime)
                else None
            ),
            "sensitivity_category": str(raw_audience.get("sensitivity_category") or "normal"),
            "source_kind": str(raw_audience.get("source_kind") or "conversation"),
        }
        try:
            await _require_memory_job_execution(
                scope_execution_allowed,
                renew_claim,
                tenant_id=tenant_id,
                session_id=session_id,
                job_id=job_id,
            )
            event = await self._get_memory_event_for_job(job)
            if event is None:
                raise RuntimeError("memory event not found for extraction job")
            structured_count = 0
            graph_counts = {
                "entities": 0,
                "facts": 0,
                "episodes": 0,
                "invalidations": 0,
                "conflicts": 0,
                "skipped": 0,
                "error": 0,
            }
            if (
                self.structured_extractor.config.enabled
                and self.structured_extractor.llm_service is not None
            ):
                await _require_memory_job_execution(
                    scope_execution_allowed,
                    renew_claim,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    job_id=job_id,
                )
                structured_count = await asyncio.wait_for(
                    self._enhance_memory_with_llm(
                        tenant_id=str(job.get("tenant_id") or ""),
                        channel=str(job.get("channel") or ""),
                        source_key=str(job.get("source_key") or "*"),
                        user_id=str(job.get("user_id") or ""),
                        session_id=str(job.get("session_id") or ""),
                        user_text=str(event.get("user_text") or ""),
                        assistant_text=str(event.get("assistant_text") or ""),
                        trace_id=str(job.get("source_trace_id") or event.get("trace_id") or ""),
                        source_event_id=(
                            int(job["source_event_id"])
                            if job.get("source_event_id") is not None
                            else None
                        ),
                        scope_execution_allowed=scope_execution_allowed,
                        claim_lease_renew=renew_claim,
                        job_id=job_id,
                        **audience_payload,
                    ),
                    timeout=timeout_seconds,
                )
            if self.graph_extractor.config.enabled and self.graph_extractor.llm_service is not None:
                try:
                    await _require_memory_job_execution(
                        scope_execution_allowed,
                        renew_claim,
                        tenant_id=tenant_id,
                        session_id=session_id,
                        job_id=job_id,
                    )
                    graph_counts = await self._enhance_memory_graph_with_llm(
                        tenant_id=str(job.get("tenant_id") or ""),
                        channel=str(job.get("channel") or ""),
                        source_key=str(job.get("source_key") or "*"),
                        user_id=str(job.get("user_id") or ""),
                        session_id=str(job.get("session_id") or ""),
                        user_text=str(event.get("user_text") or ""),
                        assistant_text=str(event.get("assistant_text") or ""),
                        trace_id=str(job.get("source_trace_id") or event.get("trace_id") or ""),
                        source_event_id=(
                            int(job["source_event_id"])
                            if job.get("source_event_id") is not None
                            else None
                        ),
                        scope_execution_allowed=scope_execution_allowed,
                        claim_lease_renew=renew_claim,
                        job_id=job_id,
                        **audience_payload,
                    )
                except (_MemoryScopeExecutionDenied, _MemoryJobClaimLost):
                    raise
                except Exception as exc:
                    graph_counts = {
                        "entities": 0,
                        "facts": 0,
                        "episodes": 0,
                        "invalidations": 0,
                        "conflicts": 0,
                        "skipped": 0,
                        "error": 1,
                        "error_type": exc.__class__.__name__,
                    }
                    logger.warning(
                        "memory.graph_llm_job_branch_failed",
                        job_id=job_id,
                        error_type=exc.__class__.__name__,
                    )
            result_payload = {
                "structured_applied": structured_count,
                "graph": graph_counts,
                "audience": audience_payload,
            }
        except _MemoryJobClaimLost:
            logger.info(
                "memory.llm_job_stale_before_write",
                job_id=job_id,
            )
            raise
        except _MemoryScopeExecutionDenied:
            raise
        except Exception as exc:
            try:
                await _require_memory_job_execution(
                    scope_execution_allowed,
                    renew_claim,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    job_id=job_id,
                )
            except _MemoryJobClaimLost:
                raise
            except _MemoryScopeExecutionDenied:
                raise
            is_dead = next_attempt >= max_attempts
            status = "dead" if is_dead else "failed"
            backoff_seconds = self._job_backoff_seconds(next_attempt)
            error_type = exc.__class__.__name__
            last_error = _truncate_error(exc) or error_type
            result_payload = {
                "structured_applied": 0,
                "graph": {
                    "entities": 0,
                    "facts": 0,
                    "episodes": 0,
                    "invalidations": 0,
                    "conflicts": 0,
                    "skipped": 0,
                    "error": 1,
                    "error_type": error_type,
                },
                "error_type": error_type,
                "audience": audience_payload,
            }
            if error_type == "TimeoutError":
                result_payload["timeout"] = True
            result_payload[_LLM_JOB_RESULT_CLAIM_TOKEN_KEY] = claim_token
            updated_rows = await _exec(
                "UPDATE plugin_memory_extraction_job SET "
                "status = CAST(:status AS VARCHAR), attempts = :attempts, "
                "next_run_at = CASE WHEN CAST(:status AS VARCHAR) = 'dead' THEN next_run_at "
                "                   ELSE NOW() + (:backoff_seconds * INTERVAL '1 second') END, "
                "locked_until = NULL, locked_by = '', last_error = :last_error, "
                "result_json = :result_json, updated_at = NOW() "
                "WHERE id = :id AND status = 'running' AND locked_by = :locked_by "
                "RETURNING id, status",
                {
                    "id": job_id,
                    "locked_by": claim_token,
                    "status": status,
                    "attempts": next_attempt,
                    "backoff_seconds": backoff_seconds,
                    "last_error": last_error,
                    "result_json": _to_json(result_payload),
                },
            )
            transition_result = await _llm_job_transition_result(
                job_id=job_id,
                claim_token=claim_token,
                intended_status=status,
                updated_rows=updated_rows,
            )
            if transition_result == "stale":
                logger.info(
                    "memory.llm_job_stale_failure_ignored",
                    job_id=job_id,
                    attempted_status=status,
                    attempts=next_attempt,
                )
                raise _MemoryJobClaimLost("job failure terminal claim lost") from exc
            logger.warning(
                "memory.llm_job_failed",
                job_id=job_id,
                status=status,
                attempts=next_attempt,
                max_attempts=max_attempts,
                error_type=error_type,
            )
            return status

        try:
            await _require_memory_job_execution(
                scope_execution_allowed,
                renew_claim,
                tenant_id=tenant_id,
                session_id=session_id,
                job_id=job_id,
            )
        except _MemoryJobClaimLost:
            raise
        except _MemoryScopeExecutionDenied:
            raise
        result_payload[_LLM_JOB_RESULT_CLAIM_TOKEN_KEY] = claim_token
        updated_rows = await _exec(
            "UPDATE plugin_memory_extraction_job SET "
            "status = 'succeeded', attempts = :attempts, locked_until = NULL, locked_by = '', "
            "last_error = '', result_json = :result_json, updated_at = NOW() "
            "WHERE id = :id AND status = 'running' AND locked_by = :locked_by "
            "RETURNING id, status",
            {
                "id": job_id,
                "locked_by": claim_token,
                "attempts": next_attempt,
                "result_json": _to_json(result_payload),
            },
        )
        transition_result = await _llm_job_transition_result(
            job_id=job_id,
            claim_token=claim_token,
            intended_status="succeeded",
            updated_rows=updated_rows,
        )
        if transition_result == "stale":
            logger.info(
                "memory.llm_job_stale_success_ignored",
                job_id=job_id,
                attempts=next_attempt,
            )
            raise _MemoryJobClaimLost("job success terminal claim lost")
        logger.info(
            "memory.llm_job_succeeded",
            job_id=job_id,
            status="succeeded",
            attempts=next_attempt,
            structured_count=result_payload["structured_applied"],
            graph_counts=result_payload["graph"],
        )
        return "succeeded"

    async def _get_memory_event_for_job(self, job: dict[str, Any]) -> dict[str, Any] | None:
        source_event_id = job.get("source_event_id")
        if source_event_id is not None:
            rows = await _exec(
                "SELECT id, tenant_id, channel, source_key, user_id, session_id, "
                "user_text, assistant_text, trace_id, event_key, source_member_id, "
                "source_message_id, expires_at, created_at "
                "FROM plugin_memory_event WHERE id = :id "
                "AND tenant_id = :tid AND channel = :channel "
                "AND source_key = :source_key AND user_id = :uid AND session_id = :sid",
                {
                    "id": int(source_event_id),
                    "tid": str(job.get("tenant_id") or ""),
                    "channel": str(job.get("channel") or ""),
                    "source_key": str(job.get("source_key") or "*"),
                    "uid": str(job.get("user_id") or ""),
                    "sid": str(job.get("session_id") or ""),
                },
            )
        else:
            rows = await _exec(
                "SELECT id, tenant_id, channel, source_key, user_id, session_id, "
                "user_text, assistant_text, trace_id, event_key, source_member_id, "
                "source_message_id, expires_at, created_at "
                "FROM plugin_memory_event "
                "WHERE tenant_id = :tid AND channel = :channel AND source_key = :source_key "
                "AND user_id = :uid AND session_id = :sid AND trace_id = :trace "
                "ORDER BY created_at DESC LIMIT 1",
                {
                    "tid": str(job.get("tenant_id") or ""),
                    "channel": str(job.get("channel") or ""),
                    "source_key": str(job.get("source_key") or "*"),
                    "uid": str(job.get("user_id") or ""),
                    "sid": str(job.get("session_id") or ""),
                    "trace": str(job.get("source_trace_id") or ""),
                },
            )
        return rows[0] if rows else None

    def _job_backoff_seconds(self, attempt: int) -> float:
        base = _settings_float(
            self.settings,
            "memory_llm_extraction_job_backoff_seconds",
            30.0,
            minimum=0.1,
        )
        return min(base * (2 ** max(0, attempt - 1)), 3600.0)

    async def list_llm_extraction_jobs(
        self,
        *,
        tenant_id: str | None = None,
        channel: str | None = None,
        source_key: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        status: str | None = None,
        error_type: str | None = None,
        created_before: Any = None,
        created_after: Any = None,
        updated_before: Any = None,
        updated_after: Any = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        error_type_expr = _llm_job_error_type_expr()
        clauses, filter_params = _llm_job_filter_sql(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            status=status,
            error_type=error_type,
            created_before=created_before,
            created_after=created_after,
            updated_before=updated_before,
            updated_after=updated_after,
        )
        params: dict[str, Any] = {"limit": max(1, min(int(limit or 50), 500))}
        params.update(filter_params)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return await _exec(
            "SELECT id, tenant_id, channel, source_key, user_id, session_id, source_event_id, "
            "source_trace_id, status, "
            f"{error_type_expr} AS error_type, "
            "attempts, max_attempts, next_run_at, locked_until, created_at, updated_at "
            f"FROM plugin_memory_extraction_job {where} "
            "ORDER BY created_at DESC LIMIT :limit",
            params,
        )

    async def get_llm_extraction_job_status_counts(
        self,
        *,
        tenant_id: str | None = None,
        channel: str | None = None,
        source_key: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        status: str | None = None,
        error_type: str | None = None,
        created_before: Any = None,
        created_after: Any = None,
        updated_before: Any = None,
        updated_after: Any = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        error_type_expr = _llm_job_error_type_expr()
        clauses, filter_params = _llm_job_filter_sql(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            status=status,
            error_type=error_type,
            created_before=created_before,
            created_after=created_after,
            updated_before=updated_before,
            updated_after=updated_after,
        )
        params: dict[str, Any] = {"limit": max(1, min(int(limit or 100), 100))}
        params.update(filter_params)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        status_rows = await _exec(
            f"SELECT status, COUNT(*) AS count FROM plugin_memory_extraction_job {where} GROUP BY status",
            params,
        )
        status_counts = {status_value: 0 for status_value in MEMORY_EXTRACTION_JOB_STATUSES}
        for row in status_rows:
            status_value = str(row.get("status") or "")
            if status_value in status_counts:
                status_counts[status_value] = int(row.get("count") or 0)

        error_type_rows = await _exec(
            f"SELECT {error_type_expr} AS error_type, COUNT(*) AS count "
            f"FROM plugin_memory_extraction_job {where} "
            f"GROUP BY {error_type_expr} "
            "HAVING COALESCE("
            f"{error_type_expr}, "
            "''"
            ") <> '' "
            "ORDER BY count DESC, error_type ASC LIMIT :limit",
            params,
        )
        error_type_counts = {
            str(row.get("error_type") or ""): int(row.get("count") or 0)
            for row in error_type_rows
            if str(row.get("error_type") or "")
        }

        group_cols = ", ".join(_LLM_JOB_SCOPE_GROUP_KEYS[:-1])
        scope_rows = await _exec(
            f"SELECT {group_cols}, {error_type_expr} AS error_type, COUNT(*) AS count "
            f"FROM plugin_memory_extraction_job {where} "
            f"GROUP BY {group_cols}, {error_type_expr} "
            "ORDER BY count DESC, MAX(updated_at) DESC NULLS LAST LIMIT :limit",
            params,
        )
        scope_counts: list[dict[str, Any]] = []
        for row in scope_rows:
            item = {key: row.get(key) for key in _LLM_JOB_SCOPE_GROUP_KEYS}
            item["count"] = int(row.get("count") or 0)
            scope_counts.append(item)

        dead_scope_rows = await _exec(
            f"SELECT {group_cols}, {error_type_expr} AS error_type, COUNT(*) AS count "
            f"FROM plugin_memory_extraction_job {where} "
            f"{' AND ' if where else 'WHERE '}status = 'dead' "
            f"GROUP BY {group_cols}, {error_type_expr} "
            "ORDER BY count DESC, MAX(updated_at) DESC NULLS LAST LIMIT :limit",
            params,
        )
        dead_scope_counts: list[dict[str, Any]] = []
        for row in dead_scope_rows:
            item = {key: row.get(key) for key in _LLM_JOB_SCOPE_GROUP_KEYS}
            item["status"] = "dead"
            item["count"] = int(row.get("count") or 0)
            dead_scope_counts.append(item)

        retry_scope_rows = await _exec(
            f"SELECT {group_cols}, {error_type_expr} AS error_type, COUNT(*) AS count "
            f"FROM plugin_memory_extraction_job {where} "
            f"{' AND ' if where else 'WHERE '}status = 'failed' AND attempts < max_attempts "
            f"GROUP BY {group_cols}, {error_type_expr} "
            "ORDER BY count DESC, MAX(updated_at) DESC NULLS LAST LIMIT :limit",
            params,
        )
        retry_scope_counts: list[dict[str, Any]] = []
        for row in retry_scope_rows:
            item = {key: row.get(key) for key in _LLM_JOB_SCOPE_GROUP_KEYS}
            item["status"] = "failed"
            item["retryable"] = True
            item["count"] = int(row.get("count") or 0)
            retry_scope_counts.append(item)

        retry_rows = await _exec(
            f"SELECT "
            "COUNT(*) FILTER (WHERE status = 'failed' AND attempts < max_attempts) AS retryable_failed, "
            "COUNT(*) FILTER (WHERE status = 'failed' AND attempts >= max_attempts) AS exhausted_failed, "
            "COUNT(*) FILTER (WHERE status IN ('pending', 'failed') AND next_run_at <= NOW()) AS ready, "
            "COUNT(*) FILTER (WHERE status IN ('pending', 'failed') AND next_run_at > NOW()) AS delayed "
            f"FROM plugin_memory_extraction_job {where}",
            params,
        )
        retry_row = retry_rows[0] if retry_rows else {}
        retry_counts = {
            "retryable_failed": int(retry_row.get("retryable_failed") or 0),
            "exhausted_failed": int(retry_row.get("exhausted_failed") or 0),
            "ready": int(retry_row.get("ready") or 0),
            "delayed": int(retry_row.get("delayed") or 0),
        }

        latency_rows = await _exec(
            "SELECT "
            "ROUND(AVG(EXTRACT(EPOCH FROM (updated_at - created_at)))::numeric, 3) AS avg_seconds, "
            "ROUND(MAX(EXTRACT(EPOCH FROM (updated_at - created_at)))::numeric, 3) AS max_seconds, "
            "ROUND((AVG(EXTRACT(EPOCH FROM (NOW() - created_at))) "
            "FILTER (WHERE status IN ('pending', 'failed')))::numeric, 3) AS pending_avg_age_seconds "
            f"FROM plugin_memory_extraction_job {where}",
            params,
        )
        latency_row = latency_rows[0] if latency_rows else {}
        latency_seconds = {
            "avg": float(latency_row.get("avg_seconds") or 0.0),
            "max": float(latency_row.get("max_seconds") or 0.0),
            "pending_avg_age": float(latency_row.get("pending_avg_age_seconds") or 0.0),
        }

        graph_result_rows = await _exec(
            "SELECT "
            "COUNT(*) FILTER (WHERE COALESCE((result_json::jsonb #>> '{graph,error}')::int, 0) > 0) "
            "AS graph_error, "
            "COUNT(*) FILTER (WHERE COALESCE((result_json::jsonb #>> '{graph,facts}')::int, 0) > 0) "
            "AS graph_facts, "
            "COUNT(*) FILTER (WHERE COALESCE((result_json::jsonb #>> '{graph,episodes}')::int, 0) > 0) "
            "AS graph_episodes, "
            "COUNT(*) FILTER (WHERE COALESCE((result_json::jsonb #>> '{graph,entities}')::int, 0) > 0) "
            "AS graph_entities, "
            "COUNT(*) FILTER (WHERE COALESCE((result_json::jsonb #>> '{graph,skipped}')::int, 0) > 0) "
            "AS graph_skipped "
            f"FROM plugin_memory_extraction_job {where}",
            params,
        )
        graph_result_row = graph_result_rows[0] if graph_result_rows else {}
        graph_result_counts = {
            "error": int(graph_result_row.get("graph_error") or 0),
            "facts": int(graph_result_row.get("graph_facts") or 0),
            "episodes": int(graph_result_row.get("graph_episodes") or 0),
            "entities": int(graph_result_row.get("graph_entities") or 0),
            "skipped": int(graph_result_row.get("graph_skipped") or 0),
        }

        return {
            "counts": status_counts,
            "status_counts": status_counts,
            "retry_counts": retry_counts,
            "error_type_counts": error_type_counts,
            "scope_counts": scope_counts,
            "dead_scope_counts": dead_scope_counts,
            "retry_scope_counts": retry_scope_counts,
            "latency_seconds": latency_seconds,
            "graph_result_counts": graph_result_counts,
        }

    async def maintain_llm_extraction_jobs(
        self,
        *,
        actions: list[str],
        dry_run: bool = True,
        tenant_id: str | None = None,
        channel: str | None = None,
        source_key: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        status: str | None = None,
        error_type: str | None = None,
        created_before: Any = None,
        created_after: Any = None,
        updated_before: Any = None,
        updated_after: Any = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        row_limit = max(1, min(int(limit or 100), 100))
        filters = {
            "tenant_id": tenant_id,
            "channel": channel,
            "source_key": source_key,
            "user_id": user_id,
            "session_id": session_id,
            "status": status,
            "error_type": error_type,
            "created_before": created_before,
            "created_after": created_after,
            "updated_before": updated_before,
            "updated_after": updated_after,
        }
        results: list[dict[str, Any]] = []
        total_ids: list[int] = []
        result_json = _safe_llm_job_result_json()

        for raw_action in actions:
            action = str(raw_action or "").strip().lower()
            clauses, params = _llm_job_filter_sql(**filters)
            params["limit"] = row_limit

            if action == "reset_stale":
                clauses.append("status = 'running'")
                if updated_before is not None:
                    clauses.append("updated_at < :updated_before")
                else:
                    clauses.append("(locked_until IS NULL OR locked_until < NOW())")
            elif action == "retry":
                clauses.append("status IN ('failed', 'dead', 'pending')")
            elif action == "mark_dead":
                pass
            elif action == "cleanup_smoke":
                clauses.append(_llm_job_scope_is_smoke_sql())
            else:
                results.append(
                    {"action": action, "error": "unsupported action", "would_affect": 0, "ids": []}
                )
                continue

            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            candidate_sql = (
                "SELECT id FROM plugin_memory_extraction_job "
                f"{where} ORDER BY updated_at ASC, created_at ASC LIMIT :limit"
            )
            claimed_candidate_sql = (
                "SELECT id, locked_by FROM plugin_memory_extraction_job "
                f"{where} ORDER BY updated_at ASC, created_at ASC LIMIT :limit "
                "FOR UPDATE SKIP LOCKED"
            )
            if dry_run:
                rows = await _exec(candidate_sql, params)
            elif action == "cleanup_smoke":
                rows = await _exec(
                    "WITH candidate AS ("
                    f"{claimed_candidate_sql}"
                    ") DELETE FROM plugin_memory_extraction_job job "
                    "USING candidate WHERE job.id = candidate.id "
                    "AND COALESCE(job.locked_by, '') = COALESCE(candidate.locked_by, '') "
                    "RETURNING job.id",
                    params,
                )
            elif action == "reset_stale":
                rows = await _exec(
                    "WITH candidate AS ("
                    f"{claimed_candidate_sql}"
                    ") UPDATE plugin_memory_extraction_job job SET "
                    "status = 'pending', locked_until = NULL, locked_by = '', last_error = '', "
                    "result_json = :result_json, updated_at = NOW() "
                    "FROM candidate WHERE job.id = candidate.id "
                    "AND COALESCE(job.locked_by, '') = COALESCE(candidate.locked_by, '') "
                    "RETURNING job.id",
                    {**params, "result_json": result_json},
                )
            elif action == "retry":
                rows = await _exec(
                    "WITH candidate AS ("
                    f"{claimed_candidate_sql}"
                    ") UPDATE plugin_memory_extraction_job job SET "
                    "status = 'pending', attempts = 0, next_run_at = NOW(), locked_until = NULL, "
                    "locked_by = '', last_error = '', result_json = :result_json, updated_at = NOW() "
                    "FROM candidate WHERE job.id = candidate.id "
                    "AND COALESCE(job.locked_by, '') = COALESCE(candidate.locked_by, '') "
                    "RETURNING job.id",
                    {**params, "result_json": result_json},
                )
            else:
                rows = await _exec(
                    "WITH candidate AS ("
                    f"{claimed_candidate_sql}"
                    ") UPDATE plugin_memory_extraction_job job SET "
                    "status = 'dead', locked_until = NULL, locked_by = '', "
                    "last_error = :last_error, result_json = :result_json, updated_at = NOW() "
                    "FROM candidate WHERE job.id = candidate.id "
                    "AND COALESCE(job.locked_by, '') = COALESCE(candidate.locked_by, '') "
                    "RETURNING job.id",
                    {
                        **params,
                        "last_error": "admin maintenance marked job dead",
                        "result_json": result_json,
                    },
                )

            ids = [int(row["id"]) for row in rows if row.get("id") is not None]
            total_ids.extend(ids)
            results.append(
                {
                    "action": action,
                    "dry_run": dry_run,
                    "would_affect": len(ids),
                    "affected": 0 if dry_run else len(ids),
                    "ids": ids,
                }
            )

        unique_ids = list(dict.fromkeys(total_ids))
        return {
            "dry_run": dry_run,
            "limit": row_limit,
            "would_affect": len(unique_ids),
            "affected": 0 if dry_run else len(unique_ids),
            "ids": unique_ids,
            "results": results,
        }
