"""
REST API for the user memory plugin.

Mounted at ``/plugins/memory/`` by the plugin framework.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from app.admin.auth_router import authenticate_admin_request, is_admin_request
from app.admin.mutation_ledger import MutationIdempotencyConflictError
from app.channel.identity import require_legacy_wxbot_history_scope
from app.common.request_models import StrictRequestModel
from plugins.memory.store import (
    GROUP_GRAPH_EDGE_TYPES,
    GROUP_GRAPH_NODE_TYPES,
    GROUP_GRAPH_SCHEMA_VERSION,
    MEMORY_ACCEPTANCE_REVIEW_ACTIONS,
    MEMORY_ACCEPTANCE_STATUSES,
    MEMORY_EXTRACTION_JOB_STATUSES,
    PROFILE_ENRICHMENT_ACCEPTANCE_STATUSES,
    PROFILE_ENRICHMENT_REVIEW_ACTIONS,
    MemoryItemConflictError,
    MemoryItemProtectedError,
    MemoryMutationError,
    MemoryProfileConflictError,
    MemoryStore,
    memory_item_version,
)

_GROUP_GRAPH_RAW_FIELD_KEYS = {
    "content",
    "value",
    "value_json",
    "original_text",
    "object_value",
    "raw_content",
    "raw_text",
    "message_text",
    "user_text",
    "assistant_text",
    "summary",
}

_GROUP_GRAPH_PUBLIC_ACCEPTANCE_STATUSES = {"accepted"}


def _scrub_group_graph_payload(value: Any) -> Any:
    if isinstance(value, list):
        return [_scrub_group_graph_payload(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _scrub_group_graph_payload(item)
            for key, item in value.items()
            if str(key).lower() not in _GROUP_GRAPH_RAW_FIELD_KEYS
        }
    return value


_SAFE_MEMORY_ITEM_FIELDS = {
    "id",
    "tenant_id",
    "channel",
    "source_key",
    "user_id",
    "session_id",
    "scope_type",
    "source_type",
    "memory_type",
    "normalized_key",
    "confidence",
    "status",
    "pinned",
    "priority",
    "sensitivity",
    "audience_scope",
    "origin_session_kind",
    "allowed_session_ids",
    "source_kind",
    "sensitivity_category",
    "expires_at",
    "source_event_id",
    "source_trace_id",
    "occurrence_count",
    "first_seen_at",
    "last_seen_at",
    "created_at",
    "updated_at",
    "deleted_at",
    "acceptance_status",
    "acceptance_score",
    "acceptance_reason",
    "extraction_confidence",
    "duplicate_hint",
    "possible_conflicts",
}

_SAFE_EVENT_FIELDS = {
    "id",
    "tenant_id",
    "channel",
    "source_key",
    "user_id",
    "session_id",
    "trace_id",
    "event_key",
    "created_at",
}

_SAFE_PROFILE_FIELDS = {
    "tenant_id",
    "channel",
    "source_key",
    "user_id",
    "session_id",
    "message_count",
    "identity_message_count",
    "session_message_count",
    "imported_message_count",
    "identity_imported_message_count",
    "session_imported_message_count",
    "last_seen_at",
    "updated_at",
    "summary_version",
}

_SAFE_GRAPH_ENTITY_FIELDS = {
    "id",
    "tenant_id",
    "channel",
    "source_key",
    "user_id",
    "entity_type",
    "name",
    "normalized_name",
    "aliases",
    "confidence",
    "status",
    "created_at",
    "updated_at",
}

_SAFE_GRAPH_FACT_FIELDS = {
    "id",
    "tenant_id",
    "channel",
    "source_key",
    "user_id",
    "subject_entity_id",
    "subject_name",
    "predicate",
    "object_entity_id",
    "object_name",
    "memory_item_id",
    "source_event_id",
    "confidence",
    "status",
    "valid_at",
    "invalid_at",
    "created_at",
    "updated_at",
}

_SAFE_GRAPH_EPISODE_FIELDS = {
    "id",
    "tenant_id",
    "channel",
    "source_key",
    "user_id",
    "session_id",
    "event_ids",
    "memory_item_ids",
    "importance",
    "status",
    "created_at",
    "updated_at",
}


def _pick_fields(row: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    return {key: row.get(key) for key in allowed if key in row}


def _shape_read_rows(
    rows: list[dict[str, Any]],
    *,
    is_admin: bool,
    safe_row: Callable[[dict[str, Any]], dict[str, Any]],
) -> list[dict[str, Any]]:
    return rows if is_admin else [safe_row(row) for row in rows]


def _shape_read_payload(
    payload: dict[str, Any],
    *,
    is_admin: bool,
    safe_payload: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    return payload if is_admin else safe_payload(payload)


def _safe_memory_item_row(row: dict[str, Any]) -> dict[str, Any]:
    return _pick_fields(row, _SAFE_MEMORY_ITEM_FIELDS)


def _safe_event_row(row: dict[str, Any]) -> dict[str, Any]:
    return _pick_fields(row, _SAFE_EVENT_FIELDS)


def _safe_profile_row(row: dict[str, Any]) -> dict[str, Any]:
    return _pick_fields(row, _SAFE_PROFILE_FIELDS)


def _safe_graph_entity_row(row: dict[str, Any]) -> dict[str, Any]:
    return _pick_fields(row, _SAFE_GRAPH_ENTITY_FIELDS)


def _safe_graph_fact_row(row: dict[str, Any]) -> dict[str, Any]:
    return _pick_fields(row, _SAFE_GRAPH_FACT_FIELDS)


def _safe_graph_episode_row(row: dict[str, Any]) -> dict[str, Any]:
    return _pick_fields(row, _SAFE_GRAPH_EPISODE_FIELDS)


def _safe_graph_preview_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "entities": [_safe_graph_entity_row(row) for row in payload.get("entities") or []],
        "facts": [_safe_graph_fact_row(row) for row in payload.get("facts") or []],
        "episodes": [_safe_graph_episode_row(row) for row in payload.get("episodes") or []],
        "counts": payload.get("counts") or {},
    }


class MemoryProfileUpsertRequest(StrictRequestModel):
    tenant_id: str
    channel: str = "wechat"
    source_key: str = "wxbot"
    user_id: str
    long_term_memory: str | None = None
    manual_notes: str | None = None
    expected_version: str | None = Field(default=None, max_length=128)


class SessionMemoryProfileUpsertRequest(StrictRequestModel):
    tenant_id: str
    channel: str = "wechat"
    source_key: str = "wxbot"
    session_id: str
    user_id: str
    short_term_memory: str | None = None
    manual_notes: str | None = None
    expected_version: str | None = Field(default=None, max_length=128)


class MemoryBackfillRequest(StrictRequestModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    connection_id: str = Field(max_length=64)
    channel: str = Field(default="wechat", min_length=1, max_length=64)
    source_key: str = Field(default="wxbot", min_length=1, max_length=128)
    user_id: str | None = Field(default=None, min_length=1, max_length=256)
    session_ids: list[str] = Field(default_factory=list, min_length=1, max_length=200)
    days_limit: int = Field(default=180, ge=0, le=3650)
    max_messages_per_session: int = Field(default=200, ge=1, le=500)
    enqueue_llm_jobs: bool = False
    target_date: str | None = Field(default=None, max_length=32)


class MemoryVectorRebuildRequest(StrictRequestModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    channel: str | None = Field(default=None, min_length=1, max_length=64)
    source_key: str | None = Field(default=None, min_length=1, max_length=128)
    user_id: str | None = Field(default=None, min_length=1, max_length=256)
    limit: int = Field(default=1000, ge=1, le=5000)
    dry_run: bool = False


class MemoryVectorSmokeRequest(StrictRequestModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    channel: str | None = None
    source_key: str | None = None
    user_id: str | None = None
    session_id: str = ""
    query: str = "memory vector smoke"
    limit: int = Field(default=3, ge=1, le=20)
    dry_run: bool = True


class MemoryAcceptanceLegacyBackfillRequest(StrictRequestModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    channel: str | None = Field(default=None, min_length=1, max_length=64)
    source_key: str | None = Field(default=None, min_length=1, max_length=128)
    user_id: str | None = Field(default=None, min_length=1, max_length=256)
    session_id: str | None = Field(default=None, min_length=1, max_length=256)
    scope_type: Literal["identity", "session"] | None = None
    source_type: Literal["manual", "auto", "explicit_user", "backfill"] | None = None
    memory_type: Literal["preference", "constraint", "profile_fact", "note", "episodic"] | None = (
        None
    )
    status: Literal["active", "pending", "archived", "invalidated"] | None = None
    dry_run: bool = True
    max_items: int | None = Field(default=None, ge=1, le=10000)
    mark_missing_as: Literal["needs_review", "candidate"] = "needs_review"


class MemoryGovernanceCleanupRequest(StrictRequestModel):
    dry_run: bool = True
    needs_review_days: int | None = Field(default=None, ge=1, le=3650)
    rejected_days: int | None = Field(default=None, ge=1, le=3650)
    auto_expire_days: int | None = Field(default=None, ge=1, le=3650)
    limit: int | None = Field(default=None, ge=1, le=5000)


class MemoryExtractionJobMaintenanceRequest(StrictRequestModel):
    action: Literal["reset_stale", "retry", "mark_dead", "cleanup_smoke"] | None = None
    actions: list[Literal["reset_stale", "retry", "mark_dead", "cleanup_smoke"]] = Field(
        default_factory=list,
        max_length=4,
    )
    tenant_id: str | None = Field(default=None, min_length=1, max_length=64)
    channel: str | None = Field(default=None, min_length=1, max_length=64)
    source_key: str | None = Field(default=None, min_length=1, max_length=128)
    user_id: str | None = Field(default=None, min_length=1, max_length=256)
    session_id: str | None = Field(default=None, min_length=1, max_length=256)
    status: Literal["pending", "running", "succeeded", "failed", "dead"] | None = None
    error_type: str | None = Field(default=None, min_length=1, max_length=128)
    created_before: datetime | None = None
    created_after: datetime | None = None
    updated_before: datetime | None = None
    updated_after: datetime | None = None
    limit: int = Field(default=100, ge=1, le=100)
    dry_run: bool = True


class MemoryItemCreateRequest(StrictRequestModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    channel: str = Field(default="wechat", min_length=1, max_length=64)
    source_key: str = Field(default="wxbot", min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(default="", max_length=256)
    scope_type: Literal["identity", "session"] = "identity"
    source_type: Literal["manual", "auto", "explicit_user", "backfill"] = "manual"
    memory_type: Literal["preference", "constraint", "profile_fact", "note", "episodic"] = "note"
    content: str = Field(min_length=1, max_length=500)
    value_json: dict | list | str | int | float | bool | None = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    status: Literal["active", "pending", "archived", "invalidated"] = "active"
    pinned: bool = True
    priority: int = Field(default=100, ge=0, le=1000)
    sensitivity: Literal["normal", "pii", "sensitive"] = "normal"
    audience_scope: Literal["private", "session", "explicit"] | None = None
    origin_session_kind: Literal["private", "group", "unknown"] | None = None
    allowed_session_ids: list[str] = Field(default_factory=list, max_length=100)
    source_kind: Literal["conversation", "manual", "backfill", "graph", "profile"] = "manual"
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    expires_at: datetime | None = None
    source_trace_id: str = Field(default="", max_length=128)
    original_text: str = Field(default="", max_length=4000)


class MemoryItemUpdateRequest(StrictRequestModel):
    content: str | None = Field(default=None, min_length=1, max_length=500)
    value_json: dict | list | str | int | float | bool | None = None
    memory_type: Literal["preference", "constraint", "profile_fact", "note", "episodic"] | None = (
        None
    )
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    status: Literal["active", "pending", "archived", "invalidated"] | None = None
    pinned: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=1000)
    sensitivity: Literal["normal", "pii", "sensitive"] | None = None
    original_text: str | None = Field(default=None, max_length=4000)


class MemoryAcceptanceReviewRequest(StrictRequestModel):
    action: str
    review_reason: str | None = None
    reviewed_by: str | None = None
    superseded_by_item_id: int | None = None
    supersedes_item_id: int | None = None


class ProfileEnrichmentCandidateCreateRequest(StrictRequestModel):
    tenant_id: str
    channel: str = "wechat"
    source_key: str = "wxbot"
    session_id: str
    user_id: str
    report_payload: dict[str, Any] = Field(default_factory=dict)
    created_by: str | None = None


class ProfileEnrichmentCandidateFromReportRequest(StrictRequestModel):
    tenant_id: str
    channel: str = "wechat"
    source_key: str = "wxbot"
    session_id: str
    user_id: str
    query: str
    hours: int = Field(default=168, ge=1, le=720)
    limit: int = Field(default=8, ge=1, le=20)
    external_candidates: list[dict[str, Any]] = Field(default_factory=list)
    created_by: str | None = None


class ProfileEnrichmentCandidateReviewRequest(StrictRequestModel):
    action: str
    notes: str | None = None
    reviewed_by: str | None = None


class MemoryDailyRelationshipExtractionRequest(StrictRequestModel):
    tenant_id: str
    channel: str = "wechat"
    source_key: str = "wxbot"
    session_id: str
    date: str
    user_id: str | None = None
    limit: int | None = Field(default=None, ge=1, le=20)
    batch_limit: int | None = None
    max_jobs: int | None = None
    continuous: bool | None = None
    time_budget_seconds: int | None = None

    def extraction_controls(self) -> dict[str, int | bool]:
        fields_set = (
            self.model_fields_set
            if hasattr(self, "model_fields_set")
            else getattr(self, "__fields_set__", set())
        )
        new_fields = {"batch_limit", "max_jobs", "continuous", "time_budget_seconds"}
        has_new_fields = bool(new_fields.intersection(fields_set))
        if not has_new_fields and self.limit is not None:
            batch_limit = max(1, min(int(self.limit or 5), 20))
            continuous = False
            max_jobs = batch_limit
        else:
            batch_limit = max(1, min(int(self.batch_limit or 50), 100))
            continuous = bool(self.continuous)
            default_max_jobs = 200 if continuous else batch_limit
            max_jobs = max(1, min(int(self.max_jobs or default_max_jobs), 500))
        time_budget_seconds = max(1, min(int(self.time_budget_seconds or 60), 180))
        return {
            "batch_limit": batch_limit,
            "max_jobs": max_jobs,
            "continuous": continuous,
            "time_budget_seconds": time_budget_seconds,
        }


class MemoryWindowRelationshipExtractionRequest(StrictRequestModel):
    tenant_id: str
    channel: str = "wechat"
    source_key: str = "wxbot"
    session_id: str
    date: str
    user_id: str | None = None
    window_size: int | None = None
    max_windows: int | None = None
    cursor_event_id: int | None = None
    dry_run: bool = False

    def extraction_controls(self) -> dict[str, int | bool]:
        return {
            "window_size": max(10, min(int(self.window_size or 50), 100)),
            "max_windows": max(1, min(int(self.max_windows or 1), 10)),
            "cursor_event_id": max(0, int(self.cursor_event_id or 0)),
            "dry_run": bool(self.dry_run),
        }


class MemoryWindowRelationshipCatchupRequest(StrictRequestModel):
    tenant_id: str
    channel: str = "wechat"
    source_key: str = "wxbot"
    session_id: str
    date: str
    user_id: str | None = None
    window_size: int | None = None
    max_windows_per_run: int | None = None
    cursor_event_id: int | None = None
    dry_run: bool = False
    time_budget_seconds: int | None = None

    def extraction_controls(self) -> dict[str, int | bool]:
        return {
            "window_size": max(10, min(int(self.window_size or 50), 100)),
            "max_windows_per_run": max(1, min(int(self.max_windows_per_run or 20), 100)),
            "cursor_event_id": max(0, int(self.cursor_event_id or 0)),
            "dry_run": bool(self.dry_run),
            "time_budget_seconds": max(1, min(int(self.time_budget_seconds or 60), 180)),
        }


class MemoryEdgeAcceptanceReviewRequest(StrictRequestModel):
    tenant_id: str
    channel: str | None = None
    source_key: str | None = None
    session_id: str | None = None
    action: str
    review_reason: str | None = None
    reviewed_by: str | None = None
    superseded_by_item_id: int | None = None
    supersedes_item_id: int | None = None


class MemoryControlScope(StrictRequestModel):
    tenant_id: str
    channel: str = "wechat"
    source_key: str = "wxbot"
    current_user_id: str | None = None
    user_id: str | None = None
    session_id: str = ""
    scope_type: str | None = None


class MemoryRememberRequest(MemoryControlScope):
    content: str
    memory_type: str = "note"
    value_json: dict | list | str | int | float | bool | None = Field(default_factory=dict)
    status: str = "active"
    pinned: bool = True
    priority: int = 100
    sensitivity: str = "normal"
    audience_scope: Literal["private", "session", "explicit"] | None = None
    origin_session_kind: Literal["private", "group", "unknown"] | None = None
    allowed_session_ids: list[str] = Field(default_factory=list, max_length=100)
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    expires_at: datetime | None = None


class MemorySearchRequest(MemoryControlScope):
    query: str = ""
    limit: int = Field(default=6, ge=1, le=20)
    debug: bool = False


class MemoryForgetRequest(MemoryControlScope):
    item_id: int | None = None
    query: str = ""
    allow_pinned: bool = False
    limit: int = Field(default=20, ge=1, le=20)


class MemoryControlUpdateRequest(MemoryControlScope):
    item_id: int
    content: str | None = None
    value_json: dict | list | str | int | float | bool | None = None
    memory_type: str | None = None
    confidence: float | None = None
    status: str | None = None
    pinned: bool | None = None
    priority: int | None = None
    sensitivity: str | None = None
    original_text: str | None = None


def _is_admin_request(request: Request, store: MemoryStore) -> bool:
    return is_admin_request(request, store.settings)


def _current_user_from_request(request: Request, body: MemoryControlScope) -> str:
    return (
        request.headers.get("X-User-Id")
        or request.headers.get("X-Actor-ID")
        or str(body.current_user_id or "").strip()
    )


def _resolve_memory_target_user(
    request: Request, body: MemoryControlScope, store: MemoryStore
) -> str:
    current_user_id = _current_user_from_request(request, body)
    requested_user_id = str(body.user_id or "").strip()
    if requested_user_id:
        if current_user_id and requested_user_id == current_user_id:
            return requested_user_id
        if _is_admin_request(request, store):
            return requested_user_id
        raise HTTPException(status_code=403, detail="admin access required to target another user")
    if current_user_id:
        return current_user_id
    raise HTTPException(status_code=400, detail="current user required")


def _require_admin_request(request: Request, store: MemoryStore) -> None:
    authenticate_admin_request(request, store.settings)


def _admin_actor(request: Request, explicit_actor: str | None = None) -> str:
    return (
        str(explicit_actor or "").strip()
        or request.headers.get("X-Actor-ID")
        or request.headers.get("X-User-Id")
        or "admin/api"
    )


def _required_mutation_key(value: str | None) -> str:
    if value is None:
        raise HTTPException(
            status_code=428,
            detail={"code": "idempotency_key_required"},
        )
    normalized = str(value).strip()
    if not normalized or len(normalized) > 128:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_idempotency_key"},
        )
    return normalized


def _mutation_actor_context(
    request: Request,
    *,
    fallback_actor: str = "",
) -> tuple[str, str, tuple[str, ...], str]:
    principal = getattr(request.state, "admin_principal", None)
    actor = str(
        getattr(principal, "subject", "")
        or fallback_actor
        or request.headers.get("X-User-Id", "")
        or request.headers.get("X-Actor-ID", "")
        or "unknown"
    )[:128]
    actor_kind = str(
        getattr(principal, "auth_kind", "") or ("user" if fallback_actor else "unknown")
    )[:32]
    roles = tuple(str(role)[:64] for role in (getattr(principal, "roles", ()) or ()))
    trace_id = str(
        getattr(request.state, "admin_request_id", "")
        or request.headers.get("X-Trace-ID", "")
        or request.headers.get("X-Request-ID", "")
    )[:128]
    return actor, actor_kind, roles, trace_id


def _raise_mutation_error(exc: Exception) -> None:
    if isinstance(exc, MutationIdempotencyConflictError):
        raise HTTPException(
            status_code=409,
            detail={"code": "idempotency_key_conflict"},
        ) from exc
    if isinstance(exc, MemoryMutationError):
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    raise exc


def _current_user_for_read(
    request: Request,
    *,
    requested_user_id: str | None = None,
    store: MemoryStore,
) -> tuple[str | None, bool]:
    if _is_admin_request(request, store):
        return requested_user_id, True
    current_user_id = (
        request.headers.get("X-User-Id") or request.headers.get("X-Actor-ID") or ""
    ).strip()
    requested = str(requested_user_id or "").strip()
    if requested:
        if current_user_id and requested == current_user_id:
            return requested, False
        raise HTTPException(status_code=403, detail="admin access required to target another user")
    if current_user_id:
        return current_user_id, False
    raise HTTPException(status_code=403, detail="admin access required")


def _body_updates(body: BaseModel, *, exclude: set[str]) -> dict[str, Any]:
    if hasattr(body, "model_dump"):
        return body.model_dump(exclude_none=True, exclude=exclude)
    return body.dict(
        exclude_none=True, exclude=exclude
    )  # pragma: no cover - pydantic v1 compatibility


def _is_group_memory_session(session_id: str) -> bool:
    return str(session_id or "").strip().lower().endswith("@chatroom")


def _manual_memory_write_fields(
    body: MemoryItemCreateRequest | MemoryRememberRequest,
    *,
    allow_explicit_audience: bool,
) -> dict[str, Any]:
    session_id = str(body.session_id or "").strip()
    requested_scope = str(body.scope_type or "identity").strip().lower()
    if requested_scope not in {"identity", "session"}:
        raise HTTPException(status_code=400, detail="scope_type must be identity or session")
    if requested_scope == "session" and not session_id:
        raise HTTPException(status_code=400, detail="session_id required for session memory")
    if body.retention_days is not None and body.expires_at is not None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "memory_retention_conflict",
                "message": "retention_days and expires_at are mutually exclusive",
            },
        )

    allowed_session_ids = list(
        dict.fromkeys(
            normalized
            for value in body.allowed_session_ids
            if (normalized := str(value or "").strip())
        )
    )
    expires_at = body.expires_at
    if body.retention_days is not None:
        expires_at = datetime.now(UTC) + timedelta(days=int(body.retention_days))

    if _is_group_memory_session(session_id):
        # A manual item created from a group management surface must be
        # recallable in that same group and nowhere else. Do not accept a
        # private/unknown default that would report success but fail retrieval.
        return {
            "session_id": session_id,
            "scope_type": "session",
            "origin_session_kind": "group",
            "audience_scope": "session",
            "allowed_session_ids": [session_id],
            "source_kind": "manual",
            "expires_at": expires_at,
        }

    audience_scope = str(body.audience_scope or "private").strip().lower()
    origin_session_kind = str(body.origin_session_kind or "private").strip().lower()
    if not allow_explicit_audience and (
        audience_scope != "private" or origin_session_kind != "private" or allowed_session_ids
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "admin_required_for_audience_expansion"},
        )
    if audience_scope in {"session", "explicit"} and not allowed_session_ids:
        raise HTTPException(
            status_code=400,
            detail={"code": "allowed_session_ids_required"},
        )
    if audience_scope == "session" and (
        origin_session_kind != "group"
        or len(allowed_session_ids) != 1
        or not _is_group_memory_session(allowed_session_ids[0])
    ):
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_session_audience_contract"},
        )
    if audience_scope == "private":
        allowed_session_ids = []
    return {
        "session_id": session_id if requested_scope == "session" else "",
        "scope_type": requested_scope,
        "origin_session_kind": origin_session_kind,
        "audience_scope": audience_scope,
        "allowed_session_ids": allowed_session_ids,
        "source_kind": "manual",
        "expires_at": expires_at,
    }


_EXTRACTION_JOB_SAFE_FIELDS = {
    "id",
    "tenant_id",
    "channel",
    "source_key",
    "user_id",
    "session_id",
    "source_event_id",
    "source_trace_id",
    "status",
    "error_type",
    "attempts",
    "max_attempts",
    "next_run_at",
    "locked_until",
    "created_at",
    "updated_at",
}


def _safe_extraction_job_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in _EXTRACTION_JOB_SAFE_FIELDS if key in row}


_MEMORY_RUNTIME_SETTING_KEYS = (
    "memory_llm_extraction_enabled",
    "memory_llm_extraction_job_enabled",
    "memory_llm_extraction_job_drain_enabled",
    "memory_retrieval_enabled",
    "memory_group_identity_memory_enabled",
    "memory_hybrid_retrieval_enabled",
    "memory_vector_index_enabled",
    "memory_graph_retrieval_enabled",
    "memory_graph_llm_extraction_enabled",
    "memory_governance_auto_cleanup_enabled",
)


def _memory_runtime_config(settings: Any) -> dict[str, Any]:
    return {key: getattr(settings, key, None) for key in _MEMORY_RUNTIME_SETTING_KEYS}


def _parse_status_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _status_count(payload: dict[str, Any], key: str) -> int:
    values = payload.get("status_counts") or payload.get("counts") or {}
    if not isinstance(values, dict):
        return 0
    try:
        return max(0, int(values.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def _memory_management_diagnostics(
    *,
    config: dict[str, Any],
    runtime_scope: dict[str, Any],
    job_stats: dict[str, Any],
    acceptance_stats: dict[str, Any],
    items: list[dict[str, Any]],
    session_id: str,
) -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    if config.get("memory_retrieval_enabled") is False:
        diagnostics.append(
            {
                "area": "recall",
                "status": "blocked",
                "code": "retrieval_disabled",
                "message": "当前进程已关闭记忆召回，已保存记录不会进入回复上下文。",
            }
        )
    if runtime_scope.get("status") == "disabled":
        diagnostics.append(
            {
                "area": "recall_and_write",
                "status": "blocked",
                "code": "runtime_scope_disabled",
                "message": "当前租户/会话的 memory 运行时开关未启用。",
            }
        )
    if config.get("memory_llm_extraction_enabled") is False:
        diagnostics.append(
            {
                "area": "write",
                "status": "info",
                "code": "automatic_extraction_disabled",
                "message": "LLM 增强抽取已关闭；基础规则抽取和手工记忆仍可用。",
            }
        )
    if (
        config.get("memory_llm_extraction_enabled") is True
        and config.get("memory_llm_extraction_job_enabled") is True
        and config.get("memory_llm_extraction_job_drain_enabled") is False
    ):
        diagnostics.append(
            {
                "area": "write",
                "status": "warning",
                "code": "job_drain_disabled",
                "message": "抽取任务可入队但消费开关关闭，任务可能持续停留在待处理状态。",
            }
        )
    failed_jobs = _status_count(job_stats, "failed") + _status_count(job_stats, "dead")
    if failed_jobs:
        diagnostics.append(
            {
                "area": "write",
                "status": "warning",
                "code": "extraction_jobs_failed",
                "message": f"当前筛选范围有 {failed_jobs} 个失败或终止的抽取任务。",
            }
        )
    acceptance_counts = acceptance_stats.get("counts") or {}
    needs_review = 0
    if isinstance(acceptance_counts, dict):
        for key in ("needs_review", "candidate", "missing_acceptance"):
            try:
                needs_review += max(0, int(acceptance_counts.get(key) or 0))
            except (TypeError, ValueError):
                continue
    if needs_review:
        diagnostics.append(
            {
                "area": "recall",
                "status": "warning",
                "code": "items_waiting_for_review",
                "message": f"有 {needs_review} 条候选或旧版记忆尚未完成复核，可能不会参与正常召回。",
            }
        )
    if _is_group_memory_session(session_id):
        visible_group_items = [
            item
            for item in items
            if str(item.get("origin_session_kind") or "") == "group"
            and str(item.get("audience_scope") or "") == "session"
            and session_id
            in {str(value or "").strip() for value in (item.get("allowed_session_ids") or [])}
        ]
        if items and not visible_group_items:
            diagnostics.append(
                {
                    "area": "recall",
                    "status": "blocked",
                    "code": "group_audience_mismatch",
                    "message": "当前范围有记忆，但没有记录声明为当前群可见；请检查 audience/origin/allowed_session_ids。",
                }
            )
    if not diagnostics:
        diagnostics.append(
            {
                "area": "runtime",
                "status": "ok",
                "code": "no_obvious_blocker",
                "message": "未发现配置、任务或可见性层面的明显阻断；可继续用检索调试检查查询匹配。",
            }
        )
    return diagnostics


def _extraction_job_filter_updates(body: MemoryExtractionJobMaintenanceRequest) -> dict[str, Any]:
    updates = _body_updates(body, exclude={"action", "actions", "dry_run", "limit"})
    filters: dict[str, Any] = {}
    for key, value in updates.items():
        if key == "status":
            status = str(value or "").strip().lower()
            if not status:
                continue
            if status not in MEMORY_EXTRACTION_JOB_STATUSES:
                raise HTTPException(
                    status_code=400, detail=f"unsupported extraction job status: {status}"
                )
            filters[key] = status
        elif isinstance(value, str):
            stripped = value.strip()
            if stripped:
                filters[key] = stripped
        else:
            filters[key] = value
    return filters


def _extraction_job_actions(body: MemoryExtractionJobMaintenanceRequest) -> list[str]:
    raw_actions = [body.action, *body.actions]
    actions: list[str] = []
    for raw_action in raw_actions:
        action = str(raw_action or "").strip().lower()
        if action and action not in actions:
            actions.append(action)
    return actions


def _has_extraction_job_filters(body: MemoryExtractionJobMaintenanceRequest) -> bool:
    return bool(_extraction_job_filter_updates(body))


def _has_smoke_scope_filter(body: MemoryExtractionJobMaintenanceRequest) -> bool:
    for field_name in ("tenant_id", "channel", "source_key", "user_id", "session_id"):
        value = str(getattr(body, field_name, "") or "").lower()
        if "smoke" in value or "test" in value:
            return True
    return False


def _memory_acceptance_filters(
    *,
    tenant_id: str,
    channel: str | None = None,
    source_key: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    scope_type: str | None = None,
    source_type: str | None = None,
    memory_type: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    filters: dict[str, Any] = {"tenant_id": tenant_id}
    for key, value in {
        "channel": channel,
        "source_key": source_key,
        "user_id": user_id,
        "session_id": session_id,
        "scope_type": scope_type,
        "source_type": source_type,
        "memory_type": memory_type,
        "status": status,
    }.items():
        if value is None:
            continue
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                filters[key] = stripped
        else:
            filters[key] = value
    return filters


def _normalize_acceptance_status_filter(value: str | None) -> str | None:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    if normalized not in {*MEMORY_ACCEPTANCE_STATUSES, "missing_acceptance", "unknown_acceptance"}:
        raise HTTPException(status_code=400, detail="unsupported acceptance_status filter")
    return normalized


def _require_admin_for_review_group_graph_statuses(
    request: Request,
    store: MemoryStore,
    acceptance_status: str | None,
) -> None:
    requested_acceptance = {
        value.strip().lower() for value in str(acceptance_status or "").split(",") if value.strip()
    }
    if requested_acceptance - _GROUP_GRAPH_PUBLIC_ACCEPTANCE_STATUSES:
        _require_admin_request(request, store)


ProfileReportBuilder = Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]]
ScopeExecutionAllowed = Callable[[str, str], Awaitable[bool]]
GroupMembershipAuthorizer = Callable[[str, str, str], Awaitable[bool]]


async def _require_runtime_scope(
    gate: ScopeExecutionAllowed | None,
    *,
    tenant_id: str,
    session_id: str,
    owner: str,
    required: bool,
) -> None:
    if not callable(gate):
        if not required:
            return
        raise HTTPException(status_code=503, detail=f"{owner}_runtime_gate_unavailable")
    try:
        allowed = await gate(str(tenant_id or ""), str(session_id or ""))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"{owner}_runtime_disabled") from exc
    if allowed is not True:
        raise HTTPException(status_code=503, detail=f"{owner}_runtime_disabled")


async def _require_group_read_access(
    request: Request,
    store: MemoryStore,
    authorizer: GroupMembershipAuthorizer | None,
    *,
    tenant_id: str,
    session_id: str | None,
) -> bool:
    if _is_admin_request(request, store):
        return True
    if authorizer is None:
        # Without a trusted membership provider, retain the fail-closed
        # admin-only behavior instead of treating an unverifiable caller as a
        # group member.
        _require_admin_request(request, store)
        return True
    current_user_id, _is_admin = _current_user_for_read(request, store=store)
    normalized_session_id = str(session_id or "").strip()
    if not normalized_session_id or authorizer is None or not current_user_id:
        raise HTTPException(status_code=403, detail="active group membership required")
    if not await authorizer(tenant_id, normalized_session_id, current_user_id):
        raise HTTPException(status_code=403, detail="active group membership required")
    return False


def build_memory_router(
    store: MemoryStore,
    *,
    profile_report_builder: ProfileReportBuilder | None = None,
    scope_execution_allowed: ScopeExecutionAllowed | None = None,
    history_scope_execution_allowed: ScopeExecutionAllowed | None = None,
    combined_scope_execution_allowed: ScopeExecutionAllowed | None = None,
    group_membership_authorizer: GroupMembershipAuthorizer | None = None,
) -> APIRouter:
    router = APIRouter()
    runtime_gates_required = bool(getattr(store, "runtime_scope_gates_required", False))

    @router.get("/profiles")
    async def list_profiles(
        request: Request,
        tenant_id: str = Query(...),
        channel: str | None = Query(default=None),
        source_key: str | None = Query(default=None),
        user_id: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
    ):
        user_id, is_admin = _current_user_for_read(request, requested_user_id=user_id, store=store)
        rows = await store.list_profiles(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            limit=limit,
        )
        return {"items": _shape_read_rows(rows, is_admin=is_admin, safe_row=_safe_profile_row)}

    @router.get("/profiles/{tenant_id}/{channel}/{source_key}/{user_id:path}")
    async def get_identity_profile(
        request: Request, tenant_id: str, channel: str, source_key: str, user_id: str
    ):
        _resolved_user_id, is_admin = _current_user_for_read(
            request, requested_user_id=user_id, store=store
        )
        profile = await store.get_identity_profile(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
        )
        return _shape_read_payload(profile, is_admin=is_admin, safe_payload=_safe_profile_row)

    @router.post("/profiles")
    async def upsert_profile(request: Request, body: MemoryProfileUpsertRequest):
        _require_admin_request(request, store)
        try:
            profile = await store.upsert_identity_profile(
                tenant_id=body.tenant_id,
                channel=body.channel,
                source_key=body.source_key,
                user_id=body.user_id,
                long_term_memory=body.long_term_memory,
                manual_notes=body.manual_notes,
                expected_version=body.expected_version,
            )
        except MemoryProfileConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": exc.code,
                    "message": "记忆档案已被其他操作更新，请刷新后重试。",
                    "expected_version": exc.expected_version,
                    "actual_version": exc.actual_version,
                },
            ) from exc
        except Exception as exc:
            _raise_mutation_error(exc)
        return profile

    @router.get("/session-profiles")
    async def list_session_profiles(
        request: Request,
        tenant_id: str = Query(...),
        channel: str | None = Query(default=None),
        source_key: str | None = Query(default=None),
        session_id: str | None = Query(default=None),
        user_id: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
    ):
        user_id, is_admin = _current_user_for_read(request, requested_user_id=user_id, store=store)
        rows = await store.list_session_profiles(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            session_id=session_id,
            user_id=user_id,
            limit=limit,
        )
        return {"items": _shape_read_rows(rows, is_admin=is_admin, safe_row=_safe_profile_row)}

    @router.get("/session-profiles/{tenant_id}/{channel}/{source_key}/{session_id:path}")
    async def get_session_profile(
        request: Request,
        tenant_id: str,
        channel: str,
        source_key: str,
        session_id: str,
        user_id: str = Query(...),
    ):
        _resolved_user_id, is_admin = _current_user_for_read(
            request, requested_user_id=user_id, store=store
        )
        profile = await store.get_session_profile(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            session_id=session_id,
            user_id=user_id,
        )
        return _shape_read_payload(profile, is_admin=is_admin, safe_payload=_safe_profile_row)

    @router.post("/session-profiles")
    async def upsert_session_profile(request: Request, body: SessionMemoryProfileUpsertRequest):
        _require_admin_request(request, store)
        try:
            profile = await store.upsert_session_profile(
                tenant_id=body.tenant_id,
                channel=body.channel,
                source_key=body.source_key,
                session_id=body.session_id,
                user_id=body.user_id,
                short_term_memory=body.short_term_memory,
                manual_notes=body.manual_notes,
                expected_version=body.expected_version,
            )
        except MemoryProfileConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": exc.code,
                    "message": "会话记忆档案已被其他操作更新，请刷新后重试。",
                    "expected_version": exc.expected_version,
                    "actual_version": exc.actual_version,
                },
            ) from exc
        except Exception as exc:
            _raise_mutation_error(exc)
        return profile

    @router.get("/runtime-profile/{tenant_id}/{channel}/{source_key}/{session_id:path}")
    async def get_runtime_profile(
        request: Request,
        tenant_id: str,
        channel: str,
        source_key: str,
        session_id: str,
        user_id: str = Query(...),
    ):
        _resolved_user_id, is_admin = _current_user_for_read(
            request, requested_user_id=user_id, store=store
        )
        profile = await store.get_runtime_profile(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            session_id=session_id,
            user_id=user_id,
        )
        return _shape_read_payload(profile, is_admin=is_admin, safe_payload=_safe_profile_row)

    @router.get("/management-status")
    async def get_memory_management_status(
        request: Request,
        tenant_id: str = Query(..., min_length=1, max_length=64),
        channel: str = Query(default="wechat", min_length=1, max_length=64),
        source_key: str = Query(default="wxbot", min_length=1, max_length=128),
        session_id: str = Query(default="", max_length=256),
        user_id: str | None = Query(default=None, min_length=1, max_length=256),
        recent_job_limit: int = Query(default=10, ge=1, le=50),
    ):
        _require_admin_request(request, store)
        settings = getattr(store, "settings", None)
        config = _memory_runtime_config(settings)
        runtime_scope: dict[str, Any] = {
            "required": runtime_gates_required,
            "tenant_id": tenant_id,
            "session_id": session_id,
            "status": "not_scoped" if not session_id else "unavailable",
        }
        if session_id and callable(scope_execution_allowed):
            try:
                runtime_scope["status"] = (
                    "enabled"
                    if await scope_execution_allowed(tenant_id, session_id) is True
                    else "disabled"
                )
            except Exception:
                runtime_scope["status"] = "unavailable"
        elif session_id and not runtime_gates_required:
            runtime_scope["status"] = "not_required"

        job_filters = {
            "tenant_id": tenant_id,
            "channel": channel,
            "source_key": source_key,
            "user_id": user_id,
            "session_id": session_id or None,
        }
        source_errors: list[str] = []
        try:
            recent_jobs = await store.list_llm_extraction_jobs(
                **job_filters,
                limit=recent_job_limit,
            )
        except Exception:
            recent_jobs = []
            source_errors.append("recent_jobs_unavailable")
        try:
            job_stats = await store.get_llm_extraction_job_status_counts(
                **job_filters,
                limit=100,
            )
        except Exception:
            job_stats = {}
            source_errors.append("job_stats_unavailable")
        if not isinstance(job_stats, dict):
            job_stats = {"counts": job_stats}
        try:
            acceptance_stats = await store.get_memory_acceptance_stats(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                session_id=session_id or None,
                scope_type="session" if session_id else None,
                source_type=None,
                memory_type=None,
                status=None,
                acceptance_status=None,
                limit=5000,
            )
        except Exception:
            acceptance_stats = {}
            source_errors.append("acceptance_stats_unavailable")
        try:
            items = await store.list_memory_items(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                session_id=session_id or None,
                scope_type="session" if session_id else None,
                include_deleted=False,
                limit=500,
            )
        except Exception:
            items = []
            source_errors.append("memory_items_unavailable")

        now = datetime.now(UTC)
        expiring_soon = 0
        expired = 0
        group_visible = 0
        for item in items:
            expiry = _parse_status_datetime(item.get("expires_at"))
            if expiry is not None:
                if expiry <= now:
                    expired += 1
                elif expiry <= now + timedelta(days=7):
                    expiring_soon += 1
            allowed = item.get("allowed_session_ids")
            allowed_values = (
                [str(value or "").strip() for value in allowed]
                if isinstance(allowed, (list, tuple, set))
                else []
            )
            if (
                session_id
                and str(item.get("origin_session_kind") or "") == "group"
                and str(item.get("audience_scope") or "") == "session"
                and session_id in allowed_values
            ):
                group_visible += 1

        diagnostics = _memory_management_diagnostics(
            config=config,
            runtime_scope=runtime_scope,
            job_stats=job_stats,
            acceptance_stats=acceptance_stats if isinstance(acceptance_stats, dict) else {},
            items=items,
            session_id=session_id,
        )
        diagnostics.extend(
            {
                "area": "observability",
                "status": "warning",
                "code": code,
                "message": "部分管理状态暂时无法读取，请查看服务日志后重试。",
            }
            for code in source_errors
        )
        return {
            "scope": {
                "tenant_id": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "session_id": session_id,
                "user_id": user_id or "",
            },
            "config": {
                "values": config,
                "source": "effective_process_settings",
                "note": "这些值是当前服务进程的生效配置；租户/会话开关以 runtime_scope 为准。",
            },
            "runtime_scope": runtime_scope,
            "jobs": {
                "stats": job_stats,
                "recent": [_safe_extraction_job_row(row) for row in recent_jobs],
            },
            "review": acceptance_stats,
            "governance": {
                "auto_cleanup_enabled": getattr(
                    settings,
                    "memory_governance_auto_cleanup_enabled",
                    None,
                ),
                "needs_review_retention_days": getattr(
                    settings,
                    "memory_needs_review_retention_days",
                    None,
                ),
                "rejected_retention_days": getattr(
                    settings,
                    "memory_rejected_retention_days",
                    None,
                ),
                "auto_expire_days": getattr(settings, "memory_auto_expire_days", None),
                "batch_size": getattr(settings, "memory_governance_batch_size", None),
                "expired_items": expired,
                "expiring_within_7_days": expiring_soon,
            },
            "visibility": {
                "scanned_items": len(items),
                "group_session_visible_items": group_visible,
            },
            "diagnostics": diagnostics,
        }

    @router.get("/events")
    async def list_events(
        request: Request,
        tenant_id: str = Query(...),
        channel: str | None = Query(default=None),
        source_key: str | None = Query(default=None),
        user_id: str | None = Query(default=None),
        session_id: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
    ):
        user_id, is_admin = _current_user_for_read(request, requested_user_id=user_id, store=store)
        rows = await store.list_events(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            limit=limit,
        )
        return {"items": _shape_read_rows(rows, is_admin=is_admin, safe_row=_safe_event_row)}

    @router.get("/group-graph")
    async def get_group_graph(
        request: Request,
        tenant_id: str = Query(...),
        channel: str | None = Query(default=None),
        source_key: str | None = Query(default=None),
        session_id: str | None = Query(default=None),
        from_: datetime | None = Query(default=None, alias="from"),  # noqa: B008
        to: datetime | None = Query(default=None),  # noqa: B008
        node_type: str | None = Query(default=None),
        edge_type: str | None = Query(default=None),
        relation_type: str | None = Query(default=None),
        acceptance_status: str | None = Query(default=None),
        min_confidence: float | None = Query(default=None, ge=0.0, le=1.0),
        limit: int = Query(default=500, ge=1, le=500),
    ):
        await _require_group_read_access(
            request,
            store,
            group_membership_authorizer,
            tenant_id=tenant_id,
            session_id=session_id,
        )
        resolved_edge_type = edge_type or relation_type
        _require_admin_for_review_group_graph_statuses(request, store, acceptance_status)
        payload = await store.get_group_relationship_graph(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            session_id=session_id,
            from_=from_,
            to=to,
            node_type=node_type,
            edge_type=resolved_edge_type,
            acceptance_status=acceptance_status,
            min_confidence=min_confidence,
            limit=limit,
        )
        safe_payload = _scrub_group_graph_payload(payload)
        if not isinstance(safe_payload, dict):
            return safe_payload
        schema = safe_payload.get("schema")
        if not isinstance(schema, dict):
            schema = {}
        schema["version"] = GROUP_GRAPH_SCHEMA_VERSION
        schema["node_types"] = list(
            dict.fromkeys(
                [
                    *[str(value) for value in schema.get("node_types", []) if value],
                    *GROUP_GRAPH_NODE_TYPES,
                ]
            )
        )
        schema["edge_types"] = list(
            dict.fromkeys(
                [
                    *[str(value) for value in schema.get("edge_types", []) if value],
                    *GROUP_GRAPH_EDGE_TYPES,
                ]
            )
        )
        safe_payload["schema"] = schema
        filters = safe_payload.get("filters")
        if isinstance(filters, dict):
            filters.setdefault("relation_type", resolved_edge_type)
            if filters.get("edge_type") is None:
                filters["edge_type"] = resolved_edge_type
        return safe_payload

    @router.get("/group-graph/evidence/{edge_id:path}")
    async def get_group_graph_evidence(
        edge_id: str,
        request: Request,
        tenant_id: str = Query(...),
        channel: str | None = Query(default=None),
        source_key: str | None = Query(default=None),
        session_id: str | None = Query(default=None),
        raw: bool = Query(default=False),
    ):
        is_admin = await _require_group_read_access(
            request,
            store,
            group_membership_authorizer,
            tenant_id=tenant_id,
            session_id=session_id,
        )
        include_raw = raw and is_admin
        if raw and not include_raw:
            raise HTTPException(status_code=403, detail="admin access required for raw evidence")
        payload = await store.get_group_relationship_edge_evidence(
            edge_id=edge_id,
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            session_id=session_id,
            include_raw=include_raw,
        )
        if not payload:
            raise HTTPException(status_code=404, detail="group graph edge evidence not found")
        safe_payload = payload if include_raw else _scrub_group_graph_payload(payload)
        if isinstance(safe_payload, dict):
            schema = safe_payload.get("schema")
            if not isinstance(schema, dict):
                schema = {}
            schema["version"] = GROUP_GRAPH_SCHEMA_VERSION
            safe_payload["schema"] = schema
        return safe_payload

    @router.post("/group-graph/extract-daily")
    async def run_group_graph_daily_extraction(
        body: MemoryDailyRelationshipExtractionRequest,
        request: Request,
    ):
        _require_admin_request(request, store)
        try:
            controls = body.extraction_controls()
            payload = await store.run_daily_group_relationship_extraction(
                tenant_id=body.tenant_id,
                channel=body.channel,
                source_key=body.source_key,
                session_id=body.session_id,
                user_id=body.user_id,
                date=body.date,
                **controls,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _scrub_group_graph_payload(payload)

    @router.post("/group-graph/extract-window")
    async def run_group_graph_window_extraction(
        body: MemoryWindowRelationshipExtractionRequest,
        request: Request,
    ):
        _require_admin_request(request, store)
        try:
            controls = body.extraction_controls()
            payload = await store.run_group_relationship_window_extraction(
                tenant_id=body.tenant_id,
                channel=body.channel,
                source_key=body.source_key,
                session_id=body.session_id,
                user_id=body.user_id,
                date=body.date,
                **controls,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _scrub_group_graph_payload(payload)

    @router.post("/group-graph/extract-window-catchup")
    async def run_group_graph_window_catchup(
        body: MemoryWindowRelationshipCatchupRequest,
        request: Request,
    ):
        _require_admin_request(request, store)
        try:
            controls = body.extraction_controls()
            payload = await store.run_group_relationship_window_catchup(
                tenant_id=body.tenant_id,
                channel=body.channel,
                source_key=body.source_key,
                session_id=body.session_id,
                user_id=body.user_id,
                date=body.date,
                **controls,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _scrub_group_graph_payload(payload)

    @router.get("/group-graph/window-stats")
    async def get_group_graph_window_stats(
        tenant_id: str,
        request: Request,
        channel: str | None = Query(default=None),
        source_key: str | None = Query(default=None),
        session_id: str | None = Query(default=None),
        user_id: str | None = Query(default=None),
        date: str | None = Query(default=None),
    ):
        _require_admin_request(request, store)
        try:
            payload = await store.get_group_relationship_window_stats(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                session_id=session_id,
                user_id=user_id,
                date=date,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _scrub_group_graph_payload(payload)

    @router.post("/group-graph/edges/{edge_id:path}/acceptance-review")
    async def review_group_graph_edge_acceptance(
        edge_id: str,
        body: MemoryEdgeAcceptanceReviewRequest,
        request: Request,
    ):
        _require_admin_request(request, store)
        action = str(body.action or "").strip().lower()
        if action not in MEMORY_ACCEPTANCE_REVIEW_ACTIONS:
            raise HTTPException(
                status_code=400, detail=f"unsupported acceptance review action: {action}"
            )
        actor = (
            str(body.reviewed_by or "").strip()
            or request.headers.get("X-Actor-ID")
            or request.headers.get("X-User-Id")
            or "admin/api"
        )
        try:
            payload = await store.review_group_relationship_edge(
                edge_id=edge_id,
                tenant_id=body.tenant_id,
                channel=body.channel,
                source_key=body.source_key,
                session_id=body.session_id,
                action=action,
                review_reason=str(body.review_reason or ""),
                reviewed_by=actor,
                superseded_by_item_id=body.superseded_by_item_id,
                supersedes_item_id=body.supersedes_item_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except MemoryItemConflictError as exc:
            raise HTTPException(409, "memory item conflicts with an existing item") from exc
        if not payload:
            raise HTTPException(status_code=404, detail="group graph edge not found")
        return _scrub_group_graph_payload(payload)

    @router.get("/group-graph/history-dates")
    async def get_group_graph_history_dates(
        request: Request,
        tenant_id: str = Query(...),
        channel: str = Query(default="wechat"),
        source_key: str = Query(default="wxbot"),
        session_id: str = Query(...),
        user_id: str | None = Query(default=None),
        recent_days: int = Query(default=14, ge=1, le=90),
    ):
        await _require_group_read_access(
            request,
            store,
            group_membership_authorizer,
            tenant_id=tenant_id,
            session_id=session_id,
        )
        try:
            return await store.get_group_graph_history_dates(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                session_id=session_id,
                user_id=user_id,
                recent_days=recent_days,
            )
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/graph/entities")
    async def list_graph_entities(
        request: Request,
        tenant_id: str = Query(...),
        channel: str | None = Query(default=None),
        source_key: str | None = Query(default=None),
        user_id: str | None = Query(default=None),
        status: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
    ):
        user_id, is_admin = _current_user_for_read(request, requested_user_id=user_id, store=store)
        rows = await store.list_memory_graph_entities(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            status=status,
            limit=limit,
        )
        return {
            "items": _shape_read_rows(rows, is_admin=is_admin, safe_row=_safe_graph_entity_row),
            "count": len(rows),
        }

    @router.get("/graph/facts")
    async def list_graph_facts(
        request: Request,
        tenant_id: str = Query(...),
        channel: str | None = Query(default=None),
        source_key: str | None = Query(default=None),
        user_id: str | None = Query(default=None),
        status: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
    ):
        user_id, is_admin = _current_user_for_read(request, requested_user_id=user_id, store=store)
        rows = await store.list_memory_graph_facts(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            status=status,
            limit=limit,
        )
        return {
            "items": _shape_read_rows(rows, is_admin=is_admin, safe_row=_safe_graph_fact_row),
            "count": len(rows),
        }

    @router.get("/graph/episodes")
    async def list_graph_episodes(
        request: Request,
        tenant_id: str = Query(...),
        channel: str | None = Query(default=None),
        source_key: str | None = Query(default=None),
        user_id: str | None = Query(default=None),
        session_id: str | None = Query(default=None),
        status: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
    ):
        user_id, is_admin = _current_user_for_read(request, requested_user_id=user_id, store=store)
        rows = await store.list_memory_graph_episodes(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            status=status,
            limit=limit,
        )
        return {
            "items": _shape_read_rows(rows, is_admin=is_admin, safe_row=_safe_graph_episode_row),
            "count": len(rows),
        }

    @router.get("/graph/preview")
    async def preview_graph(
        request: Request,
        tenant_id: str = Query(...),
        channel: str | None = Query(default=None),
        source_key: str | None = Query(default=None),
        user_id: str | None = Query(default=None),
        session_id: str | None = Query(default=None),
        status: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
    ):
        user_id, is_admin = _current_user_for_read(request, requested_user_id=user_id, store=store)
        entities = await store.list_memory_graph_entities(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            status=status,
            limit=limit,
        )
        facts = await store.list_memory_graph_facts(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            status=status,
            limit=limit,
        )
        episodes = await store.list_memory_graph_episodes(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            status=status,
            limit=limit,
        )
        payload = {
            "entities": entities,
            "facts": facts,
            "episodes": episodes,
            "counts": {
                "entities": len(entities),
                "facts": len(facts),
                "episodes": len(episodes),
            },
        }
        return _shape_read_payload(
            payload, is_admin=is_admin, safe_payload=_safe_graph_preview_payload
        )

    @router.get("/items")
    async def list_items(
        request: Request,
        tenant_id: str = Query(...),
        channel: str | None = Query(default=None),
        source_key: str | None = Query(default=None),
        user_id: str | None = Query(default=None),
        session_id: str | None = Query(default=None),
        scope_type: str | None = Query(default=None),
        source_type: str | None = Query(default=None),
        status: str | None = Query(default=None),
        include_deleted: bool = Query(default=False),
        limit: int = Query(default=100, ge=1, le=500),
    ):
        user_id, is_admin = _current_user_for_read(request, requested_user_id=user_id, store=store)
        rows = await store.list_memory_items(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            scope_type=scope_type,
            source_type=source_type,
            status=status,
            include_deleted=include_deleted,
            limit=limit,
        )
        return {"items": _shape_read_rows(rows, is_admin=is_admin, safe_row=_safe_memory_item_row)}

    @router.post("/profile-enrichment/candidates")
    async def create_profile_enrichment_candidate(
        body: ProfileEnrichmentCandidateCreateRequest,
        request: Request,
    ):
        _require_admin_request(request, store)
        actor = _admin_actor(request, body.created_by)
        try:
            item = await store.create_profile_enrichment_candidate(
                tenant_id=body.tenant_id,
                channel=body.channel,
                source_key=body.source_key,
                session_id=body.session_id,
                user_id=body.user_id,
                report_payload=body.report_payload,
                created_by=actor,
            )
        except Exception as exc:
            _raise_mutation_error(exc)
        if not item:
            raise HTTPException(400, "profile enrichment candidate content required")
        return item

    @router.post("/profile-enrichment/candidates/from-report")
    async def create_profile_enrichment_candidate_from_report(
        body: ProfileEnrichmentCandidateFromReportRequest,
        request: Request,
    ):
        _require_admin_request(request, store)
        if str(body.channel or "").strip().lower() != "wechat":
            raise HTTPException(
                status_code=400, detail="profile report builder only supports channel=wechat"
            )
        if str(body.source_key or "").strip().lower() != "wxbot":
            raise HTTPException(
                status_code=400, detail="profile report builder only supports source_key=wxbot"
            )
        try:
            if await store.member_memory_write_blocked(
                tenant_id=body.tenant_id,
                user_id=body.user_id,
                channel=body.channel,
            ):
                raise MemoryMutationError(
                    "member_memory_write_blocked",
                    status_code=409,
                )
        except Exception as exc:
            _raise_mutation_error(exc)
        if profile_report_builder is None:
            raise HTTPException(status_code=503, detail="profile report builder unavailable")
        if callable(combined_scope_execution_allowed):
            await _require_runtime_scope(
                combined_scope_execution_allowed,
                tenant_id=body.tenant_id,
                session_id=body.session_id,
                owner="memory_wxbot",
                required=runtime_gates_required,
            )
        else:
            for owner, gate in (
                ("memory", scope_execution_allowed),
                ("wxbot", history_scope_execution_allowed),
            ):
                await _require_runtime_scope(
                    gate,
                    tenant_id=body.tenant_id,
                    session_id=body.session_id,
                    owner=owner,
                    required=runtime_gates_required,
                )
        session = SimpleNamespace(
            tenant_id=body.tenant_id,
            channel=body.channel,
            source_key=body.source_key,
            session_id=body.session_id,
            user_id=body.user_id,
            metadata={},
        )
        arguments = {
            "query": body.query,
            "user_id": body.user_id,
            "hours": body.hours,
            "limit": body.limit,
            "external_candidates": body.external_candidates,
        }
        try:
            report_payload = await profile_report_builder(session, arguments)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if not isinstance(report_payload, dict) or not report_payload:
            raise HTTPException(status_code=400, detail="profile report content required")
        if callable(combined_scope_execution_allowed):
            await _require_runtime_scope(
                combined_scope_execution_allowed,
                tenant_id=body.tenant_id,
                session_id=body.session_id,
                owner="memory_wxbot",
                required=runtime_gates_required,
            )
        else:
            for owner, gate in (
                ("memory", scope_execution_allowed),
                ("wxbot", history_scope_execution_allowed),
            ):
                await _require_runtime_scope(
                    gate,
                    tenant_id=body.tenant_id,
                    session_id=body.session_id,
                    owner=owner,
                    required=runtime_gates_required,
                )
        try:
            item = await store.create_profile_enrichment_candidate(
                tenant_id=body.tenant_id,
                channel=body.channel,
                source_key=body.source_key,
                session_id=body.session_id,
                user_id=body.user_id,
                report_payload=report_payload,
                created_by=_admin_actor(request, body.created_by),
                require_history_owner=True,
            )
        except MemoryMutationError as exc:
            _raise_mutation_error(exc)
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if not item:
            raise HTTPException(400, "profile enrichment candidate content required")
        return item

    @router.get("/profile-enrichment/candidates")
    async def list_profile_enrichment_candidates(
        request: Request,
        tenant_id: str = Query(...),
        channel: str | None = Query(default=None),
        source_key: str | None = Query(default=None),
        session_id: str | None = Query(default=None),
        user_id: str | None = Query(default=None),
        review_state: str | None = Query(default=None),
        include_hidden: bool = Query(default=False),
        limit: int = Query(default=100, ge=1, le=500),
    ):
        _require_admin_request(request, store)
        if review_state and review_state not in PROFILE_ENRICHMENT_ACCEPTANCE_STATUSES:
            raise HTTPException(
                status_code=400, detail=f"unsupported profile enrichment state: {review_state}"
            )
        rows = await store.list_profile_enrichment_candidates(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            session_id=session_id,
            user_id=user_id,
            review_state=review_state,
            include_hidden=include_hidden,
            limit=limit,
        )
        return {"items": rows}

    @router.get("/profile-enrichment/candidates/{candidate_id}")
    async def get_profile_enrichment_candidate(candidate_id: int, request: Request):
        _require_admin_request(request, store)
        item = await store.get_profile_enrichment_candidate(candidate_id)
        if not item:
            raise HTTPException(404, "profile enrichment candidate not found")
        return item

    @router.post("/profile-enrichment/candidates/{candidate_id}/review")
    async def review_profile_enrichment_candidate(
        candidate_id: int,
        body: ProfileEnrichmentCandidateReviewRequest,
        request: Request,
        response: Response,
        tenant_id: str = Query(...),
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        _require_admin_request(request, store)
        action = str(body.action or "").strip().lower()
        if action not in PROFILE_ENRICHMENT_REVIEW_ACTIONS:
            raise HTTPException(
                status_code=400, detail=f"unsupported profile enrichment review action: {action}"
            )
        actor, actor_kind, roles, trace_id = _mutation_actor_context(request)
        try:
            outcome = await store.review_profile_enrichment_candidate_idempotent(
                candidate_id,
                tenant_id=tenant_id,
                action=action,
                notes=str(body.notes or ""),
                reviewed_by=actor,
                request_reviewed_by=str(body.reviewed_by or ""),
                idempotency_key=_required_mutation_key(idempotency_key),
                actor_kind=actor_kind,
                roles=roles,
                trace_id=trace_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except MemoryItemConflictError as exc:
            raise HTTPException(
                409, "profile enrichment candidate conflicts with an existing item"
            ) from exc
        except Exception as exc:
            _raise_mutation_error(exc)
        response.status_code = outcome.status_code
        if outcome.replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return outcome.response

    @router.get("/items/acceptance-stats")
    async def acceptance_stats(
        request: Request,
        tenant_id: str = Query(...),
        channel: str | None = Query(default=None),
        source_key: str | None = Query(default=None),
        user_id: str | None = Query(default=None),
        session_id: str | None = Query(default=None),
        scope_type: str | None = Query(default=None),
        source_type: str | None = Query(default=None),
        memory_type: str | None = Query(default=None),
        status: str | None = Query(default=None),
        acceptance_status: str | None = Query(default=None),
        limit: int = Query(default=5000, ge=1, le=10000),
    ):
        _require_admin_request(request, store)
        filters = _memory_acceptance_filters(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            scope_type=scope_type,
            source_type=source_type,
            memory_type=memory_type,
            status=status,
        )
        return await store.get_memory_acceptance_stats(
            **filters,
            acceptance_status=_normalize_acceptance_status_filter(acceptance_status),
            limit=limit,
        )

    @router.get("/items/acceptance-legacy-audit")
    async def acceptance_legacy_audit(
        request: Request,
        tenant_id: str = Query(...),
        channel: str | None = Query(default=None),
        source_key: str | None = Query(default=None),
        user_id: str | None = Query(default=None),
        session_id: str | None = Query(default=None),
        scope_type: str | None = Query(default=None),
        source_type: str | None = Query(default=None),
        memory_type: str | None = Query(default=None),
        status: str | None = Query(default=None),
        limit: int = Query(default=5000, ge=1, le=10000),
    ):
        _require_admin_request(request, store)
        filters = _memory_acceptance_filters(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            scope_type=scope_type,
            source_type=source_type,
            memory_type=memory_type,
            status=status,
        )
        return await store.audit_legacy_acceptance(**filters, limit=limit)

    @router.post("/items/acceptance-legacy-backfill")
    async def acceptance_legacy_backfill(
        body: MemoryAcceptanceLegacyBackfillRequest,
        request: Request,
        response: Response,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        _require_admin_request(request, store)
        target_status = str(body.mark_missing_as or "").strip().lower()
        if target_status not in {"needs_review", "candidate"}:
            raise HTTPException(
                status_code=400, detail="mark_missing_as must be needs_review or candidate"
            )
        if not body.dry_run and not body.max_items:
            raise HTTPException(
                status_code=400, detail="non-dry-run acceptance backfill requires max_items"
            )
        filters = _memory_acceptance_filters(
            tenant_id=body.tenant_id,
            channel=body.channel,
            source_key=body.source_key,
            user_id=body.user_id,
            session_id=body.session_id,
            scope_type=body.scope_type,
            source_type=body.source_type,
            memory_type=body.memory_type,
            status=body.status,
        )
        params = {
            **filters,
            "dry_run": body.dry_run,
            "max_items": body.max_items,
            "mark_missing_as": target_status,
            "reviewed_by": "admin_backfill",
        }
        actor, actor_kind, roles, trace_id = _mutation_actor_context(request)
        try:
            outcome = await store.backfill_legacy_acceptance_idempotent(
                params=params,
                idempotency_key=_required_mutation_key(idempotency_key),
                actor=actor,
                actor_kind=actor_kind,
                roles=roles,
                trace_id=trace_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            _raise_mutation_error(exc)
        response.status_code = outcome.status_code
        if outcome.replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return outcome.response

    @router.get("/items/retrieve")
    async def retrieve_items(
        request: Request,
        tenant_id: str = Query(...),
        channel: str = Query(...),
        source_key: str = Query(default="*"),
        user_id: str = Query(...),
        session_id: str = Query(default=""),
        query: str = Query(default=""),
        limit: int = Query(default=6, ge=1, le=20),
        debug: bool = Query(default=False),
    ):
        _require_admin_request(request, store)
        if bool(
            getattr(getattr(store, "settings", None), "memory_hybrid_retrieval_enabled", False)
        ) and hasattr(
            store,
            "retrieve_memory_hybrid",
        ):
            hybrid = await store.retrieve_memory_hybrid(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                session_id=session_id,
                query=query,
                limit=limit,
                include_graph=False,
                debug=debug,
            )
            response = {"items": hybrid.get("items") or []}
            if debug:
                response["debug"] = hybrid.get("debug") or {}
            return response
        rows = await store.retrieve_memory_items(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            query=query,
            limit=limit,
            debug=debug,
        )
        return {"items": rows}

    @router.post("/remember")
    async def remember_memory(
        body: MemoryRememberRequest,
        request: Request,
        response: Response,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        user_id = _resolve_memory_target_user(request, body, store)
        if _is_group_memory_session(body.session_id):
            await _require_group_read_access(
                request,
                store,
                group_membership_authorizer,
                tenant_id=body.tenant_id,
                session_id=body.session_id,
            )
        audience_fields = _manual_memory_write_fields(
            body,
            allow_explicit_audience=_is_admin_request(request, store),
        )
        item_fields = {
            "tenant_id": body.tenant_id,
            "channel": body.channel,
            "source_key": body.source_key,
            "user_id": user_id,
            "source_type": "manual",
            "memory_type": body.memory_type,
            "content": body.content,
            "value_json": body.value_json,
            "confidence": 1.0,
            "status": body.status,
            "pinned": body.pinned,
            "priority": body.priority,
            "sensitivity": body.sensitivity,
            "source_trace_id": "",
            "original_text": "",
            **audience_fields,
        }
        actor, actor_kind, roles, trace_id = _mutation_actor_context(
            request,
            fallback_actor=user_id,
        )
        try:
            outcome = await store.create_memory_item_idempotent(
                item_fields=item_fields,
                idempotency_key=_required_mutation_key(idempotency_key),
                actor=actor,
                actor_kind=actor_kind,
                roles=roles,
                trace_id=trace_id,
            )
        except Exception as exc:
            _raise_mutation_error(exc)
        response.status_code = outcome.status_code
        if outcome.replayed:
            response.headers["Idempotent-Replayed"] = "true"
        item = outcome.response
        return {"ok": True, "ids": [item["id"]], "count": 1, "item": item}

    @router.post("/search")
    async def search_memory(body: MemorySearchRequest, request: Request):
        user_id = _resolve_memory_target_user(request, body, store)
        debug_payload: dict[str, Any] | None = None
        if bool(
            getattr(getattr(store, "settings", None), "memory_hybrid_retrieval_enabled", False)
        ) and hasattr(
            store,
            "retrieve_memory_hybrid",
        ):
            hybrid = await store.retrieve_memory_hybrid(
                tenant_id=body.tenant_id,
                channel=body.channel,
                source_key=body.source_key,
                user_id=user_id,
                session_id=body.session_id,
                query=body.query,
                limit=body.limit,
                include_graph=False,
                debug=body.debug,
            )
            rows = list(hybrid.get("items") or [])
            debug_payload = hybrid.get("debug") or {}
        else:
            rows = await store.retrieve_memory_items(
                tenant_id=body.tenant_id,
                channel=body.channel,
                source_key=body.source_key,
                user_id=user_id,
                session_id=body.session_id,
                query=body.query,
                limit=body.limit,
                debug=body.debug,
            )
        if body.scope_type:
            rows = [row for row in rows if str(row.get("scope_type") or "") == body.scope_type]
        response = {"items": rows, "count": len(rows)}
        if body.debug:
            response["debug"] = debug_payload or {}
        return response

    @router.post("/forget")
    async def forget_memory(
        body: MemoryForgetRequest,
        request: Request,
        response: Response,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        user_id = _resolve_memory_target_user(request, body, store)
        actor, actor_kind, roles, trace_id = _mutation_actor_context(
            request,
            fallback_actor=user_id,
        )
        try:
            outcome = await store.forget_memory_items_idempotent(
                tenant_id=body.tenant_id,
                channel=body.channel,
                source_key=body.source_key,
                user_id=user_id,
                item_id=body.item_id,
                query=body.query,
                session_id=body.session_id,
                scope_type=body.scope_type,
                allow_pinned=body.allow_pinned,
                limit=body.limit,
                idempotency_key=_required_mutation_key(idempotency_key),
                actor=actor,
                actor_kind=actor_kind,
                roles=roles,
                trace_id=trace_id,
            )
        except MemoryItemProtectedError as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": "allow_pinned_required", "ids": exc.protected_ids},
            ) from exc
        except Exception as exc:
            _raise_mutation_error(exc)
        response.status_code = outcome.status_code
        if outcome.replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return outcome.response

    @router.post("/update")
    async def update_memory_control(body: MemoryControlUpdateRequest, request: Request):
        user_id = _resolve_memory_target_user(request, body, store)
        updates = _body_updates(
            body,
            exclude={
                "tenant_id",
                "channel",
                "source_key",
                "current_user_id",
                "user_id",
                "session_id",
                "scope_type",
                "item_id",
            },
        )
        try:
            item = await store.update_memory_item_scoped(
                body.item_id,
                tenant_id=body.tenant_id,
                channel=body.channel,
                source_key=body.source_key,
                user_id=user_id,
                session_id=body.session_id,
                **updates,
            )
        except MemoryItemConflictError as exc:
            raise HTTPException(409, "memory item conflicts with an existing item") from exc
        if not item:
            raise HTTPException(404, "memory item not found")
        return {"ok": True, "ids": [item["id"]], "count": 1, "item": item}

    @router.get("/extraction-jobs")
    async def list_extraction_jobs(
        request: Request,
        tenant_id: str | None = Query(default=None),
        channel: str | None = Query(default=None),
        source_key: str | None = Query(default=None),
        user_id: str | None = Query(default=None),
        session_id: str | None = Query(default=None),
        status: str | None = Query(default=None),
        error_type: str | None = Query(default=None),
        created_before: datetime | None = Query(default=None),  # noqa: B008
        created_after: datetime | None = Query(default=None),  # noqa: B008
        updated_before: datetime | None = Query(default=None),  # noqa: B008
        updated_after: datetime | None = Query(default=None),  # noqa: B008
        limit: int = Query(default=50, ge=1, le=500),
    ):
        _require_admin_request(request, store)
        rows = await store.list_llm_extraction_jobs(
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
            limit=limit,
        )
        return {"items": [_safe_extraction_job_row(row) for row in rows]}

    @router.get("/extraction-jobs/stats")
    async def get_extraction_job_stats(
        request: Request,
        tenant_id: str | None = Query(default=None),
        channel: str | None = Query(default=None),
        source_key: str | None = Query(default=None),
        user_id: str | None = Query(default=None),
        session_id: str | None = Query(default=None),
        status: str | None = Query(default=None),
        error_type: str | None = Query(default=None),
        created_before: datetime | None = Query(default=None),  # noqa: B008
        created_after: datetime | None = Query(default=None),  # noqa: B008
        updated_before: datetime | None = Query(default=None),  # noqa: B008
        updated_after: datetime | None = Query(default=None),  # noqa: B008
        limit: int = Query(default=100, ge=1, le=100),
    ):
        _require_admin_request(request, store)
        stats = await store.get_llm_extraction_job_status_counts(
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
            limit=limit,
        )
        if isinstance(stats, dict) and "status_counts" in stats:
            return stats
        return {"counts": stats}

    @router.post("/extraction-jobs/maintenance")
    async def maintain_extraction_jobs(
        body: MemoryExtractionJobMaintenanceRequest,
        request: Request,
        response: Response,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        _require_admin_request(request, store)
        actions = _extraction_job_actions(body)
        if not actions:
            raise HTTPException(status_code=400, detail="maintenance action required")
        allowed_actions = {"reset_stale", "retry", "mark_dead", "cleanup_smoke"}
        invalid_actions = [action for action in actions if action not in allowed_actions]
        if invalid_actions:
            raise HTTPException(
                status_code=400, detail=f"unsupported maintenance action: {invalid_actions[0]}"
            )

        has_filters = _has_extraction_job_filters(body)
        if not body.dry_run and not has_filters:
            raise HTTPException(
                status_code=400, detail="write maintenance requires at least one filter"
            )
        for action in actions:
            if action != "reset_stale" and not has_filters:
                raise HTTPException(
                    status_code=400, detail=f"{action} requires at least one filter"
                )
            if action == "cleanup_smoke" and not body.dry_run and not _has_smoke_scope_filter(body):
                raise HTTPException(
                    status_code=400,
                    detail="cleanup_smoke requires an explicit smoke/test scope filter",
                )

        filters = _extraction_job_filter_updates(body)
        params = {
            **filters,
            "actions": actions,
            "dry_run": body.dry_run,
            "limit": min(int(body.limit or 100), 100),
        }
        actor, actor_kind, roles, trace_id = _mutation_actor_context(request)
        try:
            outcome = await store.maintain_llm_extraction_jobs_idempotent(
                params=params,
                idempotency_key=_required_mutation_key(idempotency_key),
                actor=actor,
                actor_kind=actor_kind,
                roles=roles,
                trace_id=trace_id,
            )
        except Exception as exc:
            _raise_mutation_error(exc)
        response.status_code = outcome.status_code
        if outcome.replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return outcome.response

    @router.post("/items")
    async def create_item(
        body: MemoryItemCreateRequest,
        request: Request,
        response: Response,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        _require_admin_request(request, store)
        if hasattr(body, "model_dump"):
            item_fields = body.model_dump(exclude={"retention_days"})
        else:  # pragma: no cover - pydantic v1 compatibility
            item_fields = body.dict(exclude={"retention_days"})
        item_fields.update(
            _manual_memory_write_fields(
                body,
                allow_explicit_audience=True,
            )
        )
        actor, actor_kind, roles, trace_id = _mutation_actor_context(request)
        try:
            outcome = await store.create_memory_item_idempotent(
                item_fields=item_fields,
                idempotency_key=_required_mutation_key(idempotency_key),
                actor=actor,
                actor_kind=actor_kind,
                roles=roles,
                trace_id=trace_id,
            )
        except Exception as exc:
            _raise_mutation_error(exc)
        response.status_code = outcome.status_code
        if outcome.replayed:
            response.headers["Idempotent-Replayed"] = "true"
        version = memory_item_version(outcome.response)
        if version:
            response.headers["ETag"] = f'"{version}"'
        return outcome.response

    @router.post("/governance/cleanup")
    async def governance_cleanup(
        body: MemoryGovernanceCleanupRequest,
        request: Request,
    ):
        _require_admin_request(request, store)
        return await store.run_governance_cleanup(
            dry_run=body.dry_run,
            needs_review_days=body.needs_review_days,
            rejected_days=body.rejected_days,
            auto_expire_days=body.auto_expire_days,
            limit=body.limit,
        )

    @router.patch("/items/{item_id}")
    async def update_item(
        item_id: int,
        body: MemoryItemUpdateRequest,
        request: Request,
        response: Response,
        tenant_id: str = Query(..., min_length=1, max_length=64),
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    ):
        _require_admin_request(request, store)
        if hasattr(body, "model_dump"):
            updates = body.model_dump(exclude_none=True)
        else:  # pragma: no cover - pydantic v1 compatibility
            updates = body.dict(exclude_none=True)
        actor, actor_kind, roles, trace_id = _mutation_actor_context(request)
        try:
            outcome = await store.update_memory_item_idempotent(
                item_id,
                tenant_id=tenant_id,
                updates=updates,
                expected_version=str(if_match or ""),
                idempotency_key=_required_mutation_key(idempotency_key),
                actor=actor,
                actor_kind=actor_kind,
                roles=roles,
                trace_id=trace_id,
            )
        except MemoryItemConflictError as exc:
            raise HTTPException(409, "memory item conflicts with an existing item") from exc
        except Exception as exc:
            _raise_mutation_error(exc)
        response.status_code = outcome.status_code
        if outcome.replayed:
            response.headers["Idempotent-Replayed"] = "true"
        version = memory_item_version(outcome.response)
        if version:
            response.headers["ETag"] = f'"{version}"'
        return outcome.response

    @router.post("/items/{item_id}/acceptance-review")
    async def review_item_acceptance(
        item_id: int,
        body: MemoryAcceptanceReviewRequest,
        request: Request,
        response: Response,
        tenant_id: str = Query(...),
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        _require_admin_request(request, store)
        action = str(body.action or "").strip().lower()
        if action not in MEMORY_ACCEPTANCE_REVIEW_ACTIONS:
            raise HTTPException(
                status_code=400, detail=f"unsupported acceptance review action: {action}"
            )
        actor, actor_kind, roles, trace_id = _mutation_actor_context(request)
        try:
            outcome = await store.review_memory_item_acceptance_idempotent(
                item_id,
                tenant_id=tenant_id,
                action=action,
                review_reason=str(body.review_reason or ""),
                reviewed_by=actor,
                request_reviewed_by=str(body.reviewed_by or ""),
                superseded_by_item_id=body.superseded_by_item_id,
                supersedes_item_id=body.supersedes_item_id,
                idempotency_key=_required_mutation_key(idempotency_key),
                actor_kind=actor_kind,
                roles=roles,
                trace_id=trace_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except MemoryItemConflictError as exc:
            raise HTTPException(409, "memory item conflicts with an existing item") from exc
        except Exception as exc:
            _raise_mutation_error(exc)
        response.status_code = outcome.status_code
        if outcome.replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return outcome.response

    @router.delete("/items/{item_id}")
    async def delete_item(
        item_id: int,
        request: Request,
        response: Response,
        tenant_id: str = Query(...),
        allow_pinned: bool = Query(default=False),
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        actor, actor_kind, roles, trace_id = _mutation_actor_context(request)
        try:
            outcome = await store.soft_delete_memory_item_idempotent(
                item_id,
                tenant_id=tenant_id,
                allow_pinned=allow_pinned,
                idempotency_key=_required_mutation_key(idempotency_key),
                actor=actor,
                actor_kind=actor_kind,
                roles=roles,
                trace_id=trace_id,
            )
        except MemoryItemProtectedError as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": "allow_pinned_required", "ids": exc.protected_ids},
            ) from exc
        except Exception as exc:
            _raise_mutation_error(exc)
        response.status_code = outcome.status_code
        if outcome.replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return outcome.response

    @router.post("/backfill")
    async def backfill_memory(
        body: MemoryBackfillRequest,
        request: Request,
        response: Response,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        _require_admin_request(request, store)
        try:
            require_legacy_wxbot_history_scope(
                store.settings,
                tenant_id=body.tenant_id,
                connection_id=body.connection_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        for session_id in dict.fromkeys(body.session_ids):
            if callable(combined_scope_execution_allowed):
                await _require_runtime_scope(
                    combined_scope_execution_allowed,
                    tenant_id=body.tenant_id,
                    session_id=session_id,
                    owner="memory_wxbot",
                    required=runtime_gates_required,
                )
            else:
                for owner, gate in (
                    ("memory", scope_execution_allowed),
                    ("wxbot", history_scope_execution_allowed),
                ):
                    await _require_runtime_scope(
                        gate,
                        tenant_id=body.tenant_id,
                        session_id=session_id,
                        owner=owner,
                        required=runtime_gates_required,
                    )
        params = body.model_dump() if hasattr(body, "model_dump") else body.dict()
        actor, actor_kind, roles, trace_id = _mutation_actor_context(request)
        try:
            outcome = await store.backfill_from_sdk_idempotent(
                params=params,
                idempotency_key=_required_mutation_key(idempotency_key),
                actor=actor,
                actor_kind=actor_kind,
                roles=roles,
                trace_id=trace_id,
            )
        except MutationIdempotencyConflictError as exc:
            _raise_mutation_error(exc)
        except RuntimeError as exc:
            status_code = 503 if "plugin runtime disabled" in str(exc) else 400
            raise HTTPException(status_code, str(exc)) from exc
        except Exception as exc:
            _raise_mutation_error(exc)
        response.status_code = outcome.status_code
        if outcome.replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return outcome.response

    @router.post("/items/vector-rebuild")
    async def rebuild_item_vector_index(
        body: MemoryVectorRebuildRequest,
        request: Request,
        response: Response,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        _require_admin_request(request, store)
        params = body.model_dump() if hasattr(body, "model_dump") else body.dict()
        actor, actor_kind, roles, trace_id = _mutation_actor_context(request)
        try:
            outcome = await store.rebuild_memory_item_vector_index_idempotent(
                params=params,
                idempotency_key=_required_mutation_key(idempotency_key),
                actor=actor,
                actor_kind=actor_kind,
                roles=roles,
                trace_id=trace_id,
            )
        except Exception as exc:
            _raise_mutation_error(exc)
        response.status_code = outcome.status_code
        if outcome.replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return outcome.response

    @router.post("/items/vector-smoke")
    async def smoke_item_vector_index(body: MemoryVectorSmokeRequest, request: Request):
        _require_admin_request(request, store)
        return await store.smoke_memory_vector_enable(
            tenant_id=body.tenant_id,
            channel=body.channel,
            source_key=body.source_key,
            user_id=body.user_id,
            session_id=body.session_id,
            query=body.query,
            limit=body.limit,
            dry_run=body.dry_run,
        )

    @router.post("/graph/vector-rebuild")
    async def rebuild_graph_vector_index(
        body: MemoryVectorRebuildRequest,
        request: Request,
        response: Response,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        _require_admin_request(request, store)
        params = body.model_dump() if hasattr(body, "model_dump") else body.dict()
        actor, actor_kind, roles, trace_id = _mutation_actor_context(request)
        try:
            outcome = await store.rebuild_memory_graph_vector_index_idempotent(
                params=params,
                idempotency_key=_required_mutation_key(idempotency_key),
                actor=actor,
                actor_kind=actor_kind,
                roles=roles,
                trace_id=trace_id,
            )
        except Exception as exc:
            _raise_mutation_error(exc)
        response.status_code = outcome.status_code
        if outcome.replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return outcome.response

    return router
