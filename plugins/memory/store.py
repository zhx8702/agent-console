"""
Layered user memory persistence keyed by tenant/channel/source/user_id.

This plugin now keeps two distinct layers:
- identity memory: stable facts and preferences shared across sessions
- session memory: short-term context scoped to one session_id + user_id
"""

from __future__ import annotations

import asyncio as asyncio
import hashlib
import json
import re
import socket
from abc import ABC, abstractmethod
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from time import monotonic as monotonic
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from app.common.logging import get_logger
from app.egress.safe_http import safe_trusted_service_request as safe_trusted_service_request
from app.infra.db import get_engine
from app.infra.metrics import MEMORY_ACCEPTANCE_DECISIONS, MEMORY_GOVERNANCE_EVENTS
from app.infra.runtime_schema import verify_runtime_schema
from app.social.contracts import MemberMemoryCorrectionResolution, MemberPrivacyValues
from plugins.memory.graph_extractor import MemoryGraphLLMExtractor
from plugins.memory.store_acceptance import (
    MEMORY_ACCEPTANCE_AUTO_ACCEPT_MIN,
    MEMORY_ACCEPTANCE_REJECT_BELOW,
    PROMPT_AUTO_CONFIDENCE_MIN,
    _acceptance_status_for_review_action,
    _build_memory_acceptance_metadata,
    _clamp_score,
    _memory_status_for_acceptance,
)
from plugins.memory.store_mutations import (
    MemoryAdminMutationMixin,
)
from plugins.memory.store_mutations import (
    MemoryMutationError as MemoryMutationError,
)
from plugins.memory.store_mutations import (
    memory_item_version as memory_item_version,
)
from plugins.memory.store_session_state import (
    SESSION_RECENT_TURN_LIMIT,
    SESSION_STATE_VERSION,
    _as_session_list,
    _build_short_term_summary,
    _update_session_state,
)
from plugins.memory.structured_extractor import MemoryStructuredExtractor
from plugins.memory.vector_index import MemoryItemVectorIndex

logger = get_logger(__name__)
_ACTIVE_MUTATION_CONNECTION: ContextVar[AsyncConnection | None] = ContextVar(
    "memory_mutation_connection",
    default=None,
)

try:
    import zstandard as zstd

    _DCTX = zstd.ZstdDecompressor()
except ImportError:  # pragma: no cover
    _DCTX = None


_GROUP_PREFIX_RE = re.compile(r"^([a-zA-Z0-9_@]+):\n(.*)$", re.DOTALL)
_GROUP_EVENT_SENDER_PREFIX_RE = re.compile(r"^([a-zA-Z0-9_@.\-]+):\s+")
_BULLET_RE = re.compile(r"^\s*(?:[-*•]+|\d+[.)、])\s*")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_ID_CARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_BANK_CARD_RE = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
_TOKEN_RE = re.compile(r"(?i)\b(?:api[_-]?key|secret|token|password|passwd|密钥|令牌|密码)\b")
_TOKEN_VALUE_RE = re.compile(
    r"(?i)\b(?:sk-[a-z0-9_-]{12,}|[a-z0-9_-]{24,}\.[a-z0-9_-]{12,}\.[a-z0-9_-]{12,}|[a-f0-9]{32,})\b"
)
_ADDRESS_RE = re.compile(r"[\u4e00-\u9fff]{2,}(?:省|市|区|县|镇|乡|街道|路|街|弄|号楼?|单元|室)")

ACTIVE_MEMORY_STATUSES = {"active"}
MEMORY_ITEM_STATUSES = {"active", "pending", "archived", "deleted", "invalidated"}
MEMORY_AUDIENCE_SCOPES = {"private", "session", "explicit"}
MEMORY_ORIGIN_SESSION_KINDS = {"private", "group", "unknown"}
MEMORY_SENSITIVITY_CATEGORIES = {"normal", "pii", "sensitive"}
MEMORY_ACCEPTANCE_STATUSES = {
    "candidate",
    "accepted",
    "needs_review",
    "rejected",
    "superseded",
    "expired",
}
MEMORY_ACCEPTANCE_REVIEW_ACTIONS = {
    "accept",
    "reject",
    "needs_review",
    "mark_joke",
    "expire",
    "supersede",
}
MEMORY_ACCEPTANCE_HISTORY_LIMIT = 20
MEMORY_ACCEPTANCE_ID_PREVIEW_LIMIT = 25
AUTO_SOURCE_TYPES = {"auto", "explicit_user", "backfill"}
LLM_GROUP_WINDOW_SOURCE_TYPE = "llm_group_window"
DETERMINISTIC_GROUP_WINDOW_SOURCE_TYPE = "deterministic_group_window"
PROFILE_ENRICHMENT_SOURCE_TYPE = "profile_enrichment"
PROFILE_ENRICHMENT_MEMORY_TYPE = "profile_enrichment_candidate"
PROFILE_ENRICHMENT_ACCEPTANCE_STATUSES = {
    "candidate",
    "needs_review",
    "rejected",
    "hidden",
    "accepted",
}
PROFILE_ENRICHMENT_REVIEW_ACTIONS = {"accept", "reject", "hide"}
MEMORY_EXTRACTION_JOB_STATUSES = {"pending", "running", "succeeded", "failed", "dead"}
GROUP_GRAPH_SCHEMA_VERSION = "group-graph.v1"
GROUP_HISTORY_USER_ID_SCOPE = "__group__"
GROUP_GRAPH_NODE_TYPES = (
    "person",
    "group",
    "topic",
    "project",
    "tool",
    "event",
    "task",
    "artifact",
    "value",
)
GROUP_GRAPH_EDGE_TYPES = (
    "mentioned",
    "replied_to",
    "addressed",
    "asked",
    "answered",
    "co_participated",
    "requested",
    "provided_resource",
    "collaborated_with",
    "works_on",
    "interested_in",
    "maintains",
    "reported_issue",
    "fixed_issue",
    "tested",
)
GRAPH_LLM_BACKING_SOURCE_TYPE = "auto"
GROUP_WINDOW_DETERMINISTIC_MAX_SENDERS = 8
GROUP_WINDOW_DETERMINISTIC_MAX_PAIRS = 20
HYBRID_ITEM_SQL_CANDIDATE_MULTIPLIER = 4
HYBRID_GRAPH_CANDIDATE_MULTIPLIER = 3
HYBRID_GRAPH_FACT_BUDGET = 2
HYBRID_GRAPH_EPISODE_BUDGET = 1
HYBRID_VECTOR_WEIGHT = 140.0
HYBRID_KEYWORD_WEIGHT = 95.0
MEMORY_VECTOR_RELEVANCE_MIN = 0.35

# Test-time inventory for the Alembic-owned memory schema contract.
# Keep this in sync when adding migration-backed memory columns/tables.
MEMORY_DDL_CONSISTENCY_GUARD = {
    "plugin_memory_session_profile": {
        "columns": (
            "session_summary",
            "open_items_json",
            "decisions_json",
            "recent_turns_json",
            "last_compacted_at",
            "summary_version",
        ),
        "indexes": ("idx_memory_session_lookup",),
        "migrations": (
            "20260510_0005_memory_session_state.py",
            "20260718_0015_runtime_plugin_schema.py",
        ),
    },
    "plugin_memory_extraction_job": {
        "columns": (
            "status",
            "attempts",
            "max_attempts",
            "next_run_at",
            "locked_until",
            "last_error",
            "result_json",
            "idempotency_key",
        ),
        "indexes": (
            "ux_memory_extraction_job_idempotency",
            "idx_memory_extraction_job_ready",
            "idx_memory_extraction_job_scope",
        ),
        "migrations": (
            "20260512_0010_memory_extraction_job_result.py",
            "20260718_0015_runtime_plugin_schema.py",
        ),
    },
    "plugin_memory_entity": {
        "columns": (
            "entity_type",
            "name",
            "normalized_name",
            "aliases_json",
            "confidence",
            "status",
        ),
        "indexes": ("ux_memory_entity_scope_name", "idx_memory_entity_scope"),
        "migrations": (
            "20260511_0006_memory_graph.py",
            "20260718_0015_runtime_plugin_schema.py",
        ),
    },
    "plugin_memory_fact": {
        "columns": (
            "subject_entity_id",
            "predicate",
            "object_entity_id",
            "object_value",
            "memory_item_id",
            "source_event_id",
            "confidence",
            "status",
        ),
        "indexes": (
            "ux_memory_fact_memory_item",
            "idx_memory_fact_scope",
            "idx_memory_fact_subject",
        ),
        "migrations": (
            "20260511_0006_memory_graph.py",
            "20260718_0015_runtime_plugin_schema.py",
        ),
    },
    "plugin_memory_episode": {
        "columns": (
            "session_id",
            "title",
            "summary",
            "event_ids_json",
            "memory_item_ids_json",
            "importance",
            "status",
        ),
        "indexes": ("ux_memory_episode_memory_item", "idx_memory_episode_scope"),
        "migrations": (
            "20260511_0006_memory_graph.py",
            "20260718_0015_runtime_plugin_schema.py",
        ),
    },
}


class MemoryItemConflictError(RuntimeError):
    """Raised when a memory item update would duplicate an existing item."""


class MemoryItemProtectedError(RuntimeError):
    """Raised when a pinned/manual memory item deletion needs confirmation."""

    def __init__(self, protected_ids: Iterable[int]) -> None:
        self.protected_ids = [int(item_id) for item_id in protected_ids]
        super().__init__("memory item deletion requires allow_pinned confirmation")


async def _exec(sql: str, params: dict | None = None) -> list[dict]:
    active_connection = _ACTIVE_MUTATION_CONNECTION.get()
    if active_connection is not None:
        result = await active_connection.execute(text(sql), params or {})
        if result.returns_rows:
            return [dict(row._mapping) for row in result.fetchall()]
        return []
    engine = get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(text(sql), params or {})
        if result.returns_rows:
            return [dict(row._mapping) for row in result.fetchall()]
        return []


def _normalize_line(value: str) -> str:
    return " ".join(str(value or "").strip().split())


def _sanitize_db_text(value: Any) -> str:
    return str(value or "").replace("\x00", "")


def _redact_profile_enrichment_text(value: Any) -> str:
    text_value = _sanitize_db_text(value)
    if not text_value:
        return ""
    text_value = _EMAIL_RE.sub("[redacted-email]", text_value)
    text_value = _PHONE_RE.sub("[redacted-phone]", text_value)
    text_value = _ID_CARD_RE.sub("[redacted-id]", text_value)
    text_value = _BANK_CARD_RE.sub("[redacted-bank-card]", text_value)
    text_value = _TOKEN_VALUE_RE.sub("[redacted-token]", text_value)
    text_value = _ADDRESS_RE.sub("[redacted-address]", text_value)
    text_value = re.sub(
        r"(?i)\b(api[_-]?key|secret|token|password|passwd|密钥|令牌|密码)\s*[:=]\s*\S+",
        r"\1=[redacted-token]",
        text_value,
    )
    return re.sub(r"\s+", " ", text_value).strip()


def _redact_profile_enrichment_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact_profile_enrichment_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_profile_enrichment_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_profile_enrichment_payload(item) for item in value]
    if isinstance(value, str):
        return _redact_profile_enrichment_text(value)
    return value


def _profile_enrichment_allowed_status(value: Any, *, default: str = "candidate") -> str:
    status = str(value or default).strip().lower()
    return status if status in PROFILE_ENRICHMENT_ACCEPTANCE_STATUSES else default


def _profile_enrichment_content(payload: dict[str, Any]) -> str:
    profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
    review = payload.get("review") if isinstance(payload.get("review"), dict) else {}
    target = payload.get("target") if isinstance(payload.get("target"), dict) else {}
    display_names = profile.get("display_names") or target.get("display_names") or []
    display_name = ""
    if isinstance(display_names, list):
        display_name = next(
            (str(item or "").strip() for item in display_names if str(item or "").strip()), ""
        )
    if not display_name:
        display_name = str(
            profile.get("display_name") or target.get("display_name") or target.get("query") or ""
        ).strip()
    summary = str(profile.get("summary") or payload.get("summary") or "").strip()
    state = _profile_enrichment_allowed_status(review.get("state"), default="candidate")
    content = f"Profile enrichment candidate for {display_name or 'unknown member'}"
    if summary:
        content = f"{content}: {summary}"
    return _redact_profile_enrichment_text(f"{content} [{state}]")[:500]


def _prepare_profile_enrichment_report_for_storage(
    report_payload: dict[str, Any],
    *,
    initial_state: str,
) -> dict[str, Any]:
    safe_payload = _redact_profile_enrichment_payload(report_payload)
    if not isinstance(safe_payload, dict):
        safe_payload = {"report": safe_payload}
    review = safe_payload.get("review") if isinstance(safe_payload.get("review"), dict) else {}
    safe_payload["review"] = {**review, "state": initial_state}

    facets = safe_payload.get("facets")
    if isinstance(facets, list):
        normalized_facets: list[Any] = []
        for facet in facets:
            if isinstance(facet, dict):
                facet_status = str(facet.get("status") or "").strip().lower()
                if facet_status == "accepted":
                    facet = {**facet, "status": initial_state}
            normalized_facets.append(facet)
        safe_payload["facets"] = normalized_facets

    external_candidates = safe_payload.get("external_candidates")
    if isinstance(external_candidates, list):
        normalized_candidates: list[Any] = []
        for candidate in external_candidates:
            if isinstance(candidate, dict):
                binding_status = str(candidate.get("binding_status") or "").strip().lower()
                if binding_status in {"matched", "accepted", "bound"}:
                    candidate = {**candidate, "binding_status": "needs_human_review"}
            normalized_candidates.append(candidate)
        safe_payload["external_candidates"] = normalized_candidates
    return safe_payload


def _normalize_key(value: str) -> str:
    normalized = _normalize_line(value).lower()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def _safe_json_loads(raw: Any, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except Exception:
        return fallback


def _normalize_allowed_session_ids(value: Any) -> list[str]:
    """Return a bounded, stable list from JSONB, JSON text, or Python values."""

    if isinstance(value, str):
        decoded = _safe_json_loads(value, [])
    else:
        decoded = value
    if not isinstance(decoded, (list, tuple, set)):
        return []
    normalized: list[str] = []
    for item in decoded:
        session_id = str(item or "").strip()
        if not session_id or len(session_id) > 256 or session_id in normalized:
            continue
        normalized.append(session_id)
        if len(normalized) >= 100:
            break
    return sorted(normalized)


def _normalize_memory_audience_contract(
    *,
    origin_session_kind: Any,
    audience_scope: Any,
    allowed_session_ids: Any,
    session_id: str,
    expires_at: Any,
) -> dict[str, Any]:
    """Normalize persisted audience metadata, narrowing malformed input to private."""

    origin = str(origin_session_kind or "").strip().lower()
    if origin not in MEMORY_ORIGIN_SESSION_KINDS:
        origin = "unknown"
    audience = str(audience_scope or "").strip().lower()
    if audience not in MEMORY_AUDIENCE_SCOPES:
        audience = "private"
    allowed = _normalize_allowed_session_ids(allowed_session_ids)
    current_session = str(session_id or "").strip()
    if audience == "session":
        if current_session:
            allowed = [current_session]
        elif len(allowed) != 1:
            audience = "private"
            allowed = []
    elif audience == "explicit":
        if not allowed:
            audience = "private"
    if audience == "private":
        allowed = []
    # Unknown-origin rows are legacy-compatible in private conversations only.
    # They can never be made group-visible by malformed audience metadata.
    if origin == "unknown" and audience != "private":
        audience = "private"
        allowed = []
    return {
        "origin_session_kind": origin,
        "audience_scope": audience,
        "allowed_session_ids": allowed,
        "expires_at": _coerce_datetime(expires_at),
    }


def _memory_item_visible_for_audience(
    item: dict[str, Any],
    *,
    session_id: str,
    request_session_kind: str | None = None,
    user_id: str | None = None,
    allow_sensitive: bool = False,
    now: datetime | None = None,
) -> bool:
    """Apply the same fail-closed row policy to profile, SQL, vector and graph reads."""

    if str(item.get("status") or "active").strip().lower() != "active":
        return False
    if item.get("deleted_at") is not None:
        return False
    if user_id is not None and str(item.get("user_id") or "") != str(user_id or ""):
        return False
    request_session = str(session_id or "").strip()
    requested_kind = str(request_session_kind or "").strip().lower()
    if requested_kind not in {"private", "group"}:
        requested_kind = "group" if _is_group_session_id(request_session) else "private"
    audience = str(item.get("audience_scope") or "private").strip().lower()
    origin = str(item.get("origin_session_kind") or "unknown").strip().lower()
    allowed = _normalize_allowed_session_ids(item.get("allowed_session_ids"))
    row_session = str(item.get("session_id") or "").strip()

    if audience == "private":
        audience_allowed = requested_kind == "private" and origin in {"private", "unknown"}
    elif audience == "session":
        audience_allowed = bool(
            request_session
            and requested_kind == "group"
            and origin == "group"
            and allowed == [request_session]
            and (not row_session or row_session == request_session)
        )
    elif audience == "explicit":
        audience_allowed = bool(
            request_session
            and requested_kind == "group"
            and origin in {"private", "group"}
            and request_session in allowed
        )
    else:
        audience_allowed = False
    if not audience_allowed:
        return False

    sensitivity = (
        str(item.get("sensitivity_category") or item.get("sensitivity") or "").strip().lower()
    )
    if sensitivity not in MEMORY_SENSITIVITY_CATEGORIES:
        return False
    if sensitivity != "normal" and not allow_sensitive:
        return False

    expiry = _coerce_datetime(item.get("expires_at"))
    if expiry is not None:
        current = _coerce_datetime(now or datetime.now(UTC))
        if current is None or expiry <= current:
            return False
    return True


def _memory_item_matches_audience_contract(
    item: dict[str, Any],
    contract: dict[str, Any],
    *,
    session_id: str,
) -> bool:
    current = _normalize_memory_audience_contract(
        origin_session_kind=item.get("origin_session_kind"),
        audience_scope=item.get("audience_scope"),
        allowed_session_ids=item.get("allowed_session_ids"),
        session_id=session_id,
        expires_at=item.get("expires_at"),
    )
    same_audience = current["audience_scope"] == contract["audience_scope"]
    same_origin = current["origin_session_kind"] == contract["origin_session_kind"]
    if same_audience and current["audience_scope"] == "private":
        # Migration-backed legacy rows use ``unknown`` origin. Treat that as
        # private only when both contracts are already private; this preserves
        # dedupe/protection without making the row group-visible.
        same_origin = {
            current["origin_session_kind"],
            contract["origin_session_kind"],
        }.issubset({"unknown", "private"})
    return bool(
        same_origin
        and same_audience
        and set(current["allowed_session_ids"]) == set(contract["allowed_session_ids"])
    )


def _to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads_json_object_or_array(raw: Any) -> Any:
    text_value = str(raw or "").strip()
    decoder = json.JSONDecoder()
    candidates = [text_value]
    fenced = re.fullmatch(r"```(?:json|JSON)?\s*(.*?)\s*```", text_value, flags=re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1).strip())
    candidates.extend(text_value[index:] for index, char in enumerate(text_value) if char in "{[")
    for candidate in candidates:
        try:
            payload, _ = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, (dict, list)):
            return payload
    raise ValueError("LLM response did not contain a JSON object or array")


def _settings_bool(settings: Any, name: str, default: bool) -> bool:
    return bool(getattr(settings, name, default))


def _settings_int(
    settings: Any, name: str, default: int, *, minimum: int = 1, maximum: int | None = None
) -> int:
    try:
        value = int(getattr(settings, name, default) or default)
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(value, maximum)
    return value


def _clamp_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        coerced = int(value if value is not None else default)
    except (TypeError, ValueError):
        coerced = default
    return max(minimum, min(coerced, maximum))


def _settings_float(
    settings: Any,
    name: str,
    default: float,
    *,
    minimum: float = 0.1,
    maximum: float | None = None,
) -> float:
    try:
        value = float(getattr(settings, name, default) or default)
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(value, maximum)
    return value


def _memory_retrieval_tokens(query: str) -> list[str]:
    text_value = _normalize_line(query).lower()
    if not text_value:
        return []
    raw_tokens = re.findall(r"[a-z0-9][a-z0-9_-]{1,40}|[\u4e00-\u9fff]{2,}", text_value)
    tokens: list[str] = []
    for raw_token in raw_tokens:
        if re.fullmatch(r"[\u4e00-\u9fff]{2,}", raw_token):
            cjk_candidates: list[str] = []
            if len(raw_token) <= 4:
                cjk_candidates.append(raw_token)
            for size in (4, 3, 2):
                if len(raw_token) < size:
                    continue
                for start in range(0, len(raw_token) - size + 1):
                    cjk_candidates.append(raw_token[start : start + size])
            for token in cjk_candidates:
                if token not in tokens:
                    tokens.append(token)
                if len(tokens) >= 12:
                    return tokens
            continue
        token = raw_token
        if token not in tokens:
            tokens.append(token)
        if len(tokens) >= 12:
            return tokens
    if not tokens and len(text_value) >= 2:
        tokens.append(text_value[:40])
    return tokens[:12]


def _truncate_error(value: BaseException | str, *, limit: int = 500) -> str:
    text_value = str(value or "").replace("\n", " ").strip()
    return text_value[:limit]


_LLM_JOB_SCOPE_KEYS = {"tenant_id", "channel", "source_key", "user_id", "session_id"}
_LLM_JOB_SCOPE_GROUP_KEYS = (
    "tenant_id",
    "channel",
    "source_key",
    "user_id",
    "session_id",
    "status",
    "error_type",
)


def _llm_job_error_type_expr() -> str:
    return (
        "COALESCE("
        "NULLIF(result_json::jsonb ->> 'error_type', ''), "
        "NULLIF(result_json::jsonb #>> '{graph,error_type}', ''), "
        "CASE "
        "WHEN result_json::jsonb ->> 'timeout' = 'true' THEN 'TimeoutError' "
        "WHEN last_error ILIKE '%timeout%' THEN 'TimeoutError' "
        "WHEN SUBSTRING(last_error FROM '^([A-Za-z_][A-Za-z0-9_]*(Error|Exception))') <> '' "
        "THEN SUBSTRING(last_error FROM '^([A-Za-z_][A-Za-z0-9_]*(Error|Exception))') "
        "WHEN last_error <> '' THEN 'LastError' "
        "ELSE '' END"
        ")"
    )


def _safe_llm_job_result_json() -> str:
    return _to_json({"admin_maintenance": True})


def _llm_job_scope_is_smoke_sql() -> str:
    checks: list[str] = []
    for key in ("tenant_id", "channel", "source_key", "user_id", "session_id"):
        checks.append(f"LOWER({key}) LIKE '%smoke%'")
        checks.append(f"LOWER({key}) LIKE '%test%'")
    return "(" + " OR ".join(checks) + ")"


def _llm_job_filter_sql(
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
    alias: str = "",
) -> tuple[list[str], dict[str, Any]]:
    prefix = f"{alias}." if alias else ""
    clauses: list[str] = []
    params: dict[str, Any] = {}
    for key, value in (
        ("tenant_id", tenant_id),
        ("channel", channel),
        ("source_key", source_key),
        ("user_id", user_id),
        ("session_id", session_id),
    ):
        if value:
            clauses.append(f"{prefix}{key} = :{key}")
            params[key] = value
    if status:
        normalized_status = str(status).strip().lower()
        if normalized_status in MEMORY_EXTRACTION_JOB_STATUSES:
            clauses.append(f"{prefix}status = :status")
            params["status"] = normalized_status
    if error_type:
        clauses.append(f"{_llm_job_error_type_expr()} = :error_type")
        params["error_type"] = str(error_type).strip()
    if created_before is not None:
        clauses.append(f"{prefix}created_at < :created_before")
        params["created_before"] = created_before
    if created_after is not None:
        clauses.append(f"{prefix}created_at >= :created_after")
        params["created_after"] = created_after
    if updated_before is not None:
        clauses.append(f"{prefix}updated_at < :updated_before")
        params["updated_before"] = updated_before
    if updated_after is not None:
        clauses.append(f"{prefix}updated_at >= :updated_after")
        params["updated_after"] = updated_after
    return clauses, params


def _llm_job_scope_filter_sql(raw_allowlist: str | None) -> tuple[str, dict[str, str]]:
    raw = str(raw_allowlist or "").strip()
    if not raw:
        return "", {}

    scopes: list[dict[str, str]] = []
    for raw_token in re.split(r"[\n;]+", raw):
        token = raw_token.strip()
        if not token:
            continue
        scope: dict[str, str] = {}
        if "=" in token:
            for part in token.split(","):
                if "=" not in part:
                    continue
                key, value = part.split("=", 1)
                normalized_key = key.strip()
                normalized_value = value.strip()
                if normalized_key in _LLM_JOB_SCOPE_KEYS and normalized_value:
                    scope[normalized_key] = normalized_value
        else:
            parts = [part.strip() for part in token.split(":")]
            if len(parts) in {4, 5} and all(parts):
                scope = {
                    "tenant_id": parts[0],
                    "channel": parts[1],
                    "source_key": parts[2],
                    "user_id": parts[3],
                }
                if len(parts) == 5:
                    scope["session_id"] = parts[4]
        if scope:
            scopes.append(scope)

    if not scopes:
        return " AND FALSE ", {}

    params: dict[str, str] = {}
    clauses: list[str] = []
    for index, scope in enumerate(scopes):
        parts: list[str] = []
        for key in ("tenant_id", "channel", "source_key", "user_id", "session_id"):
            value = scope.get(key)
            if value is None:
                continue
            param_name = f"scope_{index}_{key}"
            params[param_name] = value
            parts.append(f"{key} = :{param_name}")
        if parts:
            clauses.append("(" + " AND ".join(parts) + ")")
    if not clauses:
        return " AND FALSE ", {}
    return " AND (" + " OR ".join(clauses) + ") ", params


