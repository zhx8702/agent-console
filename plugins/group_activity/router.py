from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response, status
from pydantic import field_validator

from app.admin.audit import set_admin_audit_context
from app.admin.mutation_ledger import (
    MutationAudit,
    MutationChange,
    MutationIdempotencyConflictError,
    MutationIdentity,
    MutationOutcome,
    hash_identifier,
)
from app.agent.scopes import DEFAULT_AGENT_SCOPE, normalize_agent_scope
from app.common.request_models import StrictRequestModel
from plugins.group_activity.service import GroupActivityService
from plugins.group_activity.store import (
    GroupActivityConfigVersionConflictError,
    GroupActivityStore,
)

_TIME_RE = re.compile(r"^\d{2}:\d{2}$")
_ALLOWED_TIERS = {"tier-1", "tier-2", "tier-3"}


class GroupActivityConfigUpdate(StrictRequestModel):
    session_name: str | None = None
    enabled: bool | None = None
    active_start: str | None = None
    active_end: str | None = None
    quiet_start: str | None = None
    quiet_end: str | None = None
    timezone: str | None = None
    idle_minutes: int | None = None
    lookback_minutes: int | None = None
    min_send_interval_minutes: int | None = None
    max_per_day: int | None = None
    topic_repeat_window_minutes: int | None = None
    llm_model_tier: str | None = None
    temperature: float | None = None
    agent_tool_scope: str | None = None

    @field_validator("active_start", "active_end", "quiet_start", "quiet_end")
    @classmethod
    def validate_time(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _TIME_RE.match(value):
            raise ValueError("time must be HH:MM")
        hour, minute = value.split(":", 1)
        if int(hour) > 23 or int(minute) > 59:
            raise ValueError("time must be HH:MM")
        return value

    @field_validator("idle_minutes")
    @classmethod
    def validate_idle(cls, value: int | None) -> int | None:
        if value is not None and value < 180:
            raise ValueError("idle_minutes must be at least 180")
        return value

    @field_validator("lookback_minutes")
    @classmethod
    def validate_lookback(cls, value: int | None) -> int | None:
        if value is not None and value < 60:
            raise ValueError("lookback_minutes must be at least 60")
        return value

    @field_validator("min_send_interval_minutes")
    @classmethod
    def validate_interval(cls, value: int | None) -> int | None:
        if value is not None and value < 60:
            raise ValueError("min_send_interval_minutes must be at least 60")
        return value

    @field_validator("max_per_day")
    @classmethod
    def validate_max_per_day(cls, value: int | None) -> int | None:
        if value is not None and not 1 <= value <= 3:
            raise ValueError("max_per_day must be between 1 and 3")
        return value

    @field_validator("topic_repeat_window_minutes")
    @classmethod
    def validate_topic_repeat_window(cls, value: int | None) -> int | None:
        if value is not None and not 60 <= value <= 10080:
            raise ValueError("topic_repeat_window_minutes must be between 60 and 10080")
        return value

    @field_validator("llm_model_tier")
    @classmethod
    def validate_tier(cls, value: str | None) -> str | None:
        if value is not None and value not in _ALLOWED_TIERS:
            raise ValueError("llm_model_tier must be tier-1, tier-2, or tier-3")
        return value

    @field_validator("temperature")
    @classmethod
    def validate_temperature(cls, value: float | None) -> float | None:
        if value is not None and not 0 <= value <= 2:
            raise ValueError("temperature must be between 0 and 2")
        return value

    @field_validator("agent_tool_scope")
    @classmethod
    def validate_scope(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_agent_scope(value) or DEFAULT_AGENT_SCOPE


class TriggerRequest(StrictRequestModel):
    dry_run: bool = True
    force: bool = False


def build_group_activity_router(store: GroupActivityStore, service: GroupActivityService) -> APIRouter:
    router = APIRouter()

    @router.get("/config/{tenant_id}/{session_id:path}")
    async def get_config(tenant_id: str, session_id: str, response: Response):
        config = await store.get_config(tenant_id, session_id)
        _set_version_headers(response, int(config["version"]))
        return config

    @router.post("/config/{tenant_id}/{session_id:path}")
    async def set_config(
        tenant_id: str,
        session_id: str,
        body: GroupActivityConfigUpdate,
        request: Request,
        response: Response,
        if_match: str | None = Header(default=None, alias="If-Match"),
    ):
        if not body.model_dump(exclude_none=True):
            raise HTTPException(status_code=400, detail="no_mutable_fields")
        expected_version = _required_if_match(if_match)
        try:
            mutation = await store.set_config(
                tenant_id,
                session_id,
                expected_version=expected_version,
                session_name=body.session_name,
                enabled=body.enabled,
                active_start=body.active_start,
                active_end=body.active_end,
                quiet_start=body.quiet_start,
                quiet_end=body.quiet_end,
                timezone=body.timezone,
                idle_minutes=body.idle_minutes,
                lookback_minutes=body.lookback_minutes,
                min_send_interval_minutes=body.min_send_interval_minutes,
                max_per_day=body.max_per_day,
                topic_repeat_window_minutes=body.topic_repeat_window_minutes,
                llm_model_tier=body.llm_model_tier,
                temperature=body.temperature,
                agent_tool_scope=body.agent_tool_scope,
            )
        except GroupActivityConfigVersionConflictError as exc:
            raise _version_conflict(exc.expected, exc.current) from exc
        after = mutation.after
        _set_version_headers(response, int(after["version"]))
        set_admin_audit_context(
            request,
            target_type="plugin_group_activity_config",
            tenant_id=tenant_id,
            session_id=session_id,
            before_state=_audit_summary(mutation.before),
            after_state=_audit_summary(after),
            policy_version=int(after["version"]),
            trace_id=_trace_id(request),
            reason="conditional_config_update",
        )
        return after

    @router.get("/configs/{tenant_id}")
    async def list_configs(
        tenant_id: str,
        enabled: bool | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
    ):
        return {"items": await store.list_configs(tenant_id, enabled=enabled, limit=limit)}

    @router.get("/events/{tenant_id}")
    async def list_events(
        tenant_id: str,
        session_id: str | None = Query(default=None),
        status: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
    ):
        return {"items": await store.list_events(tenant_id, session_id=session_id, status=status, limit=limit)}

    @router.post("/trigger/{tenant_id}/{session_id:path}")
    async def trigger(
        tenant_id: str,
        session_id: str,
        request: Request,
        response: Response,
        body: TriggerRequest | None = None,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        payload = body or TriggerRequest()
        if payload.dry_run:
            try:
                decision = await service.process_session(
                    tenant_id,
                    session_id,
                    dry_run=True,
                    force=payload.force,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return decision.as_dict()

        operation_key = _required_idempotency_key(idempotency_key)

        async def mutate() -> MutationChange:
            try:
                decision = await service.process_session(
                    tenant_id,
                    session_id,
                    dry_run=False,
                    force=payload.force,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            result = _safe_decision_payload(decision.as_dict())
            return MutationChange(
                response=result,
                before_state={"execution_requested": True},
                after_state=_decision_audit_state(result),
                resource_version=str(result.get("event_id") or result.get("command_id") or ""),
            )

        try:
            outcome = await store.run_admin_mutation(
                identity=MutationIdentity(
                    tenant_id=tenant_id,
                    plugin_name="group_activity",
                    operation="group_activity.trigger",
                    resource_key=session_id,
                    idempotency_key=operation_key,
                    request_payload={
                        "tenant_id": tenant_id,
                        "session_id": session_id,
                        "dry_run": False,
                        "force": payload.force,
                    },
                ),
                audit=_mutation_audit(
                    request,
                    scope={
                        "session_hash": hash_identifier(session_id),
                        "forced": payload.force,
                    },
                    reason_code="manual_group_activity_trigger",
                ),
                mutate=mutate,
            )
        except MutationIdempotencyConflictError as exc:
            raise _idempotency_conflict() from exc
        _set_mutation_headers(response, outcome)
        return outcome.response

    @router.post("/scheduler/run-once")
    async def run_once(
        request: Request,
        response: Response,
        limit: int = Query(default=200, ge=1, le=1000),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        operation_key = _required_idempotency_key(idempotency_key)

        async def mutate() -> MutationChange:
            result = _safe_scheduler_payload(
                await service.process_due_sessions(limit=limit)
            )
            return MutationChange(
                response=result,
                before_state={"execution_requested": True},
                after_state={"processed": int(result.get("processed") or 0)},
                resource_version=str(result.get("processed") or 0),
            )

        try:
            outcome = await store.run_admin_mutation(
                identity=MutationIdentity(
                    tenant_id="__platform__",
                    plugin_name="group_activity",
                    operation="group_activity.scheduler.run_once",
                    resource_key="all_enabled_groups",
                    idempotency_key=operation_key,
                    request_payload={"limit": limit},
                ),
                audit=_mutation_audit(
                    request,
                    scope={"limit": limit},
                    reason_code="manual_group_activity_scheduler_run",
                ),
                mutate=mutate,
            )
        except MutationIdempotencyConflictError as exc:
            raise _idempotency_conflict() from exc
        _set_mutation_headers(response, outcome)
        return outcome.response

    return router


def _required_if_match(value: str | None) -> int:
    if value is None or not value.strip():
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="if_match_required",
        )
    normalized = value.strip()
    if normalized.startswith("W/"):
        normalized = normalized[2:].strip()
    if len(normalized) >= 2 and normalized[0] == '"' and normalized[-1] == '"':
        normalized = normalized[1:-1]
    if not normalized.isdigit():
        raise HTTPException(status_code=400, detail="invalid_if_match")
    return int(normalized)


def _etag(version: int) -> str:
    return f'"{max(0, int(version))}"'


def _set_version_headers(response: Response, version: int) -> None:
    response.headers["ETag"] = _etag(version)
    response.headers["Cache-Control"] = "no-store"


def _version_conflict(expected: int, current: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "version_conflict",
            "expected_version": expected,
            "current_version": current,
        },
        headers={"ETag": _etag(current), "Cache-Control": "no-store"},
    )


def _audit_summary(config: dict[str, Any]) -> dict[str, object]:
    return {
        "version": int(config.get("version") or 0),
        "enabled": bool(config.get("enabled")),
        "active_start": str(config.get("active_start") or ""),
        "active_end": str(config.get("active_end") or ""),
        "quiet_start": str(config.get("quiet_start") or ""),
        "quiet_end": str(config.get("quiet_end") or ""),
        "timezone": str(config.get("timezone") or "")[:64],
        "idle_minutes": int(config.get("idle_minutes") or 0),
        "lookback_minutes": int(config.get("lookback_minutes") or 0),
        "min_send_interval_minutes": int(
            config.get("min_send_interval_minutes") or 0
        ),
        "max_per_day": int(config.get("max_per_day") or 0),
        "topic_repeat_window_minutes": int(
            config.get("topic_repeat_window_minutes") or 0
        ),
        "llm_model_tier": str(config.get("llm_model_tier") or "")[:32],
        "temperature": float(config.get("temperature") or 0),
        "agent_tool_scope": str(config.get("agent_tool_scope") or "")[:64],
    }


def _trace_id(request: Request) -> str:
    return str(
        request.headers.get("X-Trace-ID")
        or request.headers.get("X-Request-ID")
        or getattr(request.state, "admin_request_id", "")
    ).strip()[:128]


def _required_idempotency_key(value: str | None) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail={"code": "idempotency_key_required"},
        )
    if len(normalized) > 128:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_idempotency_key"},
        )
    return normalized


def _idempotency_conflict() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": "idempotency_key_conflict"},
    )


def _mutation_audit(
    request: Request,
    *,
    scope: dict[str, object],
    reason_code: str,
) -> MutationAudit:
    principal = getattr(request.state, "admin_principal", None)
    return MutationAudit(
        actor=str(getattr(principal, "subject", "") or "unknown")[:128],
        actor_kind=str(getattr(principal, "auth_kind", "") or "unknown")[:32],
        roles=tuple(str(role)[:64] for role in (getattr(principal, "roles", ()) or ())),
        scope=scope,
        reason_code=reason_code,
        trace_id=_trace_id(request),
    )


def _set_mutation_headers(response: Response, outcome: MutationOutcome) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Mutation-ID"] = outcome.mutation_id
    if outcome.replayed:
        response.headers["Idempotent-Replayed"] = "true"


def _safe_decision_payload(payload: dict[str, Any]) -> dict[str, Any]:
    safe_keys = (
        "status",
        "reason",
        "reason_code",
        "event_id",
        "reply_queue_id",
        "command_id",
        "voice_profile_reason",
    )
    result = {key: payload[key] for key in safe_keys if key in payload}
    config = payload.get("config")
    if isinstance(config, dict):
        result["config_version"] = int(config.get("version") or 0)
    return result


def _safe_scheduler_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw_items = payload.get("items")
    items = raw_items if isinstance(raw_items, list) else None
    result: dict[str, Any] = {
        "processed": int(payload.get("processed") or len(items or [])),
    }
    if items is not None:
        result["items"] = [
            _safe_decision_payload(item)
            for item in items
            if isinstance(item, dict)
        ]
    return result


def _decision_audit_state(payload: dict[str, Any]) -> dict[str, object]:
    return {
        "status": str(payload.get("status") or "")[:32],
        "reason_hash": hash_identifier(
            str(payload.get("reason_code") or payload.get("reason") or "")
        ),
        "event_id": int(payload.get("event_id") or 0),
        "queued": bool(payload.get("reply_queue_id") or payload.get("command_id")),
    }
