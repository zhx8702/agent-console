"""Atomic, audited administrative mutations for the memory store.

The mixin keeps the public MemoryStore API stable while isolating the
idempotency/audit boundary from ordinary persistence and retrieval code.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncConnection

from app.admin.mutation_ledger import (
    MutationAudit,
    MutationChange,
    MutationIdentity,
    MutationOutcome,
    hash_identifier,
    run_idempotent_mutation,
)
from app.social.contracts import MemberPrivacyValues


class MemoryMutationPort(Protocol):
    """Operations the admin-mutation boundary needs from MemoryStore."""

    def _mutation_transaction(self) -> AbstractAsyncContextManager[AsyncConnection]: ...

    async def get_profile_enrichment_candidate(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None: ...

    async def review_profile_enrichment_candidate(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None: ...

    async def get_memory_item(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None: ...

    async def review_memory_item_acceptance(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None: ...

    async def soft_delete_memory_item(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None: ...

    async def forget_memory_items(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...

    async def get_group_member_memory_item(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None: ...

    async def correct_group_member_memory_item(self, *args: Any, **kwargs: Any) -> dict[str, Any] | None: ...

    async def delete_group_member_memory_item(self, *args: Any, **kwargs: Any) -> bool: ...

    async def create_memory_item(self, **kwargs: Any) -> dict[str, Any] | None: ...

    async def update_memory_item(self, item_id: int, **kwargs: Any) -> dict[str, Any] | None: ...

    async def backfill_legacy_acceptance(self, **kwargs: Any) -> dict[str, Any]: ...

    async def maintain_llm_extraction_jobs(self, **kwargs: Any) -> dict[str, Any]: ...

    async def backfill_from_sdk(self, **kwargs: Any) -> dict[str, Any]: ...

    async def rebuild_memory_item_vector_index(self, **kwargs: Any) -> dict[str, Any]: ...

    async def rebuild_memory_graph_vector_index(self, **kwargs: Any) -> dict[str, Any]: ...

    def _item_audit_state(self, item: dict[str, Any] | None) -> dict[str, Any]: ...

    def _mutation_audit(
        self,
        *,
        actor: str,
        actor_kind: str,
        roles: Sequence[str],
        scope: dict[str, Any],
        reason_code: str,
        reason: str,
        trace_id: str,
    ) -> MutationAudit: ...


class MemoryMutationError(RuntimeError):
    """Stable route-facing failure from a transactional memory mutation."""

    def __init__(self, detail: str, *, status_code: int) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


_COUNT_AUDIT_FIELDS = {
    "affected",
    "deleted",
    "duplicate_count",
    "errors",
    "events_duplicate",
    "events_inserted",
    "imported_count",
    "indexed",
    "items_created",
    "items_pending",
    "items_updated",
    "jobs_enqueued",
    "processed_count",
    "scanned",
    "session_count",
    "skipped",
    "skipped_count",
    "would_affect",
    "would_index",
}


def _count_audit_state(result: dict[str, Any]) -> dict[str, Any]:
    state: dict[str, Any] = {}
    for key in _COUNT_AUDIT_FIELDS:
        value = result.get(key)
        if isinstance(value, bool):
            state[key] = value
        elif isinstance(value, (int, float)):
            state[key] = value
    if isinstance(result.get("dry_run"), bool):
        state["dry_run"] = bool(result["dry_run"])
    return state


def _scope_hash(value: Any) -> str:
    return hash_identifier(str(value or ""))


def _expected_item_version(value: str) -> str:
    normalized = str(value or "").strip()
    if normalized.startswith("W/"):
        normalized = normalized[2:].strip()
    return normalized.strip('"')


def memory_item_version(item: dict[str, Any] | None) -> str:
    if not item:
        return ""
    value = item.get("updated_at") or item.get("last_seen_at") or item.get("id") or ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class MemoryAdminMutationMixin:
    """Concrete high-risk mutation workflows backed by MemoryMutationPort."""

    @staticmethod
    def _item_audit_state(item: dict[str, Any] | None) -> dict[str, Any]:
        if not item:
            return {"exists": False}
        return {
            "exists": True,
            "item_id": int(item.get("id") or 0),
            "status": str(item.get("status") or "")[:32],
            "acceptance_status": str(item.get("acceptance_status") or "")[:32],
            "pinned": bool(item.get("pinned")),
            "sensitivity": str(
                item.get("sensitivity_category") or item.get("sensitivity") or ""
            )[:32],
        }

    @staticmethod
    def _mutation_audit(
        *,
        actor: str,
        actor_kind: str,
        roles: Sequence[str],
        scope: dict[str, Any],
        reason_code: str,
        reason: str,
        trace_id: str,
    ) -> MutationAudit:
        return MutationAudit(
            actor=actor,
            actor_kind=actor_kind,
            roles=roles,
            scope=scope,
            reason_code=reason_code,
            reason=reason,
            trace_id=trace_id,
        )

    async def _run_admin_command_idempotent(
        self: MemoryMutationPort,
        *,
        tenant_id: str,
        operation: str,
        resource_key: str,
        request_payload: dict[str, Any],
        idempotency_key: str,
        actor: str,
        actor_kind: str,
        roles: Sequence[str],
        trace_id: str,
        scope: dict[str, Any],
        reason_code: str,
        mutate: Callable[[], Awaitable[MutationChange]],
    ) -> MutationOutcome:
        async with self._mutation_transaction() as conn:
            return await run_idempotent_mutation(
                conn,
                identity=MutationIdentity(
                    tenant_id=tenant_id,
                    plugin_name="memory",
                    operation=operation,
                    resource_key=resource_key,
                    idempotency_key=idempotency_key,
                    request_payload=request_payload,
                ),
                audit=self._mutation_audit(
                    actor=actor,
                    actor_kind=actor_kind,
                    roles=roles,
                    scope=scope,
                    reason_code=reason_code,
                    reason="",
                    trace_id=trace_id,
                ),
                mutate=mutate,
            )

    async def create_memory_item_idempotent(
        self: MemoryMutationPort,
        *,
        item_fields: dict[str, Any],
        idempotency_key: str,
        actor: str,
        actor_kind: str,
        roles: Sequence[str],
        trace_id: str,
    ) -> MutationOutcome:
        tenant_id = str(item_fields.get("tenant_id") or "").strip()

        async def mutate() -> MutationChange:
            item = await self.create_memory_item(**item_fields)
            if not item:
                raise MemoryMutationError("content required", status_code=400)
            return MutationChange(
                response=item,
                before_state={"exists": False},
                after_state=self._item_audit_state(item),
                resource_version=memory_item_version(item),
            )

        return await self._run_admin_command_idempotent(
            tenant_id=tenant_id,
            operation="memory.item.create",
            resource_key=(
                f"{item_fields.get('channel')}:{item_fields.get('source_key')}:"
                f"{item_fields.get('user_id')}:{item_fields.get('session_id')}"
            ),
            request_payload=item_fields,
            idempotency_key=idempotency_key,
            actor=actor,
            actor_kind=actor_kind,
            roles=roles,
            trace_id=trace_id,
            scope={
                "channel_hash": _scope_hash(item_fields.get("channel")),
                "source_hash": _scope_hash(item_fields.get("source_key")),
                "user_hash": _scope_hash(item_fields.get("user_id")),
                "session_hash": _scope_hash(item_fields.get("session_id")),
            },
            reason_code="memory_item_create",
            mutate=mutate,
        )

    async def update_memory_item_idempotent(
        self: MemoryMutationPort,
        item_id: int,
        *,
        tenant_id: str,
        updates: dict[str, Any],
        expected_version: str,
        idempotency_key: str,
        actor: str,
        actor_kind: str,
        roles: Sequence[str],
        trace_id: str,
    ) -> MutationOutcome:
        async def mutate() -> MutationChange:
            current = await self.get_memory_item(item_id, for_update=True)
            if (
                not current
                or current.get("deleted_at")
                or str(current.get("tenant_id") or "") != tenant_id
            ):
                raise MemoryMutationError("memory item not found", status_code=404)
            expected = _expected_item_version(expected_version)
            if expected and expected != memory_item_version(current):
                raise MemoryMutationError("memory_item_precondition_failed", status_code=412)
            updated = await self.update_memory_item(item_id, **updates)
            if not updated or str(updated.get("tenant_id") or tenant_id) != tenant_id:
                raise MemoryMutationError("memory item not found", status_code=404)
            return MutationChange(
                response=updated,
                before_state=self._item_audit_state(current),
                after_state=self._item_audit_state(updated),
                resource_version=memory_item_version(updated),
            )

        return await self._run_admin_command_idempotent(
            tenant_id=tenant_id,
            operation="memory.item.update",
            resource_key=str(item_id),
            request_payload={
                "item_id": int(item_id),
                "tenant_id": tenant_id,
                "updates": updates,
                "expected_version": _expected_item_version(expected_version),
            },
            idempotency_key=idempotency_key,
            actor=actor,
            actor_kind=actor_kind,
            roles=roles,
            trace_id=trace_id,
            scope={"item_id": int(item_id)},
            reason_code="memory_item_update",
            mutate=mutate,
        )

    async def backfill_legacy_acceptance_idempotent(
        self: MemoryMutationPort,
        *,
        params: dict[str, Any],
        idempotency_key: str,
        actor: str,
        actor_kind: str,
        roles: Sequence[str],
        trace_id: str,
    ) -> MutationOutcome:
        tenant_id = str(params.get("tenant_id") or "").strip()

        async def mutate() -> MutationChange:
            result = await self.backfill_legacy_acceptance(**params)
            return MutationChange(
                response=result,
                before_state={"affected": 0},
                after_state=_count_audit_state(result),
                resource_version=hash_identifier(
                    f"{result.get('affected', 0)}:{result.get('would_affect', 0)}"
                ),
            )

        return await self._run_admin_command_idempotent(
            tenant_id=tenant_id,
            operation="memory.acceptance_legacy.backfill",
            resource_key=f"{tenant_id}:{params.get('session_id') or '*'}",
            request_payload=params,
            idempotency_key=idempotency_key,
            actor=actor,
            actor_kind=actor_kind,
            roles=roles,
            trace_id=trace_id,
            scope={
                "channel_hash": _scope_hash(params.get("channel")),
                "source_hash": _scope_hash(params.get("source_key")),
                "user_hash": _scope_hash(params.get("user_id")),
                "session_hash": _scope_hash(params.get("session_id")),
            },
            reason_code="memory_acceptance_legacy_backfill",
            mutate=mutate,
        )

    async def maintain_llm_extraction_jobs_idempotent(
        self: MemoryMutationPort,
        *,
        params: dict[str, Any],
        idempotency_key: str,
        actor: str,
        actor_kind: str,
        roles: Sequence[str],
        trace_id: str,
    ) -> MutationOutcome:
        ledger_tenant = str(params.get("tenant_id") or "__global__").strip()

        async def mutate() -> MutationChange:
            result = await self.maintain_llm_extraction_jobs(**params)
            return MutationChange(
                response=result,
                before_state={"affected": 0},
                after_state=_count_audit_state(result),
                resource_version=hash_identifier(
                    f"{result.get('affected', 0)}:{result.get('would_affect', 0)}"
                ),
            )

        return await self._run_admin_command_idempotent(
            tenant_id=ledger_tenant,
            operation="memory.extraction_jobs.maintenance",
            resource_key=f"{ledger_tenant}:{params.get('session_id') or '*'}",
            request_payload=params,
            idempotency_key=idempotency_key,
            actor=actor,
            actor_kind=actor_kind,
            roles=roles,
            trace_id=trace_id,
            scope={
                "channel_hash": _scope_hash(params.get("channel")),
                "source_hash": _scope_hash(params.get("source_key")),
                "user_hash": _scope_hash(params.get("user_id")),
                "session_hash": _scope_hash(params.get("session_id")),
                "action_count": len(params.get("actions") or []),
            },
            reason_code="memory_extraction_jobs_maintenance",
            mutate=mutate,
        )

    async def backfill_from_sdk_idempotent(
        self: MemoryMutationPort,
        *,
        params: dict[str, Any],
        idempotency_key: str,
        actor: str,
        actor_kind: str,
        roles: Sequence[str],
        trace_id: str,
    ) -> MutationOutcome:
        tenant_id = str(params.get("tenant_id") or "").strip()
        sessions = sorted({str(value or "").strip() for value in params.get("session_ids") or []})

        async def mutate() -> MutationChange:
            result = await self.backfill_from_sdk(**params)
            return MutationChange(
                response=result,
                before_state={"imported_count": 0},
                after_state=_count_audit_state(result),
                resource_version=hash_identifier(
                    f"{result.get('events_inserted', 0)}:{result.get('items_created', 0)}"
                ),
            )

        return await self._run_admin_command_idempotent(
            tenant_id=tenant_id,
            operation="memory.history.backfill",
            resource_key=f"{tenant_id}:{hash_identifier('|'.join(sessions))}",
            request_payload=params,
            idempotency_key=idempotency_key,
            actor=actor,
            actor_kind=actor_kind,
            roles=roles,
            trace_id=trace_id,
            scope={
                "session_count": len(sessions),
                "session_scope_hash": hash_identifier("|".join(sessions)),
                "user_hash": _scope_hash(params.get("user_id")),
            },
            reason_code="memory_history_backfill",
            mutate=mutate,
        )

    async def rebuild_memory_item_vector_index_idempotent(
        self: MemoryMutationPort,
        *,
        params: dict[str, Any],
        idempotency_key: str,
        actor: str,
        actor_kind: str,
        roles: Sequence[str],
        trace_id: str,
    ) -> MutationOutcome:
        return await self._rebuild_memory_vector_index_idempotent(
            kind="item",
            params=params,
            idempotency_key=idempotency_key,
            actor=actor,
            actor_kind=actor_kind,
            roles=roles,
            trace_id=trace_id,
        )

    async def rebuild_memory_graph_vector_index_idempotent(
        self: MemoryMutationPort,
        *,
        params: dict[str, Any],
        idempotency_key: str,
        actor: str,
        actor_kind: str,
        roles: Sequence[str],
        trace_id: str,
    ) -> MutationOutcome:
        return await self._rebuild_memory_vector_index_idempotent(
            kind="graph",
            params=params,
            idempotency_key=idempotency_key,
            actor=actor,
            actor_kind=actor_kind,
            roles=roles,
            trace_id=trace_id,
        )

    async def _rebuild_memory_vector_index_idempotent(
        self: MemoryMutationPort,
        *,
        kind: str,
        params: dict[str, Any],
        idempotency_key: str,
        actor: str,
        actor_kind: str,
        roles: Sequence[str],
        trace_id: str,
    ) -> MutationOutcome:
        ledger_tenant = str(params.get("tenant_id") or "__global__").strip()

        async def mutate() -> MutationChange:
            if kind == "item":
                result = await self.rebuild_memory_item_vector_index(**params)
            else:
                result = await self.rebuild_memory_graph_vector_index(**params)
            return MutationChange(
                response=result,
                before_state={"indexed": 0},
                after_state=_count_audit_state(result),
                resource_version=hash_identifier(
                    f"{result.get('scanned', 0)}:{result.get('indexed', 0)}:"
                    f"{result.get('deleted', 0)}:{result.get('errors', 0)}"
                ),
            )

        return await self._run_admin_command_idempotent(
            tenant_id=ledger_tenant,
            operation=f"memory.{kind}_vectors.rebuild",
            resource_key=f"{ledger_tenant}:{params.get('user_id') or '*'}",
            request_payload=params,
            idempotency_key=idempotency_key,
            actor=actor,
            actor_kind=actor_kind,
            roles=roles,
            trace_id=trace_id,
            scope={
                "channel_hash": _scope_hash(params.get("channel")),
                "source_hash": _scope_hash(params.get("source_key")),
                "user_hash": _scope_hash(params.get("user_id")),
                "limit": int(params.get("limit") or 0),
                "dry_run": bool(params.get("dry_run")),
            },
            reason_code=f"memory_{kind}_vectors_rebuild",
            mutate=mutate,
        )

    async def review_profile_enrichment_candidate_idempotent(
        self: MemoryMutationPort,
        candidate_id: int,
        *,
        tenant_id: str,
        action: str,
        notes: str,
        reviewed_by: str,
        request_reviewed_by: str = "",
        idempotency_key: str,
        actor_kind: str,
        roles: Sequence[str],
        trace_id: str,
    ) -> MutationOutcome:
        async with self._mutation_transaction() as conn:
            async def mutate() -> MutationChange:
                current = await self.get_profile_enrichment_candidate(candidate_id)
                if not current or str(current.get("tenant_id") or "") != tenant_id:
                    raise MemoryMutationError(
                        "profile enrichment candidate not found",
                        status_code=404,
                    )
                updated = await self.review_profile_enrichment_candidate(
                    candidate_id,
                    action=action,
                    notes=notes,
                    reviewed_by=reviewed_by,
                )
                if not updated:
                    raise MemoryMutationError(
                        "profile enrichment candidate not found",
                        status_code=404,
                    )
                return MutationChange(
                    response=updated,
                    before_state=self._item_audit_state(current),
                    after_state=self._item_audit_state(updated),
                    resource_version=str(updated.get("updated_at") or updated.get("id") or ""),
                )

            return await run_idempotent_mutation(
                conn,
                identity=MutationIdentity(
                    tenant_id=tenant_id,
                    plugin_name="memory",
                    operation="memory.profile_enrichment.review",
                    resource_key=str(candidate_id),
                    idempotency_key=idempotency_key,
                    request_payload={
                        "candidate_id": int(candidate_id),
                        "action": action,
                        "notes": notes,
                        "reviewed_by": request_reviewed_by,
                    },
                ),
                audit=self._mutation_audit(
                    actor=reviewed_by,
                    actor_kind=actor_kind,
                    roles=roles,
                    scope={
                        "item_id": int(candidate_id),
                    },
                    reason_code="memory_profile_enrichment_review",
                    reason=notes,
                    trace_id=trace_id,
                ),
                mutate=mutate,
            )

    async def review_memory_item_acceptance_idempotent(
        self: MemoryMutationPort,
        item_id: int,
        *,
        tenant_id: str,
        action: str,
        review_reason: str,
        reviewed_by: str,
        request_reviewed_by: str = "",
        superseded_by_item_id: int | None,
        supersedes_item_id: int | None,
        idempotency_key: str,
        actor_kind: str,
        roles: Sequence[str],
        trace_id: str,
    ) -> MutationOutcome:
        async with self._mutation_transaction() as conn:
            async def mutate() -> MutationChange:
                current = await self.get_memory_item(item_id)
                if (
                    not current
                    or current.get("deleted_at")
                    or str(current.get("tenant_id") or "") != tenant_id
                ):
                    raise MemoryMutationError("memory item not found", status_code=404)
                updated = await self.review_memory_item_acceptance(
                    item_id,
                    action=action,
                    review_reason=review_reason,
                    reviewed_by=reviewed_by,
                    superseded_by_item_id=superseded_by_item_id,
                    supersedes_item_id=supersedes_item_id,
                )
                if not updated:
                    raise MemoryMutationError("memory item not found", status_code=404)
                response = {"ok": True, "ids": [int(updated["id"])], "count": 1, "item": updated}
                return MutationChange(
                    response=response,
                    before_state=self._item_audit_state(current),
                    after_state=self._item_audit_state(updated),
                    resource_version=str(updated.get("updated_at") or updated.get("id") or ""),
                )

            return await run_idempotent_mutation(
                conn,
                identity=MutationIdentity(
                    tenant_id=tenant_id,
                    plugin_name="memory",
                    operation="memory.item.acceptance_review",
                    resource_key=str(item_id),
                    idempotency_key=idempotency_key,
                    request_payload={
                        "item_id": int(item_id),
                        "action": action,
                        "review_reason": review_reason,
                        "reviewed_by": request_reviewed_by,
                        "superseded_by_item_id": superseded_by_item_id,
                        "supersedes_item_id": supersedes_item_id,
                    },
                ),
                audit=self._mutation_audit(
                    actor=reviewed_by,
                    actor_kind=actor_kind,
                    roles=roles,
                    scope={
                        "item_id": int(item_id),
                    },
                    reason_code="memory_item_acceptance_review",
                    reason=review_reason,
                    trace_id=trace_id,
                ),
                mutate=mutate,
            )

    async def soft_delete_memory_item_idempotent(
        self: MemoryMutationPort,
        item_id: int,
        *,
        tenant_id: str,
        allow_pinned: bool,
        idempotency_key: str,
        actor: str,
        actor_kind: str,
        roles: Sequence[str],
        trace_id: str,
    ) -> MutationOutcome:
        async with self._mutation_transaction() as conn:
            async def mutate() -> MutationChange:
                current = await self.get_memory_item(item_id)
                if (
                    not current
                    or current.get("deleted_at")
                    or str(current.get("status") or "") == "deleted"
                    or str(current.get("tenant_id") or "") != tenant_id
                ):
                    raise MemoryMutationError("memory item not found", status_code=404)
                deleted = await self.soft_delete_memory_item(item_id, allow_pinned=allow_pinned)
                if not deleted:
                    raise MemoryMutationError("memory item not found", status_code=404)
                response = {"ok": True, "ids": [int(deleted["id"])], "count": 1, "item": deleted}
                return MutationChange(
                    response=response,
                    before_state=self._item_audit_state(current),
                    after_state=self._item_audit_state(deleted),
                    resource_version=str(deleted.get("updated_at") or deleted.get("id") or ""),
                )

            return await run_idempotent_mutation(
                conn,
                identity=MutationIdentity(
                    tenant_id=tenant_id,
                    plugin_name="memory",
                    operation="memory.item.delete",
                    resource_key=str(item_id),
                    idempotency_key=idempotency_key,
                    request_payload={"item_id": int(item_id), "allow_pinned": bool(allow_pinned)},
                ),
                audit=self._mutation_audit(
                    actor=actor,
                    actor_kind=actor_kind,
                    roles=roles,
                    scope={
                        "item_id": int(item_id),
                    },
                    reason_code="memory_item_delete",
                    reason="",
                    trace_id=trace_id,
                ),
                mutate=mutate,
            )

    async def forget_memory_items_idempotent(
        self: MemoryMutationPort,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        item_id: int | None,
        query: str,
        session_id: str,
        scope_type: str | None,
        allow_pinned: bool,
        limit: int,
        idempotency_key: str,
        actor: str,
        actor_kind: str,
        roles: Sequence[str],
        trace_id: str,
    ) -> MutationOutcome:
        request_payload = {
            "tenant_id": tenant_id,
            "channel": channel,
            "source_key": source_key,
            "user_id": user_id,
            "item_id": item_id,
            "query": query,
            "session_id": session_id,
            "scope_type": scope_type,
            "allow_pinned": bool(allow_pinned),
            "limit": int(limit),
        }
        async with self._mutation_transaction() as conn:
            async def mutate() -> MutationChange:
                result = await self.forget_memory_items(
                    tenant_id=tenant_id,
                    channel=channel,
                    source_key=source_key,
                    user_id=user_id,
                    item_id=item_id,
                    query=query,
                    session_id=session_id,
                    scope_type=scope_type,
                    allow_pinned=allow_pinned,
                    limit=limit,
                )
                ids = [int(value) for value in result.get("ids") or []]
                return MutationChange(
                    response={"ok": True, **result},
                    before_state={"matched_count": len(ids)},
                    after_state={"deleted_count": int(result.get("count") or 0)},
                    resource_version=hash_identifier(",".join(str(value) for value in ids)),
                )

            return await run_idempotent_mutation(
                conn,
                identity=MutationIdentity(
                    tenant_id=tenant_id,
                    plugin_name="memory",
                    operation="memory.items.forget",
                    resource_key=f"{session_id}:{user_id}",
                    idempotency_key=idempotency_key,
                    request_payload=request_payload,
                ),
                audit=self._mutation_audit(
                    actor=actor,
                    actor_kind=actor_kind,
                    roles=roles,
                    scope={
                        "session_hash": hash_identifier(session_id),
                        "user_hash": hash_identifier(user_id),
                        "source_hash": hash_identifier(source_key),
                        "item_id": int(item_id) if item_id is not None else None,
                    },
                    reason_code="memory_items_forget",
                    reason=query,
                    trace_id=trace_id,
                ),
                mutate=mutate,
            )

    async def correct_group_member_memory_item_idempotent(
        self: MemoryMutationPort,
        item_id: int,
        *,
        tenant_id: str,
        session_id: str,
        user_id: str,
        policy: MemberPrivacyValues,
        expected_etag: str,
        content: str,
        reason: str,
        idempotency_key: str,
        actor: str,
        actor_kind: str,
        roles: Sequence[str],
        trace_id: str,
    ) -> MutationOutcome:
        async with self._mutation_transaction() as conn:
            async def mutate() -> MutationChange:
                current = await self.get_group_member_memory_item(
                    item_id,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    user_id=user_id,
                    policy=policy,
                )
                if current is None:
                    raise MemoryMutationError("member_memory_not_found", status_code=404)
                updated = await self.correct_group_member_memory_item(
                    item_id,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    user_id=user_id,
                    policy=policy,
                    expected_etag=expected_etag,
                    content=content,
                )
                if updated is None:
                    raise MemoryMutationError("memory_correction_forbidden", status_code=403)
                return MutationChange(
                    response=updated,
                    before_state=self._item_audit_state(current),
                    after_state=self._item_audit_state(updated),
                    resource_version=str(updated.get("etag") or ""),
                )

            return await run_idempotent_mutation(
                conn,
                identity=MutationIdentity(
                    tenant_id=tenant_id,
                    plugin_name="memory",
                    operation="memory.member_item.correct",
                    resource_key=f"{session_id}:{user_id}:{item_id}",
                    idempotency_key=idempotency_key,
                    request_payload={
                        "item_id": int(item_id),
                        "expected_etag": expected_etag,
                        "content": content,
                        "reason": reason,
                    },
                ),
                audit=self._mutation_audit(
                    actor=actor,
                    actor_kind=actor_kind,
                    roles=roles,
                    scope={
                        "item_id": int(item_id),
                        "session_hash": hash_identifier(session_id),
                        "user_hash": hash_identifier(user_id),
                    },
                    reason_code="memory_member_item_correct",
                    reason=reason,
                    trace_id=trace_id,
                ),
                mutate=mutate,
            )

    async def delete_group_member_memory_item_idempotent(
        self: MemoryMutationPort,
        item_id: int,
        *,
        tenant_id: str,
        session_id: str,
        user_id: str,
        policy: MemberPrivacyValues,
        expected_etag: str,
        allow_pinned: bool,
        idempotency_key: str,
        actor: str,
        actor_kind: str,
        roles: Sequence[str],
        trace_id: str,
    ) -> MutationOutcome:
        async with self._mutation_transaction() as conn:
            async def mutate() -> MutationChange:
                current = await self.get_group_member_memory_item(
                    item_id,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    user_id=user_id,
                    policy=policy,
                )
                if current is None:
                    raise MemoryMutationError("member_memory_not_found", status_code=404)
                deleted = await self.delete_group_member_memory_item(
                    item_id,
                    tenant_id=tenant_id,
                    session_id=session_id,
                    user_id=user_id,
                    policy=policy,
                    expected_etag=expected_etag,
                    allow_pinned=allow_pinned,
                )
                if not deleted:
                    raise MemoryMutationError("memory_deletion_forbidden", status_code=403)
                return MutationChange(
                    response={
                        "item_id": int(item_id),
                        "status": "deleted",
                        "idempotent_replayed": False,
                    },
                    before_state=self._item_audit_state(current),
                    after_state={"exists": False, "item_id": int(item_id), "status": "deleted"},
                    resource_version=str(current.get("etag") or ""),
                )

            return await run_idempotent_mutation(
                conn,
                identity=MutationIdentity(
                    tenant_id=tenant_id,
                    plugin_name="memory",
                    operation="memory.member_item.delete",
                    resource_key=f"{session_id}:{user_id}:{item_id}",
                    idempotency_key=idempotency_key,
                    request_payload={
                        "item_id": int(item_id),
                        "expected_etag": expected_etag,
                        "allow_pinned": bool(allow_pinned),
                    },
                ),
                audit=self._mutation_audit(
                    actor=actor,
                    actor_kind=actor_kind,
                    roles=roles,
                    scope={
                        "item_id": int(item_id),
                        "session_hash": hash_identifier(session_id),
                        "user_hash": hash_identifier(user_id),
                    },
                    reason_code="memory_member_item_delete",
                    reason="",
                    trace_id=trace_id,
                ),
                mutate=mutate,
            )
