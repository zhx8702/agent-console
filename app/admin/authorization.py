"""Role and permission policy for the administrative control plane."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Any, NoReturn, get_args, get_origin

from fastapi import Depends, HTTPException, Request, status

from app.common.config import Settings, get_settings


class AdminPermission(StrEnum):
    READ = "admin:read"
    WRITE = "admin:write"
    DANGER = "admin:danger"


class RoutePermissionEnforcement(StrEnum):
    """Where a declared control-plane permission is enforced."""

    ADMIN_GUARD = "admin_guard"
    AUTH_HANDLER = "auth_handler"
    PUBLIC_SESSION_CLEAR = "public_session_clear"


@dataclass(frozen=True)
class RoutePermission:
    """Exact, reviewable permission declaration for one HTTP route."""

    method: str
    path: str
    permission: AdminPermission
    enforcement: RoutePermissionEnforcement = RoutePermissionEnforcement.ADMIN_GUARD

    @property
    def key(self) -> tuple[str, str]:
        return (
            str(self.method or "").upper(),
            "/" + str(self.path or "").strip().strip("/").lower(),
        )


ROUTE_PERMISSIONS_ATTR = "_agent_console_route_permissions"


class AdminRole(StrEnum):
    PLATFORM_ADMIN = "platform_admin"
    PLATFORM_OPERATOR = "platform_operator"
    PLATFORM_READER = "platform_reader"
    TENANT_ADMIN = "tenant_admin"
    GROUP_OPERATOR = "group_operator"
    MODERATOR = "moderator"
    REVIEWER = "reviewer"
    OBSERVER = "observer"
    SERVICE_ACCOUNT = "service_account"


_ROLE_PERMISSIONS: dict[str, frozenset[AdminPermission]] = {
    AdminRole.PLATFORM_ADMIN: frozenset(AdminPermission),
    AdminRole.PLATFORM_OPERATOR: frozenset({AdminPermission.READ, AdminPermission.WRITE}),
    AdminRole.PLATFORM_READER: frozenset({AdminPermission.READ}),
    AdminRole.TENANT_ADMIN: frozenset(AdminPermission),
    AdminRole.GROUP_OPERATOR: frozenset({AdminPermission.READ, AdminPermission.WRITE}),
    AdminRole.MODERATOR: frozenset({AdminPermission.READ, AdminPermission.WRITE}),
    AdminRole.REVIEWER: frozenset({AdminPermission.READ, AdminPermission.WRITE}),
    AdminRole.OBSERVER: frozenset({AdminPermission.READ}),
    AdminRole.SERVICE_ACCOUNT: frozenset({AdminPermission.READ, AdminPermission.WRITE}),
}

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_DANGEROUS_ADMIN_EXACT = frozenset(
    {
        ("POST", "/v1/admin/runtime/llm-config"),
        ("POST", "/v1/admin/message-flows/effects/probe"),
        ("POST", "/v1/admin/plugins/install"),
    }
)
_DANGEROUS_PLUGIN_MARKERS = (
    "/adjust",
    "/transfer",
    "/trigger/",
    "/clear",
    "/forget",
    "/maintenance",
    "/backfill",
    "/vector-rebuild",
    "/recover-stale",
    "/retry",
    "/resend-callback",
    "/run-once",
    "/apply-job",
    "/agent-tools/policy/",
    "/event-subscriptions",
    "/reports/send",
    "/admin/send",
)
_DANGEROUS_PLUGIN_EXACT = frozenset(
    {
        ("POST", "/plugins/amap/admin/config"),
        ("POST", "/plugins/wxbot/admin/sdk/debug/trigger-config"),
        ("POST", "/plugins/wxbot/admin/sdk/query/read"),
        (
            "POST",
            "/plugins/wxbot/admin/group-members/settings/{session_id:path}",
        ),
        (
            "POST",
            "/plugins/wxbot/admin/self-review/jobs/{job_id}/publish",
        ),
    }
)

# These endpoints expose static capability metadata only. Every other plugin
# route without a declared tenant_id is treated as platform-global/opaque and
# therefore requires the principal's explicit ``*`` tenant scope. Keeping this
# allowlist intentionally small makes new tenantless plugin routes fail closed.
_TENANT_NEUTRAL_PLUGIN_READ_ROUTES = frozenset(
    {
        ("GET", "/plugins/commands/catalog"),
        ("GET", "/plugins/wxbot/admin/agent-tools/catalog"),
    }
)

# These collection/metadata reads are explicitly filtered by their handlers or
# contain no group data.  They are the only group-less routes available to a
# group-scoped principal; every other tenant-level route still fails closed.
_GROUP_SCOPE_SAFE_COLLECTION_ROUTES = frozenset(
    {
        ("GET", "/plugins/wxbot/admin/roster/groups"),
        ("GET", "/plugins/wxbot/admin/sessions"),
        ("GET", "/v1/admin/tenants/{tenant_id}/capabilities"),
    }
)


@dataclass(frozen=True)
class Principal:
    """Authenticated control-plane actor with explicit tenant and group scopes."""

    subject: str
    roles: tuple[str, ...]
    tenant_ids: tuple[str, ...]
    auth_kind: str
    group_ids: tuple[str, ...] = ()

    @property
    def is_platform_admin(self) -> bool:
        return AdminRole.PLATFORM_ADMIN in self.roles

    @property
    def permissions(self) -> frozenset[AdminPermission]:
        return permissions_for_roles(self.roles)

    def allows(self, permission: AdminPermission) -> bool:
        return permission in self.permissions

    @property
    def has_global_tenant_scope(self) -> bool:
        platform_roles = {
            AdminRole.PLATFORM_ADMIN.value,
            AdminRole.PLATFORM_OPERATOR.value,
            AdminRole.PLATFORM_READER.value,
        }
        return "*" in _normalized_tenant_scopes(self.tenant_ids) and bool(
            platform_roles.intersection(self.roles)
        )

    def allows_tenant(self, tenant_id: str) -> bool:
        normalized = str(tenant_id or "").strip()
        scopes = _normalized_tenant_scopes(self.tenant_ids)
        return bool(normalized) and (
            self.has_global_tenant_scope or normalized in scopes
        )

    @property
    def requires_explicit_group_scope(self) -> bool:
        """Whether this principal must name every group it may operate on.

        Platform and tenant-wide roles deliberately inherit every group inside
        an allowed tenant.  Group operators, moderators, reviewers, observers,
        and service accounts fail closed unless a concrete group scope is in
        the authenticated claim.
        """

        tenant_wide_roles = {
            AdminRole.PLATFORM_ADMIN.value,
            AdminRole.PLATFORM_OPERATOR.value,
            AdminRole.PLATFORM_READER.value,
            AdminRole.TENANT_ADMIN.value,
        }
        group_scoped_roles = {
            AdminRole.GROUP_OPERATOR.value,
            AdminRole.MODERATOR.value,
            AdminRole.REVIEWER.value,
            AdminRole.OBSERVER.value,
            AdminRole.SERVICE_ACCOUNT.value,
        }
        role_set = frozenset(self.roles)
        return bool(role_set.intersection(group_scoped_roles)) and not bool(
            role_set.intersection(tenant_wide_roles)
        )

    def allows_group(self, tenant_id: str, session_id: str) -> bool:
        tenant = str(tenant_id or "").strip()
        session = str(session_id or "").strip()
        if not tenant or not session or not self.allows_tenant(tenant):
            return False
        if not self.requires_explicit_group_scope:
            return True
        scopes = _normalized_group_scopes(self.group_ids)
        return session in scopes or f"{tenant}:{session}" in scopes


@dataclass(frozen=True)
class _PluginTenantContext:
    tenant_ids: tuple[str, ...]
    validation_pending: bool = False
    invalid_explicit_value: bool = False


def permissions_for_roles(roles: tuple[str, ...] | list[str]) -> frozenset[AdminPermission]:
    permissions: set[AdminPermission] = set()
    for role in roles:
        permissions.update(_ROLE_PERMISSIONS.get(str(role), frozenset()))
    return frozenset(permissions)


def required_admin_permission(method: str, path: str) -> AdminPermission:
    """Classify a management request without inspecting its body or query values."""

    normalized_method = str(method or "").upper()
    normalized_path = "/" + str(path or "").strip().strip("/").lower()
    if normalized_method in _SAFE_METHODS:
        return AdminPermission.READ
    if _is_dangerous_request(normalized_method, normalized_path):
        return AdminPermission.DANGER
    return AdminPermission.WRITE


def enforce_admin_permission(
    principal: Principal,
    permission: AdminPermission,
) -> Principal:
    if not principal.allows(permission):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin_permission_denied",
        )
    return principal


def build_admin_authorization_dependency(
    settings: Settings | None = None,
    *,
    permission: AdminPermission | None = None,
    authentication_dependency: Callable[..., Any] | None = None,
) -> Callable[..., Any]:
    """Build the shared FastAPI control-plane authorization dependency.

    The production app binds exact :class:`RoutePermission` declarations at
    startup.  Apps which opt into strict route permissions fail closed if a
    matched route has no declaration.  The method-derived fallback is retained
    only for isolated compatibility apps which have not enabled strict mode.
    """

    configured = settings or get_settings()
    if authentication_dependency is None:
        # Lazy import avoids a module cycle: auth_router owns credential parsing,
        # while this module owns the Principal and authorization policy.
        from app.admin.auth_router import build_admin_auth_dependency

        authentication_dependency = build_admin_auth_dependency(configured)

    async def require_permission(
        request: Request,
        principal: Annotated[Principal, Depends(authentication_dependency)],
    ) -> Principal:
        route = request.scope.get("route")
        policy_path = str(getattr(route, "path", "") or request.url.path)
        required = permission or _bound_route_permission(request, route)
        if required is None:
            if bool(getattr(request.app.state, "route_permissions_strict", False)):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="admin_route_permission_undeclared",
                )
            required = required_admin_permission(request.method, policy_path)
        request.state.admin_principal = principal
        request.state.admin_permission = required.value
        enforce_admin_permission(principal, required)
        if request.url.path.startswith("/plugins/"):
            await enforce_plugin_tenant_scope(request, principal, settings=configured)
        elif request.url.path.startswith("/v1/admin/"):
            await enforce_admin_tenant_scope(request, principal)
        return principal

    return require_permission


def _bound_route_permission(request: Request, route: object) -> AdminPermission | None:
    declarations = getattr(route, ROUTE_PERMISSIONS_ATTR, None)
    if not isinstance(declarations, Mapping):
        return None
    declaration = declarations.get(str(request.method or "").upper())
    if not isinstance(declaration, RoutePermission):
        return None
    return declaration.permission


async def enforce_plugin_tenant_scope(
    request: Request,
    principal: Principal,
    *,
    settings: Settings | None = None,
) -> Principal:
    """Fail closed when a plugin request cannot be tied to an allowed tenant.

    Trusted tenant context is collected only from route-declared ``tenant_id``
    path/query parameters or a declared top-level Pydantic body field. Merely
    appending an ignored query/body key cannot manufacture authorization.
    Reading the JSON body through Starlette's request cache keeps it available
    for FastAPI's downstream validation and endpoint parsing.
    """

    route_path = _route_template(request)
    method = str(request.method or "").upper()
    if principal.has_global_tenant_scope:
        request.state.admin_tenant_scope = "*"
        return principal

    if _requires_global_tenant_scope(method, route_path):
        _deny_tenant_scope()

    context = await _plugin_tenant_context(request)
    if context.invalid_explicit_value:
        _deny_tenant_scope()

    if context.tenant_ids:
        if not all(principal.allows_tenant(tenant_id) for tenant_id in context.tenant_ids):
            _deny_tenant_scope()
        request.state.admin_tenant_scope = context.tenant_ids
        await _enforce_group_scope(
            request,
            principal,
            context.tenant_ids,
            route_path=route_path,
        )
        return principal

    # Let FastAPI return its normal 4xx validation response for malformed JSON
    # or a missing required tenant field. No endpoint action can execute then.
    if context.validation_pending:
        return principal

    implicit_tenant = _implicit_plugin_tenant(route_path, settings or get_settings())
    if implicit_tenant:
        if not principal.allows_tenant(implicit_tenant):
            _deny_tenant_scope()
        request.state.admin_tenant_scope = (implicit_tenant,)
        await _enforce_group_scope(
            request,
            principal,
            (implicit_tenant,),
            route_path=route_path,
        )
        return principal

    if (method, route_path) in _TENANT_NEUTRAL_PLUGIN_READ_ROUTES:
        request.state.admin_tenant_scope = "tenant-neutral"
        return principal

    # Tenantless resource-id routes, list endpoints, global configuration, and
    # mutations cannot be proven isolated at this layer. Only a principal that
    # explicitly carries the platform-wide scope may use them.
    _deny_tenant_scope()


async def enforce_admin_tenant_scope(
    request: Request,
    principal: Principal,
) -> Principal:
    """Apply the same explicit tenant/group boundary to core admin routes.

    Historically only plugin routes received generic scope enforcement, which
    meant a tenant-scoped role could call core FAQ/KB/DLQ endpoints with a
    different tenant in the body.  A core admin route with no declared tenant
    is platform-global and therefore remains unavailable to scoped actors.
    """

    if principal.has_global_tenant_scope:
        request.state.admin_tenant_scope = "*"
        return principal

    context = await _plugin_tenant_context(request)
    if context.invalid_explicit_value:
        _deny_tenant_scope()
    if context.tenant_ids:
        if not all(principal.allows_tenant(tenant_id) for tenant_id in context.tenant_ids):
            _deny_tenant_scope()
        request.state.admin_tenant_scope = context.tenant_ids
        await _enforce_group_scope(
            request,
            principal,
            context.tenant_ids,
            route_path=_route_template(request),
        )
        return principal
    if context.validation_pending:
        return principal
    _deny_tenant_scope()


async def _enforce_group_scope(
    request: Request,
    principal: Principal,
    tenant_ids: tuple[str, ...],
    *,
    route_path: str,
) -> None:
    if not principal.requires_explicit_group_scope:
        return
    context = await _request_group_context(request)
    if context.invalid_explicit_value:
        _deny_group_scope()
    if context.group_ids:
        if len(tenant_ids) != 1:
            _deny_group_scope()
        tenant_id = tenant_ids[0]
        if not all(principal.allows_group(tenant_id, group_id) for group_id in context.group_ids):
            _deny_group_scope()
        request.state.admin_group_scope = context.group_ids
        return
    if context.validation_pending:
        return
    if (str(request.method or "").upper(), route_path) in _GROUP_SCOPE_SAFE_COLLECTION_ROUTES:
        request.state.admin_group_scope = principal.group_ids
        return
    _deny_group_scope()


@dataclass(frozen=True)
class _RequestGroupContext:
    group_ids: tuple[str, ...]
    validation_pending: bool = False
    invalid_explicit_value: bool = False


async def _request_group_context(request: Request) -> _RequestGroupContext:
    values: list[str] = []
    invalid_explicit_value = False
    validation_pending = False

    if "session_id" in request.path_params:
        normalized = _normalize_explicit_group(request.path_params.get("session_id"))
        if normalized is None:
            invalid_explicit_value = True
        else:
            values.append(normalized)

    route = request.scope.get("route")
    dependant = getattr(route, "dependant", None)
    query_fields = tuple(getattr(dependant, "query_params", ()) or ())
    group_query_fields = [field for field in query_fields if _field_is_group(field)]
    for field in group_query_fields:
        raw_values = request.query_params.getlist(_field_alias(field))
        if not raw_values and _field_is_required(field):
            validation_pending = True
        for raw_value in raw_values:
            normalized = _normalize_explicit_group(raw_value)
            if normalized is None:
                invalid_explicit_value = True
            else:
                values.append(normalized)

    body_keys, body_required = _top_level_body_scope_contract(dependant, "session_id")
    if body_keys:
        try:
            payload = await request.json()
        except Exception:
            validation_pending = True
        else:
            present_keys = (
                [key for key in body_keys if key in payload]
                if isinstance(payload, Mapping)
                else []
            )
            if not present_keys:
                if body_required:
                    validation_pending = True
            else:
                for key in present_keys:
                    raw_group = payload.get(key)
                    if raw_group is None and not body_required:
                        continue
                    normalized = _normalize_explicit_group(raw_group)
                    if normalized is None:
                        invalid_explicit_value = True
                    else:
                        values.append(normalized)

    return _RequestGroupContext(
        group_ids=tuple(dict.fromkeys(values)),
        validation_pending=validation_pending,
        invalid_explicit_value=invalid_explicit_value,
    )


async def _plugin_tenant_context(request: Request) -> _PluginTenantContext:
    values: list[str] = []
    invalid_explicit_value = False
    validation_pending = False

    if "tenant_id" in request.path_params:
        normalized = _normalize_explicit_tenant(request.path_params.get("tenant_id"))
        if normalized is None:
            invalid_explicit_value = True
        else:
            values.append(normalized)

    route = request.scope.get("route")
    dependant = getattr(route, "dependant", None)
    query_fields = tuple(getattr(dependant, "query_params", ()) or ())
    tenant_query_fields = [field for field in query_fields if _field_is_tenant(field)]
    for field in tenant_query_fields:
        raw_values = request.query_params.getlist(_field_alias(field))
        if not raw_values and _field_is_required(field):
            validation_pending = True
        for raw_value in raw_values:
            normalized = _normalize_explicit_tenant(raw_value)
            if normalized is None:
                invalid_explicit_value = True
            else:
                values.append(normalized)

    body_keys, body_required = _top_level_body_tenant_contract(dependant)
    if body_keys:
        try:
            payload = await request.json()
        except Exception:
            validation_pending = True
        else:
            present_keys = (
                [key for key in body_keys if key in payload]
                if isinstance(payload, Mapping)
                else []
            )
            if not present_keys:
                if body_required:
                    validation_pending = True
            else:
                for key in present_keys:
                    raw_tenant = payload.get(key)
                    if raw_tenant is None and not body_required:
                        continue
                    if not isinstance(raw_tenant, str):
                        validation_pending = True
                        continue
                    normalized = _normalize_explicit_tenant(raw_tenant)
                    if normalized is None:
                        invalid_explicit_value = True
                    else:
                        values.append(normalized)

    return _PluginTenantContext(
        tenant_ids=tuple(dict.fromkeys(values)),
        validation_pending=validation_pending,
        invalid_explicit_value=invalid_explicit_value,
    )


def _top_level_body_tenant_contract(dependant: object) -> tuple[tuple[str, ...], bool]:
    return _top_level_body_scope_contract(dependant, "tenant_id")


def _top_level_body_scope_contract(
    dependant: object,
    field_name: str,
) -> tuple[tuple[str, ...], bool]:
    body_fields = tuple(getattr(dependant, "body_params", ()) or ())
    if not body_fields:
        return (), False

    # Multiple body parameters are embedded by FastAPI, so a tenant field in a
    # nested model is not a trusted top-level tenant_id.
    if len(body_fields) != 1:
        for field in body_fields:
            if _field_matches(field, field_name):
                return (_field_alias(field),), _field_is_required(field)
        return (), False

    field = body_fields[0]
    if _field_matches(field, field_name):
        return (_field_alias(field),), _field_is_required(field)
    if bool(getattr(getattr(field, "field_info", None), "embed", False)):
        return (), False

    for annotation in _field_annotations(field):
        model_fields = getattr(annotation, "model_fields", None)
        if not isinstance(model_fields, Mapping):
            model_fields = getattr(annotation, "__fields__", None)
        if not isinstance(model_fields, Mapping):
            continue
        for name, model_field in model_fields.items():
            if str(name) == field_name or _field_alias(model_field) == field_name:
                keys = tuple(
                    dict.fromkeys(
                        key
                        for key in (str(name), _field_alias(model_field))
                        if key
                    )
                )
                return keys, _field_is_required(model_field)
    return (), False


def _field_annotations(field: object) -> tuple[object, ...]:
    candidates = [
        getattr(field, "type_", None),
        getattr(field, "annotation", None),
        getattr(getattr(field, "field_info", None), "annotation", None),
    ]
    flattened: list[object] = []
    pending = [candidate for candidate in candidates if candidate is not None]
    while pending:
        candidate = pending.pop()
        if candidate in flattened:
            continue
        flattened.append(candidate)
        origin = get_origin(candidate)
        if origin is not None:
            pending.extend(arg for arg in get_args(candidate) if arg is not type(None))
    return tuple(flattened)


def _field_alias(field: object) -> str:
    return str(getattr(field, "alias", None) or getattr(field, "name", "") or "")


def _field_is_tenant(field: object) -> bool:
    return _field_matches(field, "tenant_id")


def _field_is_group(field: object) -> bool:
    return _field_matches(field, "session_id")


def _field_matches(field: object, expected: str) -> bool:
    return expected in {
        str(getattr(field, "name", "") or ""),
        _field_alias(field),
    }


def _field_is_required(field: object) -> bool:
    checker = getattr(field, "is_required", None)
    if callable(checker):
        try:
            return bool(checker())
        except TypeError:
            pass
    field_info_checker = getattr(getattr(field, "field_info", None), "is_required", None)
    if callable(field_info_checker):
        try:
            return bool(field_info_checker())
        except TypeError:
            pass
    return bool(getattr(field, "required", False))


def _normalized_tenant_scopes(values: tuple[str, ...] | list[str]) -> frozenset[str]:
    return frozenset(
        normalized
        for value in values
        if (normalized := str(value or "").strip())
    )


def _normalized_group_scopes(values: tuple[str, ...] | list[str]) -> frozenset[str]:
    return frozenset(
        normalized
        for value in values
        if (normalized := str(value or "").strip()) and normalized != "*"
    )


def _normalize_explicit_tenant(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _normalize_explicit_group(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    raw = str(getattr(route, "path", "") or request.url.path)
    return "/" + raw.strip().strip("/").lower()


def _implicit_plugin_tenant(route_path: str, settings: Settings) -> str | None:
    if route_path.startswith("/plugins/wxbot/"):
        return str(settings.wxbot_default_tenant_id or "default").strip() or "default"
    return None


def _requires_global_tenant_scope(method: str, route_path: str) -> bool:
    return method == "POST" and route_path.startswith("/plugins/repeater/config/")


def _deny_tenant_scope() -> NoReturn:
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="tenant_scope_forbidden",
    )


def _deny_group_scope() -> NoReturn:
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="group_scope_forbidden",
    )


def _is_dangerous_request(method: str, path: str) -> bool:
    if path == "/v1/admin/auth/session":
        return False
    if method == "DELETE":
        return True
    if (method, path) in _DANGEROUS_ADMIN_EXACT:
        return True
    if path.startswith("/v1/admin/dlq/messages/") and path.endswith("/replay"):
        return True
    if path.startswith("/v1/admin/plugins/") and path.endswith(
        ("/enable", "/disable", "/scopes", "/upgrade", "/uninstall")
    ):
        return True
    if not path.startswith("/plugins/"):
        return False
    if (method, path) in _DANGEROUS_PLUGIN_EXACT:
        return True
    if method == "POST" and path.startswith("/plugins/repeater/config/"):
        return True
    return any(marker in path for marker in _DANGEROUS_PLUGIN_MARKERS)
