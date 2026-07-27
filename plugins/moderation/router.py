"""
REST API for the moderation plugin.

Mounted at ``/plugins/moderation/`` by the plugin framework.
"""
from __future__ import annotations

from typing import Any

from fastapi import (
    APIRouter,
    Body,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)

from app.admin.audit import set_admin_audit_context
from app.admin.mutation_ledger import (
    MutationAudit,
    MutationChange,
    MutationIdempotencyConflictError,
    MutationIdentity,
    MutationOutcome,
    hash_identifier,
)
from app.common.request_models import StrictRequestModel
from app.common.safe_url import (
    OutboundURLPolicy,
    UnsafeOutboundURLError,
    split_allowed_hosts,
    validate_outbound_url,
)
from plugins.moderation.store import (
    VALID_REMINDER_MODES,
    ModerationConfigVersionConflictError,
    ModerationStore,
)


class ConfigUpdate(StrictRequestModel):
    enabled: bool | None = None
    webhook_url: str | None = None
    webhook_enabled: bool | None = None
    reminder_mode: str | None = None
    reminder_text: str | None = None


class KeywordEntry(StrictRequestModel):
    keyword: str
    enabled: bool | None = None


class KeywordUpsert(StrictRequestModel):
    keyword: str | None = None
    keywords: list[str | KeywordEntry] | None = None
    replace: bool = False


class KeywordDelete(StrictRequestModel):
    keyword: str | None = None
    keywords: list[str] | None = None
    clear_all: bool = False


def _keyword_entries_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw = payload.get("keywords")
    if raw is None and payload.get("keyword"):
        raw = [payload["keyword"]]
    if raw is None:
        return []

    entries: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            keyword = str(item.get("keyword") or "").strip()
            enabled = item.get("enabled")
        else:
            keyword = str(item or "").strip()
            enabled = True
        if keyword:
            entries.append({
                "keyword": keyword,
                "enabled": True if enabled is None else bool(enabled),
            })
    return entries


