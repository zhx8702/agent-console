"""Administrator authentication shared by the console and plugin APIs.

The static bearer token remains available for CLI compatibility, but browser
clients exchange it for a short-lived, signed, HttpOnly cookie.  Keeping the
verification dependency here gives every mounted plugin the same deny-by-
default boundary instead of relying on individual plugin authors to remember
authentication.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.admin.authorization import AdminRole, Principal, permissions_for_roles
from app.common.config import Settings, get_settings

_ADMIN_HTTP_BEARER = HTTPBearer(auto_error=False)


def _encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_base64url(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}".encode("ascii"))


def _session_signing_key(settings: Settings) -> bytes:
    signing_secret = str(
        settings.admin_session_signing_secret or settings.admin_bearer_token or ""
    )
    if not signing_secret:
        raise RuntimeError("admin_session_signing_secret_required")
    return hashlib.sha256(
        b"agent-console-admin-session\0" + signing_secret.encode("utf-8")
    ).digest()


def _issue_session_cookie(
    settings: Settings,
    principal: Principal,
    *,
    now: int | None = None,
) -> tuple[str, int]:
    issued_at = int(time.time()) if now is None else int(now)
    expires_at = issued_at + settings.admin_session_ttl_seconds
    payload = {
        "sub": principal.subject,
        "roles": list(principal.roles),
        "tenant_ids": list(principal.tenant_ids),
        "group_ids": list(principal.group_ids),
        "iat": issued_at,
        "exp": expires_at,
        "v": 1,
    }
    encoded_payload = _encode_base64url(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signature = hmac.new(
        _session_signing_key(settings),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{encoded_payload}.{_encode_base64url(signature)}", expires_at


def _principal_from_session_cookie(
    settings: Settings,
    cookie_value: str,
    *,
    now: int | None = None,
) -> Principal | None:
    try:
        encoded_payload, encoded_signature = cookie_value.split(".", 1)
        supplied_signature = _decode_base64url(encoded_signature)
        expected_signature = hmac.new(
            _session_signing_key(settings),
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            return None
        payload = json.loads(_decode_base64url(encoded_payload))
        current_time = int(time.time()) if now is None else int(now)
        if int(payload.get("exp", 0)) <= current_time:
            return None
        subject = str(payload.get("sub") or "").strip()
        if not subject or int(payload.get("v", 0)) != 1:
            return None
        roles = tuple(str(role) for role in payload.get("roles", []))
        tenant_ids = tuple(str(tenant_id) for tenant_id in payload.get("tenant_ids", []))
        group_ids = tuple(str(group_id) for group_id in payload.get("group_ids", []))
        if not permissions_for_roles(roles):
            return None
        principal = Principal(
            subject=subject,
            roles=roles,
            tenant_ids=tenant_ids,
            auth_kind="session",
            group_ids=group_ids,
        )
        if not principal.tenant_ids:
            return None
        if principal.requires_explicit_group_scope and not principal.group_ids:
            return None
        return principal
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _bearer_principal(
    settings: Settings,
    credentials: HTTPAuthorizationCredentials | None,
) -> Principal | None:
    if credentials is None:
        return None
    supplied_token = credentials.credentials
    if settings.admin_bearer_token and secrets.compare_digest(
        supplied_token,
        settings.admin_bearer_token,
    ):
        return Principal(
            subject="admin",
            roles=(AdminRole.PLATFORM_ADMIN.value,),
            tenant_ids=("*",),
            auth_kind="bearer",
            group_ids=("*",),
        )

    supplied_digest = hashlib.sha256(supplied_token.encode("utf-8")).hexdigest()
    try:
        delegated = _delegated_principals(settings)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="admin_principal_config_invalid",
        ) from exc
    matched: Principal | None = None
    for token_digest, principal in delegated:
        if secrets.compare_digest(supplied_digest, token_digest):
            matched = replace(principal, auth_kind="bearer")
    if matched is not None:
        return matched
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="invalid_admin_bearer",
    )


def _delegated_principals(settings: Settings) -> tuple[tuple[str, Principal], ...]:
    raw = str(settings.admin_principal_tokens_json or "").strip()
    if not raw:
        return ()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid delegated principal JSON") from exc
    if not isinstance(payload, list):
        raise ValueError("delegated principals must be a list")

    allowed_roles = {role.value for role in AdminRole}
    seen_digests: set[str] = set()
    result: list[tuple[str, Principal]] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("delegated principal must be an object")
        token_digest = str(item.get("token_sha256") or "").strip().lower()
        subject = str(item.get("subject") or "").strip()
        roles = tuple(str(value).strip() for value in item.get("roles", []) if str(value).strip())
        tenant_ids = tuple(
            str(value).strip()
            for value in item.get("tenant_ids", [])
            if str(value).strip()
        )
        group_ids = tuple(
            str(value).strip()
            for value in item.get("group_ids", [])
            if str(value).strip()
        )
        if (
            len(token_digest) != 64
            or any(character not in "0123456789abcdef" for character in token_digest)
            or token_digest in seen_digests
            or not subject
            or len(subject) > 128
            or not roles
            or any(role not in allowed_roles for role in roles)
            or not tenant_ids
            or not permissions_for_roles(roles)
        ):
            raise ValueError("invalid delegated principal claims")
        principal = Principal(
            subject=subject,
            roles=roles,
            tenant_ids=tenant_ids,
            group_ids=group_ids,
            auth_kind="bearer",
        )
        if principal.requires_explicit_group_scope and not group_ids:
            raise ValueError("group-scoped principal requires group_ids")
        seen_digests.add(token_digest)
        result.append((token_digest, principal))
    return tuple(result)


def _admin_auth_configured(settings: Settings) -> bool:
    return bool(
        str(settings.admin_bearer_token or "").strip()
        or str(settings.admin_principal_tokens_json or "").strip()
    )


def _authenticate_admin(
    settings: Settings,
    *,
    credentials: HTTPAuthorizationCredentials | None,
    session_cookie: str | None,
) -> Principal:
    if not _admin_auth_configured(settings):
        raise HTTPException(status_code=503, detail="admin_auth_not_configured")

    if credentials is not None:
        if not bool(getattr(settings, "admin_allow_bearer_fallback", True)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="admin_bearer_fallback_disabled",
            )
        principal = _bearer_principal(settings, credentials)
        if principal is not None:
            return principal

    if session_cookie:
        try:
            principal = _principal_from_session_cookie(settings, session_cookie)
        except RuntimeError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="admin_session_signing_secret_required",
            ) from exc
        if principal is not None:
            return principal
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_or_expired_admin_session",
            headers={"WWW-Authenticate": "Session"},
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="missing_admin_bearer",
        headers={"WWW-Authenticate": "Bearer"},
    )


def authenticate_admin_request(
    request: Request,
    settings: Settings | None = None,
) -> Principal:
    """Authenticate imperative plugin checks with the same browser/session policy.

    Most plugin routers are protected at mount time, but a few routes also make
    authorization decisions inside handlers (for example, targeting another
    user's memory).  Those checks must understand the console's HttpOnly cookie
    as well as the CLI bearer fallback.
    """

    configured = settings or get_settings()
    authorization = request.headers.get("authorization", "").strip()
    credentials: HTTPAuthorizationCredentials | None = None
    if authorization:
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token.strip():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="invalid_admin_bearer",
            )
        credentials = HTTPAuthorizationCredentials(
            scheme=scheme,
            credentials=token.strip(),
        )

    principal = _authenticate_admin(
        configured,
        credentials=credentials,
        session_cookie=request.cookies.get(
            str(getattr(configured, "admin_session_cookie_name", "cs_admin_session"))
        ),
    )
    request.state.admin_principal = principal
    return principal


def is_admin_request(request: Request, settings: Settings | None = None) -> bool:
    """Return whether a request is authenticated without leaking auth failures."""

    try:
        authenticate_admin_request(request, settings)
    except HTTPException:
        return False
    return True


def build_admin_auth_dependency(
    settings: Settings | None = None,
) -> Callable[..., Awaitable[Principal]]:
    """Build the common authentication dependency for admin and plugin APIs."""

    configured = settings or get_settings()
    cookie_name = configured.admin_session_cookie_name

    async def require_admin(
        request: Request,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_ADMIN_HTTP_BEARER),
        ] = None,
        session_cookie: Annotated[
            str | None,
            Cookie(alias=cookie_name),
        ] = None,
    ) -> Principal:
        principal = _authenticate_admin(
            configured,
            credentials=credentials,
            session_cookie=session_cookie,
        )
        request.state.admin_principal = principal
        return principal

    return require_admin


def build_admin_auth_router(settings: Settings | None = None) -> APIRouter:
    """Return an isolated auth router that does not depend on databases or plugins."""

    configured = settings or get_settings()
    router = APIRouter(prefix="/v1/admin/auth", tags=["admin-auth"])
    require_admin = build_admin_auth_dependency(configured)

    @router.get("/session")
    async def get_admin_session(
        _principal: Annotated[Principal, Depends(require_admin)],
    ) -> dict[str, bool]:
        return {"authenticated": True}

    @router.get("/me")
    async def get_admin_principal(
        principal: Annotated[Principal, Depends(require_admin)],
    ) -> dict[str, object]:
        configured_default_tenant = str(
            getattr(configured, "wxbot_default_tenant_id", "default") or "default"
        ).strip()
        allowed_default_tenant = (
            configured_default_tenant
            if principal.allows_tenant(configured_default_tenant)
            else next(
                (
                    tenant_id
                    for tenant_id in principal.tenant_ids
                    if str(tenant_id).strip() and str(tenant_id).strip() != "*"
                ),
                "",
            )
        )
        return {
            "authenticated": True,
            "subject": principal.subject,
            "roles": list(principal.roles),
            "tenant_ids": list(principal.tenant_ids),
            "group_ids": list(principal.group_ids),
            "default_tenant_id": allowed_default_tenant,
            "access_scope": (
                "group" if principal.requires_explicit_group_scope else "tenant"
            ),
            "auth_kind": principal.auth_kind,
        }

    @router.post("/session")
    async def create_admin_session(
        request: Request,
        response: Response,
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Security(_ADMIN_HTTP_BEARER),
        ] = None,
    ) -> dict[str, object]:
        if not _admin_auth_configured(configured):
            raise HTTPException(status_code=503, detail="admin_auth_not_configured")
        if credentials is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing_admin_bearer",
                headers={"WWW-Authenticate": "Bearer"},
            )
        principal = _bearer_principal(configured, credentials)
        if principal is None:  # Defensive; credentials are required above.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing_admin_bearer",
            )
        request.state.admin_principal = principal
        try:
            cookie_value, expires_at = _issue_session_cookie(configured, principal)
        except RuntimeError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="admin_session_signing_secret_required",
            ) from exc
        response.set_cookie(
            key=configured.admin_session_cookie_name,
            value=cookie_value,
            max_age=configured.admin_session_ttl_seconds,
            expires=configured.admin_session_ttl_seconds,
            path="/",
            secure=configured.admin_session_cookie_secure,
            httponly=True,
            samesite="strict",
        )
        return {"authenticated": True, "expires_at": expires_at}

    @router.delete("/session", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_admin_session(response: Response) -> None:
        response.delete_cookie(
            key=configured.admin_session_cookie_name,
            path="/",
            secure=configured.admin_session_cookie_secure,
            httponly=True,
            samesite="strict",
        )

    return router
