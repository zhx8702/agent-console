"""
FastAPI endpoints for the wxbot plugin.

Bridge:
  GET  /bridge/status              - Bridge health and cursor position

Admin:
  GET  /admin/reply-queue/stats    - Reply queue statistics
  POST /admin/self-review/jobs/{id}/publish - Publish an approved review draft
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from typing import Any, Literal
from urllib.parse import quote
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from PIL import Image, UnidentifiedImageError
from pydantic import Field, field_validator
from sqlalchemy import or_, select

from app.admin.audit import set_admin_audit_context
from app.admin.auth_router import authenticate_admin_request
from app.agent.engine import AgentCapabilityEngine
from app.agent.scopes import DEFAULT_AGENT_SCOPE
from app.agent.store import AgentStore
from app.bus.base import MessagePublishIdempotencyConflict
from app.common.logging import get_logger
from app.common.request_models import StrictRequestModel
from app.common.safe_url import configure_http_client, safe_get
from app.common.types import (
    Channel,
    InboundEvent,
    Message,
    SessionState,
    channel_id_value,
)
from app.common.wxbot_auth import wxbot_sdk_headers
from app.egress.safe_http import safe_trusted_service_request
from app.infra.db import get_session_factory
from app.infra.redis_client import get_redis
from app.models.session import SessionRow, TurnRow
from plugins.repeater.store import (
    RepeaterConfigVersionConflictError,
    RepeaterStore,
)
from plugins.wxbot.bridge import read_bridge_runtime_status
from plugins.wxbot.media_ids import (
    InvalidMediaID,
    MediaLocator,
    issue_media_id,
    resolve_media_id,
)
from plugins.wxbot.reports import (
    WxbotReportService,
    report_llm_metadata,
)
from plugins.wxbot.self_review import (
    WxbotSelfReviewService,
)
from plugins.wxbot.store import (
    ReplyPolicyIdempotencyConflictError,
    WxbotAdminIdempotencyConflictError,
    WxbotAdminMutationBusyError,
    WxbotAdminVersionConflictError,
    WxbotPolicyVersionConflictError,
    WxbotStore,
    compose_reply_policy_aggregate,
    normalize_wxbot_event_connection_id,
    reply_policy_composite_etag,
)

logger = get_logger(__name__)
_HANDOFF_HINT_KEYWORDS = ("转人工", "人工客服", "真人")
_REPORT_MAX_CHARS_PER_CHUNK = 12_000
_ADMIN_IMAGE_MAX_BYTES = 20 * 1024 * 1024
_ADMIN_IMAGE_MAX_PIXELS = 40_000_000
_RASTER_MEDIA_TYPES = {
    "image/bmp",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/x-ms-bmp",
}

WxbotScopeExecutionAllowed = Callable[[str, str], Awaitable[bool]]
WxbotOwnersScopeExecutionAllowed = Callable[
    [tuple[str, ...], str, str],
    Awaitable[bool],
]


async def _require_wxbot_scope_execution(
    scope_execution_allowed: WxbotScopeExecutionAllowed | None,
    *,
    tenant_id: str,
    session_id: str = "",
) -> None:
    if not callable(scope_execution_allowed):
        raise HTTPException(503, "plugin_scope_unavailable")
    try:
        allowed = await scope_execution_allowed(
            str(tenant_id or "").strip(),
            str(session_id or "").strip(),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "wxbot.admin_scope_gate_failed",
            tenant_id=tenant_id,
            session_id=session_id,
            error_class=exc.__class__.__name__,
        )
        raise HTTPException(503, "plugin_scope_unavailable") from exc
    if allowed is not True:
        raise HTTPException(503, "plugin_runtime_disabled")


async def _require_wxbot_scope_targets(
    scope_execution_allowed: WxbotScopeExecutionAllowed | None,
    targets: set[tuple[str, str]],
) -> None:
    for tenant_id, session_id in sorted(targets):
        await _require_wxbot_scope_execution(
            scope_execution_allowed,
            tenant_id=tenant_id,
            session_id=session_id,
        )


async def _require_owners_scope_execution(
    scope_execution_allowed: WxbotOwnersScopeExecutionAllowed | None,
    *,
    owners: tuple[str, ...],
    tenant_id: str,
    session_id: str = "",
) -> None:
    normalized_owners = tuple(dict.fromkeys(str(owner or "").strip() for owner in owners))
    if not normalized_owners or not callable(scope_execution_allowed):
        raise HTTPException(503, "plugin_owner_scope_unavailable")
    try:
        allowed = await scope_execution_allowed(
            normalized_owners,
            tenant_id=str(tenant_id or "").strip(),
            session_id=str(session_id or "").strip(),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "wxbot.admin_owner_scope_gate_failed",
            owners=list(normalized_owners),
            tenant_id=tenant_id,
            session_id=session_id,
            error_class=exc.__class__.__name__,
        )
        raise HTTPException(503, "plugin_owner_scope_unavailable") from exc
    if allowed is not True:
        raise HTTPException(503, "plugin_owner_runtime_disabled")


def _require_admin(store: WxbotStore, request: Request):
    return authenticate_admin_request(request, store.settings)


def _require_tenant_admin(store: WxbotStore, request: Request, tenant_id: str):
    principal = _require_admin(store, request)
    tenant = str(tenant_id or "").strip()
    if not tenant or not principal.allows_tenant(tenant):
        raise HTTPException(403, "tenant access denied")
    return principal


def _require_tenant_policy_admin(
    store: WxbotStore,
    request: Request,
    tenant_id: str,
):
    principal = _require_tenant_admin(store, request, tenant_id)
    if bool(getattr(principal, "requires_explicit_group_scope", False)):
        raise HTTPException(403, "tenant-wide policy access denied")
    return principal


def _require_session_admin(
    store: WxbotStore,
    request: Request,
    tenant_id: str,
    session_id: str,
    *,
    group_required: bool = False,
) -> tuple[str, str, Any]:
    tenant = str(tenant_id or "").strip()
    session = str(session_id or "").strip()
    if not tenant:
        raise HTTPException(400, "tenant_id required")
    if not session:
        raise HTTPException(400, "session_id required")
    if len(tenant) > 64 or len(session) > 256:
        raise HTTPException(400, "invalid tenant or session scope")
    principal = _require_tenant_admin(store, request, tenant)
    if group_required and not session.endswith("@chatroom"):
        raise HTTPException(400, "group session required")
    if bool(getattr(principal, "requires_explicit_group_scope", False)):
        if not session.endswith("@chatroom") or not principal.allows_group(
            tenant,
            session,
        ):
            raise HTTPException(403, "group access denied")
    return tenant, session, principal


def _require_default_tenant_admin(store: WxbotStore, request: Request) -> str:
    tenant_id, _principal = _require_default_tenant_principal(store, request)
    return tenant_id


def _require_default_tenant_principal(store: WxbotStore, request: Request):
    tenant_id = str(
        getattr(store.settings, "wxbot_default_tenant_id", "default") or "default"
    ).strip()
    principal = _require_tenant_admin(store, request, tenant_id)
    return tenant_id, principal


def _filter_session_payload_for_principal(
    payload: dict[str, Any],
    *,
    principal: Any,
    tenant_id: str,
) -> dict[str, Any]:
    if not bool(getattr(principal, "requires_explicit_group_scope", False)):
        return payload
    raw_sessions = payload.get("sessions")
    if not isinstance(raw_sessions, list):
        return {**payload, "sessions": [], "count": 0}
    sessions = [
        item
        for item in raw_sessions
        if isinstance(item, dict)
        and principal.allows_group(tenant_id, str(item.get("session_id") or ""))
    ]
    return {**payload, "sessions": sessions, "count": len(sessions)}


def _agent_tool_catalog(container: Any, scope: str) -> list[dict[str, Any]]:
    registry = getattr(container, "agent_tool_registry", None) or getattr(
        container, "_agent_tool_registry", None
    )
    if registry is not None:
        items = registry.catalog(scope)
        if items:
            return items
    return AgentCapabilityEngine.tool_catalog(scope)


class WxbotSendRequest(StrictRequestModel):
    tenant_id: str = ""
    session_id: str = Field(min_length=1, max_length=256)
    session_name: str = Field(default="", max_length=256)
    sender_name: str = Field(default="", max_length=256)
    sender_wxid: str = Field(default="", max_length=256)
    mention_sender: bool = False
    reply_to_msg_svr_id: str = ""
    session_kind: str = ""
    text: str = Field(default="", max_length=20_000)
    msg_type: str = "text"
    media_id: str = Field(default="", max_length=4096)
    source_message: dict[str, Any] = Field(default_factory=dict)
    delivery: dict[str, Any] = Field(default_factory=dict)


class WxbotBatchSendRequest(StrictRequestModel):
    messages: list[WxbotSendRequest] = Field(default_factory=list, max_length=100)


class WxbotInboundSimulationRequest(StrictRequestModel):
    message: str = Field(min_length=1, max_length=20_000)

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("message must not be blank")
        return normalized


class WxbotSendEnvelopeRequest(StrictRequestModel):
    target: dict[str, Any]
    content: dict[str, Any]
    sender: dict[str, Any] = Field(default_factory=dict)
    reply: dict[str, Any] = Field(default_factory=dict)
    source_message: dict[str, Any] = Field(default_factory=dict)
    delivery: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WxbotBatchSendEnvelopeRequest(StrictRequestModel):
    messages: list[WxbotSendEnvelopeRequest] = Field(default_factory=list, max_length=100)


class WxbotReplyQueueClearRequest(StrictRequestModel):
    tenant_id: str
    status: str = "pending"
    session_id: str = ""


class WxbotSdkQueueClearRequest(StrictRequestModel):
    status: str = "pending"
    session_id: str = ""


class WxbotSdkQueueReconcileRequest(StrictRequestModel):
    action: Literal["confirm_sent", "retry"]


class WxbotEventSubscriptionRequest(StrictRequestModel):
    event_type: str = Field(min_length=1, max_length=128)
    target_url: str = Field(min_length=1, max_length=2048)
    session_id: str = Field(default="", max_length=256)
    enabled: bool = True


class WxbotGroupMemberSettingsRequest(StrictRequestModel):
    welcome_enabled: bool | None = None
    welcome_template: str | None = None
    welcome_mention: bool | None = None


class WxbotReportSubscriptionRequest(StrictRequestModel):
    session_id: str
    session_name: str = ""
    daily_enabled: bool = False
    weekly_enabled: bool = True
    monthly_enabled: bool = False
    daily_hour: int = 9
    weekly_day: int = 1
    weekly_hour: int = 9
    monthly_day: int = 1
    tz: str = "Asia/Shanghai"


class WxbotParticipationPolicyRequest(StrictRequestModel):
    threshold: int | None = Field(default=None, ge=0, le=200)
    quiet_start_hour: int | None = Field(default=None, ge=0, le=23)
    quiet_end_hour: int | None = Field(default=None, ge=0, le=23)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    max_soft_replies_10m: int | None = Field(default=None, ge=0, le=20)
    max_soft_replies_hour: int | None = Field(default=None, ge=0, le=100)
    max_bot_ratio_last_40: float | None = Field(default=None, ge=0, le=1)
    max_consecutive_bot_messages: int | None = Field(default=None, ge=0, le=20)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except (KeyError, ValueError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value


class WxbotSessionPolicyRequest(StrictRequestModel):
    reply_mode: str | None = None
    mention_sender_mode: str | None = None
    trigger_keywords_text: str | None = None
    reply_cooldown_seconds: float | None = Field(default=None, ge=0.0, le=60.0)
    coalesce_window_ms: int | None = Field(default=None, ge=0, le=5000)
    adaptive_cooldown_enabled: bool | None = None
    participation_policy: WxbotParticipationPolicyRequest | None = None


class WxbotGlobalReplyPolicyRequest(StrictRequestModel):
    private_reply_mode: str | None = None
    group_reply_mode: str | None = None
    group_reply_mention_sender: bool | None = None
    trigger_keywords_text: str | None = None


class WxbotReplyPolicyAggregateRequest(StrictRequestModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=256)
    private_reply_mode: str = "all"
    group_reply_mode: str = "contains"
    group_reply_mention_sender: bool = False
    trigger_keywords_text: str = Field(default="", max_length=20_000)
    session_reply_mode: str = "inherit"
    session_mention_sender_mode: str = "inherit"
    session_trigger_keywords_text: str = Field(default="", max_length=20_000)
    participation_policy: WxbotParticipationPolicyRequest | None = None
    repeater_enabled: bool = False
    repeater_cooldown_seconds: int = Field(default=300, ge=1, le=86_400)
    sdk_group_require_at_me: bool = True

    @field_validator("tenant_id", "session_id")
    @classmethod
    def normalize_scope(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("scope must not be blank")
        return cleaned


class WxbotSdkTriggerDebugRequest(StrictRequestModel):
    group_require_at_me: bool


class WxbotSelfReviewSubscriptionRequest(StrictRequestModel):
    session_id: str
    session_name: str = ""
    enabled: bool = False
    daily_hour: int = 23
    tz: str = "Asia/Shanghai"
    focus_mode: str = "bot_interactions"
    auto_create_kb_doc: bool = False


class WxbotReportSendRequest(StrictRequestModel):
    session_id: str = Field(min_length=1, max_length=256)
    session_name: str = Field(default="", max_length=256)
    report_type: str = Field(default="daily", max_length=16)
    date: str = Field(default="", max_length=32)
    year_month: str = Field(default="", max_length=16)


class WxbotSdkReadQueryRequest(StrictRequestModel):
    database: str = Field(pattern=r"^(message|contact)$")
    sql: str = Field(min_length=1, max_length=20_000)
    params: list[Any] | dict[str, Any] | None = None
    limit: int = Field(default=100, ge=1, le=500)

    @field_validator("sql")
    @classmethod
    def validate_read_only_sql(cls, value: str) -> str:
        normalized = " ".join(value.strip().split())
        lowered = f" {normalized.lower()} "
        if not normalized.lower().startswith(("select ", "with ")):
            raise ValueError("sql must be a SELECT or read-only WITH query")
        if ";" in normalized or "--" in normalized or "/*" in normalized:
            raise ValueError("sql comments and multiple statements are not allowed")
        blocked = (
            " insert ",
            " update ",
            " delete ",
            " drop ",
            " alter ",
            " create ",
            " attach ",
            " detach ",
            " pragma ",
            " vacuum ",
            " replace ",
        )
        if any(token in lowered for token in blocked):
            raise ValueError("sql must be read-only")
        return value.strip()

    @field_validator("params")
    @classmethod
    def validate_params_size(cls, value: Any) -> Any:
        if isinstance(value, (list, dict)) and len(value) > 100:
            raise ValueError("params must contain at most 100 entries")
        return value


class WxbotSessionStateUpdateRequest(StrictRequestModel):
    state: str | None = None
    auto_reply_enabled: bool | None = None


class WxbotAgentSessionPolicyRequest(StrictRequestModel):
    enabled: bool | None = None
    allowed_tools: list[str] | None = None


def _validate_reply_mode(
    value: str | None,
    field_name: str = "reply_mode",
    *,
    allow_inherit: bool = True,
) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    allowed = {"all", "off", "contains"}
    if allow_inherit:
        allowed.add("inherit")
    if cleaned not in allowed:
        joined = "/".join(sorted(allowed))
        raise HTTPException(400, f"{field_name} must be one of {joined}")
    return cleaned


def _validate_group_reply_mode(
    value: str | None,
    field_name: str,
    *,
    allow_inherit: bool = False,
) -> str | None:
    cleaned = _validate_reply_mode(
        value,
        field_name,
        allow_inherit=allow_inherit,
    )
    if cleaned == "all":
        raise HTTPException(400, f"{field_name} does not support all for group chats")
    return cleaned


def _validate_mention_sender_mode(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    allowed = {"inherit", "on", "off"}
    if cleaned not in allowed:
        joined = "/".join(sorted(allowed))
        raise HTTPException(400, f"mention_sender_mode must be one of {joined}")
    return cleaned


def _required_version_if_match(request: Request) -> int:
    raw = str(request.headers.get("If-Match") or "").strip()
    if not raw:
        raise HTTPException(428, "if_match_required")
    if raw.startswith("W/"):
        raw = raw[2:].strip()
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        raw = raw[1:-1]
    if not raw.isdigit():
        raise HTTPException(400, "invalid_if_match")
    return int(raw)


def _required_composite_if_match(request: Request) -> str:
    raw = str(request.headers.get("If-Match") or "").strip()
    if not raw:
        raise HTTPException(428, "if_match_required")
    if raw.startswith("W/") or raw == "*":
        raise HTTPException(400, "invalid_if_match")
    return raw


def _version_etag(version: int) -> str:
    return f'"{max(0, int(version))}"'


def _set_no_store_etag(response: Response, etag: str) -> None:
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-store"


def _version_conflict(
    *,
    expected: int,
    current: int,
) -> HTTPException:
    return HTTPException(
        409,
        {
            "code": "version_conflict",
            "expected_version": expected,
            "current_version": current,
        },
        headers={
            "ETag": _version_etag(current),
            "Cache-Control": "no-store",
        },
    )


def _required_idempotency_key(request: Request) -> str:
    value = str(request.headers.get("Idempotency-Key") or "").strip()
    if len(value) < 8 or len(value) > 255:
        raise HTTPException(400, "valid Idempotency-Key header required")
    return value


def _canonical_fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


async def _observe_admin_resource(
    store: WxbotStore,
    tenant_id: str,
    resource_key: str,
    *,
    resource_kind: str,
    state_payload: Any,
) -> int:
    observe = getattr(store, "observe_admin_resource", None)
    if callable(observe):
        return int(
            await observe(
                tenant_id,
                resource_key,
                resource_kind=resource_kind,
                state_payload=state_payload,
            )
        )
    # Lightweight router fakes do not own a database.  Preserve the same CAS
    # contract in memory so route tests exercise ETag behavior.
    versions = store.__dict__.get("_wxbot_admin_resource_versions")
    if not isinstance(versions, dict):
        versions = {}
        store.__dict__["_wxbot_admin_resource_versions"] = versions
    key = (str(tenant_id), str(resource_key))
    state_hash = _canonical_fingerprint(state_payload)
    current = versions.get(key)
    if not isinstance(current, dict):
        versions[key] = {"version": 0, "state_hash": state_hash}
        return 0
    if current.get("state_hash") != state_hash:
        current["version"] = int(current.get("version") or 0) + 1
        current["state_hash"] = state_hash
    return int(current.get("version") or 0)


@dataclass(frozen=True, slots=True)
class _AdminEffectOutcome:
    response: Any
    state_payload: Any = None


async def _execute_admin_mutation(
    store: WxbotStore,
    request: Request,
    *,
    tenant_id: str,
    operation: str,
    resource_key: str,
    request_payload: Any,
    effect: Callable[[], Awaitable[_AdminEffectOutcome]],
    expected_version: int | None = None,
    desired_state: Any = None,
    recovery_response: Any = None,
) -> tuple[Any, int | None, bool]:
    """Run one at-most-once admin effect with exact completed-response replay."""

    idempotency_key = _required_idempotency_key(request)
    trace_id = _request_trace_id(request)
    claim_method = getattr(store, "claim_admin_mutation", None)
    if callable(claim_method):
        try:
            claim = await claim_method(
                tenant_id,
                operation=operation,
                resource_key=resource_key,
                idempotency_key=idempotency_key,
                request_payload=request_payload,
                trace_id=trace_id,
                expected_version=expected_version,
                desired_state=desired_state,
                recovery_response=recovery_response,
            )
        except WxbotAdminIdempotencyConflictError as exc:
            raise HTTPException(409, "idempotency_key_conflict") from exc
        except WxbotAdminMutationBusyError as exc:
            raise HTTPException(
                409,
                {
                    "code": "mutation_outcome_indeterminate",
                    "mutation_id": exc.mutation_id,
                    "status": exc.status,
                },
            ) from exc
        except WxbotAdminVersionConflictError as exc:
            raise _version_conflict(expected=exc.expected, current=exc.current) from exc
        if not bool(getattr(claim, "is_new", False)):
            status_code = int(getattr(claim, "response_status_code", 200) or 200)
            replay = getattr(claim, "response", None)
            if status_code >= 400:
                detail = replay.get("detail") if isinstance(replay, dict) else replay
                raise HTTPException(status_code, detail or "mutation_failed")
            return replay, getattr(claim, "resource_version", None), True
        mutation_id = str(claim.mutation_id)
    else:
        records = store.__dict__.get("_wxbot_admin_mutations")
        if not isinstance(records, dict):
            records = {}
            store.__dict__["_wxbot_admin_mutations"] = records
        record_key = (str(tenant_id), idempotency_key)
        intent_hash = _canonical_fingerprint(
            {
                "operation": operation,
                "resource_key": resource_key,
                "request": request_payload,
            }
        )
        existing = records.get(record_key)
        if isinstance(existing, dict):
            if existing.get("intent_hash") != intent_hash:
                raise HTTPException(409, "idempotency_key_conflict")
            if existing.get("status") != "completed":
                raise HTTPException(409, "mutation_outcome_indeterminate")
            status_code = int(existing.get("status_code") or 200)
            if status_code >= 400:
                stored = existing.get("response")
                detail = stored.get("detail") if isinstance(stored, dict) else stored
                raise HTTPException(status_code, detail or "mutation_failed")
            return existing.get("response"), existing.get("resource_version"), True
        if expected_version is not None:
            versions = store.__dict__.get("_wxbot_admin_resource_versions", {})
            current = versions.get((str(tenant_id), str(resource_key)))
            current_version = int(current.get("version") or 0) if isinstance(current, dict) else 0
            if current_version != expected_version:
                raise _version_conflict(
                    expected=expected_version,
                    current=current_version,
                )
        mutation_id = uuid4().hex
        records[record_key] = {
            "mutation_id": mutation_id,
            "intent_hash": intent_hash,
            "status": "dispatching",
        }

    try:
        outcome = await effect()
    except HTTPException as exc:
        indeterminate = exc.status_code >= 500
        fail = getattr(store, "fail_admin_mutation", None)
        error_payload = {"detail": exc.detail}
        if callable(fail):
            await fail(
                mutation_id,
                status_code=exc.status_code,
                response=error_payload,
                error_code="external_effect_failed",
                indeterminate=indeterminate,
            )
        else:
            records[record_key].update(
                {
                    "status": "indeterminate" if indeterminate else "completed",
                    "status_code": exc.status_code,
                    "response": error_payload,
                }
            )
        raise
    except Exception as exc:
        fail = getattr(store, "fail_admin_mutation", None)
        if callable(fail):
            await fail(
                mutation_id,
                status_code=502,
                response={"detail": "external_effect_outcome_unknown"},
                error_code=type(exc).__name__,
                indeterminate=True,
            )
        else:
            records[record_key].update(
                {
                    "status": "indeterminate",
                    "status_code": 502,
                    "response": {"detail": "external_effect_outcome_unknown"},
                }
            )
        raise

    complete = getattr(store, "complete_admin_mutation", None)
    if callable(complete):
        try:
            committed = await complete(
                mutation_id,
                response=outcome.response,
                response_status_code=200,
                state_payload=outcome.state_payload,
            )
        except WxbotAdminVersionConflictError as exc:
            raise _version_conflict(expected=exc.expected, current=exc.current) from exc
        return (
            getattr(committed, "response", outcome.response),
            getattr(committed, "resource_version", None),
            bool(getattr(committed, "replayed", False)),
        )
    version: int | None = None
    if expected_version is not None:
        version = expected_version + 1
        versions = store.__dict__["_wxbot_admin_resource_versions"]
        versions[(str(tenant_id), str(resource_key))] = {
            "version": version,
            "state_hash": _canonical_fingerprint(outcome.state_payload),
        }
    records[record_key].update(
        {
            "status": "completed",
            "status_code": 200,
            "response": outcome.response,
            "resource_version": version,
        }
    )
    return outcome.response, version, False


def _aggregate_conflict(current_etag: str) -> HTTPException:
    return HTTPException(
        409,
        {"code": "reply_policy_version_conflict", "current_etag": current_etag},
        headers={"ETag": current_etag, "Cache-Control": "no-store"},
    )


def _request_trace_id(request: Request) -> str:
    return str(
        request.headers.get("X-Trace-ID")
        or request.headers.get("X-Request-ID")
        or getattr(request.state, "admin_request_id", "")
        or uuid4().hex
    ).strip()[:64]


def _policy_audit_summary(policy: dict[str, Any]) -> dict[str, object]:
    keywords = str(policy.get("trigger_keywords_text") or "")
    participation = policy.get("participation_policy")
    participation_source = participation if isinstance(participation, dict) else {}
    return {
        "version": max(0, int(policy.get("version") or 0)),
        "private_reply_mode": str(policy.get("private_reply_mode") or ""),
        "group_reply_mode": str(policy.get("group_reply_mode") or ""),
        "group_reply_mention_sender": bool(policy.get("group_reply_mention_sender")),
        "reply_mode": str(policy.get("reply_mode") or ""),
        "mention_sender_mode": str(policy.get("mention_sender_mode") or ""),
        "trigger_keyword_count": len([line for line in keywords.splitlines() if line.strip()]),
        "participation_policy": {
            key: participation_source[key]
            for key in (
                "threshold",
                "quiet_start_hour",
                "quiet_end_hour",
                "timezone",
                "max_soft_replies_10m",
                "max_soft_replies_hour",
                "max_bot_ratio_last_40",
                "max_consecutive_bot_messages",
            )
            if key in participation_source
        },
    }


def _aggregate_audit_summary(aggregate: dict[str, Any]) -> dict[str, object]:
    global_policy = aggregate.get("global_policy")
    session_policy = aggregate.get("session_policy")
    repeater_config = aggregate.get("repeater_config")
    sdk_gate = aggregate.get("sdk_gate")
    return {
        "versions": dict(aggregate.get("versions") or {}),
        "global_policy": _policy_audit_summary(
            global_policy if isinstance(global_policy, dict) else {}
        ),
        "session_policy": _policy_audit_summary(
            session_policy if isinstance(session_policy, dict) else {}
        ),
        "repeater": {
            "enabled": bool(
                repeater_config.get("enabled") if isinstance(repeater_config, dict) else False
            ),
            "cooldown_seconds": int(
                repeater_config.get("cooldown_seconds") or 0
                if isinstance(repeater_config, dict)
                else 0
            ),
        },
        "sdk_gate": {
            "group_require_at_me": bool(
                sdk_gate.get("group_require_at_me") if isinstance(sdk_gate, dict) else True
            ),
            "status": str(sdk_gate.get("status") or "" if isinstance(sdk_gate, dict) else ""),
        },
    }


def _session_state_config(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": str(payload.get("state") or ""),
        "auto_reply_enabled": bool(payload.get("auto_reply_enabled")),
    }


def _agent_policy_config(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": bool(payload.get("enabled", True)),
        "allowed_tools": sorted(
            {str(item) for item in (payload.get("allowed_tools") or []) if str(item).strip()}
        ),
        "scope": str(payload.get("scope") or DEFAULT_AGENT_SCOPE),
    }


def _event_subscription_config(items: Any) -> list[dict[str, Any]]:
    source = items if isinstance(items, list) else []
    normalized = [
        {
            "id": int(item.get("id") or 0),
            "event_type": str(item.get("event_type") or ""),
            "target_url_hash": hashlib.sha256(
                str(item.get("target_url") or "").encode("utf-8")
            ).hexdigest(),
            "session_id_hash": hashlib.sha256(
                str(item.get("session_id") or "").encode("utf-8")
            ).hexdigest(),
            "enabled": bool(item.get("enabled", True)),
        }
        for item in source
        if isinstance(item, dict)
    ]
    return sorted(normalized, key=lambda item: (item["id"], item["event_type"]))


def _group_member_settings_config(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "welcome_enabled": bool(payload.get("welcome_enabled")),
        "welcome_template_hash": hashlib.sha256(
            str(payload.get("welcome_template") or "").encode("utf-8")
        ).hexdigest(),
        "welcome_mention": bool(payload.get("welcome_mention")),
    }


def _report_subscription_config(items: Any) -> list[dict[str, Any]]:
    source = items if isinstance(items, list) else []
    keys = (
        "session_id",
        "session_name",
        "daily_enabled",
        "weekly_enabled",
        "monthly_enabled",
        "daily_hour",
        "weekly_day",
        "weekly_hour",
        "monthly_day",
        "tz",
    )
    return sorted(
        [{key: item.get(key) for key in keys} for item in source if isinstance(item, dict)],
        key=lambda item: str(item.get("session_id") or ""),
    )


def _self_review_subscription_config(items: Any) -> list[dict[str, Any]]:
    source = items if isinstance(items, list) else []
    keys = (
        "session_id",
        "session_name",
        "enabled",
        "daily_hour",
        "tz",
        "focus_mode",
        "auto_create_kb_doc",
    )
    return sorted(
        [{key: item.get(key) for key in keys} for item in source if isinstance(item, dict)],
        key=lambda item: str(item.get("session_id") or ""),
    )


def _mutation_audit_summary(
    *,
    operation: str,
    affected_count: int = 0,
    enabled: bool | None = None,
    message_count: int = 0,
    message_chars: int = 0,
) -> dict[str, object]:
    result: dict[str, object] = {
        "operation": operation,
        "affected_count": max(0, int(affected_count)),
        "message_count": max(0, int(message_count)),
        "message_chars": max(0, int(message_chars)),
    }
    if enabled is not None:
        result["enabled"] = bool(enabled)
    return result


def _validate_send_payload(payload: dict[str, Any], *, image_error_field: str) -> None:
    msg_type = str(payload.get("msg_type") or payload.get("type") or "text").strip().lower()
    text = str(payload.get("text") or payload.get("reply_text") or "")
    if payload.get("image_path") or payload.get("image_url"):
        raise HTTPException(400, "server media paths and URLs are not accepted")
    media_id = str(payload.get("media_id") or "").strip()
    if msg_type == "image" and not media_id:
        raise HTTPException(400, f"{image_error_field} required for image messages")
    if msg_type == "text" and not text.strip():
        raise HTTPException(400, "text required for text messages")


def _normalize_target_session_state(body: WxbotSessionStateUpdateRequest) -> SessionState:
    raw = body.state
    if body.auto_reply_enabled is not None:
        raw = "chatting" if body.auto_reply_enabled else "escalated"
    cleaned = str(raw or "").strip().lower()
    if not cleaned:
        raise HTTPException(400, "state or auto_reply_enabled is required")
    try:
        return SessionState(cleaned)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in SessionState)
        raise HTTPException(400, f"state must be one of: {allowed}") from exc


async def _load_session_status_payload(
    container: Any, tenant_id: str, session_id: str
) -> dict[str, Any]:
    factory = get_session_factory()
    async with factory() as db:
        row = await db.scalar(
            select(SessionRow).where(
                SessionRow.tenant_id == tenant_id,
                SessionRow.session_id == session_id,
            )
        )
        if row is None:
            raise HTTPException(404, "session not found")

        latest_handoff_turn_row = await db.scalar(
            select(TurnRow)
            .where(
                TurnRow.tenant_id == tenant_id,
                TurnRow.session_id == session_id,
                TurnRow.role == "user",
                or_(*(TurnRow.content.contains(keyword) for keyword in _HANDOFF_HINT_KEYWORDS)),
            )
            .order_by(TurnRow.created_at.desc())
            .limit(1)
        )

    session = await container.session_manager.load(
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        session_id=row.session_id,
        channel=Channel(row.channel),
    )
    state = session.state
    auto_reply_enabled = state != SessionState.ESCALATED
    is_group = str(session_id or "").endswith("@chatroom")
    latest_handoff_turn = None
    if latest_handoff_turn_row is not None:
        latest_handoff_turn = {
            "content": latest_handoff_turn_row.content,
            "created_at": latest_handoff_turn_row.created_at,
            "trace_id": latest_handoff_turn_row.trace_id,
        }

    if is_group:
        explanation = (
            "当前群会话已由后台切到人工接管，AI 自动回复会被短路；群内身份提问仍应明确说明 AI 身份。"
            if state == SessionState.ESCALATED
            else (
                "当前群会话处于自动回复状态；身份提问会明确说明 AI 身份，真实的人工协助请求会提示联系群管理员，"
                "但不会谎称已转接，也不会自动冻结整个群；否定、引用或普通语境中的相关词按正常消息处理。"
            )
        )
    else:
        explanation = (
            "当前会话已转人工，AI 自动回复会被短路；命中词通常是「转人工 / 人工客服 / 真人」。"
            if state == SessionState.ESCALATED
            else "当前会话处于自动回复状态，不会被人工接管短路。"
        )

    return {
        "tenant_id": row.tenant_id,
        "session_id": row.session_id,
        "session_name": str(
            session.metadata.get("session_name") or row.meta.get("session_name") or ""
        ),
        "user_id": row.user_id,
        "channel": row.channel,
        "state": state.value,
        "auto_reply_enabled": auto_reply_enabled,
        "suppress_ai_reply": state == SessionState.ESCALATED,
        "handoff_hint_keywords": list(_HANDOFF_HINT_KEYWORDS),
        "latest_handoff_turn": latest_handoff_turn,
        "explanation": explanation,
        "last_active_at": row.last_active_at,
        "updated_at": row.updated_at,
    }


async def _set_session_state(
    container: Any,
    tenant_id: str,
    session_id: str,
    target_state: SessionState,
) -> dict[str, Any]:
    factory = get_session_factory()
    async with factory() as db:
        row = await db.scalar(
            select(SessionRow).where(
                SessionRow.tenant_id == tenant_id,
                SessionRow.session_id == session_id,
            )
        )
        if row is None:
            raise HTTPException(404, "session not found")

    session = await container.session_manager.load(
        tenant_id=tenant_id,
        user_id=row.user_id,
        session_id=session_id,
        channel=Channel(row.channel),
    )
    previous_state = session.state
    if previous_state != target_state:
        try:
            await container.session_manager.set_state(session, target_state)
        except Exception:
            session.state = target_state
            await container.session_manager.save(session)
            logger.warning(
                "wxbot.admin.session_state_forced",
                tenant_id=tenant_id,
                session_id=session_id,
                previous_state=previous_state.value,
                target_state=target_state.value,
            )
        else:
            logger.info(
                "wxbot.admin.session_state_updated",
                tenant_id=tenant_id,
                session_id=session_id,
                previous_state=previous_state.value,
                target_state=target_state.value,
            )
    payload = await _load_session_status_payload(container, tenant_id, session_id)
    payload["previous_state"] = previous_state.value
    return payload


async def _sdk_request(
    store: WxbotStore,
    bridge: Any | None,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    request_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    if bridge is not None and hasattr(bridge, "sdk_request"):
        try:
            bridge_kwargs: dict[str, Any] = {"params": params, "json_body": json_body}
            if request_headers:
                bridge_kwargs["request_headers"] = request_headers
            return await bridge.sdk_request(method, path, **bridge_kwargs)
        except ValueError as exc:
            # The long-running bridge historically supported GET/POST only.
            # DELETE admin operations safely fall back to the same trusted SDK
            # endpoint after the durable dispatch fence has been persisted.
            if str(method or "").upper() != "DELETE":
                raise
            logger.info("wxbot.admin.sdk_bridge_method_fallback", path=path, error=str(exc))
        except Exception as exc:
            status_code = int(getattr(exc, "status_code", 0) or 0)
            if 400 <= status_code <= 599:
                error_code = str(
                    getattr(exc, "error_code", "")
                    or getattr(exc, "detail", "")
                    or "wxbot_sdk_error"
                )[:96]
                raise HTTPException(
                    status_code,
                    {"code": "wxbot_sdk_error", "sdk_error": error_code},
                ) from exc
            raise

    base_url = getattr(store.settings, "wxbot_sdk_url", "http://127.0.0.1:5080").rstrip("/")
    normalized_method = str(method or "").upper()
    if normalized_method not in {"GET", "POST", "DELETE"}:
        raise HTTPException(405, "unsupported wxbot sdk method")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        **wxbot_sdk_headers(store.settings),
        **(request_headers or {}),
    }
    try:
        async with httpx.AsyncClient(
            timeout=10.0,
            trust_env=False,
        ) as client:
            resp = await safe_trusted_service_request(
                client,
                normalized_method,
                base_url,
                path,
                params=params,
                json=json_body,
                headers=headers,
                timeout_seconds=10.0,
                max_response_bytes=10 * 1024 * 1024,
                allowed_response_content_types=(
                    "application/json",
                    "application/problem+json",
                    "text/plain",
                ),
            )
    except httpx.HTTPError as exc:
        raise HTTPException(502, "wxbot sdk unavailable") from exc
    if resp.status_code >= 400:
        error_code = "wxbot_sdk_error"
        try:
            error_payload = resp.json()
            if isinstance(error_payload, dict):
                error_code = str(error_payload.get("error") or error_code)
        except (TypeError, ValueError):
            pass
        raise HTTPException(
            resp.status_code,
            {"code": "wxbot_sdk_error", "sdk_error": error_code[:96]},
        )
    payload = resp.json()
    if isinstance(payload, dict):
        return payload
    return {"data": payload}


async def _sdk_request_optional(
    store: WxbotStore,
    bridge: Any | None,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    request_headers: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    try:
        return await _sdk_request(
            store,
            bridge,
            method,
            path,
            params=params,
            json_body=json_body,
            request_headers=request_headers,
        )
    except HTTPException as exc:
        if exc.status_code == 404:
            return None
        raise
    except httpx.HTTPStatusError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return None
        raise


async def _require_verified_group(
    store: WxbotStore,
    bridge: Any | None,
    *,
    tenant_id: str,
    session_id: str,
) -> dict[str, Any]:
    session = str(session_id or "").strip()
    if not session.endswith("@chatroom"):
        raise HTTPException(400, "verified group session required")
    payload = await _sdk_request(store, bridge, "GET", "/ext/roster/groups")
    items = payload.get("sessions")
    if not isinstance(items, list):
        raise HTTPException(503, "verified group roster unavailable")
    target = next(
        (
            item
            for item in items
            if isinstance(item, dict) and str(item.get("session_id") or "").strip() == session
        ),
        None,
    )
    if target is None:
        raise HTTPException(404, "target group is not present in verified roster")
    return _client_safe_media_payload(dict(target), store, tenant_id=tenant_id)


def _resolve_send_media(
    payload: dict[str, Any],
    store: WxbotStore,
    *,
    tenant_id: str,
) -> None:
    if str(payload.get("msg_type") or payload.get("type") or "text").lower() != "image":
        payload.pop("media_id", None)
        return
    try:
        locator = resolve_media_id(
            str(payload.pop("media_id", "")),
            store.settings,
            expected_tenant_id=tenant_id,
        )
    except InvalidMediaID as exc:
        raise HTTPException(400, str(exc)) from exc
    target_key = "image_path" if locator.kind == "sdk_path" else "image_url"
    payload[target_key] = locator.value


_CLIENT_MEDIA_LOCATORS = {
    "image_path": "media_id",
    "image_url": "media_id",
    "media_path": "media_id",
    "media_url": "media_id",
    "image_preview_path": "preview_media_id",
    "image_preview_url": "preview_media_id",
    "preview_path": "preview_media_id",
    "preview_url": "preview_media_id",
    "image_thumbnail_path": "thumbnail_media_id",
    "image_thumbnail_url": "thumbnail_media_id",
    "thumbnail_path": "thumbnail_media_id",
    "thumbnail_url": "thumbnail_media_id",
    "thumb_url": "thumbnail_media_id",
    "quote_image_path": "quote_media_id",
    "quote_image_url": "quote_media_id",
    "quote_image_preview_url": "quote_preview_media_id",
    "quote_image_thumbnail_url": "quote_thumbnail_media_id",
}


def _client_safe_media_payload(value: Any, store: WxbotStore, *, tenant_id: str) -> Any:
    if isinstance(value, list):
        return [_client_safe_media_payload(item, store, tenant_id=tenant_id) for item in value]
    if not isinstance(value, dict):
        return value
    safe: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        media_key = _CLIENT_MEDIA_LOCATORS.get(key.lower())
        if media_key and isinstance(raw_value, str) and raw_value.strip():
            locator = raw_value.strip()
            if locator.startswith(("/images/", "images/")):
                locator = locator.split("images/", 1)[1]
            try:
                safe.setdefault(
                    media_key,
                    issue_media_id(
                        locator,
                        store.settings,
                        tenant_id=tenant_id,
                    ),
                )
            except InvalidMediaID:
                logger.warning(
                    "wxbot.admin.media_locator_redacted",
                    tenant_id=tenant_id,
                    field=key,
                )
            continue
        safe[key] = _client_safe_media_payload(
            raw_value,
            store,
            tenant_id=tenant_id,
        )
    return safe


def _detected_raster_media_type(content: bytes, declared: str) -> str | None:
    media_type = declared.split(";", 1)[0].strip().lower()
    if content.startswith(b"BM"):
        return "image/bmp"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    if media_type in _RASTER_MEDIA_TYPES:
        return media_type
    return None


def _bmp_as_png(content: bytes) -> bytes:
    try:
        with Image.open(BytesIO(content)) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > _ADMIN_IMAGE_MAX_PIXELS:
                raise HTTPException(413, "image dimensions exceed preview limit")
            image.load()
            converted = image.convert("RGBA") if image.mode not in {"RGB", "RGBA"} else image
            output = BytesIO()
            converted.save(output, format="PNG", optimize=True)
            return output.getvalue()
    except HTTPException:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError) as exc:
        raise HTTPException(415, "invalid BMP image") from exc


async def _sdk_admin_image(store: WxbotStore, locator: MediaLocator) -> Response:
    base_url = getattr(store.settings, "wxbot_sdk_url", "http://127.0.0.1:5080").rstrip("/")
    if locator.kind == "sdk_path":
        encoded_path = quote(locator.value, safe="/-_.()@")
        media_url = f"{base_url}/images/{encoded_path}"
    else:
        media_url = locator.value
    try:
        async with httpx.AsyncClient(
            timeout=12.0,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            configure_http_client(
                client,
                allowed_private_origins=[base_url],
                origin_headers={base_url: wxbot_sdk_headers(store.settings)},
            )
            upstream = await safe_get(
                client,
                media_url,
                max_response_bytes=_ADMIN_IMAGE_MAX_BYTES,
                timeout_seconds=12.0,
                allowed_content_types=("image/",),
            )
    except httpx.HTTPError as exc:
        raise HTTPException(502, "wxbot image service unavailable") from exc

    if upstream.status_code == 404:
        raise HTTPException(404, "wxbot image not found")
    if upstream.status_code >= 400:
        raise HTTPException(502, "wxbot image service returned an error")

    declared_length = upstream.headers.get("content-length", "").strip()
    if declared_length.isdigit() and int(declared_length) > _ADMIN_IMAGE_MAX_BYTES:
        raise HTTPException(413, "image exceeds preview limit")
    content = upstream.content
    if not content:
        raise HTTPException(404, "wxbot image is empty")
    if len(content) > _ADMIN_IMAGE_MAX_BYTES:
        raise HTTPException(413, "image exceeds preview limit")

    media_type = _detected_raster_media_type(content, upstream.headers.get("content-type", ""))
    if media_type is None:
        raise HTTPException(415, "wxbot response is not a supported image")
    if media_type in {"image/bmp", "image/x-ms-bmp"}:
        content = _bmp_as_png(content)
        media_type = "image/png"

    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _resolve_report_period(
    report_type: str,
    *,
    date: str = "",
    year_month: str = "",
    tz: str = "Asia/Shanghai",
) -> tuple[str, str]:
    report_kind = str(report_type or "daily").strip().lower()
    if report_kind not in {"daily", "monthly"}:
        raise HTTPException(400, "report_type must be daily or monthly")
    if report_kind == "daily":
        period_key = date.strip() or datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d")
        return period_key, period_key
    period_key = year_month.strip() or datetime.now(ZoneInfo(tz)).strftime("%Y-%m")
    return period_key, period_key


def _report_message_text(item: dict[str, Any]) -> str:
    msg_type = str(item.get("msg_type") or "text").strip().lower()
    text = str(item.get("text") or "").strip()
    if text:
        return text
    placeholders = {
        "image": "[图片]",
        "audio": "[语音]",
        "video": "[视频]",
        "file": "[文件]",
        "event": "[事件]",
    }
    return placeholders.get(msg_type, f"[{msg_type or '消息'}]")


def _report_message_lines(messages: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in messages:
        if bool(item.get("is_self_sent")):
            continue
        text = _report_message_text(item)
        if not text:
            continue
        sender_name = str(item.get("sender_name") or item.get("sender_wxid") or "未知成员").strip()
        timestamp = str(item.get("timestamp") or "").strip()
        if timestamp:
            lines.append(f"[{timestamp}] {sender_name}: {text}")
        else:
            lines.append(f"{sender_name}: {text}")
    return lines


def _chunk_report_lines(
    lines: list[str], max_chars: int = _REPORT_MAX_CHARS_PER_CHUNK
) -> list[str]:
    max_chars = max(1, int(max_chars or _REPORT_MAX_CHARS_PER_CHUNK))
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for raw_line in lines:
        parts = [
            raw_line[index : index + max_chars] for index in range(0, len(raw_line), max_chars)
        ] or [raw_line]
        for line in parts:
            line_len = len(line) + 1
            if current and current_len + line_len > max_chars:
                chunks.append("\n".join(current))
                current = [line]
                current_len = line_len
                continue
            current.append(line)
            current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks


async def _call_report_llm(
    store: WxbotStore,
    container: Any,
    *,
    trace_id: str,
    system: str,
    user: str,
    max_tokens: int,
) -> str:
    from app.common.types import ChatMessage, ChatRequest, Role

    llm_service = getattr(container, "llm_service", None)
    if llm_service is None:
        raise RuntimeError("LLM service not available")

    request = ChatRequest(
        tenant_id=str(getattr(store.settings, "wxbot_default_tenant_id", "default") or "default"),
        trace_id=trace_id,
        model_tier="tier-2",
        messages=[ChatMessage(role=Role.USER, content=user)],
        system=system,
        max_tokens=max_tokens,
        temperature=0.3,
        metadata=report_llm_metadata(),
    )
    timeout = float(getattr(store.settings, "wxbot_report_stage_timeout_seconds", 240.0) or 240.0)
    response = await asyncio.wait_for(llm_service.chat(request), timeout=timeout)
    return str(response.content or "").strip()


def build_wxbot_router(
    store: WxbotStore,
    container: Any,
    bridge: Any | None = None,
    scheduler: Any | None = None,
    report_service: WxbotReportService | None = None,
    self_review_service: WxbotSelfReviewService | None = None,
    agent_store: AgentStore | None = None,
    scope_execution_allowed: WxbotScopeExecutionAllowed | None = None,
    owners_scope_execution_allowed: WxbotOwnersScopeExecutionAllowed | None = None,
) -> APIRouter:
    router = APIRouter()
    repeater_store = getattr(container, "repeater_store", None) or RepeaterStore(store.settings)
    report_service = report_service or WxbotReportService(
        store,
        container,
        bridge=bridge,
        scope_execution_allowed=scope_execution_allowed,
    )
    self_review_service = self_review_service or WxbotSelfReviewService(
        store,
        container,
        bridge=bridge,
        scope_execution_allowed=scope_execution_allowed,
    )

    @router.get("/bridge/status")
    async def bridge_status():
        if bridge is not None:
            return await bridge.status()
        return await read_bridge_runtime_status(
            get_redis(),
            store,
            store.settings,
            getattr(store.settings, "wxbot_default_tenant_id", "default"),
            connection_id=str(
                getattr(store.settings, "channel_connection_id", "") or ""
            ),
        )

    @router.get("/admin/reply-queue/stats")
    async def reply_queue_stats(tenant_id: str, request: Request):
        _require_tenant_admin(store, request, tenant_id)
        stats = await store.reply_queue_stats(tenant_id)
        return stats

    @router.get("/admin/images/{media_id}")
    async def get_admin_image(media_id: str, request: Request) -> Response:
        principal = _require_admin(store, request)
        try:
            locator = resolve_media_id(media_id, store.settings)
        except InvalidMediaID as exc:
            raise HTTPException(400, str(exc)) from exc
        if not principal.allows_tenant(locator.tenant_id):
            raise HTTPException(403, "tenant access denied")
        return await _sdk_admin_image(store, locator)

    @router.get("/admin/reply-queue/messages")
    async def list_reply_queue_messages(
        tenant_id: str,
        request: Request,
        status: str = "",
        session_id: str = "",
        trace_id: str = "",
        limit: int = 100,
    ):
        _require_tenant_admin(store, request, tenant_id)
        rows = await store.list_reply_queue(
            tenant_id,
            status=status,
            session_id=session_id,
            trace_id=trace_id,
            limit=limit,
        )
        safe_rows = _client_safe_media_payload(rows, store, tenant_id=tenant_id)
        return {"items": safe_rows, "count": len(rows)}

    @router.post("/admin/reply-queue/clear")
    async def clear_reply_queue(body: WxbotReplyQueueClearRequest, request: Request):
        _require_tenant_admin(store, request, body.tenant_id)
        status = body.status.strip() or "pending"
        if status not in {"pending", "queued", "failed", "sent", "all"}:
            raise HTTPException(400, "status must be pending/queued/failed/sent/all")
        tenant_id = body.tenant_id.strip()
        session_id = body.session_id.strip()

        async def effect() -> _AdminEffectOutcome:
            return _AdminEffectOutcome(
                await store.clear_reply_queue(
                    tenant_id,
                    status=status,
                    session_id=session_id,
                )
            )

        result, _version, _replayed = await _execute_admin_mutation(
            store,
            request,
            tenant_id=tenant_id,
            operation="reply_queue_clear",
            resource_key=f"reply-queue:{session_id or '*'}:{status}",
            request_payload={"status": status, "session_id": session_id},
            effect=effect,
        )
        logger.warning(
            "wxbot.admin.reply_queue_cleared",
            tenant_id=result["tenant_id"],
            status=result["status"],
            session_id=result["session_id"],
            cleared=result["cleared"],
        )
        set_admin_audit_context(
            request,
            target_type="wxbot_reply_queue",
            tenant_id=tenant_id,
            session_id=session_id,
            after_state=_mutation_audit_summary(
                operation="clear",
                affected_count=int(result.get("cleared") or 0),
            ),
            trace_id=_request_trace_id(request),
            reason="durable_queue_clear",
        )
        return result

    @router.get("/admin/sessions")
    async def list_sessions(request: Request):
        tenant_id, principal = _require_default_tenant_principal(store, request)
        payload = await _sdk_request(store, bridge, "GET", "/sessions")
        scoped_payload = _filter_session_payload_for_principal(
            payload,
            principal=principal,
            tenant_id=tenant_id,
        )
        return _client_safe_media_payload(scoped_payload, store, tenant_id=tenant_id)

    @router.get("/admin/sdk/queue/stats")
    async def sdk_queue_stats(request: Request):
        _require_default_tenant_admin(store, request)
        return await _sdk_request(store, bridge, "GET", "/queue/stats")

    @router.get("/admin/sdk/queue/messages")
    async def sdk_queue_messages(request: Request, status: str = "pending", limit: int = 100):
        tenant_id = _require_default_tenant_admin(store, request)
        normalized_status = status.strip().lower()
        if normalized_status == "all":
            normalized_status = ""
        if normalized_status not in {
            "",
            "pending",
            "running",
            "uncertain",
            "failed",
            "sent",
            "cleared",
        }:
            raise HTTPException(
                400,
                "status must be pending/running/uncertain/failed/sent/cleared/all",
            )
        payload = await _sdk_request_optional(
            store,
            bridge,
            "GET",
            "/queue/messages",
            params={
                "status": normalized_status,
                "limit": max(1, min(limit, 500)),
            },
        )
        if payload is None:
            return {
                "items": [],
                "count": 0,
                "unsupported": True,
                "message": "current wxbot sdk does not support /queue/messages",
            }
        return _client_safe_media_payload(payload, store, tenant_id=tenant_id)

    @router.post("/admin/sdk/queue/messages/{row_id}/reconcile")
    async def reconcile_sdk_queue_message(
        row_id: int,
        body: WxbotSdkQueueReconcileRequest,
        request: Request,
    ):
        tenant_id = _require_default_tenant_admin(store, request)
        if row_id <= 0:
            raise HTTPException(400, "valid SDK queue row id required")
        row = await _sdk_request(
            store,
            bridge,
            "GET",
            f"/queue/messages/{row_id}",
        )
        session_id = str(row.get("session_id") or "").strip()
        if not session_id:
            raise HTTPException(503, "sdk_queue_scope_unavailable")
        await _require_wxbot_scope_execution(
            scope_execution_allowed,
            tenant_id=tenant_id,
            session_id=session_id,
        )
        intent = {"row_id": int(row_id), "action": body.action}

        async def effect() -> _AdminEffectOutcome:
            await _require_wxbot_scope_execution(
                scope_execution_allowed,
                tenant_id=tenant_id,
                session_id=session_id,
            )
            payload = await _sdk_request(
                store,
                bridge,
                "POST",
                f"/queue/messages/{row_id}/reconcile",
                json_body={"action": body.action},
                request_headers={"Idempotency-Key": _required_idempotency_key(request)},
            )
            return _AdminEffectOutcome(payload)

        payload, _version, _replayed = await _execute_admin_mutation(
            store,
            request,
            tenant_id=tenant_id,
            operation="sdk_queue_reconcile",
            resource_key=f"sdk-queue-message:{row_id}:reconcile",
            request_payload=intent,
            effect=effect,
        )
        set_admin_audit_context(
            request,
            target_type="wxbot_sdk_queue_message",
            tenant_id=tenant_id,
            session_id=session_id,
            after_state=_mutation_audit_summary(
                operation=body.action,
                affected_count=1,
            ),
            trace_id=_request_trace_id(request),
            reason="explicit_uncertain_delivery_reconciliation",
        )
        return payload

    @router.post("/admin/sdk/queue/clear")
    async def clear_sdk_queue(body: WxbotSdkQueueClearRequest, request: Request):
        tenant_id = _require_default_tenant_admin(store, request)
        status = body.status.strip() or "pending"
        if status not in {"pending", "failed", "sent", "all"}:
            raise HTTPException(400, "status must be pending/failed/sent/all")
        session_id = body.session_id.strip()
        intent = {"status": status, "session_id": session_id}

        async def effect() -> _AdminEffectOutcome:
            payload = await _sdk_request_optional(
                store,
                bridge,
                "POST",
                "/queue/clear",
                json_body=intent,
                request_headers={"Idempotency-Key": _required_idempotency_key(request)},
            )
            if payload is None:
                payload = {
                    "cleared": 0,
                    "ids": [],
                    "unsupported": True,
                    "message": "current wxbot sdk does not support /queue/clear",
                }
            return _AdminEffectOutcome(payload)

        payload, _version, _replayed = await _execute_admin_mutation(
            store,
            request,
            tenant_id=tenant_id,
            operation="sdk_queue_clear",
            resource_key=f"sdk-queue:{session_id or '*'}:{status}",
            request_payload=intent,
            effect=effect,
        )
        logger.warning(
            "wxbot.admin.sdk_queue_cleared",
            status=status,
            session_id=body.session_id.strip(),
            cleared=payload.get("cleared"),
        )
        set_admin_audit_context(
            request,
            target_type="wxbot_sdk_queue",
            tenant_id=tenant_id,
            session_id=session_id,
            after_state=_mutation_audit_summary(
                operation="clear",
                affected_count=int(payload.get("cleared") or 0),
            ),
            trace_id=_request_trace_id(request),
            reason="durable_sdk_queue_clear",
        )
        return payload

    @router.get("/admin/sdk/debug/trigger-config")
    async def sdk_trigger_debug_config(request: Request):
        _require_admin(store, request)
        payload = await _sdk_request_optional(store, bridge, "GET", "/debug/trigger-config")
        if payload is not None:
            return payload
        status_payload = await _sdk_request(store, bridge, "GET", "/status")
        config_payload = status_payload.get("config") if isinstance(status_payload, dict) else {}
        if not isinstance(config_payload, dict):
            config_payload = {}
        return {
            "group_require_at_me": True,
            "group_capture_mode": "mention_only",
            "my_names": config_payload.get("my_names") or [],
            "unsupported": True,
            "message": "current wxbot sdk does not support /debug/trigger-config",
        }

    @router.post("/admin/sdk/debug/trigger-config")
    async def set_sdk_trigger_debug_config(
        body: WxbotSdkTriggerDebugRequest,
        request: Request,
    ):
        _require_admin(store, request)
        raise HTTPException(
            409,
            "sdk trigger config is managed by the durable reply-policy aggregate",
        )

    @router.get("/admin/member-events")
    async def list_member_events(
        tenant_id: str,
        request: Request,
        limit: int = 50,
        connection_id: str = "",
    ):
        _require_tenant_admin(store, request, tenant_id)
        limit = max(1, min(limit, 200))
        try:
            connection_id = normalize_wxbot_event_connection_id(connection_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        rows = await store.list_member_events(
            tenant_id,
            limit=limit,
            connection_id=connection_id,
        )
        return {
            "events": _client_safe_media_payload(rows, store, tenant_id=tenant_id),
            "count": len(rows),
        }

    @router.get("/admin/media-ready-events")
    async def list_media_ready_events(
        tenant_id: str,
        request: Request,
        limit: int = 50,
        connection_id: str = "",
    ):
        _require_tenant_admin(store, request, tenant_id)
        limit = max(1, min(limit, 200))
        try:
            connection_id = normalize_wxbot_event_connection_id(connection_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        rows = await store.list_media_ready_events(
            tenant_id,
            limit=limit,
            connection_id=connection_id,
        )
        return {
            "events": _client_safe_media_payload(rows, store, tenant_id=tenant_id),
            "count": len(rows),
        }

    @router.get("/admin/roster/groups/{session_id:path}/members")
    async def get_group_roster_members(session_id: str, request: Request):
        tenant_id = _require_default_tenant_admin(store, request)
        payload = await _sdk_request(
            store,
            bridge,
            "GET",
            f"/ext/roster/groups/{session_id}/members",
        )
        return _client_safe_media_payload(payload, store, tenant_id=tenant_id)

    @router.get("/admin/roster/groups")
    async def list_group_roster_sessions(request: Request):
        tenant_id, principal = _require_default_tenant_principal(store, request)
        payload = await _sdk_request(
            store,
            bridge,
            "GET",
            "/ext/roster/groups",
        )
        scoped_payload = _filter_session_payload_for_principal(
            payload,
            principal=principal,
            tenant_id=tenant_id,
        )
        return _client_safe_media_payload(scoped_payload, store, tenant_id=tenant_id)

    @router.post("/admin/tenants/{tenant_id}/groups/{session_id:path}/simulate-inbound")
    async def simulate_group_inbound(
        tenant_id: str,
        session_id: str,
        body: WxbotInboundSimulationRequest,
        request: Request,
    ):
        principal = _require_tenant_admin(store, request, tenant_id)
        normalized_session_id = session_id.strip()
        if not normalized_session_id:
            raise HTTPException(400, "session_id required")
        if not principal.allows_group(tenant_id, normalized_session_id):
            raise HTTPException(403, "group access denied")

        idempotency_key = _required_idempotency_key(request)

        roster = await _sdk_request(store, bridge, "GET", "/ext/roster/groups")
        roster_items = roster.get("sessions")
        if not isinstance(roster_items, list):
            raise HTTPException(503, "verified group roster unavailable")
        target = next(
            (
                item
                for item in roster_items
                if isinstance(item, dict)
                and str(item.get("session_id") or "").strip() == normalized_session_id
            ),
            None,
        )
        if target is None:
            raise HTTPException(404, "target group is not present in verified roster")

        bus = getattr(container, "bus", None)
        if bus is None:
            raise HTTPException(503, "message bus unavailable")
        identity = hashlib.sha256(
            f"{tenant_id}\0{normalized_session_id}\0{idempotency_key}".encode()
        ).hexdigest()
        event = InboundEvent(
            message_id=f"admin-sim-{identity[:40]}",
            trace_id=_request_trace_id(request),
            tenant_id=tenant_id.strip(),
            channel=Channel.WECHAT,
            user_id="admin-simulator",
            session_id=normalized_session_id,
            message=Message(content=body.message),
            metadata={
                "source": "admin_console_simulator",
                "session_kind": "group",
                "session_name": str(target.get("session_name") or normalized_session_id),
                "sender_name": "模拟群成员",
                "sender_wxid": "admin-simulator",
                "admin_simulation": True,
            },
        )
        response_payload = {
            "status": "accepted",
            "message_id": event.message_id,
            "trace_id": event.trace_id,
            "session_id": event.session_id,
            "session_name": str(target.get("session_name") or event.session_id),
        }

        async def effect() -> _AdminEffectOutcome:
            try:
                publish_once = getattr(bus, "publish_once", None)
                if callable(publish_once):
                    await publish_once(
                        store.settings.bus_inbound_stream,
                        event.model_dump(mode="json"),
                        idempotency_key=f"wxbot-admin-sim:{tenant_id}:{idempotency_key}",
                        headers={
                            "tenant_id": event.tenant_id,
                            "trace_id": event.trace_id,
                            "channel": channel_id_value(event.channel),
                        },
                        partition_key=f"{event.tenant_id}:{event.session_id}",
                    )
                else:
                    await bus.publish(
                        store.settings.bus_inbound_stream,
                        event.model_dump(mode="json"),
                        headers={
                            "tenant_id": event.tenant_id,
                            "trace_id": event.trace_id,
                            "channel": channel_id_value(event.channel),
                        },
                        partition_key=f"{event.tenant_id}:{event.session_id}",
                    )
            except MessagePublishIdempotencyConflict as exc:
                raise HTTPException(409, "simulation_idempotency_conflict") from exc
            except Exception as exc:
                logger.exception(
                    "wxbot.admin.inbound_simulation_publish_failed",
                    tenant_id=event.tenant_id,
                    session_id=event.session_id,
                    trace_id=event.trace_id,
                )
                raise HTTPException(503, "simulation publish failed") from exc
            return _AdminEffectOutcome(response_payload)

        result, _version, _replayed = await _execute_admin_mutation(
            store,
            request,
            tenant_id=tenant_id,
            operation="simulate_inbound",
            resource_key=f"group:{normalized_session_id}:inbound-simulation",
            request_payload={
                "session_id": normalized_session_id,
                "message_hash": hashlib.sha256(body.message.encode("utf-8")).hexdigest(),
                "message_length": len(body.message),
            },
            effect=effect,
            recovery_response=response_payload,
        )

        set_admin_audit_context(
            request,
            target_type="wxbot_group_inbound_simulation",
            after_state={
                "tenant_id": event.tenant_id,
                "session_id": event.session_id,
                "channel": channel_id_value(event.channel),
                "verified_roster": True,
                "message_length": len(body.message),
            },
            trace_id=event.trace_id,
            reason="operator-triggered inbound simulation",
        )
        return result

    @router.get("/admin/reply-policy/aggregate")
    async def get_reply_policy_aggregate(
        tenant_id: str,
        session_id: str,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        tenant, session, _principal = _require_session_admin(
            store,
            request,
            tenant_id,
            session_id,
            group_required=True,
        )
        await _require_owners_scope_execution(
            owners_scope_execution_allowed,
            owners=("wxbot", "repeater"),
            tenant_id=tenant,
            session_id=session,
        )
        aggregate = await store.get_reply_policy_aggregate(tenant, session)
        await _require_owners_scope_execution(
            owners_scope_execution_allowed,
            owners=("wxbot", "repeater"),
            tenant_id=tenant,
            session_id=session,
        )
        _set_no_store_etag(response, reply_policy_composite_etag(aggregate))
        return aggregate

    @router.post("/admin/reply-policy/aggregate")
    async def set_reply_policy_aggregate(
        body: WxbotReplyPolicyAggregateRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        tenant_id, session_id, principal = _require_session_admin(
            store,
            request,
            body.tenant_id,
            body.session_id,
            group_required=True,
        )
        await _require_owners_scope_execution(
            owners_scope_execution_allowed,
            owners=("wxbot", "repeater"),
            tenant_id=tenant_id,
            session_id=session_id,
        )
        expected_etag = _required_composite_if_match(request)
        idempotency_key = str(request.headers.get("Idempotency-Key") or "").strip()
        if len(idempotency_key) < 8 or len(idempotency_key) > 255:
            raise HTTPException(400, "valid Idempotency-Key header required")
        message_store = getattr(container, "message_store", None)
        enqueue_intent = getattr(message_store, "enqueue_effect_intent", None)
        if not callable(enqueue_intent):
            raise HTTPException(503, "durable effect intent store unavailable")

        private_mode = _validate_reply_mode(
            body.private_reply_mode,
            "private_reply_mode",
            allow_inherit=False,
        )
        group_mode = _validate_group_reply_mode(
            body.group_reply_mode,
            "group_reply_mode",
        )
        session_mode = _validate_group_reply_mode(
            body.session_reply_mode,
            "session_reply_mode",
            allow_inherit=True,
        )
        mention_mode = _validate_mention_sender_mode(body.session_mention_sender_mode)
        participation_policy = (
            body.participation_policy.model_dump(exclude_none=True)
            if body.participation_policy is not None
            else None
        )
        canonical_request = {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "private_reply_mode": private_mode,
            "group_reply_mode": group_mode,
            "group_reply_mention_sender": body.group_reply_mention_sender,
            "trigger_keywords_text": body.trigger_keywords_text,
            "session_reply_mode": session_mode,
            "session_mention_sender_mode": mention_mode,
            "session_trigger_keywords_text": body.session_trigger_keywords_text,
            "participation_policy": participation_policy,
            "repeater_enabled": body.repeater_enabled,
            "repeater_cooldown_seconds": body.repeater_cooldown_seconds,
            "sdk_group_require_at_me": body.sdk_group_require_at_me,
        }
        request_hash = hashlib.sha256(
            json.dumps(
                canonical_request,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        trace_id = _request_trace_id(request)
        identity_digest = hashlib.sha256(
            f"{tenant_id}\0{session_id}\0{idempotency_key}".encode()
        ).hexdigest()
        effect_key = f"wxbot-sdk-trigger:{identity_digest}"
        response_payload: dict[str, Any]
        response_etag: str
        before_aggregate: dict[str, Any]
        replayed = False
        session_factory = get_session_factory()
        try:
            async with session_factory() as db:
                async with db.begin():
                    guard = await store.begin_reply_policy_idempotency(
                        db,
                        tenant_id=tenant_id,
                        session_id=session_id,
                        idempotency_key=idempotency_key,
                        request_hash=request_hash,
                    )
                    if bool(guard.get("completed")):
                        response_payload = dict(guard.get("response_json") or {})
                        response_etag = str(guard.get("response_etag") or "")
                        if not response_payload or not response_etag:
                            raise RuntimeError("reply_policy_idempotency_response_missing")
                        before_aggregate = response_payload
                        replayed = True
                    else:
                        aggregate_state = await store.lock_reply_policy_aggregate_state(
                            db,
                            tenant_id,
                            session_id,
                        )
                        global_before = await store.get_global_policy_in_transaction(
                            db,
                            tenant_id,
                            for_update=True,
                        )
                        session_before = await store.get_session_policy_in_transaction(
                            db,
                            tenant_id,
                            session_id,
                            global_policy=global_before,
                            for_update=True,
                        )
                        repeater_before = await repeater_store.get_config_in_transaction(
                            db,
                            tenant_id,
                            session_id,
                            for_update=True,
                        )
                        effect_before_status = await store.get_reply_policy_effect_status(
                            db,
                            tenant_id,
                            str(aggregate_state.get("effect_idempotency_key") or ""),
                        )
                        before_aggregate = compose_reply_policy_aggregate(
                            tenant_id=tenant_id,
                            session_id=session_id,
                            global_policy=global_before,
                            session_policy=session_before,
                            repeater_config=repeater_before,
                            aggregate_state=aggregate_state,
                            effect_status=effect_before_status,
                        )
                        current_etag = reply_policy_composite_etag(before_aggregate)
                        if expected_etag != current_etag:
                            raise _aggregate_conflict(current_etag)

                        await _require_owners_scope_execution(
                            owners_scope_execution_allowed,
                            owners=("wxbot", "repeater"),
                            tenant_id=tenant_id,
                            session_id=session_id,
                        )
                        global_mutation = await store.set_global_policy_in_transaction(
                            db,
                            tenant_id=tenant_id,
                            expected_version=int(global_before["version"]),
                            private_reply_mode=private_mode,
                            group_reply_mode=group_mode,
                            group_reply_mention_sender=(body.group_reply_mention_sender),
                            trigger_keywords_text=body.trigger_keywords_text,
                        )
                        session_mutation = await store.set_session_policy_in_transaction(
                            db,
                            tenant_id=tenant_id,
                            session_id=session_id,
                            expected_version=int(session_before["version"]),
                            reply_mode=session_mode,
                            mention_sender_mode=mention_mode,
                            trigger_keywords_text=(body.session_trigger_keywords_text),
                            participation_policy=participation_policy,
                        )
                        await _require_owners_scope_execution(
                            owners_scope_execution_allowed,
                            owners=("wxbot", "repeater"),
                            tenant_id=tenant_id,
                            session_id=session_id,
                        )
                        repeater_mutation = await repeater_store.set_config_in_transaction(
                            db,
                            tenant_id,
                            session_id,
                            expected_version=int(repeater_before["version"]),
                            enabled=body.repeater_enabled,
                            cooldown_seconds=(body.repeater_cooldown_seconds),
                        )
                        effect = await enqueue_intent(
                            db,
                            tenant_id=tenant_id,
                            session_id=session_id,
                            source_message_id=f"admin-{identity_digest[:48]}",
                            trace_id=trace_id,
                            owner="wxbot",
                            producer_owner="wxbot",
                            effect_type="sdk_trigger_config",
                            idempotency_key=effect_key,
                            payload={
                                "group_require_at_me": (body.sdk_group_require_at_me),
                            },
                            user_id=principal.subject,
                            context={"source": "reply_policy_aggregate"},
                        )
                        state_after = await store.update_reply_policy_aggregate_state(
                            db,
                            tenant_id=tenant_id,
                            session_id=session_id,
                            expected_version=int(aggregate_state["version"]),
                            sdk_group_require_at_me=(body.sdk_group_require_at_me),
                            effect_idempotency_key=effect_key,
                        )
                        after_aggregate = compose_reply_policy_aggregate(
                            tenant_id=tenant_id,
                            session_id=session_id,
                            global_policy=global_mutation.after,
                            session_policy=session_mutation.after,
                            repeater_config=repeater_mutation.after,
                            aggregate_state=state_after,
                            effect_status=str(effect.status),
                        )
                        response_etag = reply_policy_composite_etag(after_aggregate)
                        response_payload = {
                            "ok": True,
                            **after_aggregate,
                            "sdk_trigger": after_aggregate["sdk_gate"],
                        }
                        await store.complete_reply_policy_idempotency(
                            db,
                            tenant_id=tenant_id,
                            idempotency_key=idempotency_key,
                            response_payload=response_payload,
                            response_etag=response_etag,
                        )
        except ReplyPolicyIdempotencyConflictError as exc:
            raise HTTPException(
                409,
                {"code": "idempotency_key_conflict"},
                headers={"Cache-Control": "no-store"},
            ) from exc
        except (
            WxbotPolicyVersionConflictError,
            RepeaterConfigVersionConflictError,
        ) as exc:
            await _require_owners_scope_execution(
                owners_scope_execution_allowed,
                owners=("wxbot", "repeater"),
                tenant_id=tenant_id,
                session_id=session_id,
            )
            current = await store.get_reply_policy_aggregate(
                tenant_id,
                session_id,
            )
            await _require_owners_scope_execution(
                owners_scope_execution_allowed,
                owners=("wxbot", "repeater"),
                tenant_id=tenant_id,
                session_id=session_id,
            )
            raise _aggregate_conflict(reply_policy_composite_etag(current)) from exc
        except RuntimeError as exc:
            if str(exc) == "effect_intent_idempotency_conflict":
                raise HTTPException(
                    409,
                    {"code": "idempotency_key_conflict"},
                    headers={"Cache-Control": "no-store"},
                ) from exc
            raise

        await _require_owners_scope_execution(
            owners_scope_execution_allowed,
            owners=("wxbot", "repeater"),
            tenant_id=tenant_id,
            session_id=session_id,
        )
        _set_no_store_etag(response, response_etag)
        set_admin_audit_context(
            request,
            target_type="wxbot_reply_policy_aggregate",
            tenant_id=tenant_id,
            session_id=session_id,
            before_state=_aggregate_audit_summary(before_aggregate),
            after_state=_aggregate_audit_summary(response_payload),
            policy_version=int(dict(response_payload.get("versions") or {}).get("aggregate") or 0),
            trace_id=trace_id,
            reason=(
                "idempotent_replay"
                if replayed
                else "atomic_policy_update_with_durable_sdk_reconciliation"
            ),
        )
        return response_payload

    @router.get("/admin/reply-policy/global/{tenant_id}")
    async def get_global_reply_policy(
        tenant_id: str,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        _require_tenant_policy_admin(store, request, tenant_id)
        policy = await store.get_global_policy(tenant_id.strip())
        _set_no_store_etag(response, _version_etag(int(policy["version"])))
        return policy

    @router.post("/admin/reply-policy/global/{tenant_id}")
    async def set_global_reply_policy(
        tenant_id: str,
        body: WxbotGlobalReplyPolicyRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        _require_tenant_policy_admin(store, request, tenant_id)
        if all(value is None for value in body.model_dump().values()):
            raise HTTPException(400, "no_mutable_fields")
        expected_version = _required_version_if_match(request)
        try:
            mutation = await store.set_global_policy(
                tenant_id.strip(),
                expected_version=expected_version,
                private_reply_mode=_validate_reply_mode(
                    body.private_reply_mode,
                    "private_reply_mode",
                    allow_inherit=False,
                ),
                group_reply_mode=_validate_group_reply_mode(
                    body.group_reply_mode,
                    "group_reply_mode",
                ),
                group_reply_mention_sender=body.group_reply_mention_sender,
                trigger_keywords_text=body.trigger_keywords_text,
            )
        except WxbotPolicyVersionConflictError as exc:
            raise _version_conflict(
                expected=exc.expected,
                current=exc.current,
            ) from exc
        after = mutation.after
        _set_no_store_etag(response, _version_etag(int(after["version"])))
        set_admin_audit_context(
            request,
            target_type="wxbot_global_reply_policy",
            tenant_id=tenant_id,
            before_state=_policy_audit_summary(mutation.before),
            after_state=_policy_audit_summary(after),
            policy_version=int(after["version"]),
            trace_id=_request_trace_id(request),
            reason="conditional_policy_update",
        )
        return after

    @router.get("/admin/reply-policy/{tenant_id}/{session_id:path}")
    async def get_reply_policy(
        tenant_id: str,
        session_id: str,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        tenant, session, _principal = _require_session_admin(
            store,
            request,
            tenant_id,
            session_id,
        )
        policy = await store.get_session_policy(tenant, session)
        _set_no_store_etag(response, _version_etag(int(policy["version"])))
        return policy

    @router.post("/admin/reply-policy/{tenant_id}/{session_id:path}")
    async def set_reply_policy(
        tenant_id: str,
        session_id: str,
        body: WxbotSessionPolicyRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        tenant, session, _principal = _require_session_admin(
            store,
            request,
            tenant_id,
            session_id,
        )
        if all(value is None for value in body.model_dump().values()):
            raise HTTPException(400, "no_mutable_fields")
        expected_version = _required_version_if_match(request)
        trigger_keywords_text = body.trigger_keywords_text
        timing_updates = {
            key: value
            for key, value in {
                "reply_cooldown_seconds": body.reply_cooldown_seconds,
                "coalesce_window_ms": body.coalesce_window_ms,
                "adaptive_cooldown_enabled": body.adaptive_cooldown_enabled,
            }.items()
            if value is not None
        }
        try:
            mutation = await store.set_session_policy(
                tenant,
                session,
                expected_version=expected_version,
                reply_mode=(
                    _validate_group_reply_mode(
                        body.reply_mode,
                        "reply_mode",
                        allow_inherit=True,
                    )
                    if session.endswith("@chatroom")
                    else _validate_reply_mode(body.reply_mode)
                ),
                mention_sender_mode=_validate_mention_sender_mode(body.mention_sender_mode),
                trigger_keywords_text=trigger_keywords_text,
                participation_policy=(
                    body.participation_policy.model_dump(exclude_none=True)
                    if body.participation_policy is not None
                    else None
                ),
                **timing_updates,
            )
        except WxbotPolicyVersionConflictError as exc:
            raise _version_conflict(
                expected=exc.expected,
                current=exc.current,
            ) from exc
        after = mutation.after
        _set_no_store_etag(response, _version_etag(int(after["version"])))
        set_admin_audit_context(
            request,
            target_type="wxbot_session_reply_policy",
            tenant_id=tenant,
            session_id=session,
            before_state=_policy_audit_summary(mutation.before),
            after_state=_policy_audit_summary(after),
            policy_version=int(after["version"]),
            trace_id=_request_trace_id(request),
            reason="conditional_policy_update",
        )
        return after

    @router.get("/admin/session-state/{tenant_id}/{session_id:path}")
    async def get_session_state(
        tenant_id: str,
        session_id: str,
        request: Request,
        response: Response,
    ):
        tenant, session, _principal = _require_session_admin(
            store,
            request,
            tenant_id,
            session_id,
        )
        payload = await _load_session_status_payload(container, tenant, session)
        version = await _observe_admin_resource(
            store,
            tenant,
            f"session-state:{session}",
            resource_kind="session_state",
            state_payload=_session_state_config(payload),
        )
        payload["version"] = version
        _set_no_store_etag(response, _version_etag(version))
        return payload

    @router.post("/admin/session-state/{tenant_id}/{session_id:path}")
    async def set_session_state(
        tenant_id: str,
        session_id: str,
        body: WxbotSessionStateUpdateRequest,
        request: Request,
        response: Response,
    ):
        tenant, session, _principal = _require_session_admin(
            store,
            request,
            tenant_id,
            session_id,
        )
        expected_version = _required_version_if_match(request)
        target_state = _normalize_target_session_state(body)
        before_payload = await _load_session_status_payload(container, tenant, session)
        await _observe_admin_resource(
            store,
            tenant,
            f"session-state:{session}",
            resource_kind="session_state",
            state_payload=_session_state_config(before_payload),
        )
        desired_state = {
            "state": target_state.value,
            "auto_reply_enabled": target_state != SessionState.ESCALATED,
        }
        recovery_response = {
            "tenant_id": tenant,
            "session_id": session,
            **desired_state,
        }

        async def effect() -> _AdminEffectOutcome:
            payload = await _set_session_state(container, tenant, session, target_state)
            return _AdminEffectOutcome(payload, _session_state_config(payload))

        result, version, _replayed = await _execute_admin_mutation(
            store,
            request,
            tenant_id=tenant,
            operation="session_state_update",
            resource_key=f"session-state:{session}",
            request_payload=desired_state,
            expected_version=expected_version,
            desired_state=desired_state,
            recovery_response=recovery_response,
            effect=effect,
        )
        if isinstance(result, dict) and version is not None:
            result["version"] = version
        if version is not None:
            _set_no_store_etag(response, _version_etag(version))
        set_admin_audit_context(
            request,
            target_type="wxbot_session_state",
            tenant_id=tenant,
            session_id=session,
            before_state=_session_state_config(before_payload),
            after_state=desired_state,
            policy_version=int(version or 0),
            trace_id=_request_trace_id(request),
            reason="conditional_session_state_update",
        )
        return result

    from plugins.wxbot.admin_agent_event_routes import register_agent_event_routes

    register_agent_event_routes(
        router,
        store=store,
        bridge=bridge,
        container=container,
        agent_store=agent_store,
        scope_execution_allowed=scope_execution_allowed,
    )

    # Imported lazily to keep the public router facade import-compatible while
    # the report/self-review domain remains independently testable.
    from plugins.wxbot.admin_report_routes import register_report_routes

    register_report_routes(
        router,
        store=store,
        bridge=bridge,
        scheduler=scheduler,
        report_service=report_service,
        self_review_service=self_review_service,
    )

    @router.post("/admin/sdk/query/read")
    async def run_sdk_read_query(body: WxbotSdkReadQueryRequest, request: Request):
        tenant_id = _require_default_tenant_admin(store, request)
        query_payload = body.model_dump(exclude_none=True)
        payload = await _sdk_request(
            store,
            bridge,
            "POST",
            "/ext/query/read",
            json_body=query_payload,
        )
        safe_payload = _client_safe_media_payload(payload, store, tenant_id=tenant_id)
        rows = safe_payload.get("rows") if isinstance(safe_payload, dict) else []
        set_admin_audit_context(
            request,
            target_type="wxbot_sdk_read_query",
            tenant_id=tenant_id,
            after_state={
                "database": body.database,
                "sql_hash": hashlib.sha256(body.sql.encode("utf-8")).hexdigest(),
                "sql_chars": len(body.sql),
                "limit": body.limit,
                "row_count": len(rows) if isinstance(rows, list) else 0,
            },
            trace_id=_request_trace_id(request),
            reason="bounded_read_only_sdk_query",
        )
        return safe_payload

    @router.post("/admin/send")
    async def send_message(body: WxbotSendRequest, request: Request):
        principal = _require_admin(store, request)
        payload = body.model_dump()
        tenant_id = str(
            payload.pop("tenant_id", "")
            or getattr(store.settings, "wxbot_default_tenant_id", "default")
            or "default"
        ).strip()
        if not principal.allows_tenant(tenant_id):
            raise HTTPException(403, "tenant access denied")
        _validate_send_payload(payload, image_error_field="media_id")
        _resolve_send_media(payload, store, tenant_id=tenant_id)
        session_id = str(payload.get("session_id") or "").strip()
        if session_id.endswith("@chatroom"):
            await _require_verified_group(
                store,
                bridge,
                tenant_id=tenant_id,
                session_id=session_id,
            )

        async def effect() -> _AdminEffectOutcome:
            await _require_wxbot_scope_execution(
                scope_execution_allowed,
                tenant_id=tenant_id,
                session_id=session_id,
            )
            result = await _sdk_request(
                store,
                bridge,
                "POST",
                "/send",
                json_body=payload,
                request_headers={"Idempotency-Key": _required_idempotency_key(request)},
            )
            return _AdminEffectOutcome(
                _client_safe_media_payload(result, store, tenant_id=tenant_id)
            )

        result, _version, _replayed = await _execute_admin_mutation(
            store,
            request,
            tenant_id=tenant_id,
            operation="direct_send",
            resource_key=f"send:{session_id}",
            request_payload=payload,
            effect=effect,
        )
        set_admin_audit_context(
            request,
            target_type="wxbot_direct_send",
            tenant_id=tenant_id,
            session_id=session_id,
            after_state=_mutation_audit_summary(
                operation="send",
                affected_count=1,
                message_count=1,
                message_chars=len(str(payload.get("text") or "")),
            ),
            trace_id=_request_trace_id(request),
            reason="durable_direct_send",
        )
        return result

    @router.post("/admin/send/envelope")
    async def send_message_envelope(body: WxbotSendEnvelopeRequest, request: Request):
        principal = _require_admin(store, request)
        payload = body.model_dump()
        content = payload.get("content")
        target = payload.get("target")
        if not isinstance(content, dict):
            raise HTTPException(400, "content object required")
        if not isinstance(target, dict):
            raise HTTPException(400, "target object required")
        tenant_id = str(
            target.pop("tenant_id", "")
            or getattr(store.settings, "wxbot_default_tenant_id", "default")
            or "default"
        ).strip()
        if not principal.allows_tenant(tenant_id):
            raise HTTPException(403, "tenant access denied")
        _validate_send_payload(content, image_error_field="content.media_id")
        _resolve_send_media(content, store, tenant_id=tenant_id)
        session_id = str(target.get("session_id") or "").strip()
        if not session_id:
            raise HTTPException(400, "target.session_id required")
        if session_id.endswith("@chatroom"):
            await _require_verified_group(
                store,
                bridge,
                tenant_id=tenant_id,
                session_id=session_id,
            )

        async def effect() -> _AdminEffectOutcome:
            await _require_wxbot_scope_execution(
                scope_execution_allowed,
                tenant_id=tenant_id,
                session_id=session_id,
            )
            result = await _sdk_request(
                store,
                bridge,
                "POST",
                "/send/envelope",
                json_body=payload,
                request_headers={"Idempotency-Key": _required_idempotency_key(request)},
            )
            return _AdminEffectOutcome(
                _client_safe_media_payload(result, store, tenant_id=tenant_id)
            )

        result, _version, _replayed = await _execute_admin_mutation(
            store,
            request,
            tenant_id=tenant_id,
            operation="envelope_send",
            resource_key=f"send-envelope:{session_id}",
            request_payload=payload,
            effect=effect,
        )
        set_admin_audit_context(
            request,
            target_type="wxbot_envelope_send",
            tenant_id=tenant_id,
            session_id=session_id,
            after_state=_mutation_audit_summary(
                operation="send",
                affected_count=1,
                message_count=1,
                message_chars=len(str(content.get("text") or content.get("reply_text") or "")),
            ),
            trace_id=_request_trace_id(request),
            reason="durable_envelope_send",
        )
        return result

    @router.post("/admin/send/envelope/batch")
    async def send_message_envelope_batch(body: WxbotBatchSendEnvelopeRequest, request: Request):
        principal = _require_admin(store, request)
        messages = [item.model_dump() for item in body.messages]
        if not messages:
            raise HTTPException(400, "messages array required")
        tenant_ids: set[str] = set()
        group_session_ids: set[str] = set()
        scope_targets: set[tuple[str, str]] = set()
        for message in messages:
            content = message.get("content")
            target = message.get("target")
            if not isinstance(content, dict) or not isinstance(target, dict):
                raise HTTPException(400, "target and content objects required")
            tenant_id = str(
                target.pop("tenant_id", "")
                or getattr(store.settings, "wxbot_default_tenant_id", "default")
                or "default"
            ).strip()
            if not principal.allows_tenant(tenant_id):
                raise HTTPException(403, "tenant access denied")
            tenant_ids.add(tenant_id)
            _validate_send_payload(content, image_error_field="content.media_id")
            _resolve_send_media(content, store, tenant_id=tenant_id)
            session_id = str(target.get("session_id") or "").strip()
            if not session_id:
                raise HTTPException(400, "target.session_id required")
            scope_targets.add((tenant_id, session_id))
            if session_id.endswith("@chatroom"):
                group_session_ids.add(session_id)
        if len(tenant_ids) != 1:
            raise HTTPException(400, "batch messages must use one tenant")
        response_tenant = next(iter(tenant_ids))
        for group_session_id in sorted(group_session_ids):
            await _require_verified_group(
                store,
                bridge,
                tenant_id=response_tenant,
                session_id=group_session_id,
            )
        request_payload = {"messages": messages}

        async def effect() -> _AdminEffectOutcome:
            await _require_wxbot_scope_targets(
                scope_execution_allowed,
                scope_targets,
            )
            result = await _sdk_request(
                store,
                bridge,
                "POST",
                "/send/envelope/batch",
                json_body=request_payload,
                request_headers={"Idempotency-Key": _required_idempotency_key(request)},
            )
            return _AdminEffectOutcome(
                _client_safe_media_payload(result, store, tenant_id=response_tenant)
            )

        result, _version, _replayed = await _execute_admin_mutation(
            store,
            request,
            tenant_id=response_tenant,
            operation="envelope_batch_send",
            resource_key="send-envelope-batch",
            request_payload=request_payload,
            effect=effect,
        )
        set_admin_audit_context(
            request,
            target_type="wxbot_envelope_batch_send",
            tenant_id=response_tenant,
            after_state=_mutation_audit_summary(
                operation="send_batch",
                affected_count=len(messages),
                message_count=len(messages),
                message_chars=sum(
                    len(
                        str(
                            (item.get("content") or {}).get("text")
                            or (item.get("content") or {}).get("reply_text")
                            or ""
                        )
                    )
                    for item in messages
                ),
            ),
            trace_id=_request_trace_id(request),
            reason="durable_envelope_batch_send",
        )
        return result

    @router.post("/admin/send/batch")
    async def send_batch(body: WxbotBatchSendRequest, request: Request):
        principal = _require_admin(store, request)
        messages = [item.model_dump() for item in body.messages]
        if not messages:
            raise HTTPException(400, "messages array required")
        tenant_ids: set[str] = set()
        group_session_ids: set[str] = set()
        scope_targets: set[tuple[str, str]] = set()
        for message in messages:
            tenant_id = str(
                message.pop("tenant_id", "")
                or getattr(store.settings, "wxbot_default_tenant_id", "default")
                or "default"
            ).strip()
            if not principal.allows_tenant(tenant_id):
                raise HTTPException(403, "tenant access denied")
            tenant_ids.add(tenant_id)
            _validate_send_payload(message, image_error_field="media_id")
            _resolve_send_media(message, store, tenant_id=tenant_id)
            session_id = str(message.get("session_id") or "").strip()
            if not session_id:
                raise HTTPException(400, "session_id required")
            scope_targets.add((tenant_id, session_id))
            if session_id.endswith("@chatroom"):
                group_session_ids.add(session_id)
        if len(tenant_ids) != 1:
            raise HTTPException(400, "batch messages must use one tenant")
        response_tenant = next(iter(tenant_ids))
        for group_session_id in sorted(group_session_ids):
            await _require_verified_group(
                store,
                bridge,
                tenant_id=response_tenant,
                session_id=group_session_id,
            )
        request_payload = {"messages": messages}

        async def effect() -> _AdminEffectOutcome:
            await _require_wxbot_scope_targets(
                scope_execution_allowed,
                scope_targets,
            )
            result = await _sdk_request(
                store,
                bridge,
                "POST",
                "/send/batch",
                json_body=request_payload,
                request_headers={"Idempotency-Key": _required_idempotency_key(request)},
            )
            return _AdminEffectOutcome(
                _client_safe_media_payload(result, store, tenant_id=response_tenant)
            )

        result, _version, _replayed = await _execute_admin_mutation(
            store,
            request,
            tenant_id=response_tenant,
            operation="batch_send",
            resource_key="send-batch",
            request_payload=request_payload,
            effect=effect,
        )
        set_admin_audit_context(
            request,
            target_type="wxbot_batch_send",
            tenant_id=response_tenant,
            after_state=_mutation_audit_summary(
                operation="send_batch",
                affected_count=len(messages),
                message_count=len(messages),
                message_chars=sum(len(str(item.get("text") or "")) for item in messages),
            ),
            trace_id=_request_trace_id(request),
            reason="durable_batch_send",
        )
        return result

    return router
