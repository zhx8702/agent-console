"""Structured, privacy-minimizing audit trail for control-plane mutations."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import Counter
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import JSONResponse
from starlette.routing import Match

from app.admin.authorization import Principal, required_admin_permission
from app.common.config import Settings
from app.common.logging import get_logger

logger = get_logger(__name__)

ADMIN_AUDIT_WRITE_FAILURES = Counter(
    "agent_console_admin_audit_write_failures_total",
    "Administrative audit events that could not be written.",
    ("environment",),
)

_AUDITED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True)
class AdminAuditEvent:
    actor: str
    tenant_id: str
    method: str
    route: str
    status: int
    request_id: str
    source: str
    permission: str
    outcome: str
    occurred_at: str
    session_id: str = ""
    user_id: str = ""
    target_type: str = "admin_route"
    before_state: dict[str, object] | None = None
    after_state: dict[str, object] | None = None
    policy_version: int = 0
    trace_id: str = ""
    idempotency_key: str = ""
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class AdminAuditSink(Protocol):
    async def write(self, event: AdminAuditEvent) -> None: ...


class StructuredLogAdminAuditSink:
    async def write(self, event: AdminAuditEvent) -> None:
        logger.info("admin.audit", **event.as_dict())


class DatabaseAdminAuditSink:
    """Persist semantic audit records through the migrated ``audit_events`` table."""

    async def write(self, event: AdminAuditEvent) -> None:
        from app.infra.db import get_session_factory
        from app.models.social import AuditEventRow

        factory = get_session_factory()
        occurred_at = datetime.fromisoformat(event.occurred_at)
        async with factory() as session:
            session.add(
                AuditEventRow(
                    tenant_id=event.tenant_id,
                    session_id=event.session_id,
                    user_id=event.user_id,
                    actor=event.actor,
                    actor_kind=event.source,
                    action=f"{event.method.lower()}:{event.permission}"[:64],
                    target_type=event.target_type[:64],
                    before_state_json=event.before_state or {},
                    after_state_json=event.after_state or {},
                    policy_version=max(0, event.policy_version),
                    trace_id=event.trace_id[:128],
                    idempotency_key=event.idempotency_key[:128],
                    reason=(event.reason or event.outcome)[:2000],
                    created_at=occurred_at,
                )
            )
            await session.commit()


class CompositeAdminAuditSink:
    def __init__(self, *sinks: AdminAuditSink) -> None:
        self._sinks = sinks

    async def write(self, event: AdminAuditEvent) -> None:
        failures: list[Exception] = []
        for sink in self._sinks:
            try:
                await sink.write(event)
            except Exception as exc:  # pragma: no cover - summarized below
                failures.append(exc)
        if failures:
            raise RuntimeError("one or more admin audit sinks failed") from failures[0]


def set_admin_audit_context(
    request: Request,
    *,
    target_type: str,
    tenant_id: str = "",
    session_id: str = "",
    user_id: str = "",
    before_state: dict[str, object] | None = None,
    after_state: dict[str, object] | None = None,
    policy_version: int = 0,
    trace_id: str = "",
    reason: str = "",
) -> None:
    """Attach handler-owned, already-redacted semantic context to an audit event."""

    request.state.admin_audit_context = {
        "target_type": str(target_type or "admin_route"),
        # Scope values remain request-local and are pseudonymized before an
        # event reaches any sink. This covers body-scoped control-plane APIs
        # without persisting tenant, group, or member identifiers in clear.
        "tenant_id": str(tenant_id or "")[:512],
        "session_id": str(session_id or "")[:512],
        "user_id": str(user_id or "")[:512],
        "before_state": _audit_mapping(before_state),
        "after_state": _audit_mapping(after_state),
        "policy_version": max(0, int(policy_version or 0)),
        "trace_id": str(trace_id or "")[:128],
        "reason": str(reason or "")[:2000],
    }


def install_admin_audit_middleware(
    app: FastAPI,
    settings: Settings,
    *,
    sink: AdminAuditSink | None = None,
) -> None:
    """Audit control-plane mutations, including rejected and failed requests."""

    configured_sink = (
        sink
        if sink is not None
        else CompositeAdminAuditSink(
            StructuredLogAdminAuditSink(),
            DatabaseAdminAuditSink(),
        )
    )

    @app.middleware("http")
    async def admin_audit(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        if not _should_audit(request):
            return await call_next(request)

        request_id = _request_id()
        request.state.admin_request_id = request_id
        request.state.admin_route_template = _matched_route_template(app, request)

        # In production an administrative mutation may not execute without a
        # durable write-ahead audit marker.  The completion record still carries
        # the handler-owned semantic before/after diff; if that second write is
        # unavailable, the durable pending marker makes the incomplete audit
        # visible for reconciliation instead of silently losing all evidence.
        if settings.is_prod:
            _authenticate_for_write_ahead_audit(request, settings)
            attempt = replace(
                _build_event(
                    request,
                    settings,
                    request_id=request_id,
                    status_code=102,
                ),
                outcome="pending",
                reason="mutation_attempt_pending_completion",
            )
            if not await _write_required(configured_sink, attempt, settings):
                return JSONResponse(
                    status_code=503,
                    content={"detail": "admin_audit_unavailable"},
                    headers={
                        "Cache-Control": "no-store",
                        "X-Request-ID": request_id,
                    },
                )
        try:
            response = await call_next(request)
        except Exception:
            await _write_safely(
                configured_sink,
                _build_event(request, settings, request_id=request_id, status_code=500),
                settings,
            )
            raise

        await _write_safely(
            configured_sink,
            _build_event(
                request,
                settings,
                request_id=request_id,
                status_code=response.status_code,
            ),
            settings,
        )
        response.headers.setdefault("X-Request-ID", request_id)
        return response


def _should_audit(request: Request) -> bool:
    path = request.url.path
    return request.method.upper() in _AUDITED_METHODS and (
        path.startswith("/v1/admin") or path.startswith("/plugins/")
    )


def _build_event(
    request: Request,
    settings: Settings,
    *,
    request_id: str,
    status_code: int,
) -> AdminAuditEvent:
    principal = getattr(request.state, "admin_principal", None)
    permission = (
        getattr(request.state, "admin_permission", "")
        or required_admin_permission(
            request.method,
            _route_template(request),
        ).value
    )
    audit_context = _audit_context(request)
    return AdminAuditEvent(
        actor=_actor(principal, settings),
        tenant_id=(
            _context_scope_value(audit_context, "tenant_id", settings, namespace="tenant")
            or _tenant_scope(principal, settings)
        ),
        method=request.method.upper(),
        route=_route_template(request),
        status=status_code,
        request_id=request_id,
        source=_auth_source(request, settings, principal),
        permission=str(permission),
        outcome=_outcome(status_code),
        occurred_at=datetime.now(UTC).isoformat(),
        session_id=(
            _context_scope_value(audit_context, "session_id", settings, namespace="session")
            or _path_scope_value(
                request,
                settings,
                "session",
                "session_id",
                "group",
                "group_id",
                namespace="session",
            )
        ),
        user_id=(
            _context_scope_value(audit_context, "user_id", settings, namespace="user")
            or _path_scope_value(
                request,
                settings,
                "user",
                "user_id",
                "member",
                "member_id",
                namespace="user",
            )
        ),
        target_type=str(audit_context.get("target_type") or _target_type(request)),
        before_state=_audit_mapping(audit_context.get("before_state")),
        after_state=_audit_mapping(audit_context.get("after_state")),
        policy_version=_audit_nonnegative_int(audit_context.get("policy_version")),
        trace_id=str(audit_context.get("trace_id") or request_id)[:128],
        idempotency_key=_idempotency_audit_id(request, settings),
        reason=str(audit_context.get("reason") or "")[:2000],
    )


def _audit_context(request: Request) -> dict[str, object]:
    value = getattr(request.state, "admin_audit_context", None)
    return dict(value) if isinstance(value, dict) else {}


def _audit_mapping(value: Any) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    # Context is opt-in and must already be redacted. Still cap shape and avoid
    # accidental nested request bodies or binary values reaching audit storage.
    result: dict[str, object] = {}
    for key, item in list(value.items())[:64]:
        normalized_key = str(key)[:80]
        if isinstance(item, (str, int, float, bool)) or item is None:
            result[normalized_key] = item if not isinstance(item, str) else item[:500]
        elif isinstance(item, (list, tuple)):
            result[normalized_key] = [
                entry if not isinstance(entry, str) else entry[:200]
                for entry in list(item)[:40]
                if isinstance(entry, (str, int, float, bool)) or entry is None
            ]
    return result


def _audit_nonnegative_int(value: object) -> int:
    if not isinstance(value, (str, bytes, int, float)):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _path_scope_value(
    request: Request,
    settings: Settings,
    *names: str,
    namespace: str,
) -> str:
    for name in names:
        value = request.path_params.get(name)
        if value is not None and str(value).strip():
            return _pseudonym(str(value).strip(), settings, namespace=namespace)
    return ""


def _context_scope_value(
    audit_context: dict[str, object],
    key: str,
    settings: Settings,
    *,
    namespace: str,
) -> str:
    value = str(audit_context.get(key) or "").strip()
    return _pseudonym(value, settings, namespace=namespace) if value else ""


def _target_type(request: Request) -> str:
    path = _route_template(request)
    if path.endswith("/participation-policy"):
        return "social_group_policy"
    if path.endswith("/privacy-policy"):
        return "social_member_policy"
    if path.startswith("/plugins/"):
        parts = path.strip("/").split("/")
        return f"plugin:{parts[1] if len(parts) > 1 else 'unknown'}"
    return "admin_route"


def _idempotency_audit_id(request: Request, settings: Settings) -> str:
    value = request.headers.get("idempotency-key", "").strip()
    if not value:
        return ""
    return _pseudonym(value, settings, namespace="idem")


async def _write_safely(
    sink: AdminAuditSink,
    event: AdminAuditEvent,
    settings: Settings,
) -> None:
    try:
        await sink.write(event)
    except Exception as exc:
        environment = str(settings.app_env or "unknown").lower()
        ADMIN_AUDIT_WRITE_FAILURES.labels(environment=environment).inc()
        # Never log the exception message: sink failures can contain serialized
        # credentials or payload fragments.  The type and request metadata are
        # enough to alert operators and correlate the failed write.
        logger.error(
            "admin.audit_write_failed",
            environment=environment,
            error_type=type(exc).__name__,
            request_id=event.request_id,
            method=event.method,
            route=event.route,
            status=event.status,
        )


async def _write_required(
    sink: AdminAuditSink,
    event: AdminAuditEvent,
    settings: Settings,
) -> bool:
    try:
        await sink.write(event)
        return True
    except Exception as exc:
        environment = str(settings.app_env or "unknown").lower()
        ADMIN_AUDIT_WRITE_FAILURES.labels(environment=environment).inc()
        logger.error(
            "admin.audit_write_ahead_failed",
            environment=environment,
            error_type=type(exc).__name__,
            request_id=event.request_id,
            method=event.method,
            route=event.route,
        )
        return False


def _authenticate_for_write_ahead_audit(request: Request, settings: Settings) -> None:
    # Session creation validates the bearer inside its handler and must remain
    # available when direct bearer fallback for ordinary API calls is disabled.
    if request.url.path == "/v1/admin/auth/session":
        return
    try:
        from app.admin.auth_router import authenticate_admin_request

        authenticate_admin_request(request, settings)
    except HTTPException:
        # Rejections are still audited as anonymous attempts.  The normal auth
        # dependency remains the authority and produces the final 401/403.
        return


def _matched_route_template(app: FastAPI, request: Request) -> str:
    for route in app.routes:
        matcher = getattr(route, "matches", None)
        if not callable(matcher):
            continue
        match, _child_scope = matcher(request.scope)
        if match is Match.FULL:
            template = str(getattr(route, "path", "") or "")
            if template.startswith(("/v1/admin", "/plugins/")):
                return template
            # FastAPI may group included APIRoutes behind an internal router
            # object.  Its effective candidates retain the already-prefixed
            # path regex and method set, so resolve without ever copying the
            # sensitive concrete URL into the audit record.
            for candidate in tuple(getattr(route, "_effective_candidates", ()) or ()):
                path_regex = getattr(candidate, "path_regex", None)
                methods = set(getattr(candidate, "methods", ()) or ())
                if (
                    path_regex is not None
                    and path_regex.match(request.url.path)
                    and request.method.upper() in methods
                ):
                    candidate_path = str(getattr(candidate, "path", "") or "")
                    if candidate_path.startswith(("/v1/admin", "/plugins/")):
                        return candidate_path
            original_router = getattr(route, "original_router", None)
            for child_route in tuple(getattr(original_router, "routes", ()) or ()):
                child_match, _child_scope = child_route.matches(request.scope)
                if child_match is Match.FULL:
                    child_path = str(getattr(child_route, "path", "") or "")
                    if child_path.startswith(("/v1/admin", "/plugins/")):
                        return child_path
    if request.url.path.startswith("/v1/admin"):
        return "/v1/admin/<unmatched>"
    return "/plugins/<unmatched>"


def _request_id() -> str:
    # Request identifiers are emitted to both logs and responses.  Do not copy
    # an untrusted header into the audit record: a caller could deliberately put
    # a credential or personal identifier there.  The control plane owns this
    # opaque correlation ID instead.
    return f"admin_{secrets.token_hex(16)}"


def _route_template(request: Request) -> str:
    pre_resolved = str(getattr(request.state, "admin_route_template", "") or "")
    if pre_resolved.startswith(("/v1/admin", "/plugins/")):
        return pre_resolved
    route = request.scope.get("route")
    template = str(getattr(route, "path", "") or "")
    if template.startswith(("/v1/admin", "/plugins/")):
        return template
    if request.url.path.startswith("/v1/admin"):
        return "/v1/admin/<unmatched>"
    return "/plugins/<unmatched>"


def _actor(principal: object, settings: Settings) -> str:
    if not isinstance(principal, Principal):
        return "anonymous"
    subject = str(principal.subject or "").strip()
    if not subject:
        return "unknown"
    if subject == "admin":
        return subject
    return _pseudonym(subject, settings, namespace="actor")


def _tenant_scope(principal: object, settings: Settings) -> str:
    if not isinstance(principal, Principal) or not principal.tenant_ids:
        return ""
    if "*" in principal.tenant_ids:
        return "*"
    if len(principal.tenant_ids) == 1:
        tenant_id = str(principal.tenant_ids[0])
        return _pseudonym(tenant_id, settings, namespace="tenant")
    tenant_ids = "\0".join(sorted(str(value) for value in principal.tenant_ids))
    return _pseudonym(tenant_ids, settings, namespace="tenants")


def _auth_source(request: Request, settings: Settings, principal: object) -> str:
    if isinstance(principal, Principal):
        auth_kind = str(principal.auth_kind or "").lower()
        if auth_kind in {"bearer", "session", "test"}:
            return auth_kind
        return "authenticated"
    if request.headers.get("authorization"):
        return "bearer"
    if request.cookies.get(settings.admin_session_cookie_name):
        return "session"
    return "anonymous"


def _outcome(status_code: int) -> str:
    if status_code < 400:
        return "success"
    if status_code in {401, 403}:
        return "denied"
    if status_code < 500:
        return "rejected"
    return "error"


def _pseudonym(value: str, settings: Settings, *, namespace: str) -> str:
    """Return a stable audit identifier without persisting the source PII."""

    signing_material = str(
        settings.admin_session_signing_secret or settings.admin_bearer_token or ""
    )
    key = hashlib.sha256(
        b"agent-console-admin-audit\0" + signing_material.encode("utf-8")
    ).digest()
    digest = hmac.new(
        key,
        f"{namespace}\0{value}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{namespace}_{digest[:24]}"