def _job_idempotency_key(
    *,
    tenant_id: str,
    channel: str,
    source_key: str,
    user_id: str,
    session_id: str,
    trace_id: str,
    source_event_id: int | None,
) -> str:
    if source_event_id is not None:
        components = {
            "tenant_id": tenant_id,
            "channel": channel,
            "source_key": source_key,
            "user_id": user_id,
            "session_id": session_id,
            "source_event_id": source_event_id,
        }
    else:
        components = {
            "tenant_id": tenant_id,
            "channel": channel,
            "source_key": source_key,
            "user_id": user_id,
            "session_id": session_id,
            "trace_id": trace_id,
        }
    digest_payload = json.dumps(
        components, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    digest = hashlib.sha256(digest_payload).hexdigest()
    if source_event_id is not None:
        return f"memory:llm:event:{source_event_id}:{digest[:32]}"
    readable_trace = _normalize_key(trace_id or f"{session_id}:{user_id}")[:16]
    return f"memory:llm:trace:{readable_trace}:{digest[:32]}"


def _backfill_event_key(
    *,
    tenant_id: str,
    channel: str,
    source_key: str,
    user_id: str,
    session_id: str,
    timestamp: Any,
    user_text: str,
) -> str:
    normalized_text = _normalize_line(user_text)
    content_hash = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
    components = {
        "tenant_id": tenant_id,
        "channel": channel,
        "source_key": source_key,
        "user_id": user_id,
        "session_id": session_id,
        "timestamp": str(timestamp or ""),
        "content_hash": content_hash,
    }
    digest_payload = json.dumps(
        components, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    digest = hashlib.sha256(digest_payload).hexdigest()
    return f"memory:backfill:{digest}"


def _worker_id(settings: Any) -> str:
    configured = str(getattr(settings, "worker_instance_id", "") or "").strip()
    if configured:
        return configured[:128]
    return f"{socket.gethostname()}-{uuid4().hex[:12]}"[:128]


def _needs_source_fallback(source_key: str | None) -> bool:
    normalized = str(source_key or "").strip()
    return bool(normalized) and normalized != "*"


def _detect_sensitivity(content: str) -> str:
    text_value = str(content or "")
    if _TOKEN_RE.search(text_value):
        return "sensitive"
    if _ID_CARD_RE.search(text_value) or _BANK_CARD_RE.search(text_value):
        return "pii"
    pii_keywords = (
        "手机号",
        "手机号码",
        "微信号",
        "wxid",
        "身份证",
        "银行卡",
        "收货地址",
        "家庭地址",
        "住在",
    )
    if _PHONE_RE.search(text_value) or any(keyword in text_value for keyword in pii_keywords):
        return "pii"
    return "normal"


def _memory_type_for_content(content: str) -> str:
    if any(keyword in content for keyword in ("我叫", "我是", "我的名字")):
        return "profile_fact"
    if any(keyword in content for keyword in ("不要", "别给我", "别再", "禁止", "必须")):
        return "constraint"
    if any(keyword in content for keyword in ("喜欢", "不喜欢", "习惯", "默认", "偏好")):
        return "preference"
    return "note"


def _extract_ascii_terms(content: str) -> list[str]:
    terms: list[str] = []
    for match in re.finditer(r"\b[A-Za-z][A-Za-z0-9_-]{1,40}\b", content):
        value = match.group(0).strip()
        if value.lower() in {"api", "key", "token", "password", "secret"}:
            continue
        if value not in terms:
            terms.append(value)
    return terms


def _semantic_key(memory_type: str, field: str, value: str) -> str:
    normalized = _normalize_line(value).lower()
    normalized = re.sub(r"[^a-z0-9:_-]+", "_", normalized).strip("_")
    if not normalized:
        normalized = _normalize_key(value)[:16]
    return f"{memory_type}:{field}:{normalized}"[:64]


def _bounded_memory_key(value: str) -> str:
    key = str(value or "").strip()
    if len(key) <= 64:
        return key
    suffix = ":" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return key[: 64 - len(suffix)] + suffix


def _memory_action(
    *,
    op: str,
    content: str,
    memory_type: str,
    normalized_key: str,
    confidence: float,
    sensitivity: str,
    reason: str,
    source_type: str = "auto",
    status: str | None = None,
    invalidates_normalized_key: str | None = None,
    target_item_id: int | None = None,
) -> dict[str, Any]:
    effective_status = status or (
        "active"
        if sensitivity == "normal" and confidence >= PROMPT_AUTO_CONFIDENCE_MIN
        else "pending"
    )
    return {
        "op": op,
        "content": _normalize_line(content)[:200],
        "source_type": source_type,
        "memory_type": memory_type,
        "normalized_key": normalized_key,
        "confidence": float(confidence),
        "status": effective_status,
        "sensitivity": sensitivity,
        "reason": reason,
        "invalidates_normalized_key": invalidates_normalized_key,
        "target_item_id": target_item_id,
    }


def _ignore_memory_action(content: str, reason: str) -> dict[str, Any]:
    return _memory_action(
        op="ignore",
        content=content,
        memory_type="note",
        normalized_key=_semantic_key("ignore", "text", content[:80]),
        confidence=0.0,
        sensitivity=_detect_sensitivity(content),
        reason=reason,
        source_type="auto",
        status="pending",
    )


def extract_structured_memory_actions(user_text: str) -> list[dict[str, Any]]:
    content = _normalize_line(user_text)
    one_off_service_words = (
        "需要",
        "想要",
        "售后",
        "下单",
        "退款",
        "发票",
        "订单",
    )
    lookup_words = ("查一下", "帮我查", "查询", "看看", "看一下")
    has_explicit_marker = bool(
        re.search(r"(?:^|[，,。；;：:])\s*(?:请|帮我)?(?:记住|记一下|长期记住)", content)
        or re.search(r"(?:以后|下次|默认|长期)(?:都|请|用|使用|保持|不要|别|记住)", content)
        # Natural preference phrasing often puts the subject between the
        # future marker and "default", e.g. "以后退款默认原路退回".
        or re.search(r"(?:以后|下次).{0,24}默认", content)
    )
    has_profile_marker = bool(
        re.search(
            r"(?:^|[，,。；;：:])\s*(?:"
            r"我叫|我的名字(?:是|叫)|"
            r"我(?:现在|已经|最近)?(?:不再|不)?(?:喜欢|偏好)|"
            r"我习惯"
            r")",
            content,
        )
        or re.search(r"(?:^|[，,。；;：:])\s*我是(?!来|想|要|需要|正在|问|看|查|咨询)", content)
        or re.search(r"(?:以后|下次|始终|一直)(?:不要|别给我|别再)", content)
    )
    has_one_off_service_word = any(word in content for word in one_off_service_words)
    has_lookup_word = any(word in content for word in lookup_words)
    if len(content) < 6 and not has_explicit_marker and not has_profile_marker:
        return [_ignore_memory_action(content, "too_short_without_memory_marker")]
    if not has_explicit_marker and not has_profile_marker:
        return [_ignore_memory_action(content, "no_long_term_memory_marker")]
    if has_one_off_service_word and not (
        "以后" in content
        or "下次" in content
        or "默认" in content
        or "长期" in content
        or has_profile_marker
    ):
        return [_ignore_memory_action(content, "one_off_service_request")]
    if has_lookup_word and not has_explicit_marker and not has_profile_marker:
        return [_ignore_memory_action(content, "one_off_lookup_request")]

    source_type = "explicit_user" if has_explicit_marker else "auto"
    sensitivity = _detect_sensitivity(content)
    confidence = 0.95 if source_type == "explicit_user" else 0.82
    status = "active"
    if sensitivity != "normal":
        status = "pending"
        confidence = min(confidence, 0.5)

    actions: list[dict[str, Any]] = []
    ascii_terms = _extract_ascii_terms(content)
    chinese_preference = re.search(
        r"(?:^|[，,。；;：:])\s*我(?P<negative>不)?(?:喜欢|偏好)(?P<value>[^，,。；;！？!?]{1,40})",
        content,
    )
    if chinese_preference:
        value = _normalize_line(chinese_preference.group("value")).strip(" ：:的")
        if value:
            negative = bool(chinese_preference.group("negative"))
            actions.append(
                _memory_action(
                    op="add",
                    content=f"用户{'不' if negative else ''}喜欢 {value}",
                    memory_type="preference",
                    normalized_key=_semantic_key("preference", "value", value),
                    confidence=confidence,
                    sensitivity=_detect_sensitivity(value),
                    reason="preference_statement",
                    source_type=source_type,
                    status=status,
                )
            )
            return actions
    revoked_preference = any(
        marker in content for marker in ("不再喜欢", "现在不喜欢", "不喜欢")
    ) and ("了" in content or "换成" in content or "改成" in content)
    if revoked_preference and ascii_terms:
        old_value = ascii_terms[0]
        old_key = _semantic_key("preference", "brand", old_value)
        actions.append(
            _memory_action(
                op="invalidate",
                content=f"用户不再喜欢 {old_value}",
                memory_type="preference",
                normalized_key=old_key,
                confidence=max(confidence, 0.86),
                sensitivity=sensitivity,
                reason="user_revoked_preference",
                source_type=source_type,
                status="active" if sensitivity == "normal" else "pending",
                invalidates_normalized_key=old_key,
            )
        )
        if len(ascii_terms) >= 2 and any(marker in content for marker in ("换成", "改成")):
            new_value = ascii_terms[1]
            actions.append(
                _memory_action(
                    op="add",
                    content=f"用户喜欢 {new_value}",
                    memory_type="preference",
                    normalized_key=_semantic_key("preference", "brand", new_value),
                    confidence=max(confidence, 0.86),
                    sensitivity=_detect_sensitivity(new_value),
                    reason="replacement_preference",
                    source_type=source_type,
                )
            )
        return actions

    if ("喜欢" in content or "偏好" in content) and ascii_terms:
        value = ascii_terms[0]
        negative = "不喜欢" in content
        actions.append(
            _memory_action(
                op="add",
                content=f"用户{'不' if negative else ''}喜欢 {value}",
                memory_type="preference",
                normalized_key=_semantic_key("preference", "brand", value),
                confidence=confidence,
                sensitivity=sensitivity,
                reason="preference_statement",
                source_type=source_type,
                status=status,
            )
        )
        return actions

    if any(marker in content for marker in ("简洁", "中文回答", "中文简洁回答")) or (
        "默认" in content and ("回答" in content or "回复" in content or "中文" in content)
    ):
        response_tokens: list[str] = []
        if "中文" in content:
            response_tokens.append("中文")
        if "简洁" in content or "短" in content:
            response_tokens.append("简洁")
        if "默认" in content:
            response_tokens.insert(0, "默认")
        extracted = "".join(response_tokens) + (
            "回答" if "回答" in content or response_tokens else ""
        )
        extracted = extracted or content
        actions.append(
            _memory_action(
                op="update",
                content=extracted,
                memory_type="constraint" if "默认" in content else "preference",
                normalized_key=_semantic_key("constraint", "response_defaults", "language_style"),
                confidence=confidence,
                sensitivity=sensitivity,
                reason="response_default_preference",
                source_type=source_type,
                status=status,
            )
        )
        return actions

    memory_type = _memory_type_for_content(content)
    if any(marker in content for marker in ("我叫", "我的名字")):
        normalized_key = _semantic_key("profile_fact", "name", "name")
    elif any(marker in content for marker in ("不要", "别给我", "别再", "禁止", "必须")):
        normalized_key = _semantic_key("constraint", "instruction", content[:80])
    else:
        normalized_key = _semantic_key(memory_type, "text", content[:80])
    actions.append(
        _memory_action(
            op="add",
            content=content[:200],
            memory_type=memory_type,
            normalized_key=normalized_key,
            confidence=confidence,
            sensitivity=sensitivity,
            reason="explicit_or_profile_memory",
            source_type=source_type,
            status=status,
        )
    )
    return actions


def _extract_long_term_candidates(user_text: str) -> list[dict[str, Any]]:
    return [
        action
        for action in extract_structured_memory_actions(user_text)
        if str(action.get("op") or "add") != "ignore"
    ]


def _merge_long_term_items(
    existing: list[str],
    candidates: list[str],
    manual_notes: str,
) -> tuple[list[str], str]:
    seen = {_normalize_line(item) for item in existing if _normalize_line(item)}
    merged = [item for item in existing if _normalize_line(item)]
    for candidate in candidates:
        normalized = _normalize_line(candidate)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        merged.append(normalized[:200])
    sections: list[str] = []
    if merged:
        sections.append("已知用户事实与偏好：")
        sections.extend(f"- {item}" for item in merged)
    manual = str(manual_notes or "").strip()
    if manual:
        sections.append("人工补充记忆：")
        sections.extend(f"- {line.strip()}" for line in manual.splitlines() if line.strip())
    return merged, "\n".join(sections)[:4000]


def _merge_manual_notes(identity_notes: str, session_notes: str) -> str:
    identity = str(identity_notes or "").strip()
    session = str(session_notes or "").strip()
    if identity and session:
        return f"全局记忆备注：\n{identity}\n\n当前会话备注：\n{session}"[:4000]
    return identity or session


def _split_note_lines(value: str) -> list[str]:
    seen: set[str] = set()
    lines: list[str] = []
    for raw_line in str(value or "").splitlines():
        line = _BULLET_RE.sub("", raw_line).strip()
        if not line or line.endswith("：") or line.endswith(":"):
            continue
        normalized = _normalize_line(line)
        if normalized and normalized not in seen:
            seen.add(normalized)
            lines.append(normalized[:500])
    return lines


def _legacy_long_term_lines(profile: dict[str, Any]) -> list[str]:
    items = _safe_json_loads(profile.get("long_term_items_json"), [])
    lines: list[str] = []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, str):
                lines.append(item)
            elif isinstance(item, dict) and item.get("content"):
                lines.append(str(item.get("content") or ""))
    if not lines:
        for raw_line in str(profile.get("long_term_memory") or "").splitlines():
            line = _BULLET_RE.sub("", raw_line).strip()
            if not line or line.endswith("：") or line.endswith(":"):
                continue
            if line in {"已知用户事实与偏好", "人工补充记忆", "人工补充"}:
                continue
            lines.append(line)
    seen: set[str] = set()
    unique: list[str] = []
    for line in lines:
        normalized = _normalize_line(line)
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique.append(normalized[:500])
    return unique


def _item_content(item: dict[str, Any]) -> str:
    return _normalize_line(str(item.get("content") or ""))[:500]


def _looks_like_memory_item_row(row: dict[str, Any]) -> bool:
    return (
        "content" in row
        and "scope_type" in row
        and "source_type" in row
        and "normalized_key" in row
    )


def _graph_normalized_name(value: Any) -> str:
    normalized = _normalize_line(str(value or "")).lower()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized[:128]


def _graph_status_for_memory_item(item: dict[str, Any]) -> str:
    status = str(item.get("status") or "active").strip().lower()
    return status if status in MEMORY_ITEM_STATUSES else "active"


def _graph_stale_status_for_memory_item(item: dict[str, Any]) -> str:
    status = _graph_status_for_memory_item(item)
    return "invalidated" if status == "active" else status


def _graph_confidence_for_memory_item(item: dict[str, Any]) -> float:
    try:
        confidence = float(item.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if item.get("pinned") or str(item.get("source_type") or "") == "manual":
        return max(confidence, 1.0)
    try:
        priority = int(item.get("priority") or 0)
    except (TypeError, ValueError):
        priority = 0
    if priority > 0:
        return max(confidence, 0.9)
    return confidence


def _graph_entity_name_from_key(item: dict[str, Any]) -> tuple[str, str] | None:
    normalized_key = str(item.get("normalized_key") or "").strip()
    content = str(item.get("content") or "")
    ascii_terms = _extract_ascii_terms(content)
    key_parts = [part for part in normalized_key.split(":") if part]
    if normalized_key.startswith("preference:brand:"):
        name = ascii_terms[0] if ascii_terms else key_parts[-1]
        return "brand", name
    if ascii_terms:
        return "thing", ascii_terms[0]
    return None


def _memory_graph_mapping_for_item(item: dict[str, Any]) -> dict[str, Any] | None:
    item_id = item.get("id")
    if item_id is None:
        return None
    memory_type = str(item.get("memory_type") or "note").strip().lower()
    content = _normalize_line(str(item.get("content") or ""))
    if not content:
        return None
    status = _graph_status_for_memory_item(item)
    confidence = _graph_confidence_for_memory_item(item)
    value = item.get("value")
    if not isinstance(value, dict):
        value = _safe_json_loads(item.get("value_json"), {})
    if str(item.get("source_type") or "") in {
        LLM_GROUP_WINDOW_SOURCE_TYPE,
        DETERMINISTIC_GROUP_WINDOW_SOURCE_TYPE,
    } and isinstance(value, dict):
        relation = value.get("relation") if isinstance(value.get("relation"), dict) else value
        predicate = str(relation.get("predicate") or "").strip().lower()
        subject = _normalize_line(str(relation.get("subject") or ""))
        object_value = _normalize_line(str(relation.get("object") or ""))
        if predicate in GROUP_GRAPH_EDGE_TYPES and subject and object_value:
            evidence_ids = sorted(_coerce_int_set(relation.get("evidence_event_ids") or []))
            subject_type = str(relation.get("subject_type") or "person").strip().lower()
            object_type = str(relation.get("object_type") or "person").strip().lower()
            if subject_type not in GROUP_GRAPH_NODE_TYPES:
                subject_type = "person"
            if object_type not in GROUP_GRAPH_NODE_TYPES:
                object_type = "person"
            return {
                "memory_item_id": int(item_id),
                "status": status,
                "confidence": confidence,
                "source_event_id": evidence_ids[0] if evidence_ids else item.get("source_event_id"),
                "kind": "fact",
                "predicate": predicate,
                "subject_entity": {"entity_type": subject_type, "name": subject},
                "object_entity": {"entity_type": object_type, "name": object_value},
                "object_value": "",
            }
    base = {
        "memory_item_id": int(item_id),
        "status": status,
        "confidence": confidence,
        "source_event_id": item.get("source_event_id"),
    }
    if memory_type == "episodic":
        try:
            importance = int(item.get("priority") or 0)
        except (TypeError, ValueError):
            importance = 0
        return {
            **base,
            "kind": "episode",
            "session_id": str(item.get("session_id") or ""),
            "title": content[:160],
            "summary": content[:1000],
            "event_ids": [int(item["source_event_id"])]
            if item.get("source_event_id") is not None
            else [],
            "memory_item_ids": [int(item_id)],
            "importance": min(max(importance, 0), 100),
        }
    if memory_type not in {"preference", "constraint", "profile_fact", "note"}:
        return None

    entity_ref = _graph_entity_name_from_key(item)
    object_entity = None
    object_value = content
    predicate = memory_type or "note"
    if memory_type == "preference":
        if "不喜欢" in content or re.search(r"\bdislike[sd]?\b", content, flags=re.IGNORECASE):
            predicate = "dislikes"
        else:
            predicate = "likes"
        if entity_ref is not None:
            object_entity = {"entity_type": entity_ref[0], "name": entity_ref[1]}
            object_value = ""
    elif memory_type == "constraint":
        if any(
            token in content
            for token in ("回答", "回复", "response", "style", "默认", "简洁", "中文")
        ):
            predicate = "prefers_response_style"
        else:
            predicate = "constraint"
    elif memory_type == "profile_fact":
        predicate = "profile_fact"
        if entity_ref is not None:
            object_entity = {"entity_type": entity_ref[0], "name": entity_ref[1]}
            object_value = ""
    return {
        **base,
        "kind": "fact",
        "predicate": predicate,
        "object_entity": object_entity,
        "object_value": object_value[:1000],
    }


def _item_sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
    source_rank = {
        "manual": 0,
        "explicit_user": 1,
        "auto": 2,
        DETERMINISTIC_GROUP_WINDOW_SOURCE_TYPE: 3,
        LLM_GROUP_WINDOW_SOURCE_TYPE: 4,
        "backfill": 5,
    }
    return (
        source_rank.get(str(item.get("source_type") or "auto"), 9),
        -int(item.get("priority") or 0),
        str(item.get("first_seen_at") or item.get("created_at") or ""),
    )


def _memory_acceptance_status_from_item(item: dict[str, Any]) -> str:
    value = item.get("value")
    if not isinstance(value, dict):
        value = _safe_json_loads(item.get("value_json"), {})
    acceptance = value.get("acceptance") if isinstance(value, dict) else None
    if isinstance(acceptance, dict):
        return str(acceptance.get("status") or "")
    return ""


def _memory_acceptance_status_bucket(item: dict[str, Any]) -> str:
    acceptance_status = _memory_acceptance_status_from_item(item).strip().lower()
    if acceptance_status:
        return (
            acceptance_status
            if acceptance_status in MEMORY_ACCEPTANCE_STATUSES
            else "unknown_acceptance"
        )
    return "missing_acceptance"


def _memory_item_missing_acceptance(item: dict[str, Any]) -> bool:
    return _memory_acceptance_status_bucket(item) == "missing_acceptance"


def _memory_sensitivity_bucket(value: Any) -> str:
    sensitivity = str(value or "normal").strip().lower()
    if sensitivity in {"", "normal"}:
        return "normal"
    if sensitivity in {"private", "pii"}:
        return "private"
    return "sensitive"


def _is_prompt_eligible_memory_item(item: dict[str, Any]) -> bool:
    if str(item.get("source_type") or "") == PROFILE_ENRICHMENT_SOURCE_TYPE:
        return False
    if str(item.get("memory_type") or "") == PROFILE_ENRICHMENT_MEMORY_TYPE:
        return False
    if str(item.get("status") or "") != "active":
        return False
    if str(item.get("sensitivity") or "normal") != "normal":
        return False
    if item.get("deleted_at") is not None:
        return False
    acceptance_status = _memory_acceptance_status_from_item(item)
    return acceptance_status in {"", "accepted"}


def _is_legacy_prompt_eligible_manual_item(item: dict[str, Any]) -> bool:
    if str(item.get("status") or "") != "active" or item.get("source_type") != "manual":
        return False
    acceptance_status = _memory_acceptance_status_from_item(item)
    return acceptance_status in {"", "accepted"}


def _render_legacy_identity_from_items(items: list[dict[str, Any]]) -> tuple[list[str], str, str]:
    active = [
        item
        for item in items
        if _is_prompt_eligible_memory_item(item) or _is_legacy_prompt_eligible_manual_item(item)
    ]
    active.sort(key=_item_sort_key)
    manual_lines = [_item_content(item) for item in active if item.get("source_type") == "manual"]
    long_items = [
        _item_content(item)
        for item in active
        if item.get("source_type") != "manual"
        and str(item.get("sensitivity") or "normal") == "normal"
        and float(item.get("confidence") or 0.0) >= PROMPT_AUTO_CONFIDENCE_MIN
    ]
    manual_lines = [line for line in manual_lines if line]
    long_items = [line for line in long_items if line]
    sections: list[str] = []
    if long_items:
        sections.append("已知用户事实与偏好：")
        sections.extend(f"- {item}" for item in long_items)
    if manual_lines:
        sections.append("人工补充记忆：")
        sections.extend(f"- {line}" for line in manual_lines)
    return long_items, "\n".join(sections)[:4000], "\n".join(manual_lines)[:4000]


def _render_session_manual_from_items(items: list[dict[str, Any]]) -> str:
    active = [
        item
        for item in items
        if (_is_prompt_eligible_memory_item(item) or _is_legacy_prompt_eligible_manual_item(item))
        and item.get("source_type") == "manual"
    ]
    active.sort(key=_item_sort_key)
    return "\n".join(_item_content(item) for item in active if _item_content(item))[:4000]


def _timestamp_sort_value(value: Any) -> str:
    return str(value or "")


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _normalize_vector_score(value: Any) -> float:
    score = _safe_float(value, 0.0)
    if score < 0.0:
        return 0.0
    if score > 1.0:
        return 1.0
    return score


def _recency_score(value: Any) -> float:
    if not value:
        return 0.0
    if isinstance(value, datetime):
        timestamp = value
    else:
        raw = str(value)
        timestamp = None
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                timestamp = datetime.strptime(raw[:19], fmt)
                break
            except ValueError:
                continue
        if timestamp is None:
            return 0.0
    if timestamp.tzinfo is not None:
        timestamp = timestamp.replace(tzinfo=None)
    now_utc = datetime.now(UTC).replace(tzinfo=None)
    age_days = max(0.0, (now_utc - timestamp).total_seconds() / 86400.0)
    return 1.0 / (1.0 + age_days / 30.0)


def _memory_item_hybrid_breakdown(
    item: dict[str, Any],
    *,
    source_key: str,
    session_id: str,
    has_query: bool,
) -> dict[str, float | int | bool]:
    vector_score = _normalize_vector_score(item.get("vector_score"))
    match_count = _safe_int(item.get("match_count"), 0)
    source_type = str(item.get("source_type") or "auto")
    priority = min(max(_safe_int(item.get("priority"), 0), 0), 100)
    confidence = min(max(_safe_float(item.get("confidence"), 0.0), 0.0), 1.0)
    occurrence_count = min(max(_safe_int(item.get("occurrence_count"), 0), 0), 20)
    recency = _recency_score(
        item.get("last_seen_at") or item.get("updated_at") or item.get("created_at")
    )
    scope_type = str(item.get("scope_type") or "")
    item_source = str(item.get("source_key") or "")
    source_boost = {"manual": 38.0, "explicit_user": 30.0, "auto": 7.0, "backfill": 3.0}.get(
        source_type, 0.0
    )
    if item.get("pinned"):
        source_boost += 42.0
    scope_boost = 0.0
    if scope_type == "session" and str(item.get("session_id") or "") == session_id:
        scope_boost += 28.0
    elif scope_type == "identity":
        scope_boost += 8.0
    if _needs_source_fallback(source_key) and item_source == source_key:
        scope_boost += 4.0
    keyword_score = float(match_count) * HYBRID_KEYWORD_WEIGHT
    if not has_query or match_count == 0:
        keyword_score += {"manual": 16.0, "explicit_user": 10.0}.get(source_type, 0.0)
    vector_component = vector_score * HYBRID_VECTOR_WEIGHT
    priority_score = priority * 0.45
    confidence_score = confidence * 12.0
    occurrence_score = occurrence_count * 0.7
    recency_score = recency * 8.0
    hybrid_score = (
        vector_component
        + keyword_score
        + source_boost
        + scope_boost
        + priority_score
        + confidence_score
        + occurrence_score
        + recency_score
    )
    return {
        "hybrid_score": round(hybrid_score, 3),
        "vector_score": round(vector_score, 6),
        "keyword_score": round(keyword_score, 3),
        "match_count": match_count,
        "source_boost": round(source_boost, 3),
        "scope_boost": round(scope_boost, 3),
        "priority_score": round(priority_score, 3),
        "confidence_score": round(confidence_score, 3),
        "occurrence_score": round(occurrence_score, 3),
        "recency_score": round(recency_score, 3),
        "pinned": bool(item.get("pinned")),
    }


def _attach_hybrid_item_score(
    item: dict[str, Any],
    *,
    source_key: str,
    session_id: str,
    has_query: bool,
) -> dict[str, Any]:
    scored = dict(item)
    breakdown = _memory_item_hybrid_breakdown(
        scored,
        source_key=source_key,
        session_id=session_id,
        has_query=has_query,
    )
    scored["hybrid_score"] = breakdown["hybrid_score"]
    scored["hybrid_score_breakdown"] = breakdown
    return scored


def _rank_retrieved_memory_items(
    items: list[dict[str, Any]],
    *,
    source_key: str,
    user_id: str,
    session_id: str,
    has_query: bool,
    limit: int,
    request_session_kind: str | None = None,
    allow_sensitive: bool = False,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for item in items:
        if not _is_prompt_eligible_memory_item(item):
            continue
        if str(item.get("user_id") or "") != user_id:
            continue
        if not _memory_item_visible_for_audience(
            item,
            session_id=session_id,
            request_session_kind=request_session_kind,
            user_id=user_id,
            allow_sensitive=allow_sensitive,
        ):
            continue
        item_source = str(item.get("source_key") or "")
        if _needs_source_fallback(source_key):
            if item_source not in {source_key, "*"}:
                continue
        elif item_source != (source_key or "*"):
            continue
        scope_type = str(item.get("scope_type") or "")
        if scope_type == "session" and str(item.get("session_id") or "") != session_id:
            continue
        if scope_type not in {"identity", "session"}:
            continue
        if has_query:
            match_count = _safe_int(item.get("match_count"), 0)
            vector_score = _normalize_vector_score(item.get("vector_score"))
            if match_count <= 0 and vector_score < MEMORY_VECTOR_RELEVANCE_MIN:
                continue
        filtered.append(item)

    def score(item: dict[str, Any]) -> tuple[float, int, int, str, int]:
        try:
            match_count = int(item.get("match_count") or 0)
        except (TypeError, ValueError):
            match_count = 0
        source_type = str(item.get("source_type") or "auto")
        priority = int(item.get("priority") or 0)
        occurrence_count = int(item.get("occurrence_count") or 0)
        confidence = float(item.get("confidence") or 0.0)
        value = 0.0
        value += match_count * 100.0
        if item.get("scope_type") == "session":
            value += 24.0
        if item.get("pinned"):
            value += 22.0
        value += {"manual": 18.0, "explicit_user": 14.0, "auto": 4.0, "backfill": 1.0}.get(
            source_type,
            0.0,
        )
        value += min(max(priority, 0), 100) * 0.35
        value += confidence * 8.0
        value += min(max(occurrence_count, 0), 20) * 0.5
        if not has_query or match_count == 0:
            value += {"manual": 12.0, "explicit_user": 8.0}.get(source_type, 0.0)
        return (
            value,
            match_count,
            priority,
            _timestamp_sort_value(
                item.get("last_seen_at") or item.get("updated_at") or item.get("created_at")
            ),
            -int(item.get("id") or 0),
        )

    ranked = sorted(filtered, key=score, reverse=True)
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for item in ranked:
        content = _normalize_line(str(item.get("content") or ""))
        dedupe_key = (str(item.get("normalized_key") or ""), content)
        if not content or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        unique.append(item)
        if len(unique) >= limit:
            break
    return unique


def _coerce_int_set(values: Iterable[Any] | None) -> set[int]:
    result: set[int] = set()
    for value in values or []:
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            continue
    return result


def _graph_query_match_count(values: Iterable[Any], tokens: list[str]) -> int:
    if not tokens:
        return 0
    haystack = _normalize_line(" ".join(str(value or "") for value in values)).lower()
    return sum(1 for token in tokens if token and token in haystack)


def _graph_reason(
    *,
    match_count: int,
    source_type: str = "",
    pinned: bool = False,
    priority: int = 0,
    confidence: float = 0.0,
    importance: int = 0,
    excluded: bool = False,
) -> str:
    reasons: list[str] = []
    if match_count:
        reasons.append("query_match")
    if source_type in {"manual", "explicit_user"}:
        reasons.append(source_type)
    if pinned:
        reasons.append("pinned")
    if priority > 0:
        reasons.append("priority")
    if confidence >= 0.9:
        reasons.append("high_confidence")
    if importance > 0:
        reasons.append("important")
    if excluded:
        reasons.append("deweighted_duplicate")
    return ",".join(reasons or ["recent"])


def _finalize_graph_episode(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    payload["event_ids"] = _safe_json_loads(payload.get("event_ids_json"), [])
    payload["memory_item_ids"] = _safe_json_loads(payload.get("memory_item_ids_json"), [])
    payload["importance"] = int(payload.get("importance") or 0)
    return payload


def _group_graph_scope(
    *,
    tenant_id: str,
    channel: str | None,
    source_key: str | None,
    session_id: str | None,
) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "channel": channel,
        "source_key": source_key,
        "session_id": session_id,
    }


def _group_graph_acceptance_status(row: dict[str, Any]) -> str:
    acceptance_status = str(row.get("acceptance_status") or "").strip().lower()
    if acceptance_status:
        return acceptance_status
    return "accepted" if str(row.get("status") or "active") == "active" else "needs_review"


def _group_graph_timestamp(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value:
            return value
    return None


def _group_graph_entity_aliases(row: dict[str, Any]) -> list[str]:
    raw_aliases = row.get("aliases")
    if raw_aliases is None:
        raw_aliases = _safe_json_loads(row.get("aliases_json"), [])
    if not isinstance(raw_aliases, list):
        return []
    aliases: list[str] = []
    for alias in raw_aliases:
        alias_text = _normalize_line(_sanitize_db_text(alias))[:80]
        if alias_text and alias_text not in aliases:
            aliases.append(alias_text)
    return aliases


def _group_graph_label_is_technical(value: Any) -> bool:
    label = _normalize_line(str(value or ""))
    if not label:
        return True
    return bool(
        re.match(r"(?i)^(wxid_|gh_|openid_|unionid_|user[_-]?|userid|uid[_:-]?)", label)
        or re.match(r"(?i)^[a-z0-9_@.\-]{24,}$", label)
        or re.match(r"(?i)^entity:\d+$", label)
    )


def _group_graph_entity_display_label(row: dict[str, Any]) -> str:
    candidates = [
        *(_group_graph_entity_aliases(row) or []),
        row.get("name"),
        row.get("normalized_name"),
    ]
    for candidate in candidates:
        label = _normalize_line(_sanitize_db_text(candidate))
        if label and not _group_graph_label_is_technical(label):
            return label[:80]
    for candidate in candidates:
        label = _normalize_line(_sanitize_db_text(candidate))
        if label:
            return label[:80]
    return _group_graph_node_id(row)


def _looks_like_wechat_username(value: Any) -> bool:
    username = _normalize_line(_sanitize_db_text(value))
    if not username or username.endswith("@chatroom"):
        return False
    if re.match(r"(?i)^(wxid_|gh_|openid_|unionid_)", username):
        return True
    if re.match(r"(?i)^[a-z0-9_@.\-]{24,}$", username):
        return True
    return bool(re.match(r"(?i)^[a-z][a-z0-9_.\-]{5,31}$", username))


def _wechat_contact_display_label(metadata: dict[str, Any]) -> str:
    for key in ("remark", "nick_name", "alias"):
        label = _normalize_line(_sanitize_db_text(metadata.get(key)))[:80]
        if label:
            return label
    return ""


def _merge_group_graph_aliases(existing: list[str], candidates: Iterable[Any]) -> list[str]:
    aliases = list(existing or [])
    for candidate in candidates:
        alias = _normalize_line(_sanitize_db_text(candidate))[:80]
        if alias and alias not in aliases:
            aliases.append(alias)
    return aliases


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        timestamp = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        timestamp = None
        try:
            timestamp = datetime.fromisoformat(raw)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    timestamp = datetime.strptime(raw[: len(fmt)], fmt)
                    break
                except ValueError:
                    continue
        if timestamp is None:
            return None
    if timestamp.tzinfo is not None:
        timestamp = timestamp.astimezone(UTC).replace(tzinfo=None)
    return timestamp


def _member_memory_etag(item: dict[str, Any]) -> str:
    updated_at = _coerce_datetime(item.get("updated_at"))
    material = f"{int(item.get('id') or 0)}:{updated_at.isoformat() if updated_at else ''}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]
    return f'"memory-{digest}"'


def _encode_member_memory_cursor(updated_at: Any, item_id: int) -> str:
    timestamp = _coerce_datetime(updated_at)
    if timestamp is None:
        raise ValueError("member memory cursor requires updated_at")
    raw = json.dumps(
        {"updated_at": timestamp.isoformat(), "id": int(item_id)},
        separators=(",", ":"),
    ).encode("utf-8")
    return urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_member_memory_cursor(cursor: str | None) -> tuple[datetime, int] | None:
    normalized = str(cursor or "").strip()
    if not normalized:
        return None
    try:
        padded = normalized + "=" * (-len(normalized) % 4)
        payload = json.loads(urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        timestamp = _coerce_datetime(payload["updated_at"])
        item_id = int(payload["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_cursor") from exc
    if timestamp is None or item_id <= 0:
        raise ValueError("invalid_cursor")
    return timestamp, item_id


_CORRECTION_PROPERTY_ALIASES: dict[str, tuple[str, ...]] = {
    "residence": ("住", "居住", "城市", "地址", "家在", "residence", "city", "address"),
    "preference": ("喜欢", "偏好", "爱吃", "不吃", "讨厌", "preference", "like"),
    "name": ("名字", "姓名", "我叫", "name"),
    "work": ("工作", "公司", "职业", "职位", "work", "company", "job"),
    "contact": ("电话", "手机号", "邮箱", "phone", "email"),
    "family": ("孩子", "伴侣", "妻子", "丈夫", "老婆", "family"),
}


def _correction_match_terms(text_value: str) -> tuple[set[str], set[str]]:
    text_value = _normalize_line(text_value).lower()
    for phrase in ("你记错了", "这个记错了", "那不是我", "记错了"):
        text_value = text_value.replace(phrase, " ")
    properties = {
        property_name
        for property_name, aliases in _CORRECTION_PROPERTY_ALIASES.items()
        if any(alias in text_value for alias in aliases)
    }
    properties.update(
        value.strip().lower()
        for value in re.findall(
            r"我的([\u4e00-\u9fffa-z0-9_]{1,12})(?:不是|是|叫|为)",
            text_value,
        )
        if value.strip()
    )
    values = {
        value.strip(" 了吧呀啊呢").lower()
        for value in re.findall(
            r"(?:不住在|住在|不是|不喜欢|喜欢|我叫|是)([^，。！？；;]{1,24})",
            text_value,
        )
        if value.strip(" 了吧呀啊呢")
    }
    return properties, values


def _correction_candidate_score(
    row: dict[str, Any],
    *,
    properties: set[str],
    values: set[str],
) -> int:
    haystack = " ".join(
        str(row.get(key) or "").lower() for key in ("memory_type", "normalized_key", "content")
    )
    property_matches = 0
    for property_name in properties:
        aliases = _CORRECTION_PROPERTY_ALIASES.get(property_name, (property_name,))
        if any(alias in haystack for alias in aliases):
            property_matches += 1
    value_matches = sum(value in haystack for value in values if len(value) >= 1)
    if properties and property_matches == 0:
        return 0
    if not properties and values and value_matches == 0:
        return 0
    return (property_matches * 4) + (value_matches * 3)


def _group_graph_node_id(row: dict[str, Any]) -> str:
    entity_id = row.get("id")
    if entity_id is not None:
        return f"entity:{entity_id}"
    entity_type = row.get("entity_type") or "thing"
    normalized_name = row.get("normalized_name") or row.get("name") or ""
    return f"entity:{entity_type}:{normalized_name}"


def _group_graph_edge_id(row: dict[str, Any]) -> str:
    fact_id = row.get("id")
    if fact_id is not None:
        return f"fact:{fact_id}"
    object_entity_id = row.get("object_entity_id")
    object_ref = (
        f"entity:{object_entity_id}"
        if object_entity_id is not None
        else f"value:{_normalize_key(str(row.get('object_value') or ''))}"
    )
    return f"fact:{row.get('subject_entity_id') or ''}:{row.get('predicate') or ''}:{object_ref}"


def _group_graph_default_acceptance_allowed(row: dict[str, Any]) -> bool:
    if _group_graph_acceptance_status(row) != "accepted":
        return False
    if str(row.get("status") or "active") != "active":
        return False
    return row.get("deleted_at") is None


def _daily_relationship_run_key(
    *,
    tenant_id: str,
    channel: str,
    source_key: str,
    session_id: str,
    user_id: str,
    target_date: str,
) -> str:
    raw = "|".join([tenant_id, channel, source_key, session_id, user_id, target_date])
    return "group-rel-daily:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _window_relationship_normalized_key(
    *,
    target_date: str,
    session_id: str,
    predicate: str,
    subject: str,
    object_value: str,
) -> str:
    relation_scope = "|".join(
        [
            _normalize_line(session_id).lower(),
            _normalize_line(predicate).lower(),
            _graph_normalized_name(subject),
            _graph_normalized_name(object_value),
        ]
    )
    relation_hash = hashlib.sha256(relation_scope.encode("utf-8")).hexdigest()[:24]
    readable_predicate = _normalize_line(predicate).lower()[:32] or "relation"
    return f"group-window-rel:{readable_predicate}:{relation_hash}"


def _merge_int_lists(*values: Any, max_items: int = 200) -> list[int]:
    merged: list[int] = []
    seen: set[int] = set()
    for value in values:
        for item in sorted(_coerce_int_set(value or [])):
            if item in seen:
                continue
            seen.add(item)
            merged.append(item)
            if len(merged) >= max_items:
                return merged
    return merged


def _memory_item_original_text_for_source(
    source_type: str, original_text: str, content: str
) -> str:
    if source_type in {LLM_GROUP_WINDOW_SOURCE_TYPE, DETERMINISTIC_GROUP_WINDOW_SOURCE_TYPE}:
        return original_text
    return original_text or content


def _extract_group_event_sender_id(value: Any) -> str:
    matched = _GROUP_EVENT_SENDER_PREFIX_RE.match(str(value or ""))
    return str(matched.group(1) or "").strip() if matched else ""


def _split_group_event_text(value: Any) -> tuple[str, str]:
    text_value = str(value or "")
    matched = _GROUP_EVENT_SENDER_PREFIX_RE.match(text_value)
    if matched:
        return str(matched.group(1) or "").strip(), text_value[matched.end() :].strip()
    sender, body = _parse_group_body(text_value)
    return str(sender or "").strip(), str(body or "").strip()


def _parse_daily_relationship_date(value: Any) -> datetime:
    if isinstance(value, datetime):
        return datetime.combine(value.date(), datetime.min.time())
    raw = str(value or "").strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except ValueError as exc:
        raise RuntimeError("date must use YYYY-MM-DD") from exc


def _llm_extraction_job_enqueue_eligible(
    *,
    job_enabled: bool,
    structured_enabled: bool,
    structured_llm_available: bool,
    graph_enabled: bool,
    graph_llm_available: bool,
) -> bool:
    structured_available = structured_enabled and structured_llm_available
    graph_available = graph_enabled and graph_llm_available
    return bool(job_enabled and (structured_available or graph_available))


class HistorySyncAdapter(ABC):
    """Provider-neutral history collector interface used by memory backfill."""

    provider: str

    @abstractmethod
    async def collect_session_history(
        self,
        *,
        tenant_id: str,
        session_id: str,
        user_id: str | None,
        cutoff_ts: int,
        max_messages: int,
        end_ts: int | None = None,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError


class WeChatHistorySyncAdapter(HistorySyncAdapter):
    provider = "wechat"

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    async def collect_session_history(
        self,
        *,
        tenant_id: str,
        session_id: str,
        user_id: str | None,
        cutoff_ts: int,
        max_messages: int,
        end_ts: int | None = None,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {
            "session_id": session_id,
            "user_id": user_id,
            "cutoff_ts": cutoff_ts,
            "max_messages": max_messages,
            "end_ts": end_ts,
        }
        # Keep the pure store adapter usable in domain tests while requiring
        # the explicit tenant on the runtime-wired cross-plugin path.
        if bool(getattr(self.store, "runtime_scope_gates_required", False)):
            kwargs["tenant_id"] = tenant_id
        return await self.store._collect_session_history(
            **kwargs,
        )


def _history_sync_adapter_for_channel(channel: str, store: MemoryStore) -> HistorySyncAdapter:
    normalized = str(channel or "").strip().lower()
    if normalized == WeChatHistorySyncAdapter.provider:
        return WeChatHistorySyncAdapter(store)
    raise RuntimeError(
        f"memory history sync does not support provider/channel: {normalized or 'unknown'}"
    )


def _append_unique_int(target: list[int], value: Any, *, max_items: int = 50) -> None:
    if len(target) >= max_items:
        return
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return
    if normalized not in target:
        target.append(normalized)


def _memory_item_matches_scope(
    item: dict[str, Any],
    *,
    tenant_id: str,
    channel: str,
    source_key: str,
    user_id: str,
    session_id: str = "",
) -> bool:
    if str(item.get("tenant_id") or "") != tenant_id:
        return False
    if str(item.get("channel") or "") != channel:
        return False
    if str(item.get("source_key") or "") != source_key:
        return False
    if str(item.get("user_id") or "") != user_id:
        return False
    if str(item.get("session_id") or "") != session_id:
        return False
    return True


def _memory_item_requires_delete_confirmation(item: dict[str, Any]) -> bool:
    return bool(item.get("pinned")) or str(item.get("source_type") or "") == "manual"


def _build_runtime_profile_from_items(
    identity_profile: dict[str, Any],
    session_profile: dict[str, Any],
    identity_items: list[dict[str, Any]],
    session_items: list[dict[str, Any]],
) -> dict[str, Any]:
    runtime = _build_runtime_profile(identity_profile, session_profile)
    identity_items = sorted(identity_items, key=_item_sort_key)
    session_items = sorted(session_items, key=_item_sort_key)
    long_items, long_term_memory, identity_manual = _render_legacy_identity_from_items(
        identity_items
    )
    session_manual = _render_session_manual_from_items(session_items)

    # Runtime prompt fields are always rebuilt from row-level authorized
    # items. Falling back to profile caches would reintroduce expired/private
    # content after the item filter deliberately removed it.
    runtime["long_term_memory"] = long_term_memory
    runtime["identity_manual_notes"] = identity_manual
    runtime["identity_profile"]["long_term_memory"] = long_term_memory
    runtime["identity_profile"]["manual_notes"] = identity_manual
    runtime["identity_profile"]["long_term_items"] = long_items
    runtime["identity_profile"]["long_term_items_json"] = _to_json(long_items)
    runtime["session_manual_notes"] = session_manual
    runtime["session_profile"]["manual_notes"] = session_manual
    runtime["manual_notes"] = _merge_manual_notes(
        runtime.get("identity_manual_notes") or "",
        runtime.get("session_manual_notes") or "",
    )
    runtime["memory_items"] = {
        "identity": identity_items,
        "session": session_items,
    }
    return runtime


def _message_table_name(session_id: str) -> str:
    return "Msg_" + hashlib.md5(session_id.encode()).hexdigest()


def _is_group_session_id(session_id: str) -> bool:
    return str(session_id or "").strip().endswith("@chatroom")


def _group_history_user_scope(session_id: str, user_id: str | None) -> tuple[str, bool]:
    requested_user_id = str(user_id or "").strip()
    if _is_group_session_id(session_id) and (
        not requested_user_id or requested_user_id == GROUP_HISTORY_USER_ID_SCOPE
    ):
        return GROUP_HISTORY_USER_ID_SCOPE, True
    return requested_user_id, False


def _parse_group_body(content: str) -> tuple[str | None, str]:
    matched = _GROUP_PREFIX_RE.match(content or "")
    if matched:
        return matched.group(1), matched.group(2)
    return None, content or ""


def _decode_message_hex(raw_hex: str, compression_type: Any) -> str:
    value = str(raw_hex or "").strip()
    if not value:
        return ""
    try:
        raw = bytes.fromhex(value)
    except Exception:
        return ""
    if compression_type == 4 and _DCTX:
        try:
            return _DCTX.decompress(raw).decode("utf-8", errors="replace")
        except Exception:
            return ""
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _format_history_timestamp(ts: int | float | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _history_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if value:
        try:
            return datetime.fromtimestamp(float(value))
        except Exception:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(str(value), fmt)
            except Exception:
                continue
    return datetime.now(UTC).replace(tzinfo=None)


def _parse_history_target_date(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except ValueError as exc:
        raise RuntimeError("target_date must use YYYY-MM-DD") from exc


def _empty_identity_profile(
    *,
    tenant_id: str,
    channel: str,
    source_key: str,
    user_id: str,
) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "channel": channel,
        "source_key": source_key,
        "user_id": user_id,
        "long_term_memory": "",
        "manual_notes": "",
        "long_term_items_json": "[]",
        "message_count": 0,
        "imported_message_count": 0,
        "last_session_id": "",
        "last_seen_at": None,
        "updated_at": None,
        "long_term_items": [],
    }


def _empty_session_profile(
    *,
    tenant_id: str,
    channel: str,
    source_key: str,
    session_id: str,
    user_id: str,
) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "channel": channel,
        "source_key": source_key,
        "session_id": session_id,
        "user_id": user_id,
        "short_term_memory": "",
        "manual_notes": "",
        "short_term_items_json": "[]",
        "session_summary": "",
        "open_items_json": "[]",
        "decisions_json": "[]",
        "recent_turns_json": "[]",
        "last_compacted_at": None,
        "summary_version": SESSION_STATE_VERSION,
        "message_count": 0,
        "imported_message_count": 0,
        "last_seen_at": None,
        "updated_at": None,
        "short_term_items": [],
        "open_items": [],
        "decisions": [],
        "recent_turns": [],
    }


def _finalize_identity_profile(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    payload["long_term_items"] = _safe_json_loads(payload.get("long_term_items_json"), [])
    payload["message_count"] = int(payload.get("message_count") or 0)
    payload["imported_message_count"] = int(payload.get("imported_message_count") or 0)
    return payload


def _build_group_relationship_edge_evidence_payload(
    *,
    fact: dict[str, Any],
    backing_item: dict[str, Any] | None,
    evidence_items: list[dict[str, Any]],
    events: list[dict[str, Any]],
    evidence_episodes: list[dict[str, Any]],
    memory_item_ids: Iterable[Any],
    event_ids: Iterable[Any],
) -> dict[str, Any]:
    edge = {
        "id": _group_graph_edge_id(fact),
        "source": _group_graph_node_id({"id": fact.get("subject_entity_id")}),
        "target": (
            _group_graph_node_id({"id": fact.get("object_entity_id")})
            if fact.get("object_entity_id") is not None
            else f"value:fact:{fact.get('id') or _normalize_key(str(fact.get('predicate') or ''))}"
        ),
        "type": str(fact.get("predicate") or ""),
        "label": str(fact.get("predicate") or ""),
        "confidence": _clamp_score(fact.get("confidence")),
        "acceptance_status": _group_graph_acceptance_status(backing_item or fact),
        "status": str(fact.get("status") or ""),
        "first_seen": _group_graph_timestamp(fact, "valid_at", "created_at", "updated_at"),
        "last_seen": _group_graph_timestamp(fact, "updated_at", "valid_at", "created_at"),
    }
    safe_items = [
        {
            "id": item.get("id"),
            "tenant_id": item.get("tenant_id"),
            "channel": item.get("channel"),
            "source_key": item.get("source_key"),
            "user_id": item.get("user_id"),
            "session_id": item.get("session_id"),
            "scope_type": item.get("scope_type"),
            "source_type": item.get("source_type"),
            "memory_type": item.get("memory_type"),
            "normalized_key": item.get("normalized_key"),
            "confidence": item.get("confidence"),
            "status": item.get("status"),
            "sensitivity": item.get("sensitivity"),
            "source_event_id": item.get("source_event_id"),
            "acceptance_status": item.get("acceptance_status"),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
        }
        for item in evidence_items
    ]
    safe_events = [
        {
            "id": event.get("id"),
            "tenant_id": event.get("tenant_id"),
            "channel": event.get("channel"),
            "source_key": event.get("source_key"),
            "user_id": event.get("user_id"),
            "session_id": event.get("session_id"),
            "trace_id": event.get("trace_id"),
            "event_key": event.get("event_key"),
            "created_at": event.get("created_at"),
        }
        for event in events
    ]
    safe_episodes = [
        {
            "id": episode.get("id"),
            "tenant_id": episode.get("tenant_id"),
            "channel": episode.get("channel"),
            "source_key": episode.get("source_key"),
            "user_id": episode.get("user_id"),
            "session_id": episode.get("session_id"),
            "event_ids": episode.get("event_ids") or [],
            "memory_item_ids": episode.get("memory_item_ids") or [],
            "importance": episode.get("importance"),
            "status": episode.get("status"),
            "created_at": episode.get("created_at"),
            "updated_at": episode.get("updated_at"),
        }
        for episode in evidence_episodes
    ]
    return {
        "schema": {"version": GROUP_GRAPH_SCHEMA_VERSION},
        "edge": edge,
        "evidence_ids": {
            "backing_memory_item_id": fact.get("memory_item_id"),
            "memory_item_ids": sorted(_coerce_int_set(memory_item_ids)),
            "event_ids": sorted(_coerce_int_set(event_ids)),
            "episode_ids": sorted(
                _coerce_int_set(episode.get("id") for episode in evidence_episodes)
            ),
        },
        "evidence_counts": {
            "memory_items": len(safe_items),
            "events": len(safe_events),
            "episodes": len(safe_episodes),
        },
        "memory_items": safe_items,
        "events": safe_events,
        "episodes": safe_episodes,
    }


def _finalize_session_profile(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    payload["short_term_items"] = _safe_json_loads(payload.get("short_term_items_json"), [])
    payload["open_items"] = _as_session_list(payload.get("open_items_json"))
    payload["decisions"] = _as_session_list(payload.get("decisions_json"))
    recent_turns = _as_session_list(payload.get("recent_turns_json"))
    if not recent_turns:
        recent_turns = _as_session_list(payload.get("short_term_items_json"))
    payload["recent_turns"] = recent_turns[-SESSION_RECENT_TURN_LIMIT:]
    payload["session_summary"] = str(payload.get("session_summary") or "")
    payload["summary_version"] = int(payload.get("summary_version") or SESSION_STATE_VERSION)
    payload["message_count"] = int(payload.get("message_count") or 0)
    payload["imported_message_count"] = int(payload.get("imported_message_count") or 0)
    return payload


def _build_runtime_profile(
    identity_profile: dict[str, Any],
    session_profile: dict[str, Any],
) -> dict[str, Any]:
    identity = _finalize_identity_profile(identity_profile)
    session = _finalize_session_profile(session_profile)
    return {
        "tenant_id": identity["tenant_id"],
        "channel": identity["channel"],
        "source_key": identity["source_key"],
        "user_id": identity["user_id"],
        "session_id": session["session_id"],
        "short_term_memory": session.get("short_term_memory") or "",
        "session_summary": session.get("session_summary") or "",
        "open_items": session.get("open_items") or [],
        "decisions": session.get("decisions") or [],
        "recent_turns": session.get("recent_turns") or [],
        "last_compacted_at": session.get("last_compacted_at"),
        "summary_version": session.get("summary_version") or SESSION_STATE_VERSION,
        "long_term_memory": identity.get("long_term_memory") or "",
        "manual_notes": _merge_manual_notes(
            str(identity.get("manual_notes") or ""),
            str(session.get("manual_notes") or ""),
        ),
        "identity_manual_notes": identity.get("manual_notes") or "",
        "session_manual_notes": session.get("manual_notes") or "",
        "message_count": int(identity.get("message_count") or 0),
        "identity_message_count": int(identity.get("message_count") or 0),
        "session_message_count": int(session.get("message_count") or 0),
        "imported_message_count": int(identity.get("imported_message_count") or 0),
        "session_imported_message_count": int(session.get("imported_message_count") or 0),
        "last_session_id": identity.get("last_session_id") or session.get("session_id") or "",
        "identity_profile": identity,
        "session_profile": session,
    }


# These mixins depend on the pure helpers above, so they are imported only
# after the helper ports are initialized.  The facade remains MemoryStore.
from plugins.memory.store_backfill import MemoryBackfillStoreMixin  # noqa: E402
from plugins.memory.store_group_graph import MemoryGroupGraphStoreMixin  # noqa: E402
from plugins.memory.store_jobs import MemoryExtractionJobStoreMixin  # noqa: E402
from plugins.memory.store_retrieval import MemoryRetrievalStoreMixin  # noqa: E402


class MemoryStore(
    MemoryAdminMutationMixin,
    MemoryRetrievalStoreMixin,
    MemoryExtractionJobStoreMixin,
    MemoryGroupGraphStoreMixin,
    MemoryBackfillStoreMixin,
):
    def __init__(
        self,
        settings: Any,
        *,
        llm_service: Any | None = None,
        vector_store: Any | None = None,
    ) -> None:
        self.settings = settings
        self.vector_index = MemoryItemVectorIndex(
            settings,
            vector_store=vector_store,
            llm_service=llm_service,
        )
        self.structured_extractor = MemoryStructuredExtractor(
            settings=settings,
            llm_service=llm_service,
            deterministic_extractor=extract_structured_memory_actions,
            semantic_key_builder=_semantic_key,
            sensitivity_detector=_detect_sensitivity,
        )
        self.graph_extractor = MemoryGraphLLMExtractor(
            settings=settings,
            llm_service=llm_service,
        )

    @asynccontextmanager
    async def _mutation_transaction(self) -> AsyncIterator[AsyncConnection]:
        """Bind all nested ``_exec`` calls to one database transaction."""

        async with get_engine().begin() as conn:
            token = _ACTIVE_MUTATION_CONNECTION.set(conn)
            try:
                yield conn
            finally:
                _ACTIVE_MUTATION_CONNECTION.reset(token)

    async def _lock_member_memory_mutation(
        self,
        *,
        tenant_id: str,
        user_id: str,
    ) -> None:
        """Serialize saves and erasure for one tenant/member in PostgreSQL."""

        active_connection = _ACTIVE_MUTATION_CONNECTION.get()
        if active_connection is None:
            raise RuntimeError("member memory mutation requires an active transaction")
        if str(active_connection.dialect.name or "").lower() != "postgresql":
            return
        await _exec(
            "SELECT pg_advisory_xact_lock("
            "hashtextextended(:member_scope, 0)) AS locked",
            {"member_scope": f"memory-member-v1:{tenant_id}:{user_id}"},
        )

    async def _member_memory_write_blocked(
        self,
        *,
        tenant_id: str,
        user_id: str,
    ) -> bool:
        rows = await _exec(
            "SELECT memory_opt_out, deletion_state "
            "FROM social_tenant_member_control "
            "WHERE tenant_id = :tid AND user_id = :uid LIMIT 1",
            {"tid": tenant_id, "uid": user_id},
        )
        if not rows:
            return False
        return bool(rows[0].get("memory_opt_out")) or str(
            rows[0].get("deletion_state") or "none"
        ) in {"requested", "failed"}

    def _can_enqueue_llm_extraction_jobs(self) -> bool:
        return _llm_extraction_job_enqueue_eligible(
            job_enabled=_settings_bool(self.settings, "memory_llm_extraction_job_enabled", True),
            structured_enabled=bool(self.structured_extractor.config.enabled),
            structured_llm_available=self.structured_extractor.llm_service is not None,
            graph_enabled=bool(self.graph_extractor.config.enabled),
            graph_llm_available=self.graph_extractor.llm_service is not None,
        )

    async def run_governance_cleanup(
        self,
        *,
        dry_run: bool = True,
        needs_review_days: int | None = None,
        rejected_days: int | None = None,
        auto_expire_days: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Expire stale candidates and auto memories; purge old rejections.

        Manual and pinned memories are never selected. The operation is
        bounded and idempotent so it can run on a schedule and from the admin
        maintenance endpoint.
        """
        review_days = max(
            1,
            int(
                needs_review_days
                or getattr(self.settings, "memory_needs_review_retention_days", 30)
            ),
        )
        reject_days = max(
            1,
            int(rejected_days or getattr(self.settings, "memory_rejected_retention_days", 7)),
        )
        expire_days = max(
            1,
            int(auto_expire_days or getattr(self.settings, "memory_auto_expire_days", 180)),
        )
        batch = max(
            1,
            min(
                int(limit or getattr(self.settings, "memory_governance_batch_size", 500)),
                5000,
            ),
        )

        async def candidate_ids(condition: str, days: int) -> list[int]:
            rows = await _exec(
                "SELECT id FROM plugin_memory_item WHERE deleted_at IS NULL "
                "AND pinned = FALSE AND source_type NOT IN ('manual', 'explicit_user') "
                f"AND ({condition}) "
                "AND updated_at < NOW() - (:days * INTERVAL '1 day') "
                "ORDER BY updated_at ASC, id ASC LIMIT :limit",
                {"days": days, "limit": batch},
            )
            return [int(row["id"]) for row in rows]

        review_ids = await candidate_ids(
            "COALESCE(NULLIF(value_json, '')::jsonb #>> '{acceptance,status}', '') "
            "IN ('candidate', 'needs_review')",
            review_days,
        )
        rejected_ids = await candidate_ids(
            "COALESCE(NULLIF(value_json, '')::jsonb #>> '{acceptance,status}', '') = 'rejected'",
            reject_days,
        )
        stale_ids = await candidate_ids(
            "status = 'active' AND source_type IN ('auto', 'backfill') "
            "AND COALESCE(NULLIF(value_json, '')::jsonb #>> '{acceptance,status}', 'accepted') "
            "IN ('accepted', '')",
            expire_days,
        )
        result = {
            "dry_run": dry_run,
            "needs_review_expired": len(review_ids),
            "rejected_purged": len(rejected_ids),
            "stale_auto_expired": len(stale_ids),
            "selected": len(set(review_ids + rejected_ids + stale_ids)),
            "ids": {
                "needs_review": review_ids,
                "rejected": rejected_ids,
                "stale_auto": stale_ids,
            },
        }
        if dry_run or not result["selected"]:
            MEMORY_GOVERNANCE_EVENTS.labels(action="cleanup", result="dry_run").inc()
            return result

        expire_ids = sorted(set(review_ids + stale_ids))
        if expire_ids:
            await _exec(
                "UPDATE plugin_memory_item SET status = 'expired', "
                "value_json = jsonb_set(COALESCE(NULLIF(value_json, '')::jsonb, '{}'::jsonb), "
                "'{acceptance,status}', '\"expired\"'::jsonb, TRUE)::text, updated_at = NOW() "
                "WHERE id = ANY(:ids)",
                {"ids": expire_ids},
            )
            await _exec(
                "UPDATE plugin_memory_fact SET status = 'invalidated', invalid_at = NOW(), "
                "updated_at = NOW() WHERE memory_item_id = ANY(:ids) AND status = 'active'",
                {"ids": expire_ids},
            )
        if rejected_ids:
            await _exec(
                "UPDATE plugin_memory_item SET status = 'deleted', deleted_at = NOW(), "
                "updated_at = NOW() WHERE id = ANY(:ids)",
                {"ids": rejected_ids},
            )
            await _exec(
                "UPDATE plugin_memory_fact SET status = 'invalidated', invalid_at = NOW(), "
                "updated_at = NOW() WHERE memory_item_id = ANY(:ids) AND status = 'active'",
                {"ids": rejected_ids},
            )
        for item_id in sorted(set(expire_ids + rejected_ids)):
            try:
                await self.vector_index.delete_item(item_id)
            except Exception as exc:
                logger.warning(
                    "memory.governance_vector_cleanup_failed",
                    item_id=item_id,
                    error=str(exc),
                )
        MEMORY_GOVERNANCE_EVENTS.labels(action="cleanup", result="success").inc()
        return result


    async def ensure_tables(self) -> None:
        await verify_runtime_schema(get_engine(), component="memory store")
        logger.info("memory.schema_verified")

    async def get_group_member_privacy_policy(
        self,
        *,
        tenant_id: str,
        session_id: str,
        user_id: str,
    ) -> MemberPrivacyValues:
        """Load the fail-closed member policy used by group memory hooks."""

        rows = await _exec(
            "SELECT member.policy_json, control.memory_opt_out, "
            "control.participation_opt_out, control.no_group_mentions "
            "FROM social_member_policy AS member "
            "LEFT JOIN social_tenant_member_control AS control "
            "ON control.tenant_id = member.tenant_id AND control.user_id = member.user_id "
            "WHERE member.tenant_id = :tid AND member.session_id = :sid "
            "AND member.user_id = :uid "
            "LIMIT 1",
            {
                "tid": str(tenant_id or "").strip(),
                "sid": str(session_id or "").strip(),
                "uid": str(user_id or "").strip(),
            },
        )
        if not rows:
            return MemberPrivacyValues()
        raw = rows[0].get("policy_json")
        if isinstance(raw, str):
            raw = _safe_json_loads(raw, {})
        try:
            policy = MemberPrivacyValues.model_validate(raw if isinstance(raw, dict) else {})
            values = policy.model_dump()
            if bool(rows[0].get("memory_opt_out")):
                values.update(
                    memory_enabled=False,
                    allow_group_recall=False,
                    allow_private_recall=False,
                    sensitive_memory_enabled=False,
                )
            if bool(rows[0].get("participation_opt_out")):
                values.update(
                    proactive_participation_enabled=False,
                    soft_reply_opt_out=True,
                )
            if bool(rows[0].get("no_group_mentions")):
                values["no_group_mentions"] = True
            return MemberPrivacyValues.model_validate(values)
        except Exception:
            logger.warning(
                "memory.member_privacy_policy_invalid",
                tenant_id=tenant_id,
                session_id=session_id,
                user_id=user_id,
            )
            return MemberPrivacyValues()

    async def list_group_member_memory_items(
        self,
        *,
        tenant_id: str,
        session_id: str,
        user_id: str,
        policy: MemberPrivacyValues,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Return only memory content authorized for this exact group/member.

        The SQL scope is deliberately non-configurable.  Audience, retention,
        sensitivity and current member policy are then enforced fail-closed in
        Python so JSON semantics remain identical across supported databases.
        """

        tenant = str(tenant_id or "").strip()
        session = str(session_id or "").strip()
        member = str(user_id or "").strip()
        if not tenant or not session or not member:
            raise ValueError("tenant_id, session_id and user_id are required")
        if not self._group_member_policy_allows_content(policy, session):
            return {"items": [], "next_cursor": None}
        cursor_value = _decode_member_memory_cursor(cursor)
        conditions = [
            "tenant_id = :tid",
            "channel = 'wechat'",
            "user_id = :uid",
            "deleted_at IS NULL",
            "status NOT IN ('deleted', 'invalidated')",
        ]
        params: dict[str, Any] = {"tid": tenant, "uid": member, "lim": 501}
        if cursor_value is not None:
            before_time, before_id = cursor_value
            conditions.append(
                "(updated_at < :before_time OR (updated_at = :before_time AND id < :before_id))"
            )
            params.update(before_time=before_time, before_id=before_id)
        rows = await _exec(
            "SELECT id, tenant_id, channel, source_key, user_id, session_id, "
            "scope_type, memory_type, content, status, pinned, sensitivity, "
            "audience_scope, allowed_session_ids, sensitivity_category, expires_at, "
            "created_at, updated_at FROM plugin_memory_item "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY updated_at DESC, id DESC LIMIT :lim",
            params,
        )
        authorized = [
            self._minimal_group_member_memory_item(row)
            for row in rows
            if self._group_member_memory_row_allowed(
                row,
                session_id=session,
                policy=policy,
            )
        ]
        page_limit = max(1, min(int(limit or 50), 200))
        has_more = len(authorized) > page_limit
        items = authorized[:page_limit]
        return {
            "items": items,
            "next_cursor": (
                _encode_member_memory_cursor(items[-1]["updated_at"], int(items[-1]["id"]))
                if has_more and items
                else None
            ),
        }

    async def get_group_member_memory_item(
        self,
        item_id: int,
        *,
        tenant_id: str,
        session_id: str,
        user_id: str,
        policy: MemberPrivacyValues,
    ) -> dict[str, Any] | None:
        if not self._group_member_policy_allows_content(policy, session_id):
            return None
        rows = await _exec(
            "SELECT id, tenant_id, channel, source_key, user_id, session_id, "
            "scope_type, memory_type, content, status, pinned, sensitivity, "
            "audience_scope, allowed_session_ids, sensitivity_category, expires_at, "
            "created_at, updated_at FROM plugin_memory_item "
            "WHERE id = :id AND tenant_id = :tid AND channel = 'wechat' "
            "AND user_id = :uid AND deleted_at IS NULL "
            "AND status NOT IN ('deleted', 'invalidated') LIMIT 1",
            {
                "id": int(item_id),
                "tid": str(tenant_id or "").strip(),
                "uid": str(user_id or "").strip(),
            },
        )
        if not rows or not self._group_member_memory_row_allowed(
            rows[0],
            session_id=str(session_id or "").strip(),
            policy=policy,
        ):
            return None
        return self._minimal_group_member_memory_item(rows[0])

    async def correct_group_member_memory_item(
        self,
        item_id: int,
        *,
        tenant_id: str,
        session_id: str,
        user_id: str,
        policy: MemberPrivacyValues,
        expected_etag: str,
        content: str,
    ) -> dict[str, Any] | None:
        if not policy.correction_enabled:
            return None
        current = await self.get_group_member_memory_item(
            item_id,
            tenant_id=tenant_id,
            session_id=session_id,
            user_id=user_id,
            policy=policy,
        )
        if current is None:
            return None
        if _member_memory_etag(current) != str(expected_etag or "").strip():
            raise MemoryItemConflictError("member memory etag conflict")
        normalized = _normalize_line(str(content or ""))[:500]
        if not normalized:
            raise ValueError("content is required")
        rows = await _exec(
            "UPDATE plugin_memory_item SET content = :content, normalized_key = :normalized_key, "
            "updated_at = NOW() WHERE id = :id AND tenant_id = :tid "
            "AND channel = 'wechat' AND user_id = :uid AND updated_at = :expected_updated_at "
            "AND deleted_at IS NULL AND status NOT IN ('deleted', 'invalidated') RETURNING id",
            {
                "id": int(item_id),
                "tid": str(tenant_id or "").strip(),
                "uid": str(user_id or "").strip(),
                "content": normalized,
                "normalized_key": _normalize_key(normalized),
                "expected_updated_at": current["updated_at"],
            },
        )
        if not rows:
            raise MemoryItemConflictError("member memory etag conflict")
        updated = await self.get_group_member_memory_item(
            item_id,
            tenant_id=tenant_id,
            session_id=session_id,
            user_id=user_id,
            policy=policy,
        )
        if updated is not None:
            await self._refresh_legacy_cache_for_item_scope(
                {
                    **updated,
                    "tenant_id": tenant_id,
                    "channel": "wechat",
                    "user_id": user_id,
                    "source_key": current.get("source_key", "*"),
                    "session_id": current.get("session_id", ""),
                }
            )
            await self._sync_memory_vector_for_item_safe(updated)
        return updated

    async def delete_group_member_memory_item(
        self,
        item_id: int,
        *,
        tenant_id: str,
        session_id: str,
        user_id: str,
        policy: MemberPrivacyValues,
        expected_etag: str,
        allow_pinned: bool = False,
    ) -> bool:
        if not policy.deletion_enabled:
            return False
        current = await self.get_group_member_memory_item(
            item_id,
            tenant_id=tenant_id,
            session_id=session_id,
            user_id=user_id,
            policy=policy,
        )
        if current is None:
            return False
        if _member_memory_etag(current) != str(expected_etag or "").strip():
            raise MemoryItemConflictError("member memory etag conflict")
        if current.get("pinned") and not allow_pinned:
            raise MemoryItemProtectedError([int(item_id)])
        rows = await _exec(
            "UPDATE plugin_memory_item SET status = 'deleted', deleted_at = NOW(), updated_at = NOW() "
            "WHERE id = :id AND tenant_id = :tid AND channel = 'wechat' AND user_id = :uid "
            "AND updated_at = :expected_updated_at AND deleted_at IS NULL "
            "RETURNING id",
            {
                "id": int(item_id),
                "tid": str(tenant_id or "").strip(),
                "uid": str(user_id or "").strip(),
                "expected_updated_at": current["updated_at"],
            },
        )
        if not rows:
            raise MemoryItemConflictError("member memory etag conflict")
        await self._delete_memory_vector_for_item_safe(int(item_id))
        return True

    @staticmethod
    def _group_member_policy_allows_content(
        policy: MemberPrivacyValues,
        session_id: str,
    ) -> bool:
        if not policy.memory_enabled or not policy.allow_group_recall:
            return False
        if policy.audience_scope == "private":
            return False
        if policy.audience_scope == "explicit":
            return session_id in set(policy.allowed_session_ids)
        return policy.audience_scope == "session"

    @staticmethod
    def _group_member_memory_row_allowed(
        row: dict[str, Any],
        *,
        session_id: str,
        policy: MemberPrivacyValues,
    ) -> bool:
        audience = str(row.get("audience_scope") or "private")
        row_session = str(row.get("session_id") or "")
        allowed_sessions = _safe_json_loads(row.get("allowed_session_ids"), [])
        if audience == "session":
            audience_allowed = row_session == session_id
        elif audience == "explicit":
            audience_allowed = session_id in {
                str(item) for item in allowed_sessions if isinstance(item, str)
            }
        else:
            audience_allowed = False
        if not audience_allowed:
            return False
        sensitivity = str(row.get("sensitivity_category") or row.get("sensitivity") or "normal")
        if sensitivity != "normal" and not policy.sensitive_memory_enabled:
            return False
        now = datetime.now(UTC).replace(tzinfo=None)
        expires_at = _coerce_datetime(row.get("expires_at"))
        created_at = _coerce_datetime(row.get("created_at"))
        retention_expiry = (
            created_at + timedelta(days=policy.retention_days) if created_at is not None else None
        )
        effective_expiry = min(
            [value for value in (expires_at, retention_expiry) if value is not None],
            default=None,
        )
        return effective_expiry is None or effective_expiry > now

    @staticmethod
    def _minimal_group_member_memory_item(row: dict[str, Any]) -> dict[str, Any]:
        item = {
            "id": int(row["id"]),
            "content": str(row.get("content") or "")[:500],
            "memory_type": str(row.get("memory_type") or "note")[:64],
            "scope_type": str(row.get("scope_type") or "identity"),
            "audience_scope": str(row.get("audience_scope") or "private"),
            "status": str(row.get("status") or "active")[:32],
            "sensitivity_category": str(
                row.get("sensitivity_category") or row.get("sensitivity") or "normal"
            )[:32],
            "pinned": bool(row.get("pinned")),
            "expires_at": _coerce_datetime(row.get("expires_at")),
            "updated_at": _coerce_datetime(row.get("updated_at")) or datetime.now(UTC),
            # Needed only for cache refresh after a scoped mutation; the public
            # contract never exposes these locator fields.
            "source_key": str(row.get("source_key") or "*"),
            "session_id": str(row.get("session_id") or ""),
        }
        item["etag"] = _member_memory_etag(item)
        return item

    async def _insert_or_touch_memory_item(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        session_id: str = "",
        scope_type: str = "identity",
        source_type: str = "auto",
        memory_type: str = "note",
        content: str,
        value_json: Any | None = None,
        normalized_key: str | None = None,
        confidence: float = 0.0,
        status: str = "active",
        pinned: bool = False,
        priority: int = 0,
        sensitivity: str = "normal",
        origin_session_kind: str = "unknown",
        audience_scope: str = "private",
        allowed_session_ids: Iterable[str] | None = None,
        sensitivity_category: str | None = None,
        expires_at: datetime | str | None = None,
        source_kind: str = "conversation",
        source_event_id: int | None = None,
        source_trace_id: str = "",
        original_text: str = "",
    ) -> dict[str, Any] | None:
        original_text = _sanitize_db_text(original_text)
        content = _normalize_line(_sanitize_db_text(content))[:500]
        if not content:
            return None
        scope_type = scope_type if scope_type in {"identity", "session"} else "identity"
        session_id = session_id if scope_type == "session" else ""
        source_type = (
            source_type
            if source_type
            in {
                "manual",
                "explicit_user",
                "auto",
                "backfill",
                LLM_GROUP_WINDOW_SOURCE_TYPE,
                DETERMINISTIC_GROUP_WINDOW_SOURCE_TYPE,
                PROFILE_ENRICHMENT_SOURCE_TYPE,
            }
            else "auto"
        )
        status = status if status in MEMORY_ITEM_STATUSES else "active"
        detected_sensitivity = (
            "normal"
            if source_type == DETERMINISTIC_GROUP_WINDOW_SOURCE_TYPE
            else _detect_sensitivity(content)
        )
        if detected_sensitivity != "normal":
            sensitivity = detected_sensitivity
        else:
            sensitivity = sensitivity if sensitivity in {"normal", "pii", "sensitive"} else "normal"
        requested_category = str(sensitivity_category or sensitivity).strip().lower()
        sensitivity_category = (
            sensitivity
            if sensitivity != "normal"
            else (
                requested_category
                if requested_category in MEMORY_SENSITIVITY_CATEGORIES
                else "normal"
            )
        )
        if sensitivity_category != "normal":
            sensitivity = sensitivity_category
        audience_contract = _normalize_memory_audience_contract(
            origin_session_kind=origin_session_kind,
            audience_scope=audience_scope,
            allowed_session_ids=allowed_session_ids,
            session_id=session_id,
            expires_at=expires_at,
        )
        source_kind = str(source_kind or "conversation").strip().lower()
        if source_kind not in {"conversation", "manual", "backfill", "graph", "profile"}:
            source_kind = "conversation"
        if source_type == "manual":
            pinned = True
            confidence = 1.0
            if status in {"deleted", "invalidated"}:
                status = "active"
        elif source_type == "explicit_user":
            confidence = max(float(confidence or 0.0), 0.9)
            if status in {"deleted", "invalidated"}:
                status = "active"
        elif source_type != DETERMINISTIC_GROUP_WINDOW_SOURCE_TYPE and (
            sensitivity != "normal" or float(confidence or 0.0) < PROMPT_AUTO_CONFIDENCE_MIN
        ):
            status = "pending"
        if sensitivity != "normal":
            status = "pending"
        normalized_key = normalized_key or _normalize_key(content)
        value_payload = value_json if value_json is not None else {}

        existing = await _exec(
            "SELECT id, audience_scope, origin_session_kind, allowed_session_ids, "
            "sensitivity, sensitivity_category, expires_at FROM plugin_memory_item "
            "WHERE tenant_id = :tid AND channel = :channel AND source_key = :source_key "
            "AND user_id = :uid AND scope_type = :scope_type AND session_id = :sid "
            "AND source_type = :source_type AND normalized_key = :normalized_key "
            "AND deleted_at IS NULL "
            "ORDER BY pinned DESC, id ASC LIMIT 256",
            {
                "tid": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "uid": user_id,
                "scope_type": scope_type,
                "sid": session_id,
                "source_type": source_type,
                "normalized_key": normalized_key,
            },
        )
        existing = [row for row in existing if row.get("id") is not None]
        matching_existing = next(
            (
                row
                for row in existing
                if _memory_item_matches_audience_contract(
                    row,
                    audience_contract,
                    session_id=session_id,
                )
            ),
            None,
        )
        if matching_existing is not None:
            current = matching_existing
            current_sensitivity = (
                str(current.get("sensitivity_category") or current.get("sensitivity") or "normal")
                .strip()
                .lower()
            )
            if current_sensitivity not in MEMORY_SENSITIVITY_CATEGORIES:
                current_sensitivity = "sensitive"
            sensitivity_order = {"normal": 0, "pii": 1, "sensitive": 2}
            if sensitivity_order.get(current_sensitivity, 3) > sensitivity_order.get(
                sensitivity_category, 3
            ):
                sensitivity_category = current_sensitivity
                sensitivity = current_sensitivity
            current_expiry = _coerce_datetime(current.get("expires_at"))
            requested_expiry = audience_contract["expires_at"]
            effective_expiry = min(
                [value for value in (current_expiry, requested_expiry) if value is not None],
                default=None,
            )
            await _exec(
                "UPDATE plugin_memory_item SET "
                "content = :content, value_json = :value_json, memory_type = :memory_type, "
                "confidence = GREATEST(confidence, :confidence), status = :status, "
                "pinned = pinned OR :pinned, priority = GREATEST(priority, :priority), "
                "sensitivity = :sensitivity, sensitivity_category = :sensitivity_category, "
                "expires_at = :expires_at, "
                "source_event_id = COALESCE(source_event_id, :source_event_id), "
                "source_trace_id = COALESCE(NULLIF(source_trace_id, ''), :source_trace_id), "
                "original_text = COALESCE(NULLIF(original_text, ''), :original_text), "
                "occurrence_count = occurrence_count + 1, last_seen_at = NOW(), updated_at = NOW() "
                "WHERE id = :id",
                {
                    "id": current["id"],
                    "content": content,
                    "value_json": _to_json(value_payload),
                    "memory_type": memory_type,
                    "confidence": float(confidence or 0.0),
                    "status": status,
                    "pinned": bool(pinned),
                    "priority": int(priority or 0),
                    "sensitivity": sensitivity,
                    "sensitivity_category": sensitivity_category,
                    "expires_at": effective_expiry,
                    "source_event_id": source_event_id,
                    "source_trace_id": source_trace_id or "",
                    "original_text": _memory_item_original_text_for_source(
                        source_type,
                        original_text,
                        content,
                    ),
                },
            )
            return await self.get_memory_item(int(current["id"]))

        if existing:
            logger.info(
                "memory.audience_dedupe_sibling",
                sibling_count=len(existing),
                requested_origin=audience_contract["origin_session_kind"],
                requested_audience=audience_contract["audience_scope"],
            )

        rows = await _exec(
            "INSERT INTO plugin_memory_item "
            "(tenant_id, channel, source_key, user_id, session_id, scope_type, source_type, "
            "memory_type, content, value_json, normalized_key, confidence, status, pinned, "
            "priority, sensitivity, audience_scope, origin_session_kind, allowed_session_ids, "
            "source_kind, sensitivity_category, expires_at, "
            "source_event_id, source_trace_id, original_text, "
            "occurrence_count, first_seen_at, last_seen_at, created_at, updated_at) "
            "VALUES (:tid, :channel, :source_key, :uid, :sid, :scope_type, :source_type, "
            ":memory_type, :content, :value_json, :normalized_key, :confidence, :status, :pinned, "
            ":priority, :sensitivity, :audience_scope, :origin_session_kind, "
            "CAST(:allowed_session_ids AS JSONB), :source_kind, :sensitivity_category, :expires_at, "
            ":source_event_id, :source_trace_id, :original_text, "
            "1, NOW(), NOW(), NOW(), NOW()) ON CONFLICT DO NOTHING "
            "RETURNING id, tenant_id, channel, source_key, user_id, session_id, scope_type, source_type, "
            "memory_type, content, value_json, normalized_key, confidence, status, pinned, priority, "
            "sensitivity, audience_scope, origin_session_kind, allowed_session_ids, source_kind, "
            "sensitivity_category, expires_at, source_event_id, source_trace_id, original_text, occurrence_count, "
            "first_seen_at, last_seen_at, created_at, updated_at, deleted_at",
            {
                "tid": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "uid": user_id,
                "sid": session_id,
                "scope_type": scope_type,
                "source_type": source_type,
                "memory_type": memory_type,
                "content": content,
                "value_json": _to_json(value_payload),
                "normalized_key": normalized_key,
                "confidence": float(confidence or 0.0),
                "status": status,
                "pinned": bool(pinned),
                "priority": int(priority or 0),
                "sensitivity": sensitivity,
                "audience_scope": audience_contract["audience_scope"],
                "origin_session_kind": audience_contract["origin_session_kind"],
                "allowed_session_ids": _to_json(audience_contract["allowed_session_ids"]),
                "source_kind": source_kind,
                "sensitivity_category": sensitivity_category,
                "expires_at": audience_contract["expires_at"],
                "source_event_id": source_event_id,
                "source_trace_id": source_trace_id or "",
                "original_text": _memory_item_original_text_for_source(
                    source_type,
                    original_text,
                    content,
                ),
            },
        )
        rows = [row for row in rows if _looks_like_memory_item_row(row)]
        if rows:
            return self._finalize_memory_item(rows[0])

        # A concurrent insert or the dedupe index's allowed-session hash may
        # have won the race. Re-read and compare the full audience contract;
        # never merge a hash collision into a row with a broader audience.
        conflict_rows = await _exec(
            "SELECT id, audience_scope, origin_session_kind, allowed_session_ids, "
            "sensitivity, sensitivity_category, expires_at FROM plugin_memory_item "
            "WHERE tenant_id = :tid AND channel = :channel AND source_key = :source_key "
            "AND user_id = :uid AND scope_type = :scope_type AND session_id = :sid "
            "AND source_type = :source_type AND normalized_key = :normalized_key "
            "AND deleted_at IS NULL ORDER BY id ASC LIMIT 256",
            {
                "tid": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "uid": user_id,
                "scope_type": scope_type,
                "sid": session_id,
                "source_type": source_type,
                "normalized_key": normalized_key,
            },
        )
        exact_match = next(
            (
                row
                for row in conflict_rows
                if _memory_item_matches_audience_contract(
                    row,
                    audience_contract,
                    session_id=session_id,
                )
            ),
            None,
        )
        logger.warning(
            "memory.audience_dedupe_insert_refused",
            item_id=exact_match.get("id") if exact_match else None,
            exact_contract_match=exact_match is not None,
            conflicting_rows=len(conflict_rows),
        )
        return None

    def _finalize_memory_item(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = dict(row)
        payload["value"] = _safe_json_loads(payload.get("value_json"), {})
        payload["confidence"] = float(payload.get("confidence") or 0.0)
        value = payload["value"] if isinstance(payload["value"], dict) else {}
        acceptance = value.get("acceptance") if isinstance(value.get("acceptance"), dict) else {}
        payload["acceptance_status"] = str(acceptance.get("status") or "")
        payload["acceptance_score"] = (
            _clamp_score(acceptance.get("score")) if acceptance.get("score") is not None else None
        )
        payload["acceptance_reason"] = str(acceptance.get("reason") or "")
        payload["acceptance_signals"] = (
            acceptance.get("signals") if isinstance(acceptance.get("signals"), dict) else {}
        )
        payload["acceptance_history"] = (
            acceptance.get("history") if isinstance(acceptance.get("history"), list) else []
        )
        payload["superseded_by_item_id"] = acceptance.get("superseded_by_item_id")
        payload["supersedes_item_id"] = acceptance.get("supersedes_item_id")
        payload["extraction_confidence"] = (
            _clamp_score(acceptance.get("extraction_confidence"))
            if acceptance.get("extraction_confidence") is not None
            else payload["confidence"]
        )
        payload["priority"] = int(payload.get("priority") or 0)
        payload["occurrence_count"] = int(payload.get("occurrence_count") or 0)
        payload["pinned"] = bool(payload.get("pinned"))
        payload["audience_scope"] = str(payload.get("audience_scope") or "private")
        payload["origin_session_kind"] = str(payload.get("origin_session_kind") or "unknown")
        payload["allowed_session_ids"] = _normalize_allowed_session_ids(
            payload.get("allowed_session_ids")
        )
        payload["sensitivity_category"] = str(
            payload.get("sensitivity_category") or payload.get("sensitivity") or "normal"
        )
        payload["expires_at"] = _coerce_datetime(payload.get("expires_at"))
        return payload

    def _annotate_duplicate_hints(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        groups: dict[tuple[str, str, str, str, str, str, str], list[dict[str, Any]]] = {}
        for item in items:
            normalized_key = str(item.get("normalized_key") or "").strip()
            if not normalized_key:
                continue
            key = (
                str(item.get("tenant_id") or ""),
                str(item.get("channel") or ""),
                str(item.get("source_key") or ""),
                str(item.get("user_id") or ""),
                str(item.get("scope_type") or ""),
                str(item.get("memory_type") or ""),
                normalized_key,
            )
            groups.setdefault(key, []).append(item)

        for group in groups.values():
            visible = [
                item
                for item in group
                if not item.get("deleted_at") and str(item.get("status") or "") != "deleted"
            ]
            if len(visible) < 2:
                continue
            summaries = [
                {
                    "id": item.get("id"),
                    "status": item.get("status"),
                    "acceptance_status": item.get("acceptance_status") or "",
                    "normalized_key": item.get("normalized_key") or "",
                }
                for item in visible
            ]
            for item in visible:
                others = [summary for summary in summaries if summary.get("id") != item.get("id")]
                item["possible_conflicts"] = {
                    "type": "duplicate_normalized_key",
                    "normalized_key": item.get("normalized_key") or "",
                    "count": len(others),
                    "ids": [summary.get("id") for summary in others],
                    "items": others[:10],
                }
                item["duplicate_hint"] = {
                    "count": len(others),
                    "ids": [summary.get("id") for summary in others],
                    "normalized_key": item.get("normalized_key") or "",
                }
        return items

    async def _sync_memory_vector_for_item_safe(self, item: dict[str, Any] | None) -> None:
        if not item or not self.vector_index.is_enabled:
            return
        try:
            await self.vector_index.upsert_item(item)
        except Exception as exc:
            logger.warning(
                "memory.vector_sync_failed",
                item_id=item.get("id") if isinstance(item, dict) else None,
                error_type=exc.__class__.__name__,
                error=_truncate_error(exc),
            )

    async def _delete_memory_vector_for_item_safe(
        self, item_or_id: dict[str, Any] | int | None
    ) -> None:
        if not self.vector_index.is_enabled or item_or_id is None:
            return
        item_id = item_or_id.get("id") if isinstance(item_or_id, dict) else item_or_id
        try:
            await self.vector_index.delete_item(item_id)
        except Exception as exc:
            logger.warning(
                "memory.vector_delete_failed",
                item_id=item_id,
                error_type=exc.__class__.__name__,
                error=_truncate_error(exc),
            )

    async def _get_graph_fact_for_memory_item(self, item_id: int) -> dict[str, Any] | None:
        rows = await _exec(
            "SELECT fact.id, fact.tenant_id, fact.channel, fact.source_key, fact.user_id, "
            "fact.subject_entity_id, subject.name AS subject_name, "
            "subject.normalized_name AS subject_normalized_name, fact.predicate, "
            "fact.object_entity_id, object_entity.name AS object_name, "
            "object_entity.normalized_name AS object_normalized_name, fact.object_value, "
            "fact.memory_item_id, fact.source_event_id, fact.confidence, fact.status, "
            "fact.valid_at, fact.invalid_at, fact.created_at, fact.updated_at "
            "FROM plugin_memory_fact fact "
            "LEFT JOIN plugin_memory_entity subject ON subject.id = fact.subject_entity_id "
            "AND subject.tenant_id = fact.tenant_id AND subject.channel = fact.channel "
            "AND subject.source_key = fact.source_key AND subject.user_id = fact.user_id "
            "LEFT JOIN plugin_memory_entity object_entity ON object_entity.id = fact.object_entity_id "
            "AND object_entity.tenant_id = fact.tenant_id AND object_entity.channel = fact.channel "
            "AND object_entity.source_key = fact.source_key AND object_entity.user_id = fact.user_id "
            "WHERE fact.memory_item_id = :memory_item_id "
            "ORDER BY fact.updated_at DESC, fact.id DESC LIMIT 1",
            {"memory_item_id": int(item_id)},
        )
        return rows[0] if rows else None

    async def _get_graph_episode_for_memory_item(
        self,
        item_id: int,
        *,
        item: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        item_id_int = int(item_id)
        scoped_item = item or await self.get_memory_item(item_id_int)
        params: dict[str, Any] = {"memory_item_id": item_id_int, "lim": 80}
        conditions: list[str] = []
        if scoped_item:
            tenant_id = str(scoped_item.get("tenant_id") or "")
            channel = str(scoped_item.get("channel") or "")
            source_key = str(scoped_item.get("source_key") or "*")
            user_id = str(scoped_item.get("user_id") or "")
            session_id = str(scoped_item.get("session_id") or "")
            if tenant_id and channel and user_id:
                conditions.extend(
                    [
                        "tenant_id = :tid",
                        "channel = :channel",
                        "source_key = :source_key",
                        "user_id = :uid",
                    ]
                )
                params.update(
                    {
                        "tid": tenant_id,
                        "channel": channel,
                        "source_key": source_key,
                        "uid": user_id,
                        "sid": session_id,
                    }
                )
                if session_id:
                    conditions.append("(session_id = '' OR session_id = :sid)")
                else:
                    conditions.append("session_id = ''")
        if not conditions:
            conditions.append("memory_item_ids_json LIKE :memory_item_id_match")
            params["memory_item_id_match"] = f"%{item_id_int}%"
        rows = await _exec(
            "SELECT id, tenant_id, channel, source_key, user_id, session_id, title, summary, "
            "event_ids_json, memory_item_ids_json, importance, status, created_at, updated_at "
            "FROM plugin_memory_episode "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY updated_at DESC, id DESC LIMIT :lim",
            params,
        )
        for row in rows:
            episode = _finalize_graph_episode(row)
            if item_id_int in _coerce_int_set(episode.get("memory_item_ids")):
                return episode
        return None

    async def _get_memory_items_by_ids(self, item_ids: Iterable[Any]) -> list[dict[str, Any]]:
        ids = sorted(_coerce_int_set(item_ids))
        if not ids:
            return []
        rows = await _exec(
            "SELECT id, tenant_id, channel, source_key, user_id, session_id, scope_type, source_type, "
            "memory_type, content, value_json, normalized_key, confidence, status, pinned, priority, "
            "sensitivity, audience_scope, origin_session_kind, allowed_session_ids, source_kind, "
            "sensitivity_category, expires_at, source_event_id, source_trace_id, original_text, occurrence_count, "
            "first_seen_at, last_seen_at, created_at, updated_at, deleted_at "
            "FROM plugin_memory_item WHERE id = ANY(:memory_item_ids)",
            {"memory_item_ids": ids},
        )
        return [self._finalize_memory_item(row) for row in rows if _looks_like_memory_item_row(row)]

    async def _get_sanitized_memory_items_by_ids(
        self, item_ids: Iterable[Any]
    ) -> list[dict[str, Any]]:
        ids = sorted(_coerce_int_set(item_ids))
        if not ids:
            return []
        rows = await _exec(
            "SELECT id, tenant_id, channel, source_key, user_id, session_id, scope_type, source_type, "
            "memory_type, value_json, normalized_key, confidence, status, pinned, priority, "
            "sensitivity, source_event_id, source_trace_id, occurrence_count, "
            "first_seen_at, last_seen_at, created_at, updated_at, deleted_at "
            "FROM plugin_memory_item WHERE id = ANY(:memory_item_ids)",
            {"memory_item_ids": ids},
        )
        items: list[dict[str, Any]] = []
        allowed_keys = {
            "id",
            "tenant_id",
            "channel",
            "source_key",
            "user_id",
            "session_id",
            "scope_type",
            "source_type",
            "memory_type",
            "value_json",
            "normalized_key",
            "confidence",
            "status",
            "pinned",
            "priority",
            "sensitivity",
            "source_event_id",
            "source_trace_id",
            "occurrence_count",
            "first_seen_at",
            "last_seen_at",
            "created_at",
            "updated_at",
            "deleted_at",
        }
        for row in rows:
            item = {key: row.get(key) for key in allowed_keys if key in row}
            item["value"] = _safe_json_loads(item.get("value_json"), {})
            item["confidence"] = float(item.get("confidence") or 0.0)
            item["priority"] = int(item.get("priority") or 0)
            item["occurrence_count"] = int(item.get("occurrence_count") or 0)
            item["pinned"] = bool(item.get("pinned"))
            item["acceptance_status"] = _memory_acceptance_status_from_item(item)
            items.append(item)
        return items

    async def _get_memory_events_by_ids(self, event_ids: Iterable[Any]) -> list[dict[str, Any]]:
        ids = sorted(_coerce_int_set(event_ids))
        if not ids:
            return []
        return await _exec(
            "SELECT id, tenant_id, channel, source_key, user_id, session_id, "
            "user_text, assistant_text, trace_id, event_key, created_at "
            "FROM plugin_memory_event WHERE id = ANY(:event_ids) "
            "ORDER BY created_at DESC, id DESC",
            {"event_ids": ids},
        )

    async def _get_memory_event_metadata_by_ids(
        self, event_ids: Iterable[Any]
    ) -> list[dict[str, Any]]:
        ids = sorted(_coerce_int_set(event_ids))
        if not ids:
            return []
        return await _exec(
            "SELECT id, tenant_id, channel, source_key, user_id, session_id, created_at "
            "FROM plugin_memory_event WHERE id = ANY(:event_ids) "
            "ORDER BY created_at DESC, id DESC",
            {"event_ids": ids},
        )

    async def _sync_graph_vectors_for_memory_item_safe(self, item: dict[str, Any]) -> None:
        if not self.vector_index.is_enabled or item.get("id") is None:
            return
        item_id = int(item["id"])
        try:
            fact = await self._get_graph_fact_for_memory_item(item_id)
            if fact:
                await self.vector_index.upsert_fact(fact, backing_item=item)
            episode = await self._get_graph_episode_for_memory_item(item_id, item=item)
            if episode:
                backing_items = await self._get_memory_items_by_ids(
                    episode.get("memory_item_ids") or []
                )
                await self.vector_index.upsert_episode(episode, backing_items=backing_items)
        except Exception as exc:
            logger.warning(
                "memory.graph_vector_sync_failed",
                item_id=item_id,
                error_type=exc.__class__.__name__,
                error=_truncate_error(exc),
            )

    async def _get_or_create_graph_entity(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        entity_type: str,
        name: str,
        confidence: float,
        status: str = "active",
    ) -> int:
        entity_type = _normalize_line(entity_type or "thing")[:64] or "thing"
        name = _normalize_line(name)[:500]
        normalized_name = _graph_normalized_name(name)
        if not name or not normalized_name:
            name = entity_type
            normalized_name = _graph_normalized_name(entity_type)
        rows = await _exec(
            "INSERT INTO plugin_memory_entity "
            "(tenant_id, channel, source_key, user_id, entity_type, name, normalized_name, "
            "aliases_json, confidence, status, created_at, updated_at) "
            "VALUES (:tid, :channel, :source_key, :uid, :entity_type, :name, :normalized_name, "
            "'[]', :confidence, :status, NOW(), NOW()) "
            "ON CONFLICT (tenant_id, channel, source_key, user_id, entity_type, normalized_name) "
            "DO UPDATE SET name = EXCLUDED.name, "
            "confidence = GREATEST(plugin_memory_entity.confidence, EXCLUDED.confidence), "
            "status = CASE WHEN plugin_memory_entity.status = 'deleted' THEN EXCLUDED.status "
            "              ELSE plugin_memory_entity.status END, "
            "updated_at = NOW() "
            "RETURNING id",
            {
                "tid": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "uid": user_id,
                "entity_type": entity_type,
                "name": name,
                "normalized_name": normalized_name,
                "confidence": float(confidence or 0.0),
                "status": status,
            },
        )
        return int(rows[0]["id"])

    async def _get_graph_entity_id_by_key(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        entity_key: str,
        entities_by_key: dict[str, dict[str, Any]],
        default_type: str = "thing",
        default_name: str = "",
        confidence: float = 0.0,
        status: str = "active",
    ) -> int:
        entity = entities_by_key.get(entity_key) or {}
        entity_type = str(
            entity.get("type") or entity.get("entity_type") or default_type or "thing"
        )
        name = str(entity.get("name") or default_name or entity_key or entity_type)
        entity_confidence = float(entity.get("confidence") or confidence or 0.0)
        entity_status = str(entity.get("status") or status or "active")
        return await self._get_or_create_graph_entity(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            entity_type=entity_type,
            name=name,
            confidence=entity_confidence,
            status=entity_status,
        )

    async def _find_memory_item_for_graph_fact(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        fact: dict[str, Any],
    ) -> dict[str, Any] | None:
        memory_item_id = fact.get("memory_item_id")
        if memory_item_id is not None:
            item = await self.get_memory_item(int(memory_item_id))
            if item and _memory_item_matches_scope(
                item,
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                session_id=str(item.get("session_id") or ""),
            ):
                return item
            return None
        memory_key = _bounded_memory_key(str(fact.get("memory_key") or "").strip())
        if memory_key:
            matches = await self._find_memory_item_by_normalized_key(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                scope_type="identity",
                session_id="",
                normalized_key=memory_key,
                statuses={"active", "pending"},
                limit=1,
            )
            if matches:
                return matches[0]
        return None

    async def _ensure_graph_backing_memory_item(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        session_id: str,
        fact: dict[str, Any],
        source_event_id: int | None,
        source_trace_id: str,
        original_text: str,
        origin_session_kind: str = "private",
        audience_scope: str = "private",
        allowed_session_ids: Iterable[str] | None = None,
        sensitivity_category: str = "normal",
        expires_at: datetime | str | None = None,
        source_kind: str = "graph",
    ) -> dict[str, Any] | None:
        audience_contract = _normalize_memory_audience_contract(
            origin_session_kind=origin_session_kind,
            audience_scope=audience_scope,
            allowed_session_ids=allowed_session_ids,
            session_id="",
            expires_at=expires_at,
        )
        existing = await self._find_memory_item_for_graph_fact(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            fact=fact,
        )
        if existing:
            if not _memory_item_matches_audience_contract(
                existing,
                audience_contract,
                session_id="",
            ):
                return None
            if existing.get("source_type") == "manual" or existing.get("pinned"):
                return None
            return existing
        status = str(fact.get("status") or "pending")
        if status == "skipped":
            return None
        sensitivity = str(
            fact.get("sensitivity") or _detect_sensitivity(str(fact.get("content") or ""))
        )
        confidence = float(fact.get("confidence") or 0.0)
        if sensitivity != "normal" or confidence < _settings_float(
            self.settings,
            "memory_graph_llm_extraction_min_confidence",
            0.8,
            minimum=0.0,
        ):
            status = "pending"
        normalized_key = _bounded_memory_key(str(fact.get("memory_key") or "").strip())
        if not normalized_key:
            key_value = "|".join(
                [
                    str(fact.get("subject_key") or "user"),
                    str(fact.get("predicate") or "fact"),
                    str(
                        fact.get("object_key")
                        or fact.get("object_value")
                        or fact.get("content")
                        or ""
                    ),
                ]
            )
            normalized_key = _semantic_key(
                "graph_fact", str(fact.get("predicate") or "fact"), key_value
            )
        item = await self._insert_or_touch_memory_item(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            scope_type="identity",
            source_type=GRAPH_LLM_BACKING_SOURCE_TYPE,
            memory_type="profile_fact",
            content=str(fact.get("content") or "")[:500],
            value_json={
                "op": "graph_llm_extract",
                "predicate": fact.get("predicate"),
                "subject_key": fact.get("subject_key"),
                "object_key": fact.get("object_key"),
                "object_value": fact.get("object_value"),
                "evidence": [
                    {
                        "source_event_id": source_event_id,
                        "source_trace_id": source_trace_id,
                        "reason": "llm_graph_extraction",
                    }
                ],
            },
            normalized_key=normalized_key,
            confidence=confidence,
            status=status,
            pinned=False,
            priority=0,
            sensitivity=sensitivity,
            origin_session_kind=origin_session_kind,
            audience_scope=audience_scope,
            allowed_session_ids=allowed_session_ids,
            sensitivity_category=sensitivity_category,
            expires_at=expires_at,
            source_kind=source_kind,
            source_event_id=source_event_id,
            source_trace_id=source_trace_id,
            original_text=original_text or str(fact.get("content") or ""),
        )
        await self._sync_memory_vector_for_item_safe(item)
        return item

    async def _upsert_llm_graph_fact(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        fact: dict[str, Any],
        entities_by_key: dict[str, dict[str, Any]],
        backing_item: dict[str, Any],
        source_event_id: int | None,
    ) -> bool:
        memory_item_id = backing_item.get("id")
        if memory_item_id is None:
            return False
        if backing_item.get("source_type") == "manual" or backing_item.get("pinned"):
            return False
        subject_id = await self._get_graph_entity_id_by_key(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            entity_key=str(fact.get("subject_key") or "user"),
            entities_by_key=entities_by_key,
            default_type="user",
            default_name=f"user:{user_id}",
            confidence=1.0,
            status="active",
        )
        object_entity_id = None
        object_key = str(fact.get("object_key") or "")
        if object_key:
            object_entity_id = await self._get_graph_entity_id_by_key(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                entity_key=object_key,
                entities_by_key=entities_by_key,
                default_type="thing",
                default_name=object_key,
                confidence=float(fact.get("confidence") or 0.0),
                status=str(fact.get("status") or "active"),
            )
        await _exec(
            "INSERT INTO plugin_memory_fact "
            "(tenant_id, channel, source_key, user_id, subject_entity_id, predicate, "
            "object_entity_id, object_value, memory_item_id, source_event_id, confidence, "
            "status, valid_at, invalid_at, created_at, updated_at) "
            "VALUES (:tid, :channel, :source_key, :uid, :subject_id, :predicate, "
            ":object_entity_id, :object_value, :memory_item_id, :source_event_id, :confidence, "
            ":status, NOW(), CASE WHEN CAST(:status AS VARCHAR) IN ('deleted', 'invalidated', 'archived') THEN NOW() ELSE NULL END, "
            "NOW(), NOW()) "
            "ON CONFLICT (memory_item_id) DO UPDATE SET "
            "subject_entity_id = EXCLUDED.subject_entity_id, predicate = EXCLUDED.predicate, "
            "object_entity_id = EXCLUDED.object_entity_id, object_value = EXCLUDED.object_value, "
            "source_event_id = COALESCE(plugin_memory_fact.source_event_id, EXCLUDED.source_event_id), "
            "confidence = GREATEST(plugin_memory_fact.confidence, EXCLUDED.confidence), "
            "status = EXCLUDED.status, "
            "invalid_at = CASE WHEN EXCLUDED.status IN ('deleted', 'invalidated', 'archived') "
            "                  THEN COALESCE(plugin_memory_fact.invalid_at, NOW()) ELSE NULL END, "
            "updated_at = NOW()",
            {
                "tid": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "uid": user_id,
                "subject_id": subject_id,
                "predicate": str(fact.get("predicate") or "profile_fact")[:128],
                "object_entity_id": object_entity_id,
                "object_value": str(fact.get("object_value") or "")[:2000],
                "memory_item_id": int(memory_item_id),
                "source_event_id": source_event_id,
                "confidence": float(fact.get("confidence") or 0.0),
                "status": str(fact.get("status") or "pending"),
            },
        )
        await self._sync_graph_vectors_for_memory_item_safe(backing_item)
        return True

    async def _upsert_llm_graph_episode(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        session_id: str,
        episode: dict[str, Any],
        source_event_id: int | None,
        source_trace_id: str = "",
        original_text: str = "",
        origin_session_kind: str = "private",
        audience_scope: str = "private",
        allowed_session_ids: Iterable[str] | None = None,
        sensitivity_category: str = "normal",
        expires_at: datetime | str | None = None,
        source_kind: str = "graph",
    ) -> bool:
        if str(episode.get("status") or "") == "skipped":
            return False
        memory_item_ids = sorted(_coerce_int_set(episode.get("memory_item_ids") or []))
        event_ids = sorted(_coerce_int_set(episode.get("event_ids") or []))
        if source_event_id is not None:
            event_ids = sorted(set(event_ids) | {int(source_event_id)})
        if not memory_item_ids:
            item = await self._insert_or_touch_memory_item(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                session_id=session_id,
                scope_type="session" if session_id else "identity",
                source_type=GRAPH_LLM_BACKING_SOURCE_TYPE,
                memory_type="episodic",
                content=str(episode.get("summary") or episode.get("title") or "")[:500],
                value_json={"op": "graph_llm_extract", "episode_title": episode.get("title")},
                normalized_key=_semantic_key(
                    "graph_episode",
                    "summary",
                    str(episode.get("title") or "") + "|" + str(source_event_id or ""),
                ),
                confidence=float(episode.get("confidence") or 0.0),
                status=str(episode.get("status") or "pending"),
                pinned=False,
                priority=int(episode.get("importance") or 0),
                sensitivity=str(episode.get("sensitivity") or "normal"),
                origin_session_kind=origin_session_kind,
                audience_scope=audience_scope,
                allowed_session_ids=allowed_session_ids,
                sensitivity_category=sensitivity_category,
                expires_at=expires_at,
                source_kind=source_kind,
                source_event_id=source_event_id,
                source_trace_id=source_trace_id,
                original_text=original_text or str(episode.get("summary") or ""),
            )
            if not item or item.get("id") is None:
                return False
            memory_item_ids = [int(item["id"])]
        await _exec(
            "INSERT INTO plugin_memory_episode "
            "(tenant_id, channel, source_key, user_id, session_id, title, summary, "
            "event_ids_json, memory_item_ids_json, importance, status, created_at, updated_at) "
            "VALUES (:tid, :channel, :source_key, :uid, :sid, :title, :summary, "
            ":event_ids, :memory_item_ids, :importance, :status, NOW(), NOW()) "
            "ON CONFLICT (memory_item_ids_json) DO UPDATE SET "
            "tenant_id = EXCLUDED.tenant_id, channel = EXCLUDED.channel, "
            "source_key = EXCLUDED.source_key, user_id = EXCLUDED.user_id, "
            "session_id = EXCLUDED.session_id, title = EXCLUDED.title, summary = EXCLUDED.summary, "
            "event_ids_json = EXCLUDED.event_ids_json, importance = EXCLUDED.importance, "
            "status = EXCLUDED.status, updated_at = NOW()",
            {
                "tid": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "uid": user_id,
                "sid": session_id,
                "title": str(episode.get("title") or "")[:500],
                "summary": str(episode.get("summary") or "")[:2000],
                "event_ids": _to_json(event_ids),
                "memory_item_ids": _to_json(memory_item_ids),
                "importance": int(episode.get("importance") or 0),
                "status": str(episode.get("status") or "pending"),
            },
        )
        backing_items = await self._get_memory_items_by_ids(memory_item_ids)
        for item in backing_items:
            await self._sync_graph_vectors_for_memory_item_safe(item)
        return True

    async def _sync_memory_graph_for_item(self, item: dict[str, Any]) -> None:
        tenant_id = str(item.get("tenant_id") or "")
        channel = str(item.get("channel") or "")
        source_key = str(item.get("source_key") or "*")
        user_id = str(item.get("user_id") or "")
        item_id = item.get("id")
        if not tenant_id or not channel or not user_id or item_id is None:
            return

        mapping = _memory_graph_mapping_for_item(item)
        status = _graph_status_for_memory_item(item)
        stale_status = _graph_stale_status_for_memory_item(item)
        if mapping is None:
            await _exec(
                "UPDATE plugin_memory_fact SET status = :status, "
                "invalid_at = CASE WHEN CAST(:status AS VARCHAR) IN ('deleted', 'invalidated', 'archived') "
                "                  THEN COALESCE(invalid_at, NOW()) ELSE invalid_at END, "
                "updated_at = NOW() "
                "WHERE memory_item_id = :memory_item_id",
                {"memory_item_id": int(item_id), "status": stale_status},
            )
            await _exec(
                "UPDATE plugin_memory_episode SET status = :status, updated_at = NOW() "
                "WHERE memory_item_ids_json = :memory_item_ids_json",
                {"memory_item_ids_json": _to_json([int(item_id)]), "status": stale_status},
            )
            await self._sync_graph_vectors_for_memory_item_safe(item)
            return

        confidence = float(mapping.get("confidence") or 0.0)
        if mapping["kind"] == "episode":
            await _exec(
                "INSERT INTO plugin_memory_episode "
                "(tenant_id, channel, source_key, user_id, session_id, title, summary, "
                "event_ids_json, memory_item_ids_json, importance, status, created_at, updated_at) "
                "VALUES (:tid, :channel, :source_key, :uid, :sid, :title, :summary, "
                ":event_ids, :memory_item_ids, :importance, :status, NOW(), NOW()) "
                "ON CONFLICT (memory_item_ids_json) DO UPDATE SET "
                "tenant_id = EXCLUDED.tenant_id, channel = EXCLUDED.channel, "
                "source_key = EXCLUDED.source_key, user_id = EXCLUDED.user_id, "
                "session_id = EXCLUDED.session_id, title = EXCLUDED.title, summary = EXCLUDED.summary, "
                "event_ids_json = EXCLUDED.event_ids_json, importance = EXCLUDED.importance, "
                "status = EXCLUDED.status, updated_at = NOW()",
                {
                    "tid": tenant_id,
                    "channel": channel,
                    "source_key": source_key,
                    "uid": user_id,
                    "sid": str(mapping.get("session_id") or ""),
                    "title": str(mapping.get("title") or "")[:500],
                    "summary": str(mapping.get("summary") or "")[:2000],
                    "event_ids": _to_json(mapping.get("event_ids") or []),
                    "memory_item_ids": _to_json(mapping.get("memory_item_ids") or [int(item_id)]),
                    "importance": int(mapping.get("importance") or 0),
                    "status": status,
                },
            )
            await _exec(
                "UPDATE plugin_memory_fact SET status = :status, "
                "invalid_at = CASE WHEN CAST(:status AS VARCHAR) IN ('deleted', 'invalidated', 'archived') "
                "                  THEN COALESCE(invalid_at, NOW()) ELSE invalid_at END, "
                "updated_at = NOW() "
                "WHERE memory_item_id = :memory_item_id",
                {"memory_item_id": int(item_id), "status": stale_status},
            )
            await self._sync_graph_vectors_for_memory_item_safe(item)
            return

        subject_entity = mapping.get("subject_entity")
        if isinstance(subject_entity, dict):
            subject_id = await self._get_or_create_graph_entity(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                entity_type=str(subject_entity.get("entity_type") or "person"),
                name=str(subject_entity.get("name") or ""),
                confidence=confidence,
                status=status,
            )
        else:
            subject_id = await self._get_or_create_graph_entity(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                entity_type="user",
                name=f"user:{user_id}",
                confidence=1.0,
                status="active",
            )
        object_entity_id = None
        object_entity = mapping.get("object_entity")
        if isinstance(object_entity, dict):
            object_entity_id = await self._get_or_create_graph_entity(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                entity_type=str(object_entity.get("entity_type") or "thing"),
                name=str(object_entity.get("name") or ""),
                confidence=confidence,
                status=status,
            )
        await _exec(
            "INSERT INTO plugin_memory_fact "
            "(tenant_id, channel, source_key, user_id, subject_entity_id, predicate, "
            "object_entity_id, object_value, memory_item_id, source_event_id, confidence, "
            "status, valid_at, invalid_at, created_at, updated_at) "
            "VALUES (:tid, :channel, :source_key, :uid, :subject_id, :predicate, "
            ":object_entity_id, :object_value, :memory_item_id, :source_event_id, :confidence, "
            ":status, NOW(), CASE WHEN CAST(:status AS VARCHAR) IN ('deleted', 'invalidated', 'archived') THEN NOW() ELSE NULL END, "
            "NOW(), NOW()) "
            "ON CONFLICT (memory_item_id) DO UPDATE SET "
            "tenant_id = EXCLUDED.tenant_id, channel = EXCLUDED.channel, "
            "source_key = EXCLUDED.source_key, user_id = EXCLUDED.user_id, "
            "subject_entity_id = EXCLUDED.subject_entity_id, predicate = EXCLUDED.predicate, "
            "object_entity_id = EXCLUDED.object_entity_id, object_value = EXCLUDED.object_value, "
            "source_event_id = COALESCE(plugin_memory_fact.source_event_id, EXCLUDED.source_event_id), "
            "confidence = GREATEST(plugin_memory_fact.confidence, EXCLUDED.confidence), "
            "status = EXCLUDED.status, "
            "invalid_at = CASE WHEN EXCLUDED.status IN ('deleted', 'invalidated', 'archived') "
            "                  THEN COALESCE(plugin_memory_fact.invalid_at, NOW()) ELSE NULL END, "
            "updated_at = NOW()",
            {
                "tid": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "uid": user_id,
                "subject_id": subject_id,
                "predicate": str(mapping.get("predicate") or "note")[:128],
                "object_entity_id": object_entity_id,
                "object_value": str(mapping.get("object_value") or "")[:2000],
                "memory_item_id": int(item_id),
                "source_event_id": mapping.get("source_event_id"),
                "confidence": confidence,
                "status": status,
            },
        )
        await _exec(
            "UPDATE plugin_memory_episode SET status = :status, updated_at = NOW() "
            "WHERE memory_item_ids_json = :memory_item_ids_json",
            {"memory_item_ids_json": _to_json([int(item_id)]), "status": stale_status},
        )
        await self._sync_graph_vectors_for_memory_item_safe(item)

    async def _sync_memory_graph_for_item_safe(self, item: dict[str, Any] | None) -> None:
        if not item:
            return
        try:
            await self._sync_memory_graph_for_item(item)
        except Exception as exc:
            logger.warning(
                "memory.graph_sync_failed",
                item_id=item.get("id") if isinstance(item, dict) else None,
                error_type=exc.__class__.__name__,
                error=_truncate_error(exc),
            )

    async def get_memory_item(
        self,
        item_id: int,
        *,
        for_update: bool = False,
    ) -> dict[str, Any] | None:
        active_conn = _ACTIVE_MUTATION_CONNECTION.get()
        dialect = str(active_conn.dialect.name or "").lower() if active_conn is not None else ""
        lock_clause = (
            " FOR UPDATE" if for_update and active_conn is not None and dialect != "sqlite" else ""
        )
        rows = await _exec(
            "SELECT id, tenant_id, channel, source_key, user_id, session_id, scope_type, source_type, "
            "memory_type, content, value_json, normalized_key, confidence, status, pinned, priority, "
            "sensitivity, audience_scope, origin_session_kind, allowed_session_ids, source_kind, "
            "sensitivity_category, expires_at, source_event_id, source_trace_id, original_text, occurrence_count, "
            "first_seen_at, last_seen_at, created_at, updated_at, deleted_at "
            f"FROM plugin_memory_item WHERE id = :id{lock_clause}",
            {"id": item_id},
        )
        return self._finalize_memory_item(rows[0]) if rows else None

    async def list_memory_items(
        self,
        *,
        tenant_id: str,
        channel: str | None = None,
        source_key: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        scope_type: str | None = None,
        source_type: str | None = None,
        status: str | None = None,
        include_deleted: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id = :tid"]
        params: dict[str, Any] = {"tid": tenant_id, "lim": max(1, min(int(limit or 100), 500))}
        if channel:
            conditions.append("channel = :channel")
            params["channel"] = channel
        if _needs_source_fallback(source_key):
            conditions.append("source_key IN (:source_key, '*')")
            params["source_key"] = source_key
        elif source_key:
            conditions.append("source_key = :source_key")
            params["source_key"] = source_key
        if user_id:
            conditions.append("user_id = :uid")
            params["uid"] = user_id
        if session_id is not None:
            conditions.append("session_id = :sid")
            params["sid"] = session_id
        if scope_type:
            conditions.append("scope_type = :scope_type")
            params["scope_type"] = scope_type
        if source_type:
            conditions.append("source_type = :source_type")
            params["source_type"] = source_type
        if status:
            conditions.append("status = :status")
            params["status"] = status
        if not include_deleted:
            conditions.append("deleted_at IS NULL")
            conditions.append("status <> 'deleted'")
        rows = await _exec(
            "SELECT id, tenant_id, channel, source_key, user_id, session_id, scope_type, source_type, "
            "memory_type, content, value_json, normalized_key, confidence, status, pinned, priority, "
            "sensitivity, audience_scope, origin_session_kind, allowed_session_ids, source_kind, "
            "sensitivity_category, expires_at, source_event_id, source_trace_id, original_text, occurrence_count, "
            "first_seen_at, last_seen_at, created_at, updated_at, deleted_at "
            "FROM plugin_memory_item "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY pinned DESC, priority DESC, updated_at DESC LIMIT :lim",
            params,
        )
        items = [
            self._finalize_memory_item(row) for row in rows if _looks_like_memory_item_row(row)
        ]
        return self._annotate_duplicate_hints(items)

    def _memory_item_filter_conditions(
        self,
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
        include_deleted: bool = False,
    ) -> tuple[list[str], dict[str, Any]]:
        conditions = ["tenant_id = :tid"]
        params: dict[str, Any] = {"tid": tenant_id}
        if channel:
            conditions.append("channel = :channel")
            params["channel"] = channel
        if _needs_source_fallback(source_key):
            conditions.append("source_key IN (:source_key, '*')")
            params["source_key"] = source_key
        elif source_key:
            conditions.append("source_key = :source_key")
            params["source_key"] = source_key
        if user_id:
            conditions.append("user_id = :uid")
            params["uid"] = user_id
        if session_id is not None:
            conditions.append("session_id = :sid")
            params["sid"] = session_id
        if scope_type:
            conditions.append("scope_type = :scope_type")
            params["scope_type"] = scope_type
        if source_type:
            conditions.append("source_type = :source_type")
            params["source_type"] = source_type
        if memory_type:
            conditions.append("memory_type = :memory_type")
            params["memory_type"] = memory_type
        if status:
            conditions.append("status = :status")
            params["status"] = status
        if not include_deleted:
            conditions.append("deleted_at IS NULL")
            conditions.append("status <> 'deleted'")
        return conditions, params

    async def _list_memory_acceptance_audit_items(
        self,
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
        include_deleted: bool = False,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        conditions, params = self._memory_item_filter_conditions(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            scope_type=scope_type,
            source_type=source_type,
            memory_type=memory_type,
            status=status,
            include_deleted=include_deleted,
        )
        params["lim"] = max(1, min(int(limit or 5000), 10000))
        rows = await _exec(
            "SELECT id, tenant_id, channel, source_key, user_id, session_id, scope_type, source_type, "
            "memory_type, value_json, confidence, status, pinned, priority, sensitivity, "
            "source_event_id, source_trace_id, occurrence_count, first_seen_at, last_seen_at, "
            "created_at, updated_at, deleted_at "
            "FROM plugin_memory_item "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY updated_at DESC, id DESC LIMIT :lim",
            params,
        )
        return rows

    @staticmethod
    def _increment_count(bucket: dict[str, int], key: Any) -> None:
        normalized = str(key or "unknown").strip() or "unknown"
        bucket[normalized] = int(bucket.get(normalized, 0)) + 1

    @staticmethod
    def _group_increment(
        groups: dict[str, dict[str, Any]], key_parts: tuple[Any, ...], item_id: Any
    ) -> None:
        key = "|".join(str(part or "") for part in key_parts)
        group = groups.setdefault(
            key,
            {
                "scope_type": str(key_parts[0] or ""),
                "status": str(key_parts[1] or ""),
                "memory_type": str(key_parts[2] or ""),
                "source_type": str(key_parts[3] or ""),
                "count": 0,
                "ids_preview": [],
                "ids_truncated": 0,
                "suggested_action": "needs_review",
            },
        )
        group["count"] = int(group.get("count") or 0) + 1
        ids_preview = group["ids_preview"] if isinstance(group.get("ids_preview"), list) else []
        if len(ids_preview) < MEMORY_ACCEPTANCE_ID_PREVIEW_LIMIT:
            ids_preview.append(item_id)
            group["ids_preview"] = ids_preview
        else:
            group["ids_truncated"] = int(group.get("ids_truncated") or 0) + 1

    async def get_memory_acceptance_stats(
        self,
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
        acceptance_status: str | None = None,
        limit: int = 5000,
    ) -> dict[str, Any]:
        rows = await self._list_memory_acceptance_audit_items(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            scope_type=scope_type,
            source_type=source_type,
            memory_type=memory_type,
            status=status,
            include_deleted=False,
            limit=limit,
        )
        requested_acceptance = str(acceptance_status or "").strip().lower()
        if requested_acceptance and requested_acceptance not in {
            *MEMORY_ACCEPTANCE_STATUSES,
            "missing_acceptance",
            "unknown_acceptance",
        }:
            raise ValueError("unsupported acceptance_status filter")
        counts = {key: 0 for key in [*sorted(MEMORY_ACCEPTANCE_STATUSES), "missing_acceptance"]}
        sensitivity_counts = {"normal": 0, "private": 0, "sensitive": 0}
        item_status_counts: dict[str, int] = {}
        source_type_counts: dict[str, int] = {}
        memory_type_counts: dict[str, int] = {}
        scope_type_counts: dict[str, int] = {}
        by_status: dict[str, dict[str, int]] = {}
        by_source_type: dict[str, dict[str, int]] = {}
        by_memory_type: dict[str, dict[str, int]] = {}
        ids_preview: list[Any] = []
        ids_truncated = 0
        total = 0

        for row in rows:
            bucket = _memory_acceptance_status_bucket(row)
            if requested_acceptance and requested_acceptance != bucket:
                continue
            total += 1
            counts[bucket] = int(counts.get(bucket, 0)) + 1
            sensitivity_counts[_memory_sensitivity_bucket(row.get("sensitivity"))] += 1
            self._increment_count(item_status_counts, row.get("status"))
            self._increment_count(source_type_counts, row.get("source_type"))
            self._increment_count(memory_type_counts, row.get("memory_type"))
            self._increment_count(scope_type_counts, row.get("scope_type"))
            for group_map, group_key in (
                (by_status, row.get("status")),
                (by_source_type, row.get("source_type")),
                (by_memory_type, row.get("memory_type")),
            ):
                normalized_group_key = str(group_key or "unknown").strip() or "unknown"
                group_counts = group_map.setdefault(normalized_group_key, {})
                group_counts[bucket] = int(group_counts.get(bucket, 0)) + 1
            if len(ids_preview) < MEMORY_ACCEPTANCE_ID_PREVIEW_LIMIT:
                ids_preview.append(row.get("id"))
            else:
                ids_truncated += 1

        return {
            "total": total,
            "counts": counts,
            "sensitivity_counts": sensitivity_counts,
            "status_counts": item_status_counts,
            "source_type_counts": source_type_counts,
            "memory_type_counts": memory_type_counts,
            "scope_type_counts": scope_type_counts,
            "by_status": by_status,
            "by_source_type": by_source_type,
            "by_memory_type": by_memory_type,
            "ids_preview": ids_preview,
            "ids_truncated": ids_truncated,
            "limit": max(1, min(int(limit or 5000), 10000)),
        }

    async def audit_legacy_acceptance(
        self,
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
        limit: int = 5000,
    ) -> dict[str, Any]:
        rows = await self._list_memory_acceptance_audit_items(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            scope_type=scope_type,
            source_type=source_type,
            memory_type=memory_type,
            status=status,
            include_deleted=False,
            limit=limit,
        )
        groups: dict[str, dict[str, Any]] = {}
        ids_preview: list[Any] = []
        ids_truncated = 0
        missing_count = 0
        for row in rows:
            if not _memory_item_missing_acceptance(row):
                continue
            missing_count += 1
            if len(ids_preview) < MEMORY_ACCEPTANCE_ID_PREVIEW_LIMIT:
                ids_preview.append(row.get("id"))
            else:
                ids_truncated += 1
            self._group_increment(
                groups,
                (
                    row.get("scope_type"),
                    row.get("status"),
                    row.get("memory_type"),
                    row.get("source_type"),
                ),
                row.get("id"),
            )

        grouped = sorted(
            groups.values(),
            key=lambda item: (
                -int(item.get("count") or 0),
                str(item.get("scope_type") or ""),
                str(item.get("status") or ""),
                str(item.get("memory_type") or ""),
            ),
        )
        return {
            "dry_run": True,
            "missing_acceptance": missing_count,
            "suggested_action": "needs_review",
            "groups": grouped,
            "ids_preview": ids_preview,
            "ids_truncated": ids_truncated,
            "limit": max(1, min(int(limit or 5000), 10000)),
        }

    async def backfill_legacy_acceptance(
        self,
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
        dry_run: bool = True,
        max_items: int | None = None,
        mark_missing_as: str = "needs_review",
        reviewed_by: str = "admin_backfill",
    ) -> dict[str, Any]:
        target_status = str(mark_missing_as or "needs_review").strip().lower()
        if target_status not in {"needs_review", "candidate"}:
            raise ValueError("mark_missing_as must be needs_review or candidate")
        if not dry_run and not max_items:
            raise ValueError("non-dry-run acceptance backfill requires max_items")
        safe_limit = max(1, min(int(max_items or 5000), 10000))
        scan_limit = 10000 if max_items else safe_limit
        rows = await self._list_memory_acceptance_audit_items(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            scope_type=scope_type,
            source_type=source_type,
            memory_type=memory_type,
            status=status,
            include_deleted=False,
            limit=scan_limit,
        )
        candidates = [row for row in rows if _memory_item_missing_acceptance(row)]
        ids = [row.get("id") for row in candidates[:safe_limit]]
        if dry_run:
            return {
                "dry_run": True,
                "mark_missing_as": target_status,
                "would_affect": len(ids),
                "affected": 0,
                "ids_preview": ids[:MEMORY_ACCEPTANCE_ID_PREVIEW_LIMIT],
                "ids_truncated": max(0, len(ids) - MEMORY_ACCEPTANCE_ID_PREVIEW_LIMIT),
                "limit": safe_limit,
            }

        affected_ids: list[int] = []
        skipped_ids: list[Any] = []
        reviewed_at = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"
        actor = _normalize_line(str(reviewed_by or "admin_backfill"))[:120] or "admin_backfill"
        reason = f"legacy_acceptance_backfill:{target_status}"
        for row in candidates[:safe_limit]:
            item_id = _safe_int(row.get("id"), 0)
            if item_id <= 0:
                continue
            current = await self.get_memory_item(item_id)
            if (
                not current
                or current.get("deleted_at")
                or not _memory_item_missing_acceptance(current)
            ):
                skipped_ids.append(row.get("id"))
                continue
            value = current.get("value") if isinstance(current.get("value"), dict) else {}
            next_value = dict(value or {})
            history_entry = {
                "action": "backfill",
                "status": target_status,
                "reason": reason,
                "reviewed_at": reviewed_at,
                "reviewed_by": actor,
                "previous_status": "",
                "previous_acceptance_status": "",
                "previous_item_status": str(current.get("status") or ""),
                "current_item_status": _memory_status_for_acceptance(
                    target_status,
                    sensitivity=str(current.get("sensitivity") or "normal"),
                ),
            }
            next_value["acceptance"] = {
                "status": target_status,
                "reason": reason,
                "reviewed_at": reviewed_at,
                "reviewed_by": actor,
                "review_reason": reason,
                "history": [history_entry],
            }
            updated = await self.update_memory_item(
                item_id,
                value_json=next_value,
                status=history_entry["current_item_status"],
            )
            if updated and updated.get("acceptance_status") == target_status:
                affected_ids.append(item_id)
            else:
                skipped_ids.append(item_id)

        return {
            "dry_run": False,
            "mark_missing_as": target_status,
            "would_affect": len(ids),
            "affected": len(affected_ids),
            "ids": affected_ids[:MEMORY_ACCEPTANCE_ID_PREVIEW_LIMIT],
            "ids_truncated": max(0, len(affected_ids) - MEMORY_ACCEPTANCE_ID_PREVIEW_LIMIT),
            "skipped_ids": skipped_ids[:MEMORY_ACCEPTANCE_ID_PREVIEW_LIMIT],
            "skipped_truncated": max(0, len(skipped_ids) - MEMORY_ACCEPTANCE_ID_PREVIEW_LIMIT),
            "limit": safe_limit,
        }

    async def create_memory_item(self, **kwargs: Any) -> dict[str, Any] | None:
        item = await self._insert_or_touch_memory_item(**kwargs)
        if item:
            await self._refresh_legacy_cache_for_item_scope(item)
            await self._sync_memory_graph_for_item_safe(item)
            await self._sync_memory_vector_for_item_safe(item)
        return item

    async def create_profile_enrichment_candidate(
        self,
        *,
        tenant_id: str,
        channel: str = "wechat",
        source_key: str = "wxbot",
        session_id: str,
        user_id: str,
        report_payload: dict[str, Any],
        created_by: str = "",
        require_history_owner: bool = False,
    ) -> dict[str, Any] | None:
        preliminary_payload = _redact_profile_enrichment_payload(report_payload)
        if not isinstance(preliminary_payload, dict):
            preliminary_payload = {"report": preliminary_payload}
        review = (
            preliminary_payload.get("review")
            if isinstance(preliminary_payload.get("review"), dict)
            else {}
        )
        requested_state = _profile_enrichment_allowed_status(
            review.get("state"), default="candidate"
        )
        initial_state = (
            "needs_review" if requested_state in {"accepted", "needs_review"} else "candidate"
        )
        safe_payload = _prepare_profile_enrichment_report_for_storage(
            preliminary_payload,
            initial_state=initial_state,
        )
        review = safe_payload.get("review") if isinstance(safe_payload.get("review"), dict) else {}
        report_hash = hashlib.sha1(_to_json(safe_payload).encode("utf-8")).hexdigest()
        candidate_key_payload = {
            "tenant_id": tenant_id,
            "channel": channel,
            "source_key": source_key,
            "session_id": session_id,
            "user_id": user_id,
            "profile_id": (safe_payload.get("profile") or {}).get("profile_id")
            if isinstance(safe_payload.get("profile"), dict)
            else "",
            "query": (safe_payload.get("target") or {}).get("query")
            if isinstance(safe_payload.get("target"), dict)
            else "",
            "report_hash": report_hash,
        }
        candidate_key = hashlib.sha1(_to_json(candidate_key_payload).encode("utf-8")).hexdigest()
        created_at = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"
        value_payload = {
            "kind": PROFILE_ENRICHMENT_MEMORY_TYPE,
            "schema_version": "profile-enrichment-candidate.v1",
            "candidate_key": candidate_key,
            "report": safe_payload,
            "review": {
                **review,
                "state": initial_state,
                "created_by": _redact_profile_enrichment_text(created_by)[:120],
                "created_at": created_at,
            },
            "acceptance": {
                "status": initial_state,
                "score": _clamp_score(safe_payload.get("confidence"), 0.0),
                "reason": "profile_enrichment_candidate_created",
                "extraction_confidence": _clamp_score(safe_payload.get("confidence"), 0.0),
                "history": [],
            },
        }
        if require_history_owner:
            gate = getattr(self, "combined_history_scope_execution_allowed", None)
            if not callable(gate) or (
                await gate(str(tenant_id or ""), str(session_id or ""))
                is not True
            ):
                raise RuntimeError(
                    "memory/wxbot plugin runtime disabled for profile enrichment"
                )
        elif bool(getattr(self, "runtime_scope_gates_required", False)):
            gate = getattr(self, "scope_execution_allowed", None)
            if not callable(gate) or (
                await gate(str(tenant_id or ""), str(session_id or ""))
                is not True
            ):
                raise RuntimeError("memory plugin runtime disabled for profile enrichment")
        item = await self._insert_or_touch_memory_item(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            scope_type="session",
            source_type=PROFILE_ENRICHMENT_SOURCE_TYPE,
            memory_type=PROFILE_ENRICHMENT_MEMORY_TYPE,
            content=_profile_enrichment_content(
                {**safe_payload, "review": value_payload["review"]}
            ),
            value_json=value_payload,
            normalized_key=f"profile_enrichment:{candidate_key}",
            confidence=_clamp_score(safe_payload.get("confidence"), 0.0),
            status="pending",
            pinned=False,
            priority=0,
            sensitivity="normal",
            original_text="",
        )
        return item

    async def list_profile_enrichment_candidates(
        self,
        *,
        tenant_id: str,
        channel: str | None = None,
        source_key: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        review_state: str | None = None,
        include_hidden: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions, params = self._memory_item_filter_conditions(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            scope_type="session",
            source_type=PROFILE_ENRICHMENT_SOURCE_TYPE,
            memory_type=PROFILE_ENRICHMENT_MEMORY_TYPE,
            include_deleted=False,
        )
        params["lim"] = max(1, min(int(limit or 100), 500))
        rows = await _exec(
            "SELECT id, tenant_id, channel, source_key, user_id, session_id, scope_type, source_type, "
            "memory_type, content, value_json, normalized_key, confidence, status, pinned, priority, "
            "sensitivity, audience_scope, origin_session_kind, allowed_session_ids, source_kind, "
            "sensitivity_category, expires_at, source_event_id, source_trace_id, original_text, occurrence_count, "
            "first_seen_at, last_seen_at, created_at, updated_at, deleted_at "
            "FROM plugin_memory_item "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY updated_at DESC LIMIT :lim",
            params,
        )
        items = [
            self._finalize_memory_item(row) for row in rows if _looks_like_memory_item_row(row)
        ]
        state_filter = (
            _profile_enrichment_allowed_status(review_state, default="") if review_state else ""
        )
        filtered: list[dict[str, Any]] = []
        for item in items:
            state = str(item.get("acceptance_status") or "").strip().lower()
            if state_filter and state != state_filter:
                continue
            if not include_hidden and state == "hidden":
                continue
            filtered.append(item)
        return filtered

    async def get_profile_enrichment_candidate(self, candidate_id: int) -> dict[str, Any] | None:
        item = await self.get_memory_item(candidate_id)
        if not item or item.get("deleted_at"):
            return None
        if str(item.get("source_type") or "") != PROFILE_ENRICHMENT_SOURCE_TYPE:
            return None
        if str(item.get("memory_type") or "") != PROFILE_ENRICHMENT_MEMORY_TYPE:
            return None
        return item

    async def review_profile_enrichment_candidate(
        self,
        candidate_id: int,
        *,
        action: str,
        notes: str = "",
        reviewed_by: str = "",
    ) -> dict[str, Any] | None:
        current = await self.get_profile_enrichment_candidate(candidate_id)
        if not current:
            return None
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in PROFILE_ENRICHMENT_REVIEW_ACTIONS:
            raise ValueError(f"unsupported profile enrichment review action: {normalized_action}")
        next_state = {
            "accept": "accepted",
            "reject": "rejected",
            "hide": "hidden",
        }[normalized_action]
        value = current.get("value") if isinstance(current.get("value"), dict) else {}
        next_value = dict(value or {})
        review = next_value.get("review") if isinstance(next_value.get("review"), dict) else {}
        acceptance = (
            next_value.get("acceptance") if isinstance(next_value.get("acceptance"), dict) else {}
        )
        previous_state = _profile_enrichment_allowed_status(
            review.get("state") or acceptance.get("status") or current.get("acceptance_status"),
            default="candidate",
        )
        actor = _redact_profile_enrichment_text(reviewed_by)[:120] or "admin/api"
        safe_notes = _redact_profile_enrichment_text(notes)[:240]
        reviewed_at = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"
        history = acceptance.get("history") if isinstance(acceptance.get("history"), list) else []
        history_entry = {
            "action": normalized_action,
            "status": next_state,
            "previous_status": previous_state,
            "notes": safe_notes,
            "reviewed_by": actor,
            "reviewed_at": reviewed_at,
        }
        next_review = {
            **review,
            "state": next_state,
            "notes": safe_notes,
            "reviewed_by": actor,
            "reviewed_at": reviewed_at,
        }
        next_acceptance = {
            **acceptance,
            "status": next_state,
            "reason": safe_notes or f"profile_enrichment_{normalized_action}",
            "review_reason": safe_notes,
            "reviewed_by": actor,
            "reviewed_at": reviewed_at,
            "previous_status": previous_state,
            "history": [*history, history_entry][-MEMORY_ACCEPTANCE_HISTORY_LIMIT:],
        }
        next_value["review"] = next_review
        next_value["acceptance"] = next_acceptance
        item_status = "active" if next_state == "accepted" else "pending"
        updated = await self.update_memory_item(
            candidate_id, value_json=next_value, status=item_status
        )
        if updated:
            await self.append_memory_acceptance_audit(
                item=current,
                action=f"profile_{normalized_action}",
                previous_status=previous_state,
                new_status=next_state,
                previous_item_status=str(current.get("status") or ""),
                new_item_status=item_status,
                reviewed_by=actor,
                reason=(
                    f"sha256:{hashlib.sha256(safe_notes.encode('utf-8')).hexdigest()}"
                    if safe_notes
                    else ""
                ),
                reviewed_at=reviewed_at,
            )
        return updated

    async def update_memory_item(self, item_id: int, **updates: Any) -> dict[str, Any] | None:
        current = await self.get_memory_item(item_id)
        if not current or current.get("deleted_at"):
            return None
        allowed = {
            "content",
            "value_json",
            "memory_type",
            "confidence",
            "status",
            "pinned",
            "priority",
            "sensitivity",
            "original_text",
        }
        assignments: list[str] = []
        params: dict[str, Any] = {"id": item_id}
        if "content" in updates:
            updates["content"] = _normalize_line(str(updates.get("content") or ""))[:500]
            updates["normalized_key"] = _normalize_key(str(updates["content"]))
            allowed.add("normalized_key")
        for key, value in updates.items():
            if key not in allowed:
                continue
            column_value = _to_json(value) if key == "value_json" else value
            assignments.append(f"{key} = :{key}")
            params[key] = column_value
        if not assignments:
            return current
        assignments.append("updated_at = NOW()")
        try:
            await _exec(
                f"UPDATE plugin_memory_item SET {', '.join(assignments)} WHERE id = :id",
                params,
            )
        except IntegrityError as exc:
            raise MemoryItemConflictError("memory item dedupe conflict") from exc
        updated = await self.get_memory_item(item_id)
        if updated:
            await self._refresh_legacy_cache_for_item_scope(updated)
            await self._sync_memory_graph_for_item_safe(updated)
            await self._sync_memory_vector_for_item_safe(updated)
        return updated

    async def append_memory_acceptance_audit(
        self,
        *,
        item: dict[str, Any],
        action: str,
        previous_status: str,
        new_status: str,
        previous_item_status: str,
        new_item_status: str,
        reviewed_by: str,
        reason: str,
        reviewed_at: str | datetime | None = None,
        superseded_by_item_id: int | None = None,
        supersedes_item_id: int | None = None,
    ) -> None:
        item_id = _safe_int(item.get("id"), 0)
        if item_id <= 0:
            return
        await _exec(
            "INSERT INTO plugin_memory_acceptance_audit "
            "(item_id, tenant_id, channel, source_key, user_id, session_id, scope_type, source_type, "
            "action, previous_status, new_status, previous_item_status, new_item_status, "
            "reviewed_by, actor, reason, superseded_by_item_id, supersedes_item_id, reviewed_at, created_at) "
            "VALUES (:item_id, :tenant_id, :channel, :source_key, :user_id, :session_id, :scope_type, :source_type, "
            ":action, :previous_status, :new_status, :previous_item_status, :new_item_status, "
            ":reviewed_by, :actor, :reason, :superseded_by_item_id, :supersedes_item_id, "
            "COALESCE(CAST(:reviewed_at AS TIMESTAMP), NOW()), NOW())",
            {
                "item_id": item_id,
                "tenant_id": str(item.get("tenant_id") or ""),
                "channel": str(item.get("channel") or ""),
                "source_key": str(item.get("source_key") or "*"),
                "user_id": str(item.get("user_id") or ""),
                "session_id": str(item.get("session_id") or ""),
                "scope_type": str(item.get("scope_type") or ""),
                "source_type": str(item.get("source_type") or ""),
                "action": str(action or "")[:32],
                "previous_status": str(previous_status or "")[:32],
                "new_status": str(new_status or "")[:32],
                "previous_item_status": str(previous_item_status or "")[:32],
                "new_item_status": str(new_item_status or "")[:32],
                "reviewed_by": str(reviewed_by or "")[:128],
                "actor": str(reviewed_by or "")[:128],
                "reason": str(reason or "")[:1000],
                "superseded_by_item_id": superseded_by_item_id,
                "supersedes_item_id": supersedes_item_id,
                "reviewed_at": reviewed_at,
            },
        )

    async def review_memory_item_acceptance(
        self,
        item_id: int,
        *,
        action: str,
        review_reason: str = "",
        reviewed_by: str = "",
        superseded_by_item_id: int | None = None,
        supersedes_item_id: int | None = None,
    ) -> dict[str, Any] | None:
        current = await self.get_memory_item(item_id)
        if not current or current.get("deleted_at"):
            return None
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in MEMORY_ACCEPTANCE_REVIEW_ACTIONS:
            raise ValueError(f"unsupported acceptance review action: {normalized_action}")
        superseded_by_id = (
            _safe_int(superseded_by_item_id, 0) if superseded_by_item_id is not None else 0
        )
        supersedes_id = _safe_int(supersedes_item_id, 0) if supersedes_item_id is not None else 0
        if superseded_by_id == item_id or supersedes_id == item_id:
            raise ValueError("memory item cannot supersede itself")

        value = current.get("value") if isinstance(current.get("value"), dict) else {}
        next_value = dict(value or {})
        existing_acceptance = (
            next_value.get("acceptance") if isinstance(next_value.get("acceptance"), dict) else {}
        )
        acceptance = dict(existing_acceptance or {})
        previous_status = str(acceptance.get("status") or current.get("acceptance_status") or "")
        next_status = (
            "accepted"
            if normalized_action == "supersede" and supersedes_id > 0
            else _acceptance_status_for_review_action(normalized_action)
        )
        reason = _normalize_line(str(review_reason or ""))
        if normalized_action == "mark_joke" and not reason:
            reason = "joking_or_hyperbole"
        reviewed_at = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"
        actor = _normalize_line(str(reviewed_by or "admin/api"))[:120] or "admin/api"

        history = acceptance.get("history") if isinstance(acceptance.get("history"), list) else []
        history_entry = {
            "action": normalized_action,
            "status": next_status,
            "reason": reason[:240],
            "reviewed_at": reviewed_at,
            "reviewed_by": actor,
            "previous_status": previous_status,
            "previous_acceptance_status": previous_status,
            "previous_item_status": str(current.get("status") or ""),
            "current_item_status": _memory_status_for_acceptance(
                next_status,
                sensitivity=str(current.get("sensitivity") or "normal"),
            ),
        }
        if superseded_by_id > 0:
            history_entry["superseded_by_item_id"] = superseded_by_id
        if supersedes_id > 0:
            history_entry["supersedes_item_id"] = supersedes_id

        acceptance.update(
            {
                "status": next_status,
                "reviewed_at": reviewed_at,
                "reviewed_by": actor,
                "review_reason": reason[:240],
                "previous_status": previous_status,
                "history": [*history, history_entry][-MEMORY_ACCEPTANCE_HISTORY_LIMIT:],
            }
        )
        if superseded_by_id > 0:
            acceptance["superseded_by_item_id"] = superseded_by_id
        if supersedes_id > 0:
            acceptance["supersedes_item_id"] = supersedes_id
        if normalized_action == "mark_joke":
            acceptance["reason"] = reason[:240] or "joking_or_hyperbole"
            signals = (
                acceptance.get("signals") if isinstance(acceptance.get("signals"), dict) else {}
            )
            acceptance["signals"] = {
                **signals,
                "joke_score": max(_clamp_score(signals.get("joke_score")), 1.0),
            }
        elif reason and not str(acceptance.get("reason") or "").strip():
            acceptance["reason"] = reason[:240]

        next_value["acceptance"] = acceptance
        item_status = _memory_status_for_acceptance(
            next_status, sensitivity=str(current.get("sensitivity") or "normal")
        )
        updated = await self.update_memory_item(
            item_id,
            value_json=next_value,
            status=item_status,
        )
        if updated:
            await self.append_memory_acceptance_audit(
                item=current,
                action=normalized_action,
                previous_status=previous_status,
                new_status=next_status,
                previous_item_status=str(current.get("status") or ""),
                new_item_status=item_status,
                reviewed_by=actor,
                reason=(
                    f"sha256:{hashlib.sha256(reason.encode('utf-8')).hexdigest()}" if reason else ""
                ),
                reviewed_at=reviewed_at,
                superseded_by_item_id=superseded_by_id if superseded_by_id > 0 else None,
                supersedes_item_id=supersedes_id if supersedes_id > 0 else None,
            )
        if normalized_action == "supersede" and supersedes_id > 0 and updated:
            counterpart = await self.get_memory_item(supersedes_id)
            if (
                counterpart
                and not counterpart.get("deleted_at")
                and _memory_item_matches_scope(
                    counterpart,
                    tenant_id=str(current.get("tenant_id") or ""),
                    channel=str(current.get("channel") or ""),
                    source_key=str(current.get("source_key") or ""),
                    user_id=str(current.get("user_id") or ""),
                    session_id=str(current.get("session_id") or ""),
                )
            ):
                counterpart_scope_matches = str(counterpart.get("scope_type") or "") == str(
                    current.get("scope_type") or ""
                )
                if counterpart_scope_matches:
                    counterpart_value = (
                        counterpart.get("value")
                        if isinstance(counterpart.get("value"), dict)
                        else {}
                    )
                    next_counterpart_value = dict(counterpart_value or {})
                    counterpart_acceptance = (
                        next_counterpart_value.get("acceptance")
                        if isinstance(next_counterpart_value.get("acceptance"), dict)
                        else {}
                    )
                    next_counterpart_acceptance = dict(counterpart_acceptance or {})
                    counterpart_previous_status = str(
                        next_counterpart_acceptance.get("status")
                        or counterpart.get("acceptance_status")
                        or ""
                    )
                    counterpart_history = (
                        next_counterpart_acceptance.get("history")
                        if isinstance(next_counterpart_acceptance.get("history"), list)
                        else []
                    )
                    counterpart_item_status = _memory_status_for_acceptance(
                        "superseded",
                        sensitivity=str(counterpart.get("sensitivity") or "normal"),
                    )
                    counterpart_history_entry = {
                        "action": "supersede",
                        "status": "superseded",
                        "reason": reason[:240],
                        "reviewed_at": reviewed_at,
                        "reviewed_by": actor,
                        "previous_status": counterpart_previous_status,
                        "previous_acceptance_status": counterpart_previous_status,
                        "previous_item_status": str(counterpart.get("status") or ""),
                        "current_item_status": counterpart_item_status,
                        "superseded_by_item_id": item_id,
                    }
                    next_counterpart_acceptance.update(
                        {
                            "status": "superseded",
                            "reviewed_at": reviewed_at,
                            "reviewed_by": actor,
                            "review_reason": reason[:240],
                            "previous_status": counterpart_previous_status,
                            "history": [*counterpart_history, counterpart_history_entry][
                                -MEMORY_ACCEPTANCE_HISTORY_LIMIT:
                            ],
                            "superseded_by_item_id": item_id,
                        }
                    )
                    next_counterpart_value["acceptance"] = next_counterpart_acceptance
                    await self.update_memory_item(
                        supersedes_id,
                        value_json=next_counterpart_value,
                        status=counterpart_item_status,
                    )
                    await self.append_memory_acceptance_audit(
                        item=counterpart,
                        action="supersede",
                        previous_status=counterpart_previous_status,
                        new_status="superseded",
                        previous_item_status=str(counterpart.get("status") or ""),
                        new_item_status=counterpart_item_status,
                        reviewed_by=actor,
                        reason=(
                            f"sha256:{hashlib.sha256(reason.encode('utf-8')).hexdigest()}"
                            if reason
                            else ""
                        ),
                        reviewed_at=reviewed_at,
                        superseded_by_item_id=item_id,
                        supersedes_item_id=None,
                    )
        return updated

    async def update_memory_item_scoped(
        self,
        item_id: int,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        session_id: str = "",
        **updates: Any,
    ) -> dict[str, Any] | None:
        current = await self.get_memory_item(item_id)
        if not current or current.get("deleted_at"):
            return None
        if not _memory_item_matches_scope(
            current,
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
        ):
            return None
        return await self.update_memory_item(item_id, **updates)

    async def soft_delete_memory_item(
        self,
        item_id: int,
        *,
        allow_pinned: bool = False,
    ) -> dict[str, Any] | None:
        current = await self.get_memory_item(item_id)
        if not current:
            return None
        if not allow_pinned and _memory_item_requires_delete_confirmation(current):
            raise MemoryItemProtectedError([item_id])
        await _exec(
            "UPDATE plugin_memory_item SET status = 'deleted', deleted_at = NOW(), updated_at = NOW() "
            "WHERE id = :id AND deleted_at IS NULL",
            {"id": item_id},
        )
        deleted = await self.get_memory_item(item_id)
        await self._refresh_legacy_cache_for_item_scope(current)
        await self._sync_memory_graph_for_item_safe(deleted)
        await self._delete_memory_vector_for_item_safe(item_id)
        return deleted

    async def forget_memory_items(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        item_id: int | None = None,
        query: str = "",
        session_id: str = "",
        scope_type: str | None = None,
        allow_pinned: bool = False,
        limit: int = 20,
    ) -> dict[str, Any]:
        if item_id is None and not _normalize_line(query):
            return {"ids": [], "count": 0}
        candidates: list[dict[str, Any]]
        if item_id is not None:
            item = await self.get_memory_item(item_id)
            if (
                not item
                or item.get("deleted_at")
                or str(item.get("status") or "") == "deleted"
                or not _memory_item_matches_scope(
                    item,
                    tenant_id=tenant_id,
                    channel=channel,
                    source_key=source_key,
                    user_id=user_id,
                    session_id=session_id,
                )
            ):
                return {"ids": [], "count": 0}
            if scope_type and str(item.get("scope_type") or "") != scope_type:
                return {"ids": [], "count": 0}
            candidates = [item]
        else:
            candidates = await self.retrieve_memory_items(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                session_id=session_id,
                query=query,
                limit=min(max(int(limit or 20), 1), 20),
            )
            candidates = [
                item
                for item in candidates
                if _memory_item_matches_scope(
                    item,
                    tenant_id=tenant_id,
                    channel=channel,
                    source_key=source_key,
                    user_id=user_id,
                    session_id=session_id,
                )
            ]
            if scope_type:
                candidates = [
                    item for item in candidates if str(item.get("scope_type") or "") == scope_type
                ]

        protected_ids = [
            int(item["id"])
            for item in candidates
            if item.get("id") is not None and _memory_item_requires_delete_confirmation(item)
        ]
        if protected_ids and not allow_pinned:
            raise MemoryItemProtectedError(protected_ids)

        affected_ids: list[int] = []
        refresh_scopes: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
        for item in candidates:
            candidate_id = item.get("id")
            if candidate_id is None:
                continue
            rows = await _exec(
                "UPDATE plugin_memory_item SET status = 'deleted', deleted_at = NOW(), updated_at = NOW() "
                "WHERE id = :id AND tenant_id = :tid AND channel = :channel AND source_key = :source_key "
                "AND user_id = :uid AND session_id = :sid AND deleted_at IS NULL "
                "RETURNING id",
                {
                    "id": int(candidate_id),
                    "tid": tenant_id,
                    "channel": channel,
                    "source_key": source_key,
                    "uid": user_id,
                    "sid": str(item.get("session_id") or ""),
                },
            )
            if not rows:
                continue
            affected_ids.append(int(candidate_id))
            deleted_item = await self.get_memory_item(int(candidate_id))
            await self._sync_memory_graph_for_item_safe(deleted_item)
            await self._delete_memory_vector_for_item_safe(candidate_id)
            scope_key = (
                str(item.get("tenant_id") or ""),
                str(item.get("channel") or ""),
                str(item.get("source_key") or ""),
                str(item.get("user_id") or ""),
                str(item.get("session_id") or ""),
                str(item.get("scope_type") or ""),
            )
            refresh_scopes[scope_key] = item

        for item in refresh_scopes.values():
            await self._refresh_legacy_cache_for_item_scope(item)
        return {"ids": affected_ids, "count": len(affected_ids)}

    async def forget_member(
        self,
        *,
        tenant_id: str,
        session_id: str,
        user_id: str,
        idempotency_key: str,
    ) -> int:
        """Physically erase every WeChat memory for one tenant/member.

        The durable social control is committed before this compensation is
        invoked.  Database content and every known vector projection must be
        gone before the handler may report completion.  Save and erase share
        a member-scoped transaction lock, preventing an in-flight save from
        recreating memory after the final scan.
        """

        _ = session_id, idempotency_key
        tenant = str(tenant_id or "").strip()
        member = str(user_id or "").strip()
        if not tenant or not member:
            raise ValueError("tenant_id and user_id are required")
        if _ACTIVE_MUTATION_CONNECTION.get() is None:
            async with self._mutation_transaction():
                return await self.forget_member(
                    tenant_id=tenant,
                    session_id=session_id,
                    user_id=member,
                    idempotency_key=idempotency_key,
                )

        await self._lock_member_memory_mutation(
            tenant_id=tenant,
            user_id=member,
        )
        scope = {"tid": tenant, "uid": member}
        item_rows = await _exec(
            "SELECT id FROM plugin_memory_item "
            "WHERE tenant_id = :tid AND channel = 'wechat' AND user_id = :uid "
            "ORDER BY id ASC",
            scope,
        )
        fact_rows = await _exec(
            "SELECT id FROM plugin_memory_fact "
            "WHERE tenant_id = :tid AND channel = 'wechat' AND user_id = :uid "
            "ORDER BY id ASC",
            scope,
        )
        episode_rows = await _exec(
            "SELECT id FROM plugin_memory_episode "
            "WHERE tenant_id = :tid AND channel = 'wechat' AND user_id = :uid "
            "ORDER BY id ASC",
            scope,
        )

        # Vector deletion is strict for privacy compensation.  A disabled
        # indexing feature may still have residue from an earlier deployment,
        # so force uses any configured vector store without requiring embeds.
        for row in item_rows:
            await self.vector_index.delete_item(row.get("id"), force=True)
        for row in fact_rows:
            await self.vector_index.delete_fact(row.get("id"), force=True)
        for row in episode_rows:
            await self.vector_index.delete_episode(row.get("id"), force=True)

        # Delete content-bearing graph projections before their source rows.
        # Every statement repeats the tenant/channel/member scope; ids are
        # inventory for vector cleanup, never authorization.
        for table in (
            "plugin_memory_fact",
            "plugin_memory_episode",
            "plugin_memory_entity",
            "plugin_memory_acceptance_audit",
            "plugin_memory_extraction_job",
            "plugin_memory_event",
            "plugin_memory_item",
            "plugin_memory_identity_profile",
            "plugin_memory_session_profile",
        ):
            await _exec(
                f"DELETE FROM {table} "
                "WHERE tenant_id = :tid AND channel = 'wechat' AND user_id = :uid",
                scope,
            )

        # One-release cleanup for databases that still retain the pre-split
        # profile table.  Avoid referencing it when it no longer exists.
        active_connection = _ACTIVE_MUTATION_CONNECTION.get()
        if (
            active_connection is not None
            and str(active_connection.dialect.name or "").lower() == "postgresql"
        ):
            legacy_table = await _exec(
                "SELECT to_regclass('plugin_memory_profile') AS table_name"
            )
            if legacy_table and legacy_table[0].get("table_name"):
                await _exec(
                    "DELETE FROM plugin_memory_profile "
                    "WHERE tenant_id = :tid AND channel = 'wechat' AND user_id = :uid",
                    scope,
                )
        return len(item_rows)

    async def resolve_member_fact_correction(
        self,
        *,
        tenant_id: str,
        session_id: str,
        user_id: str,
        correction_text: str,
        idempotency_key: str,
    ) -> MemberMemoryCorrectionResolution:
        """Resolve a correction to one scoped fact or request confirmation.

        Recency is only a tie-breaker.  A correction without an entity/property
        anchor never silently deletes the latest unrelated fact.
        """

        tenant = str(tenant_id or "").strip()
        session = str(session_id or "").strip()
        member = str(user_id or "").strip()
        operation_key = str(idempotency_key or "").strip()[:256]
        if not tenant or not member or not operation_key:
            raise ValueError("tenant_id, user_id and idempotency_key are required")
        if _ACTIVE_MUTATION_CONNECTION.get() is None:
            async with self._mutation_transaction():
                return await self.resolve_member_fact_correction(
                    tenant_id=tenant,
                    session_id=session,
                    user_id=member,
                    correction_text=correction_text,
                    idempotency_key=operation_key,
                )

        # Candidate selection, the final owner-policy check and invalidation
        # form one member-scoped transaction.  This prevents a concurrent save,
        # erase or correction from changing the target between read and write.
        await self._lock_member_memory_mutation(
            tenant_id=tenant,
            user_id=member,
        )
        operation_marker = (
            operation_key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        replay = await _exec(
            "SELECT id FROM plugin_memory_item WHERE tenant_id = :tid "
            "AND channel = 'wechat' AND user_id = :uid "
            "AND status = 'invalidated' "
            "AND CAST(value_json AS TEXT) LIKE :operation_marker ESCAPE '\\' "
            "ORDER BY id DESC LIMIT 1",
            {
                "tid": tenant,
                "uid": member,
                # The operation key is stored inside the content-free
                # invalidation metadata.  Searching that marker preserves the
                # original source trace while making correction retries stable.
                "operation_marker": f"%{operation_marker}%",
            },
        )
        if replay:
            return MemberMemoryCorrectionResolution(
                status="applied",
                changed=1,
                candidate_count=1,
            )
        properties, values = _correction_match_terms(correction_text)
        rows = await _exec(
            "SELECT id, tenant_id, channel, user_id, session_id, memory_type, content, "
            "normalized_key, updated_at FROM plugin_memory_item WHERE tenant_id = :tid "
            "AND channel = 'wechat' AND user_id = :uid AND status = 'active' "
            "AND deleted_at IS NULL AND source_type <> 'manual' AND pinned = FALSE "
            "ORDER BY CASE WHEN session_id = :sid THEN 0 WHEN session_id = '' THEN 1 ELSE 2 END, "
            "updated_at DESC, id DESC LIMIT 20",
            {"tid": tenant, "uid": member, "sid": session},
        )
        if not rows:
            return MemberMemoryCorrectionResolution(status="not_found")
        if not properties and not values:
            return MemberMemoryCorrectionResolution(
                status="confirmation_required",
                candidate_count=len(rows),
            )
        scored = [
            (_correction_candidate_score(row, properties=properties, values=values), row)
            for row in rows
        ]
        scored = [item for item in scored if item[0] > 0]
        if not scored:
            return MemberMemoryCorrectionResolution(status="not_found")
        scored.sort(key=lambda item: item[0], reverse=True)
        top_score, selected = scored[0]
        if len(scored) > 1 and top_score - scored[1][0] < 2:
            return MemberMemoryCorrectionResolution(
                status="confirmation_required",
                candidate_count=len(scored),
            )
        item_id = int(selected["id"])
        await self._before_member_fact_correction_mutation(
            tenant_id=tenant,
            session_id=session,
            user_id=member,
        )
        invalidated = await self._mark_memory_item_invalidated(
            item_id,
            reason="natural_feedback_correction",
            source_event_id=None,
            source_trace_id=operation_key,
            original_text="",
            include_original_text_metadata=False,
            expected_tenant_id=tenant,
            expected_channel="wechat",
            expected_user_id=member,
            expected_updated_at=selected.get("updated_at"),
            expected_normalized_key=str(selected.get("normalized_key") or ""),
        )
        if not invalidated:
            return MemberMemoryCorrectionResolution(status="not_found")
        # Re-read the immutable identity fields before reporting success.  This
        # guards future refactors from weakening the tenant/member selection.
        if (
            str(invalidated.get("tenant_id") or "") != tenant
            or str(invalidated.get("channel") or "") != "wechat"
            or str(invalidated.get("user_id") or "") != member
        ):
            raise RuntimeError("invalidated memory escaped requested member scope")
        return MemberMemoryCorrectionResolution(
            status="applied",
            changed=1,
            candidate_count=len(scored),
        )

    async def _before_member_fact_correction_mutation(
        self,
        *,
        tenant_id: str,
        session_id: str,
        user_id: str,
    ) -> None:
        """Extension boundary for a final runtime authorization check."""

        _ = tenant_id, session_id, user_id

    async def invalidate_recent_member_fact(
        self,
        *,
        tenant_id: str,
        session_id: str,
        user_id: str,
        correction_text: str,
        idempotency_key: str,
    ) -> int:
        """Compatibility wrapper preserving the old integer return shape."""

        resolution = await self.resolve_member_fact_correction(
            tenant_id=tenant_id,
            session_id=session_id,
            user_id=user_id,
            correction_text=correction_text,
            idempotency_key=idempotency_key,
        )
        return resolution.changed

    async def _find_memory_item_by_normalized_key(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        scope_type: str,
        session_id: str = "",
        normalized_key: str,
        statuses: set[str] | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        conditions = [
            "tenant_id = :tid",
            "channel = :channel",
            "source_key = :source_key",
            "user_id = :uid",
            "scope_type = :scope_type",
            "session_id = :sid",
            "normalized_key = :normalized_key",
            "deleted_at IS NULL",
        ]
        params: dict[str, Any] = {
            "tid": tenant_id,
            "channel": channel,
            "source_key": source_key,
            "uid": user_id,
            "scope_type": scope_type,
            "sid": session_id if scope_type == "session" else "",
            "normalized_key": normalized_key,
            "lim": max(1, min(int(limit or 20), 100)),
        }
        if statuses:
            placeholders: list[str] = []
            for index, status in enumerate(sorted(statuses)):
                key = f"status_{index}"
                placeholders.append(f":{key}")
                params[key] = status
            conditions.append(f"status IN ({', '.join(placeholders)})")
        rows = await _exec(
            "SELECT id, tenant_id, channel, source_key, user_id, session_id, scope_type, source_type, "
            "memory_type, content, value_json, normalized_key, confidence, status, pinned, priority, "
            "sensitivity, audience_scope, origin_session_kind, allowed_session_ids, source_kind, "
            "sensitivity_category, expires_at, source_event_id, source_trace_id, original_text, occurrence_count, "
            "first_seen_at, last_seen_at, created_at, updated_at, deleted_at "
            "FROM plugin_memory_item "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY pinned DESC, updated_at DESC LIMIT :lim",
            params,
        )
        return [self._finalize_memory_item(row) for row in rows if _looks_like_memory_item_row(row)]

    async def _mark_memory_item_invalidated(
        self,
        item_id: int,
        *,
        reason: str,
        source_event_id: int | None,
        source_trace_id: str,
        original_text: str,
        include_original_text_metadata: bool = True,
        expected_tenant_id: str | None = None,
        expected_channel: str | None = None,
        expected_user_id: str | None = None,
        expected_updated_at: object | None = None,
        expected_normalized_key: str | None = None,
    ) -> dict[str, Any] | None:
        if _ACTIVE_MUTATION_CONNECTION.get() is None:
            async with self._mutation_transaction():
                return await self._mark_memory_item_invalidated(
                    item_id,
                    reason=reason,
                    source_event_id=source_event_id,
                    source_trace_id=source_trace_id,
                    original_text=original_text,
                    include_original_text_metadata=include_original_text_metadata,
                    expected_tenant_id=expected_tenant_id,
                    expected_channel=expected_channel,
                    expected_user_id=expected_user_id,
                    expected_updated_at=expected_updated_at,
                    expected_normalized_key=expected_normalized_key,
                )

        original_text = _sanitize_db_text(original_text)
        current = await self.get_memory_item(item_id, for_update=True)
        if not current or current.get("deleted_at"):
            return None
        if current.get("source_type") == "manual" or current.get("pinned"):
            return None
        expected_scope = (
            ("tenant_id", expected_tenant_id),
            ("channel", expected_channel),
            ("user_id", expected_user_id),
        )
        if any(
            expected is not None and str(current.get(field) or "") != expected
            for field, expected in expected_scope
        ):
            return None
        if (
            expected_updated_at is not None
            and current.get("updated_at") != expected_updated_at
        ):
            return None
        if (
            expected_normalized_key is not None
            and str(current.get("normalized_key") or "") != expected_normalized_key
        ):
            return None
        value = current.get("value")
        if not isinstance(value, dict):
            value = {}
        invalidations = value.get("invalidations")
        if not isinstance(invalidations, list):
            invalidations = []
        invalidation_metadata = {
            "reason": reason,
            "source_event_id": source_event_id,
            "source_trace_id": source_trace_id,
        }
        if include_original_text_metadata:
            invalidation_metadata["original_text"] = _normalize_line(original_text)[:500]
        invalidations.append(invalidation_metadata)
        value["invalidations"] = invalidations[-5:]
        updated = await _exec(
            "UPDATE plugin_memory_item SET status = 'invalidated', value_json = :value_json, "
            "source_trace_id = COALESCE(NULLIF(source_trace_id, ''), :source_trace_id), "
            "updated_at = NOW() "
            "WHERE id = :id AND tenant_id = :tenant_id AND channel = :channel "
            "AND user_id = :user_id AND status = :status "
            "AND source_type <> 'manual' AND pinned = FALSE AND deleted_at IS NULL "
            "RETURNING id",
            {
                "id": item_id,
                "tenant_id": str(current.get("tenant_id") or ""),
                "channel": str(current.get("channel") or ""),
                "user_id": str(current.get("user_id") or ""),
                "status": str(current.get("status") or "active"),
                "value_json": _to_json(value),
                "source_trace_id": source_trace_id or "",
            },
        )
        if not updated:
            return None
        invalidated = await self.get_memory_item(item_id)
        if invalidated:
            await self._refresh_legacy_cache_for_item_scope(invalidated)
            await self._sync_memory_graph_for_item_safe(invalidated)
            await self._delete_memory_vector_for_item_safe(invalidated)
        return invalidated

    async def _apply_structured_memory_action(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        action: dict[str, Any],
        source_event_id: int | None = None,
        source_trace_id: str = "",
        original_text: str = "",
        source_type_override: str | None = None,
        scope_type: str = "identity",
        session_id: str = "",
        origin_session_kind: str = "unknown",
        audience_scope: str = "private",
        allowed_session_ids: Iterable[str] | None = None,
        sensitivity_category: str | None = None,
        expires_at: datetime | str | None = None,
        source_kind: str = "conversation",
    ) -> dict[str, Any] | None:
        original_text = _sanitize_db_text(original_text)
        op = str(action.get("op") or "add")
        if op == "ignore":
            return None

        normalized_key = str(action.get("normalized_key") or "").strip()
        content = _normalize_line(_sanitize_db_text(action.get("content")))
        if not normalized_key or not content:
            return None

        source_type = source_type_override or str(action.get("source_type") or "auto")
        if source_type not in AUTO_SOURCE_TYPES:
            source_type = "auto"
        confidence = float(action.get("confidence") or 0.0)
        sensitivity = str(action.get("sensitivity") or _detect_sensitivity(content)).strip().lower()
        detected_sensitivity = _detect_sensitivity(content)
        if detected_sensitivity != "normal":
            sensitivity = detected_sensitivity
        elif sensitivity not in MEMORY_SENSITIVITY_CATEGORIES:
            sensitivity = "sensitive"
        requested_category = str(sensitivity_category or sensitivity).strip().lower()
        sensitivity_category = (
            sensitivity
            if sensitivity != "normal"
            else (
                requested_category
                if requested_category in MEMORY_SENSITIVITY_CATEGORIES
                else "sensitive"
            )
        )
        if sensitivity_category != "normal":
            sensitivity = sensitivity_category
        status = str(action.get("status") or "")
        reason = str(action.get("reason") or op)
        memory_type = str(action.get("memory_type") or "note")
        scope_type = scope_type if scope_type in {"identity", "session"} else "identity"
        session_id = str(session_id or "") if scope_type == "session" else ""
        audience_contract = _normalize_memory_audience_contract(
            origin_session_kind=origin_session_kind,
            audience_scope=audience_scope,
            allowed_session_ids=allowed_session_ids,
            session_id=session_id,
            expires_at=expires_at,
        )

        existing_items = await self._find_memory_item_by_normalized_key(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            scope_type=scope_type,
            session_id=session_id,
            normalized_key=normalized_key,
            statuses={"active", "pending"},
        )
        existing_items = [
            item
            for item in existing_items
            if _memory_item_matches_audience_contract(
                item,
                audience_contract,
                session_id=session_id,
            )
        ]
        protected_items = [
            item
            for item in existing_items
            if item.get("source_type") == "manual" or item.get("pinned")
        ]
        acceptance = _build_memory_acceptance_metadata(
            action=action,
            source_type=source_type,
            memory_type=memory_type,
            content=content,
            confidence=confidence,
            sensitivity=sensitivity,
            original_text=original_text,
            reason=reason,
            has_conflict=bool(protected_items),
            auto_accept_min=_settings_float(
                self.settings,
                "memory_acceptance_auto_accept_min",
                MEMORY_ACCEPTANCE_AUTO_ACCEPT_MIN,
                minimum=0.0,
                maximum=1.0,
            ),
            reject_below=_settings_float(
                self.settings,
                "memory_acceptance_reject_below",
                MEMORY_ACCEPTANCE_REJECT_BELOW,
                minimum=0.0,
                maximum=1.0,
            ),
        )
        status = _memory_status_for_acceptance(
            str(acceptance.get("status") or ""), sensitivity=sensitivity
        )
        if status not in MEMORY_ITEM_STATUSES:
            status = "pending"
        if protected_items:
            acceptance["status"] = "needs_review"
            acceptance["score"] = min(_clamp_score(acceptance.get("score")), 0.7)
            acceptance["reason"] = f"manual_or_pinned_conflict:{acceptance.get('reason') or reason}"
            signals = (
                acceptance.get("signals") if isinstance(acceptance.get("signals"), dict) else {}
            )
            signals["contradiction_score"] = max(
                _clamp_score(signals.get("contradiction_score")), 1.0
            )
            acceptance["signals"] = signals
            status = "pending"
            reason = f"manual_or_pinned_conflict:{reason}"
            if op in {"update", "invalidate"}:
                op = "add"

        MEMORY_ACCEPTANCE_DECISIONS.labels(
            status=str(acceptance.get("status") or "unknown"),
            source=source_type,
        ).inc()

        if op == "invalidate":
            target_ids = [
                int(item["id"])
                for item in existing_items
                if item.get("id") is not None
                and item.get("source_type") != "manual"
                and not item.get("pinned")
            ]
            explicit_target_id = action.get("target_item_id")
            if explicit_target_id is not None:
                target_ids.insert(0, int(explicit_target_id))
            invalidates_key = str(action.get("invalidates_normalized_key") or normalized_key)
            if invalidates_key != normalized_key:
                for item in await self._find_memory_item_by_normalized_key(
                    tenant_id=tenant_id,
                    channel=channel,
                    source_key=source_key,
                    user_id=user_id,
                    scope_type=scope_type,
                    session_id=session_id,
                    normalized_key=invalidates_key,
                    statuses={"active", "pending"},
                ):
                    if (
                        item.get("id") is not None
                        and item.get("source_type") != "manual"
                        and not item.get("pinned")
                        and _memory_item_matches_audience_contract(
                            item,
                            audience_contract,
                            session_id=session_id,
                        )
                    ):
                        target_ids.append(int(item["id"]))
            seen_target_ids: set[int] = set()
            for target_id in target_ids:
                if target_id in seen_target_ids:
                    continue
                seen_target_ids.add(target_id)
                target_item = await self.get_memory_item(target_id)
                if not target_item or not _memory_item_matches_scope(
                    target_item,
                    tenant_id=tenant_id,
                    channel=channel,
                    source_key=source_key,
                    user_id=user_id,
                    session_id=session_id,
                ):
                    continue
                if not _memory_item_matches_audience_contract(
                    target_item,
                    audience_contract,
                    session_id=session_id,
                ):
                    continue
                await self._mark_memory_item_invalidated(
                    target_id,
                    reason=reason,
                    source_event_id=source_event_id,
                    source_trace_id=source_trace_id,
                    original_text=original_text,
                )
            if content == _normalize_line(original_text) or content.startswith("用户不再"):
                return None
            op = "add"

        if op == "update":
            writable = [
                item
                for item in existing_items
                if item.get("id") is not None
                and item.get("source_type") in AUTO_SOURCE_TYPES
                and item.get("source_type") != "manual"
                and not item.get("pinned")
                and str(item.get("status") or "active") == "active"
            ]
            if writable:
                item = writable[0]
                value = item.get("value")
                if not isinstance(value, dict):
                    value = {}
                evidence = value.get("evidence")
                if not isinstance(evidence, list):
                    evidence = []
                evidence.append(
                    {
                        "source_event_id": source_event_id,
                        "source_trace_id": source_trace_id,
                        "original_text": _normalize_line(original_text)[:500],
                        "reason": reason,
                    }
                )
                value.update(
                    {
                        "last_action": "update",
                        "last_reason": reason,
                        "acceptance": acceptance,
                        "extraction_confidence": acceptance.get("extraction_confidence"),
                        "evidence": evidence[-5:],
                    }
                )
                await _exec(
                    "UPDATE plugin_memory_item SET content = :content, value_json = :value_json, "
                    "memory_type = :memory_type, confidence = GREATEST(confidence, :confidence), status = :status, "
                    "sensitivity = :sensitivity, sensitivity_category = :sensitivity_category, "
                    "source_event_id = COALESCE(source_event_id, :source_event_id), "
                    "source_trace_id = COALESCE(NULLIF(source_trace_id, ''), :source_trace_id), "
                    "original_text = COALESCE(NULLIF(original_text, ''), :original_text), "
                    "occurrence_count = occurrence_count + 1, last_seen_at = NOW(), updated_at = NOW() "
                    "WHERE id = :id AND source_type <> 'manual' AND pinned = FALSE",
                    {
                        "id": int(item["id"]),
                        "content": content[:500],
                        "value_json": _to_json(value),
                        "memory_type": memory_type,
                        "confidence": confidence,
                        "status": status,
                        "sensitivity": sensitivity,
                        "sensitivity_category": sensitivity_category,
                        "source_event_id": source_event_id,
                        "source_trace_id": source_trace_id or "",
                        "original_text": original_text or content,
                    },
                )
                updated = await self.get_memory_item(int(item["id"]))
                if updated:
                    await self._refresh_legacy_cache_for_item_scope(updated)
                    await self._sync_memory_graph_for_item_safe(updated)
                    await self._sync_memory_vector_for_item_safe(updated)
                return updated
            op = "add"

        value_payload = {
            "op": op,
            "reason": reason,
            "acceptance": acceptance,
            "extraction_confidence": acceptance.get("extraction_confidence"),
            "invalidates_normalized_key": action.get("invalidates_normalized_key"),
            "target_item_id": action.get("target_item_id"),
            "evidence": [
                {
                    "source_event_id": source_event_id,
                    "source_trace_id": source_trace_id,
                    "original_text": _normalize_line(original_text)[:500],
                    "reason": reason,
                }
            ],
        }
        item = await self._insert_or_touch_memory_item(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            scope_type=scope_type,
            session_id=session_id,
            source_type=source_type,
            memory_type=memory_type,
            content=content,
            value_json=value_payload,
            normalized_key=normalized_key,
            confidence=confidence,
            status=status,
            pinned=False,
            priority=50 if source_type == "explicit_user" else 0,
            sensitivity=sensitivity,
            origin_session_kind=origin_session_kind,
            audience_scope=audience_scope,
            allowed_session_ids=allowed_session_ids,
            sensitivity_category=sensitivity_category,
            expires_at=expires_at,
            source_kind=source_kind,
            source_event_id=source_event_id,
            source_trace_id=source_trace_id,
            original_text=original_text or content,
        )
        await self._sync_memory_graph_for_item_safe(item)
        await self._sync_memory_vector_for_item_safe(item)
        return item

    async def _refresh_legacy_cache_for_item_scope(self, item: dict[str, Any]) -> None:
        scope_type = str(item.get("scope_type") or "identity")
        tenant_id = str(item.get("tenant_id") or "")
        channel = str(item.get("channel") or "")
        source_key = str(item.get("source_key") or "*")
        user_id = str(item.get("user_id") or "")
        session_id = str(item.get("session_id") or "")
        if not tenant_id or not channel or not user_id:
            return

        if scope_type == "session":
            current = await self.get_session_profile(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                session_id=session_id,
                user_id=user_id,
            )
            items = await self.list_memory_items(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                session_id=session_id,
                scope_type="session",
                limit=500,
            )
            manual_notes = _render_session_manual_from_items(items)
            await _exec(
                "INSERT INTO plugin_memory_session_profile "
                "(tenant_id, channel, source_key, session_id, user_id, short_term_memory, "
                "manual_notes, short_term_items_json, session_summary, open_items_json, decisions_json, "
                "recent_turns_json, last_compacted_at, summary_version, message_count, imported_message_count, "
                "last_seen_at, updated_at) "
                "VALUES (:tid, :channel, :source_key, :sid, :uid, :short_term, :manual, "
                ":short_items, :session_summary, :open_items, :decisions, :recent_turns, "
                ":last_compacted_at, :summary_version, :message_count, :imported_message_count, NOW(), NOW()) "
                "ON CONFLICT (tenant_id, channel, source_key, session_id, user_id) DO UPDATE SET "
                "manual_notes = EXCLUDED.manual_notes, updated_at = EXCLUDED.updated_at",
                {
                    "tid": tenant_id,
                    "channel": channel,
                    "source_key": source_key,
                    "sid": session_id,
                    "uid": user_id,
                    "short_term": current.get("short_term_memory") or "",
                    "manual": manual_notes,
                    "short_items": _to_json(current.get("short_term_items") or []),
                    "session_summary": current.get("session_summary") or "",
                    "open_items": _to_json(current.get("open_items") or []),
                    "decisions": _to_json(current.get("decisions") or []),
                    "recent_turns": _to_json(current.get("recent_turns") or []),
                    "last_compacted_at": current.get("last_compacted_at"),
                    "summary_version": int(current.get("summary_version") or SESSION_STATE_VERSION),
                    "message_count": int(current.get("message_count") or 0),
                    "imported_message_count": int(current.get("imported_message_count") or 0),
                },
            )
            return

        current = await self.get_identity_profile(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
        )
        items = await self.list_memory_items(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id="",
            scope_type="identity",
            limit=500,
        )
        long_items, long_term_memory, manual_notes = _render_legacy_identity_from_items(items)
        await _exec(
            "INSERT INTO plugin_memory_identity_profile "
            "(tenant_id, channel, source_key, user_id, long_term_memory, manual_notes, "
            "long_term_items_json, message_count, imported_message_count, last_session_id, "
            "last_seen_at, updated_at) "
            "VALUES (:tid, :channel, :source_key, :uid, :long_term, :manual, :long_items, "
            ":message_count, :imported_message_count, :last_session_id, NOW(), NOW()) "
            "ON CONFLICT (tenant_id, channel, source_key, user_id) DO UPDATE SET "
            "long_term_memory = EXCLUDED.long_term_memory, manual_notes = EXCLUDED.manual_notes, "
            "long_term_items_json = EXCLUDED.long_term_items_json, updated_at = EXCLUDED.updated_at",
            {
                "tid": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "uid": user_id,
                "long_term": long_term_memory,
                "manual": manual_notes,
                "long_items": _to_json(long_items),
                "message_count": int(current.get("message_count") or 0),
                "imported_message_count": int(current.get("imported_message_count") or 0),
                "last_session_id": current.get("last_session_id") or "",
            },
        )

    async def _import_legacy_identity_items(self, profile: dict[str, Any]) -> None:
        tenant_id = str(profile.get("tenant_id") or "")
        channel = str(profile.get("channel") or "")
        source_key = str(profile.get("source_key") or "*")
        user_id = str(profile.get("user_id") or "")
        if not tenant_id or not channel or not user_id:
            return
        inserted = False
        for line in _split_note_lines(str(profile.get("manual_notes") or "")):
            item = await self._insert_or_touch_memory_item(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                scope_type="identity",
                source_type="manual",
                memory_type="note",
                content=line,
                confidence=1.0,
                status="active",
                pinned=True,
                priority=100,
                sensitivity=_detect_sensitivity(line),
                original_text=line,
            )
            inserted = inserted or bool(item)
            await self._sync_memory_graph_for_item_safe(item)
            await self._sync_memory_vector_for_item_safe(item)
        manual_set = {
            _normalize_line(line)
            for line in _split_note_lines(str(profile.get("manual_notes") or ""))
        }
        for line in _legacy_long_term_lines(profile):
            if _normalize_line(line) in manual_set:
                continue
            sensitivity = _detect_sensitivity(line)
            item = await self._insert_or_touch_memory_item(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                scope_type="identity",
                source_type="auto",
                memory_type="note",
                content=line,
                confidence=0.8,
                status="active" if sensitivity == "normal" else "pending",
                pinned=False,
                priority=0,
                sensitivity=sensitivity,
                original_text=line,
            )
            inserted = inserted or bool(item)
            await self._sync_memory_graph_for_item_safe(item)
            await self._sync_memory_vector_for_item_safe(item)
        if inserted:
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

    async def _import_legacy_session_items(self, profile: dict[str, Any]) -> None:
        tenant_id = str(profile.get("tenant_id") or "")
        channel = str(profile.get("channel") or "")
        source_key = str(profile.get("source_key") or "*")
        user_id = str(profile.get("user_id") or "")
        session_id = str(profile.get("session_id") or "")
        if not tenant_id or not channel or not user_id or not session_id:
            return
        inserted = False
        for line in _split_note_lines(str(profile.get("manual_notes") or "")):
            item = await self._insert_or_touch_memory_item(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                session_id=session_id,
                scope_type="session",
                source_type="manual",
                memory_type="note",
                content=line,
                confidence=1.0,
                status="active",
                pinned=True,
                priority=100,
                sensitivity=_detect_sensitivity(line),
                original_text=line,
            )
            inserted = inserted or bool(item)
            await self._sync_memory_graph_for_item_safe(item)
            await self._sync_memory_vector_for_item_safe(item)
        if inserted:
            await self._refresh_legacy_cache_for_item_scope(
                {
                    "tenant_id": tenant_id,
                    "channel": channel,
                    "source_key": source_key,
                    "user_id": user_id,
                    "session_id": session_id,
                    "scope_type": "session",
                }
            )

    async def _get_identity_row(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
    ) -> dict[str, Any]:
        params = {
            "tid": tenant_id,
            "channel": channel,
            "source_key": source_key,
            "uid": user_id,
        }
        if _needs_source_fallback(source_key):
            rows = await _exec(
                "SELECT tenant_id, channel, source_key, user_id, long_term_memory, manual_notes, "
                "long_term_items_json, message_count, imported_message_count, last_session_id, "
                "last_seen_at, updated_at "
                "FROM plugin_memory_identity_profile "
                "WHERE tenant_id = :tid AND channel = :channel "
                "AND source_key IN (:source_key, '*') AND user_id = :uid "
                "ORDER BY CASE WHEN source_key = :source_key THEN 0 ELSE 1 END, updated_at DESC "
                "LIMIT 1",
                params,
            )
        else:
            rows = await _exec(
                "SELECT tenant_id, channel, source_key, user_id, long_term_memory, manual_notes, "
                "long_term_items_json, message_count, imported_message_count, last_session_id, "
                "last_seen_at, updated_at "
                "FROM plugin_memory_identity_profile "
                "WHERE tenant_id = :tid AND channel = :channel AND source_key = :source_key AND user_id = :uid",
                params,
            )
        if rows:
            return _finalize_identity_profile(rows[0])
        return _empty_identity_profile(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
        )

    async def _get_session_row(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        params = {
            "tid": tenant_id,
            "channel": channel,
            "source_key": source_key,
            "sid": session_id,
            "uid": user_id,
        }
        if _needs_source_fallback(source_key):
            rows = await _exec(
                "SELECT tenant_id, channel, source_key, session_id, user_id, short_term_memory, "
                "manual_notes, short_term_items_json, session_summary, open_items_json, decisions_json, "
                "recent_turns_json, last_compacted_at, summary_version, message_count, imported_message_count, "
                "last_seen_at, updated_at "
                "FROM plugin_memory_session_profile "
                "WHERE tenant_id = :tid AND channel = :channel "
                "AND source_key IN (:source_key, '*') "
                "AND session_id = :sid AND user_id = :uid "
                "ORDER BY CASE WHEN source_key = :source_key THEN 0 ELSE 1 END, updated_at DESC "
                "LIMIT 1",
                params,
            )
        else:
            rows = await _exec(
                "SELECT tenant_id, channel, source_key, session_id, user_id, short_term_memory, "
                "manual_notes, short_term_items_json, session_summary, open_items_json, decisions_json, "
                "recent_turns_json, last_compacted_at, summary_version, message_count, imported_message_count, "
                "last_seen_at, updated_at "
                "FROM plugin_memory_session_profile "
                "WHERE tenant_id = :tid AND channel = :channel AND source_key = :source_key "
                "AND session_id = :sid AND user_id = :uid",
                params,
            )
        if rows:
            return _finalize_session_profile(rows[0])
        return _empty_session_profile(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            session_id=session_id,
            user_id=user_id,
        )

    async def get_identity_profile(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
    ) -> dict[str, Any]:
        return await self._get_identity_row(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
        )

    async def get_session_profile(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        session_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        return await self._get_session_row(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            session_id=session_id,
            user_id=user_id,
        )

    async def get_runtime_profile(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        session_id: str,
        user_id: str,
        request_session_kind: str | None = None,
    ) -> dict[str, Any]:
        identity = await self.get_identity_profile(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
        )
        session = await self.get_session_profile(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            session_id=session_id,
            user_id=user_id,
        )
        identity_items = await self.list_memory_items(
            tenant_id=identity["tenant_id"],
            channel=identity["channel"],
            source_key=identity["source_key"],
            user_id=identity["user_id"],
            session_id="",
            scope_type="identity",
            limit=500,
        )
        if not identity_items and (
            str(identity.get("manual_notes") or "").strip()
            or str(identity.get("long_term_memory") or "").strip()
            or _safe_json_loads(identity.get("long_term_items_json"), [])
        ):
            await self._import_legacy_identity_items(identity)
            identity_items = await self.list_memory_items(
                tenant_id=identity["tenant_id"],
                channel=identity["channel"],
                source_key=identity["source_key"],
                user_id=identity["user_id"],
                session_id="",
                scope_type="identity",
                limit=500,
            )
            identity = await self.get_identity_profile(
                tenant_id=identity["tenant_id"],
                channel=identity["channel"],
                source_key=identity["source_key"],
                user_id=identity["user_id"],
            )

        session_items = await self.list_memory_items(
            tenant_id=session["tenant_id"],
            channel=session["channel"],
            source_key=session["source_key"],
            user_id=session["user_id"],
            session_id=session["session_id"],
            scope_type="session",
            limit=500,
        )
        if not session_items and str(session.get("manual_notes") or "").strip():
            await self._import_legacy_session_items(session)
            session_items = await self.list_memory_items(
                tenant_id=session["tenant_id"],
                channel=session["channel"],
                source_key=session["source_key"],
                user_id=session["user_id"],
                session_id=session["session_id"],
                scope_type="session",
                limit=500,
            )
            session = await self.get_session_profile(
                tenant_id=session["tenant_id"],
                channel=session["channel"],
                source_key=session["source_key"],
                session_id=session["session_id"],
                user_id=session["user_id"],
            )
        effective_session_kind = str(request_session_kind or "").strip().lower()
        if effective_session_kind not in {"private", "group"}:
            effective_session_kind = "group" if _is_group_session_id(session_id) else "private"
        identity_items = [
            item
            for item in identity_items
            if _memory_item_visible_for_audience(
                item,
                session_id=session_id,
                request_session_kind=effective_session_kind,
                user_id=user_id,
            )
        ]
        session_items = [
            item
            for item in session_items
            if _memory_item_visible_for_audience(
                item,
                session_id=session_id,
                request_session_kind=effective_session_kind,
                user_id=user_id,
            )
        ]
        return _build_runtime_profile_from_items(identity, session, identity_items, session_items)

    async def get_profile(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
    ) -> dict[str, Any]:
        return await self.get_identity_profile(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
        )

    async def upsert_identity_profile(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        long_term_memory: str | None = None,
        manual_notes: str | None = None,
    ) -> dict[str, Any]:
        current = await self.get_identity_profile(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
        )
        if manual_notes is not None:
            current["manual_notes"] = manual_notes
        if long_term_memory is not None:
            current["long_term_memory"] = long_term_memory
        else:
            current["long_term_items"], current["long_term_memory"] = _merge_long_term_items(
                list(current.get("long_term_items") or []),
                [],
                str(current.get("manual_notes") or ""),
            )

        await _exec(
            "INSERT INTO plugin_memory_identity_profile "
            "(tenant_id, channel, source_key, user_id, long_term_memory, manual_notes, "
            "long_term_items_json, message_count, imported_message_count, last_session_id, "
            "last_seen_at, updated_at) "
            "VALUES (:tid, :channel, :source_key, :uid, :long_term, :manual, "
            ":long_items, :message_count, :imported_message_count, :last_session_id, NOW(), NOW()) "
            "ON CONFLICT (tenant_id, channel, source_key, user_id) DO UPDATE SET "
            "long_term_memory = EXCLUDED.long_term_memory, "
            "manual_notes = EXCLUDED.manual_notes, "
            "long_term_items_json = EXCLUDED.long_term_items_json, "
            "message_count = EXCLUDED.message_count, "
            "imported_message_count = EXCLUDED.imported_message_count, "
            "last_session_id = EXCLUDED.last_session_id, "
            "last_seen_at = EXCLUDED.last_seen_at, "
            "updated_at = EXCLUDED.updated_at",
            {
                "tid": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "uid": user_id,
                "long_term": current.get("long_term_memory") or "",
                "manual": current.get("manual_notes") or "",
                "long_items": _to_json(current.get("long_term_items") or []),
                "message_count": int(current.get("message_count") or 0),
                "imported_message_count": int(current.get("imported_message_count") or 0),
                "last_session_id": current.get("last_session_id") or "",
            },
        )
        if manual_notes is not None:
            deleted_manual = await self.list_memory_items(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                session_id="",
                scope_type="identity",
                source_type="manual",
                include_deleted=True,
                limit=500,
            )
            await _exec(
                "UPDATE plugin_memory_item SET status = 'deleted', deleted_at = NOW(), updated_at = NOW() "
                "WHERE tenant_id = :tid AND channel = :channel AND source_key = :source_key "
                "AND user_id = :uid AND scope_type = 'identity' AND session_id = '' "
                "AND source_type = 'manual' AND deleted_at IS NULL",
                {
                    "tid": tenant_id,
                    "channel": channel,
                    "source_key": source_key,
                    "uid": user_id,
                },
            )
            for item in deleted_manual:
                if item.get("deleted_at") is None and str(item.get("status") or "") != "deleted":
                    await self._sync_memory_graph_for_item_safe({**item, "status": "deleted"})
                    await self._delete_memory_vector_for_item_safe(item)
            for line in _split_note_lines(manual_notes):
                item = await self._insert_or_touch_memory_item(
                    tenant_id=tenant_id,
                    channel=channel,
                    source_key=source_key,
                    user_id=user_id,
                    scope_type="identity",
                    source_type="manual",
                    memory_type="note",
                    content=line,
                    confidence=1.0,
                    status="active",
                    pinned=True,
                    priority=100,
                    sensitivity=_detect_sensitivity(line),
                    original_text=line,
                )
                await self._sync_memory_graph_for_item_safe(item)
                await self._sync_memory_vector_for_item_safe(item)
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
        elif long_term_memory is not None:
            await self._import_legacy_identity_items(
                {
                    **current,
                    "tenant_id": tenant_id,
                    "channel": channel,
                    "source_key": source_key,
                    "user_id": user_id,
                    "long_term_memory": long_term_memory,
                }
            )
        return await self.get_identity_profile(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
        )

    async def upsert_session_profile(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        session_id: str,
        user_id: str,
        short_term_memory: str | None = None,
        manual_notes: str | None = None,
    ) -> dict[str, Any]:
        current = await self.get_session_profile(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            session_id=session_id,
            user_id=user_id,
        )
        if short_term_memory is not None:
            current["short_term_memory"] = short_term_memory
        if manual_notes is not None:
            current["manual_notes"] = manual_notes

        await _exec(
            "INSERT INTO plugin_memory_session_profile "
            "(tenant_id, channel, source_key, session_id, user_id, short_term_memory, "
            "manual_notes, short_term_items_json, session_summary, open_items_json, decisions_json, "
            "recent_turns_json, last_compacted_at, summary_version, message_count, imported_message_count, "
            "last_seen_at, updated_at) "
            "VALUES (:tid, :channel, :source_key, :sid, :uid, :short_term, :manual, "
            ":short_items, :session_summary, :open_items, :decisions, :recent_turns, "
            ":last_compacted_at, :summary_version, :message_count, :imported_message_count, NOW(), NOW()) "
            "ON CONFLICT (tenant_id, channel, source_key, session_id, user_id) DO UPDATE SET "
            "short_term_memory = EXCLUDED.short_term_memory, "
            "manual_notes = EXCLUDED.manual_notes, "
            "short_term_items_json = EXCLUDED.short_term_items_json, "
            "session_summary = EXCLUDED.session_summary, "
            "open_items_json = EXCLUDED.open_items_json, "
            "decisions_json = EXCLUDED.decisions_json, "
            "recent_turns_json = EXCLUDED.recent_turns_json, "
            "last_compacted_at = EXCLUDED.last_compacted_at, "
            "summary_version = EXCLUDED.summary_version, "
            "message_count = EXCLUDED.message_count, "
            "imported_message_count = EXCLUDED.imported_message_count, "
            "last_seen_at = EXCLUDED.last_seen_at, "
            "updated_at = EXCLUDED.updated_at",
            {
                "tid": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "sid": session_id,
                "uid": user_id,
                "short_term": current.get("short_term_memory") or "",
                "manual": current.get("manual_notes") or "",
                "short_items": _to_json(current.get("short_term_items") or []),
                "session_summary": current.get("session_summary") or "",
                "open_items": _to_json(current.get("open_items") or []),
                "decisions": _to_json(current.get("decisions") or []),
                "recent_turns": _to_json(current.get("recent_turns") or []),
                "last_compacted_at": current.get("last_compacted_at"),
                "summary_version": int(current.get("summary_version") or SESSION_STATE_VERSION),
                "message_count": int(current.get("message_count") or 0),
                "imported_message_count": int(current.get("imported_message_count") or 0),
            },
        )
        if manual_notes is not None:
            deleted_manual = await self.list_memory_items(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                session_id=session_id,
                scope_type="session",
                source_type="manual",
                include_deleted=True,
                limit=500,
            )
            await _exec(
                "UPDATE plugin_memory_item SET status = 'deleted', deleted_at = NOW(), updated_at = NOW() "
                "WHERE tenant_id = :tid AND channel = :channel AND source_key = :source_key "
                "AND user_id = :uid AND scope_type = 'session' AND session_id = :sid "
                "AND source_type = 'manual' AND deleted_at IS NULL",
                {
                    "tid": tenant_id,
                    "channel": channel,
                    "source_key": source_key,
                    "uid": user_id,
                    "sid": session_id,
                },
            )
            for item in deleted_manual:
                if item.get("deleted_at") is None and str(item.get("status") or "") != "deleted":
                    await self._sync_memory_graph_for_item_safe({**item, "status": "deleted"})
                    await self._delete_memory_vector_for_item_safe(item)
            for line in _split_note_lines(manual_notes):
                item = await self._insert_or_touch_memory_item(
                    tenant_id=tenant_id,
                    channel=channel,
                    source_key=source_key,
                    user_id=user_id,
                    session_id=session_id,
                    scope_type="session",
                    source_type="manual",
                    memory_type="note",
                    content=line,
                    confidence=1.0,
                    status="active",
                    pinned=True,
                    priority=100,
                    sensitivity=_detect_sensitivity(line),
                    original_text=line,
                )
                await self._sync_memory_graph_for_item_safe(item)
                await self._sync_memory_vector_for_item_safe(item)
            await self._refresh_legacy_cache_for_item_scope(
                {
                    "tenant_id": tenant_id,
                    "channel": channel,
                    "source_key": source_key,
                    "user_id": user_id,
                    "session_id": session_id,
                    "scope_type": "session",
                }
            )
        return await self.get_session_profile(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            session_id=session_id,
            user_id=user_id,
        )

    async def upsert_profile(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        short_term_memory: str | None = None,
        long_term_memory: str | None = None,
        manual_notes: str | None = None,
    ) -> dict[str, Any]:
        _ = short_term_memory  # legacy field no longer stored at identity scope
        return await self.upsert_identity_profile(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            long_term_memory=long_term_memory,
            manual_notes=manual_notes,
        )

    async def remember_interaction(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        session_id: str,
        user_text: str,
        assistant_text: str,
        trace_id: str = "",
        identity_scope: bool = True,
        origin_session_kind: str = "private",
        audience_scope: str = "private",
        allowed_session_ids: Iterable[str] | None = None,
        sensitivity_category: str = "normal",
        expires_at: datetime | str | None = None,
        source_kind: str = "conversation",
    ) -> dict[str, Any]:
        tenant_id = str(tenant_id or "").strip()
        channel = str(channel or "").strip()
        source_key = str(source_key or "*").strip() or "*"
        user_id = str(user_id or "").strip()
        session_id = str(session_id or "").strip()
        if not tenant_id or not channel or not user_id:
            raise ValueError("tenant_id, channel and user_id are required")
        if _ACTIVE_MUTATION_CONNECTION.get() is None:
            async with self._mutation_transaction():
                return await self.remember_interaction(
                    tenant_id=tenant_id,
                    channel=channel,
                    source_key=source_key,
                    user_id=user_id,
                    session_id=session_id,
                    user_text=user_text,
                    assistant_text=assistant_text,
                    trace_id=trace_id,
                    identity_scope=identity_scope,
                    origin_session_kind=origin_session_kind,
                    audience_scope=audience_scope,
                    allowed_session_ids=allowed_session_ids,
                    sensitivity_category=sensitivity_category,
                    expires_at=expires_at,
                    source_kind=source_kind,
                )

        await self._lock_member_memory_mutation(
            tenant_id=tenant_id,
            user_id=user_id,
        )
        if channel == "wechat" and await self._member_memory_write_blocked(
            tenant_id=tenant_id,
            user_id=user_id,
        ):
            logger.info(
                "memory.remember_suppressed_by_member_control",
                tenant_id=tenant_id,
                user_id=user_id,
            )
            return {
                "tenant_id": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "user_id": user_id,
                "session_id": session_id,
                "short_term_memory": "",
                "long_term_memory": "",
                "manual_notes": "",
                "short_term_items": [],
                "long_term_items": [],
                "memory_items": [],
                "message_count": 0,
            }

        user_text = _sanitize_db_text(user_text)
        assistant_text = _sanitize_db_text(assistant_text)
        allowed_session_ids = _normalize_allowed_session_ids(allowed_session_ids)
        identity = await self.get_identity_profile(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
        )
        session = await self.get_session_profile(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            session_id=session_id,
            user_id=user_id,
        )
        if identity_scope and not await self.list_memory_items(
            tenant_id=identity["tenant_id"],
            channel=identity["channel"],
            source_key=identity["source_key"],
            user_id=identity["user_id"],
            session_id="",
            scope_type="identity",
            limit=1,
        ):
            await self._import_legacy_identity_items(identity)
        if str(session.get("manual_notes") or "").strip() and not await self.list_memory_items(
            tenant_id=session["tenant_id"],
            channel=session["channel"],
            source_key=session["source_key"],
            user_id=session["user_id"],
            session_id=session["session_id"],
            scope_type="session",
            limit=1,
        ):
            await self._import_legacy_session_items(session)

        created_at = datetime.now(UTC).replace(tzinfo=None).isoformat()
        short_items = list(session.get("short_term_items") or [])
        short_items.append(
            {
                "session_id": session_id,
                "user_text": user_text[:500],
                "assistant_text": assistant_text[:500],
                "created_at": created_at,
            }
        )
        short_items = short_items[-6:]
        short_term_memory = _build_short_term_summary(short_items)
        session_state = _update_session_state(
            session,
            session_id=session_id,
            user_text=user_text,
            assistant_text=assistant_text,
            created_at=created_at,
        )
        deterministic_candidates = _extract_long_term_candidates(user_text)

        event_rows = await _exec(
            "INSERT INTO plugin_memory_event "
            "(tenant_id, channel, source_key, user_id, session_id, user_text, assistant_text, trace_id) "
            "VALUES (:tid, :channel, :source_key, :uid, :sid, :user_text, :assistant_text, :trace) "
            "RETURNING id",
            {
                "tid": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "uid": user_id,
                "sid": session_id,
                "user_text": user_text[:2000],
                "assistant_text": assistant_text[:2000],
                "trace": trace_id,
            },
        )
        source_event_id = int(event_rows[0]["id"]) if event_rows else None

        if identity_scope:
            await _exec(
                "INSERT INTO plugin_memory_identity_profile "
                "(tenant_id, channel, source_key, user_id, long_term_memory, manual_notes, "
                "long_term_items_json, message_count, imported_message_count, last_session_id, "
                "last_seen_at, updated_at) "
                "VALUES (:tid, :channel, :source_key, :uid, :long_term, :manual, :long_items, "
                ":message_count, :imported_message_count, :last_session_id, NOW(), NOW()) "
                "ON CONFLICT (tenant_id, channel, source_key, user_id) DO UPDATE SET "
                "long_term_memory = EXCLUDED.long_term_memory, "
                "manual_notes = EXCLUDED.manual_notes, "
                "long_term_items_json = EXCLUDED.long_term_items_json, "
                "message_count = plugin_memory_identity_profile.message_count + 1, "
                "imported_message_count = plugin_memory_identity_profile.imported_message_count, "
                "last_session_id = EXCLUDED.last_session_id, "
                "last_seen_at = EXCLUDED.last_seen_at, "
                "updated_at = EXCLUDED.updated_at",
                {
                    "tid": tenant_id,
                    "channel": channel,
                    "source_key": source_key,
                    "uid": user_id,
                    "long_term": identity.get("long_term_memory") or "",
                    "manual": identity.get("manual_notes") or "",
                    "long_items": _to_json(identity.get("long_term_items") or []),
                    "message_count": int(identity.get("message_count") or 0) + 1,
                    "imported_message_count": int(identity.get("imported_message_count") or 0),
                    "last_session_id": session_id,
                },
            )

        await _exec(
            "INSERT INTO plugin_memory_session_profile "
            "(tenant_id, channel, source_key, session_id, user_id, short_term_memory, "
            "manual_notes, short_term_items_json, session_summary, open_items_json, decisions_json, "
            "recent_turns_json, last_compacted_at, summary_version, message_count, imported_message_count, "
            "last_seen_at, updated_at) "
            "VALUES (:tid, :channel, :source_key, :sid, :uid, :short_term, :manual, "
            ":short_items, :session_summary, :open_items, :decisions, :recent_turns, "
            ":last_compacted_at, :summary_version, :message_count, :imported_message_count, NOW(), NOW()) "
            "ON CONFLICT (tenant_id, channel, source_key, session_id, user_id) DO UPDATE SET "
            "short_term_memory = EXCLUDED.short_term_memory, "
            "manual_notes = EXCLUDED.manual_notes, "
            "short_term_items_json = EXCLUDED.short_term_items_json, "
            "session_summary = EXCLUDED.session_summary, "
            "open_items_json = EXCLUDED.open_items_json, "
            "decisions_json = EXCLUDED.decisions_json, "
            "recent_turns_json = EXCLUDED.recent_turns_json, "
            "last_compacted_at = EXCLUDED.last_compacted_at, "
            "summary_version = EXCLUDED.summary_version, "
            "message_count = plugin_memory_session_profile.message_count + 1, "
            "imported_message_count = plugin_memory_session_profile.imported_message_count, "
            "last_seen_at = EXCLUDED.last_seen_at, "
            "updated_at = EXCLUDED.updated_at",
            {
                "tid": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "sid": session_id,
                "uid": user_id,
                "short_term": short_term_memory,
                "manual": session.get("manual_notes") or "",
                "short_items": _to_json(short_items),
                "session_summary": session_state["session_summary"],
                "open_items": _to_json(session_state["open_items"]),
                "decisions": _to_json(session_state["decisions"]),
                "recent_turns": _to_json(session_state["recent_turns"]),
                "last_compacted_at": session_state.get("last_compacted_at"),
                "summary_version": int(
                    session_state.get("summary_version") or SESSION_STATE_VERSION
                ),
                "message_count": int(session.get("message_count") or 0) + 1,
                "imported_message_count": int(session.get("imported_message_count") or 0),
            },
        )

        for candidate in deterministic_candidates:
            await self._apply_structured_memory_action(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                action=candidate,
                source_event_id=source_event_id,
                source_trace_id=trace_id,
                original_text=user_text,
                scope_type="identity" if identity_scope else "session",
                session_id="" if identity_scope else session_id,
                origin_session_kind=origin_session_kind,
                audience_scope=audience_scope,
                allowed_session_ids=allowed_session_ids,
                sensitivity_category=sensitivity_category,
                expires_at=expires_at,
                source_kind=source_kind,
            )
        if deterministic_candidates:
            await self._refresh_legacy_cache_for_item_scope(
                {
                    "tenant_id": tenant_id,
                    "channel": channel,
                    "source_key": source_key,
                    "user_id": user_id,
                    "session_id": "" if identity_scope else session_id,
                    "scope_type": "identity" if identity_scope else "session",
                }
            )

        if identity_scope and self._can_enqueue_llm_extraction_jobs():
            await self.enqueue_llm_extraction_job(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                session_id=session_id,
                trace_id=trace_id,
                source_event_id=source_event_id,
                origin_session_kind=origin_session_kind,
                audience_scope=audience_scope,
                allowed_session_ids=allowed_session_ids,
                sensitivity_category=sensitivity_category,
                expires_at=expires_at,
                source_kind=source_kind,
            )

        return await self.get_runtime_profile(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            session_id=session_id,
            user_id=user_id,
            request_session_kind=origin_session_kind,
        )

    async def list_profiles(
        self,
        *,
        tenant_id: str,
        channel: str | None = None,
        source_key: str | None = None,
        user_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id = :tid"]
        params: dict[str, Any] = {"tid": tenant_id, "lim": limit}
        if channel:
            conditions.append("channel = :channel")
            params["channel"] = channel
        if _needs_source_fallback(source_key):
            conditions.append("source_key IN (:source_key, '*')")
            params["source_key"] = source_key
        elif source_key:
            conditions.append("source_key = :source_key")
            params["source_key"] = source_key
        if user_id:
            conditions.append("user_id = :uid")
            params["uid"] = user_id
        if _needs_source_fallback(source_key):
            rows = await _exec(
                "SELECT tenant_id, channel, source_key, user_id, long_term_memory, manual_notes, "
                "message_count, imported_message_count, last_session_id, last_seen_at, updated_at "
                "FROM ("
                "SELECT tenant_id, channel, source_key, user_id, long_term_memory, manual_notes, "
                "message_count, imported_message_count, last_session_id, last_seen_at, updated_at, "
                "ROW_NUMBER() OVER ("
                "PARTITION BY tenant_id, channel, user_id "
                "ORDER BY CASE WHEN source_key = :source_key THEN 0 ELSE 1 END, updated_at DESC"
                ") AS rn "
                "FROM plugin_memory_identity_profile "
                f"WHERE {' AND '.join(conditions)}"
                ") ranked "
                "WHERE rn = 1 "
                "ORDER BY updated_at DESC LIMIT :lim",
                params,
            )
        else:
            rows = await _exec(
                "SELECT tenant_id, channel, source_key, user_id, long_term_memory, manual_notes, "
                "message_count, imported_message_count, last_session_id, last_seen_at, updated_at "
                "FROM plugin_memory_identity_profile "
                f"WHERE {' AND '.join(conditions)} "
                "ORDER BY updated_at DESC LIMIT :lim",
                params,
            )
        return rows

    async def list_session_profiles(
        self,
        *,
        tenant_id: str,
        channel: str | None = None,
        source_key: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id = :tid"]
        params: dict[str, Any] = {"tid": tenant_id, "lim": limit}
        if channel:
            conditions.append("channel = :channel")
            params["channel"] = channel
        if _needs_source_fallback(source_key):
            conditions.append("source_key IN (:source_key, '*')")
            params["source_key"] = source_key
        elif source_key:
            conditions.append("source_key = :source_key")
            params["source_key"] = source_key
        if session_id:
            conditions.append("session_id = :sid")
            params["sid"] = session_id
        if user_id:
            conditions.append("user_id = :uid")
            params["uid"] = user_id
        if _needs_source_fallback(source_key):
            return await _exec(
                "SELECT tenant_id, channel, source_key, session_id, user_id, short_term_memory, manual_notes, "
                "session_summary, open_items_json, decisions_json, recent_turns_json, "
                "last_compacted_at, summary_version, message_count, imported_message_count, last_seen_at, updated_at "
                "FROM ("
                "SELECT tenant_id, channel, source_key, session_id, user_id, short_term_memory, manual_notes, "
                "session_summary, open_items_json, decisions_json, recent_turns_json, "
                "last_compacted_at, summary_version, message_count, imported_message_count, last_seen_at, updated_at, "
                "ROW_NUMBER() OVER ("
                "PARTITION BY tenant_id, channel, session_id, user_id "
                "ORDER BY CASE WHEN source_key = :source_key THEN 0 ELSE 1 END, updated_at DESC"
                ") AS rn "
                "FROM plugin_memory_session_profile "
                f"WHERE {' AND '.join(conditions)}"
                ") ranked "
                "WHERE rn = 1 "
                "ORDER BY updated_at DESC LIMIT :lim",
                params,
            )
        return await _exec(
            "SELECT tenant_id, channel, source_key, session_id, user_id, short_term_memory, manual_notes, "
            "session_summary, open_items_json, decisions_json, recent_turns_json, "
            "last_compacted_at, summary_version, message_count, imported_message_count, last_seen_at, updated_at "
            "FROM plugin_memory_session_profile "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY updated_at DESC LIMIT :lim",
            params,
        )

    async def list_events(
        self,
        *,
        tenant_id: str,
        channel: str | None = None,
        source_key: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id = :tid"]
        params: dict[str, Any] = {"tid": tenant_id, "lim": limit}
        if channel:
            conditions.append("channel = :channel")
            params["channel"] = channel
        if _needs_source_fallback(source_key):
            conditions.append("source_key IN (:source_key, '*')")
            params["source_key"] = source_key
        elif source_key:
            conditions.append("source_key = :source_key")
            params["source_key"] = source_key
        if user_id:
            conditions.append("user_id = :uid")
            params["uid"] = user_id
        if session_id:
            conditions.append("session_id = :sid")
            params["sid"] = session_id
        return await _exec(
            "SELECT id, tenant_id, channel, source_key, user_id, session_id, "
            "user_text, assistant_text, trace_id, event_key, created_at "
            "FROM plugin_memory_event "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY created_at DESC LIMIT :lim",
            params,
        )