def build_moderation_router(store: ModerationStore) -> APIRouter:
    router = APIRouter()

    @router.get("/config/{tenant_id}/{session_id}")
    async def get_config(tenant_id: str, session_id: str, response: Response):
        config = await store.get_config(tenant_id, session_id)
        _set_version_headers(response, int(config["version"]))
        return config

    @router.post("/config/{tenant_id}/{session_id}")
    async def set_config(
        tenant_id: str,
        session_id: str,
        body: ConfigUpdate,
        request: Request,
        response: Response,
        if_match: str | None = Header(default=None, alias="If-Match"),
    ):
        updates = {k: v for k, v in body.model_dump().items() if v is not None}
        if not updates:
            raise HTTPException(status_code=400, detail="no_mutable_fields")
        expected_version = _required_if_match(if_match)
        reminder_mode = updates.get("reminder_mode")
        if reminder_mode is not None and reminder_mode not in VALID_REMINDER_MODES:
            raise HTTPException(400, "reminder_mode must be one of: off, append, replace")
        current = await store.get_config(tenant_id, session_id)
        current_version = int(current.get("version") or 0)
        if current_version != expected_version:
            raise _version_conflict(expected_version, current_version)
        webhook_url = str(updates.get("webhook_url", current.get("webhook_url")) or "").strip()
        webhook_enabled = bool(
            updates.get("webhook_enabled", current.get("webhook_enabled"))
        )
        if webhook_enabled:
            if not webhook_url:
                raise HTTPException(400, "webhook_url is required when webhook is enabled")
            settings = getattr(store, "settings", None)
            policy = OutboundURLPolicy(
                require_https=True,
                allowed_hosts=split_allowed_hosts(
                    getattr(
                        settings,
                        "moderation_webhook_allowed_hosts",
                        "qyapi.weixin.qq.com",
                    )
                ),
            )
            try:
                await validate_outbound_url(webhook_url, policy=policy)
            except UnsafeOutboundURLError as exc:
                raise HTTPException(400, f"unsafe webhook_url: {exc}") from exc
        try:
            mutation = await store.set_config(
                tenant_id,
                session_id,
                expected_version=expected_version,
                **updates,
            )
        except ModerationConfigVersionConflictError as exc:
            raise _version_conflict(exc.expected, exc.current) from exc
        after = mutation.after
        _set_version_headers(response, int(after["version"]))
        set_admin_audit_context(
            request,
            target_type="plugin_moderation_config",
            tenant_id=tenant_id,
            session_id=session_id,
            before_state=_config_audit_summary(mutation.before),
            after_state=_config_audit_summary(after),
            policy_version=int(after["version"]),
            trace_id=_trace_id(request),
            reason="conditional_config_update",
        )
        return after

    @router.get("/sessions/{tenant_id}")
    async def list_sessions(
        tenant_id: str,
        limit: int = Query(default=200, ge=1, le=500),
    ):
        rows = await store.list_sessions(tenant_id, limit=limit)
        return {"items": rows, "count": len(rows)}

    @router.get("/keywords/{tenant_id}/{session_id}")
    async def get_keywords(
        tenant_id: str,
        session_id: str,
        response: Response,
        enabled_only: bool = Query(default=False),
    ):
        rows, version = await store.get_keywords_resource(
            tenant_id,
            session_id,
            enabled_only=enabled_only,
        )
        _set_version_headers(response, version)
        return {"items": rows, "count": len(rows), "version": version}

    @router.post("/keywords/{tenant_id}/{session_id}")
    async def add_keywords(
        tenant_id: str,
        session_id: str,
        body: KeywordUpsert,
        request: Request,
        response: Response,
        if_match: str | None = Header(default=None, alias="If-Match"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        expected_version = _required_if_match(if_match)
        payload = body.model_dump()
        entries = _keyword_entries_from_payload(payload)
        replace = bool(payload.get("replace"))
        if not entries and not replace:
            raise HTTPException(400, "keyword or keywords required")
        entries = sorted(
            entries,
            key=lambda item: (str(item.get("keyword") or ""), bool(item.get("enabled"))),
        )

        if replace:
            operation_key = _required_idempotency_key(idempotency_key)
            mutation_holder: list[Any] = []

            async def mutate() -> MutationChange:
                mutation = await store.upsert_keywords(
                    tenant_id,
                    session_id,
                    entries,
                    replace=True,
                    expected_version=expected_version,
                )
                mutation_holder.append(mutation)
                result = _keyword_response(mutation, replace=True)
                return MutationChange(
                    response=result,
                    before_state=_keyword_audit_summary(
                        mutation.before,
                        expected_version,
                    ),
                    after_state=_keyword_audit_summary(
                        mutation.after,
                        mutation.version,
                    ),
                    resource_version=str(mutation.version),
                )

            try:
                outcome = await store.run_admin_mutation(
                    identity=MutationIdentity(
                        tenant_id=tenant_id,
                        plugin_name="moderation",
                        operation="moderation.keywords.replace",
                        resource_key=session_id,
                        idempotency_key=operation_key,
                        request_payload={
                            "expected_version": expected_version,
                            "entries": entries,
                            "replace": True,
                        },
                    ),
                    audit=_mutation_audit(
                        request,
                        scope={
                            "session_hash": hash_identifier(session_id),
                            "keyword_count": len(entries),
                            "replace": True,
                        },
                        reason_code="conditional_keyword_replace",
                    ),
                    mutate=mutate,
                )
            except ModerationConfigVersionConflictError as exc:
                raise _version_conflict(exc.expected, exc.current) from exc
            except MutationIdempotencyConflictError as exc:
                raise _idempotency_conflict() from exc
            result = dict(outcome.response)
            version = int(result.get("version") or 0)
            _set_version_headers(response, version)
            _set_mutation_headers(response, outcome)
            after_items = result.get("items")
            after_rows = after_items if isinstance(after_items, list) else []
            before_state = (
                _keyword_audit_summary(mutation_holder[0].before, expected_version)
                if mutation_holder
                else _keyword_audit_summary(after_rows, version)
            )
            set_admin_audit_context(
                request,
                target_type="plugin_moderation_keywords",
                tenant_id=tenant_id,
                session_id=session_id,
                before_state=before_state,
                after_state=_keyword_audit_summary(after_rows, version),
                policy_version=version,
                trace_id=_trace_id(request),
                reason=(
                    "conditional_keyword_replace_replay"
                    if outcome.replayed
                    else "conditional_keyword_replace"
                ),
            )
            return result

        try:
            mutation = await store.upsert_keywords(
                tenant_id,
                session_id,
                entries,
                replace=replace,
                expected_version=expected_version,
            )
        except ModerationConfigVersionConflictError as exc:
            raise _version_conflict(exc.expected, exc.current) from exc
        _set_version_headers(response, mutation.version)
        set_admin_audit_context(
            request,
            target_type="plugin_moderation_keywords",
            tenant_id=tenant_id,
            session_id=session_id,
            before_state=_keyword_audit_summary(mutation.before, expected_version),
            after_state=_keyword_audit_summary(mutation.after, mutation.version),
            policy_version=mutation.version,
            trace_id=_trace_id(request),
            reason="conditional_keyword_replace" if replace else "conditional_keyword_upsert",
        )
        return _keyword_response(mutation, replace=replace)

    @router.delete("/keywords/{tenant_id}/{session_id}")
    async def remove_keyword(
        tenant_id: str,
        session_id: str,
        request: Request,
        response: Response,
        if_match: str | None = Header(default=None, alias="If-Match"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        keyword: str | None = Query(default=None),
        body: KeywordDelete | None = Body(default=None),  # noqa: B008
    ):
        expected_version = _required_if_match(if_match)
        keywords: list[str] = []
        clear_all = False
        if keyword:
            keywords.append(keyword)
        if body:
            payload = body.model_dump()
            if payload.get("keyword"):
                keywords.append(str(payload["keyword"]))
            if payload.get("keywords"):
                keywords.extend(str(item) for item in payload["keywords"] or [])
            if payload.get("clear_all"):
                clear_all = True
                keywords = []
        if not keywords and not clear_all:
            raise HTTPException(
                status_code=400,
                detail="keyword, keywords, or clear_all=true required",
            )
        normalized_keywords = sorted(
            {
                str(item or "").strip()
                for item in keywords
                if str(item or "").strip()
            }
        )
        operation_key = _required_idempotency_key(idempotency_key)
        mutation_holder: list[Any] = []

        async def mutate() -> MutationChange:
            mutation = await store.remove_keywords(
                tenant_id,
                session_id,
                None if clear_all else normalized_keywords,
                expected_version=expected_version,
            )
            mutation_holder.append(mutation)
            result = _keyword_delete_response(
                mutation,
                removed=[] if clear_all else normalized_keywords,
                clear_all=clear_all,
            )
            return MutationChange(
                response=result,
                before_state=_keyword_audit_summary(
                    mutation.before,
                    expected_version,
                ),
                after_state=_keyword_audit_summary(
                    mutation.after,
                    mutation.version,
                ),
                resource_version=str(mutation.version),
            )

        try:
            outcome = await store.run_admin_mutation(
                identity=MutationIdentity(
                    tenant_id=tenant_id,
                    plugin_name="moderation",
                    operation="moderation.keywords.delete",
                    resource_key=session_id,
                    idempotency_key=operation_key,
                    request_payload={
                        "expected_version": expected_version,
                        "keywords": normalized_keywords,
                        "clear_all": clear_all,
                    },
                ),
                audit=_mutation_audit(
                    request,
                    scope={
                        "session_hash": hash_identifier(session_id),
                        "keyword_count": len(normalized_keywords),
                        "clear_all": clear_all,
                    },
                    reason_code="conditional_keyword_delete",
                ),
                mutate=mutate,
            )
        except ModerationConfigVersionConflictError as exc:
            raise _version_conflict(exc.expected, exc.current) from exc
        except MutationIdempotencyConflictError as exc:
            raise _idempotency_conflict() from exc
        result = dict(outcome.response)
        version = int(result.get("version") or 0)
        _set_version_headers(response, version)
        _set_mutation_headers(response, outcome)
        after_items = result.get("items")
        after_rows = after_items if isinstance(after_items, list) else []
        before_state = (
            _keyword_audit_summary(mutation_holder[0].before, expected_version)
            if mutation_holder
            else _keyword_audit_summary(after_rows, version)
        )
        set_admin_audit_context(
            request,
            target_type="plugin_moderation_keywords",
            tenant_id=tenant_id,
            session_id=session_id,
            before_state=before_state,
            after_state=_keyword_audit_summary(after_rows, version),
            policy_version=version,
            trace_id=_trace_id(request),
            reason=(
                "conditional_keyword_delete_replay"
                if outcome.replayed
                else "conditional_keyword_delete"
            ),
        )
        return result

    @router.get("/events/{tenant_id}")
    async def get_tenant_events(
        tenant_id: str,
        session_id: str = Query(default=""),
        action: str = Query(default=""),
        webhook_status: str = Query(default=""),
        keyword: str = Query(default=""),
        limit: int = Query(default=50, ge=1, le=200),
    ):
        rows = await store.get_events(
            tenant_id,
            session_id=session_id or None,
            action=action,
            webhook_status=webhook_status,
            keyword=keyword,
            limit=limit,
        )
        return {
            "items": rows,
            "count": len(rows),
            "filters": {
                "session_id": session_id,
                "action": action,
                "webhook_status": webhook_status,
                "keyword": keyword,
                "limit": limit,
            },
        }

    @router.get("/events/{tenant_id}/{session_id}")
    async def get_session_events(
        tenant_id: str,
        session_id: str,
        action: str = Query(default=""),
        webhook_status: str = Query(default=""),
        keyword: str = Query(default=""),
        limit: int = Query(default=50, ge=1, le=200),
    ):
        rows = await store.get_events(
            tenant_id,
            session_id=session_id,
            action=action,
            webhook_status=webhook_status,
            keyword=keyword,
            limit=limit,
        )
        return {
            "items": rows,
            "count": len(rows),
            "filters": {
                "session_id": session_id,
                "action": action,
                "webhook_status": webhook_status,
                "keyword": keyword,
                "limit": limit,
            },
        }

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


def _config_audit_summary(config: dict[str, Any]) -> dict[str, object]:
    reminder_text = str(config.get("reminder_text") or "")
    return {
        "version": int(config.get("version") or 0),
        "enabled": bool(config.get("enabled")),
        "webhook_enabled": bool(config.get("webhook_enabled")),
        "has_webhook_url": bool(str(config.get("webhook_url") or "").strip()),
        "reminder_mode": str(config.get("reminder_mode") or "off")[:16],
        "reminder_text_length": len(reminder_text),
    }


def _keyword_audit_summary(
    keywords: list[dict[str, Any]],
    version: int,
) -> dict[str, object]:
    return {
        "version": int(version),
        "keyword_count": len(keywords),
        "enabled_keyword_count": sum(
            1 for item in keywords if bool(item.get("enabled", True))
        ),
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
    response.headers["X-Mutation-ID"] = outcome.mutation_id
    if outcome.replayed:
        response.headers["Idempotent-Replayed"] = "true"


def _keyword_response(mutation: Any, *, replace: bool) -> dict[str, Any]:
    return {
        "items": mutation.after,
        "count": len(mutation.after),
        "replace": replace,
        "version": mutation.version,
    }


def _keyword_delete_response(
    mutation: Any,
    *,
    removed: list[str],
    clear_all: bool,
) -> dict[str, Any]:
    return {
        "items": mutation.after,
        "count": len(mutation.after),
        "removed": removed,
        "clear_all": clear_all,
        "version": mutation.version,
    }
