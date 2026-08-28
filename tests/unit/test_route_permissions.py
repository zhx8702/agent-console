from __future__ import annotations

import re
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException

from app.admin.auth_router import build_admin_auth_router
from app.admin.authorization import (
    ROUTE_PERMISSIONS_ATTR,
    AdminPermission,
    AdminRole,
    Principal,
    RoutePermission,
    RoutePermissionEnforcement,
    build_admin_authorization_dependency,
)
from app.admin.kb_router import build_admin_router
from app.admin.route_permissions import (
    DEFAULT_ROUTE_PERMISSION_REGISTRY,
    RoutePermissionRegistry,
    declare_route_permission,
)
from app.common.config import Settings
from app.social.router import build_social_admin_router
from plugins.amap.router import build_amap_router
from plugins.commands.router import build_commands_router
from plugins.credits.router import build_credits_router
from plugins.draw.router import build_draw_router
from plugins.group_activity.router import build_group_activity_router
from plugins.local_agent.router import build_local_agent_router
from plugins.memory.router import build_memory_router
from plugins.moderation.router import build_moderation_router
from plugins.persona_extract.router import build_persona_extract_router
from plugins.repeater.router import build_repeater_router
from plugins.speaker_portrait.router import build_speaker_portrait_router
from plugins.tibo_reset.router import build_tibo_reset_router
from plugins.wxbot.router import build_wxbot_router


def _settings() -> Settings:
    return Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )


def _operator() -> Principal:
    return Principal(
        subject="operator",
        roles=(AdminRole.PLATFORM_OPERATOR.value,),
        tenant_ids=("*",),
        auth_kind="test",
    )


def _guarded_app() -> FastAPI:
    async def authenticate() -> Principal:
        return _operator()

    guard = build_admin_authorization_dependency(
        _settings(),
        authentication_dependency=authenticate,
    )
    app = FastAPI()
    router = APIRouter(
        prefix="/v1/admin",
        dependencies=[Depends(guard)],
    )

    @router.post("/explicit-danger")
    async def explicit_danger() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)
    return app


def _permission_matrix_app(
    principal: Principal | None,
    declarations: tuple[RoutePermission, ...],
) -> FastAPI:
    """Mirror every reviewed guard route through the production dependency.

    The separate shipped-route coverage test proves that these declarations are
    bound one-for-one to the real control plane.  Keeping the handler here inert
    lets this matrix exercise authentication and authorization for every route
    without invoking databases, queues, SDKs, or destructive side effects.
    """

    if principal is None:

        async def authenticate() -> Principal:
            raise HTTPException(status_code=401, detail="not_authenticated")

    else:

        async def authenticate() -> Principal:
            return principal

    guard = build_admin_authorization_dependency(
        _settings(),
        authentication_dependency=authenticate,
    )
    app = FastAPI()

    async def inert_endpoint() -> dict[str, bool]:
        return {"ok": True}

    guarded = tuple(
        declaration
        for declaration in declarations
        if declaration.enforcement is RoutePermissionEnforcement.ADMIN_GUARD
    )
    for declaration in guarded:
        app.add_api_route(
            declaration.path,
            inert_endpoint,
            methods=[declaration.method],
            dependencies=[Depends(guard)],
        )
    RoutePermissionRegistry(guarded).bind_and_validate(app)
    return app


def _concrete_path(path_template: str) -> str:
    return re.sub(r"\{[^{}]+\}", "scope-value", path_template)


def _shipped_control_plane_app() -> FastAPI:
    settings = _settings()
    app = FastAPI()
    app.include_router(build_admin_auth_router(settings))
    mock = MagicMock()
    app.include_router(
        build_admin_router(
            mock,
            mock,
            settings=settings,
            dlq_service=mock,
            stream_service=mock,
        )
    )
    app.include_router(build_social_admin_router(mock, settings))

    routers = {
        "amap": build_amap_router(settings),
        "commands": build_commands_router(mock, mock),
        "credits": build_credits_router(mock),
        "draw": build_draw_router(mock),
        "group_activity": build_group_activity_router(mock, mock),
        "local_agent": build_local_agent_router(mock, mock),
        "memory": build_memory_router(mock),
        "moderation": build_moderation_router(mock),
        "persona_extract": build_persona_extract_router(mock, mock),
        "repeater": build_repeater_router(mock),
        "speaker_portrait": build_speaker_portrait_router(mock),
        "tibo_reset": build_tibo_reset_router(mock, mock, settings),
        "wxbot": build_wxbot_router(
            mock,
            container=mock,
            bridge=mock,
            report_service=mock,
            self_review_service=mock,
        ),
    }
    for name, router in routers.items():
        app.include_router(router, prefix=f"/plugins/{name}")

    DEFAULT_ROUTE_PERMISSION_REGISTRY.bind_and_validate(app)
    return app


def test_registry_rejects_duplicate_exact_declarations() -> None:
    declaration = RoutePermission(
        method="GET",
        path="/v1/admin/resources",
        permission=AdminPermission.READ,
    )

    with pytest.raises(ValueError, match="duplicate route permission declarations"):
        RoutePermissionRegistry((declaration, declaration))


def test_startup_validation_rejects_a_new_undeclared_route() -> None:
    app = FastAPI()

    @app.get("/v1/admin/new-resource")
    async def new_resource() -> dict[str, bool]:
        return {"ok": True}

    registry = RoutePermissionRegistry(())

    with pytest.raises(
        RuntimeError,
        match=r"undeclared=GET /v1/admin/new-resource",
    ):
        registry.bind_and_validate(app)

    assert app.state.route_permissions_strict is True


@pytest.mark.asyncio
async def test_bound_permission_is_used_instead_of_method_inference() -> None:
    app = _guarded_app()
    registry = RoutePermissionRegistry(
        (
            RoutePermission(
                method="POST",
                path="/v1/admin/explicit-danger",
                permission=AdminPermission.DANGER,
            ),
        )
    )
    registry.bind_and_validate(app)

    included = next(
        mounted
        for mounted in app.routes
        if callable(getattr(mounted, "effective_candidates", None))
    )
    route = included.effective_candidates()[0].original_route
    declarations = getattr(route, ROUTE_PERMISSIONS_ATTR)
    assert declarations["POST"].permission is AdminPermission.DANGER
    assert route.openapi_extra["x-route-permissions"]["POST"] == {
        "permission": AdminPermission.DANGER.value,
        "enforcement": "admin_guard",
    }

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/v1/admin/explicit-danger")

    assert response.status_code == 403
    assert response.json() == {"detail": "admin_permission_denied"}


@pytest.mark.asyncio
async def test_strict_mode_denies_a_route_added_after_validation() -> None:
    app = FastAPI()
    RoutePermissionRegistry(()).bind_and_validate(app)

    async def authenticate() -> Principal:
        return _operator()

    guard = build_admin_authorization_dependency(
        _settings(),
        authentication_dependency=authenticate,
    )
    router = APIRouter(dependencies=[Depends(guard)])

    @router.get("/v1/admin/late-route")
    async def late_route() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/v1/admin/late-route")

    assert response.status_code == 403
    assert response.json() == {"detail": "admin_route_permission_undeclared"}


def test_router_endpoint_can_self_register_an_exact_typed_declaration() -> None:
    app = FastAPI()
    router = APIRouter(prefix="/v1/admin")

    @router.get("/self-declared")
    @declare_route_permission(
        RoutePermission(
            method="GET",
            path="/v1/admin/self-declared",
            permission=AdminPermission.READ,
        )
    )
    async def self_declared() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)
    RoutePermissionRegistry(()).bind_and_validate(app)

    assert app.state.bound_route_permissions == (
        RoutePermission(
            method="GET",
            path="/v1/admin/self-declared",
            permission=AdminPermission.READ,
        ),
    )


def test_default_manifest_covers_every_shipped_admin_and_plugin_route() -> None:
    """A newly added shipped route must add a reviewed manifest declaration."""

    app = _shipped_control_plane_app()

    bound_control_routes = [
        context.original_route
        for mounted in app.routes
        if callable(getattr(mounted, "effective_candidates", None))
        for context in mounted.effective_candidates()
        if context.path.startswith(("/v1/admin", "/plugins/"))
    ]
    assert bound_control_routes
    assert all(hasattr(route, ROUTE_PERMISSIONS_ATTR) for route in bound_control_routes)
    assert set(DEFAULT_ROUTE_PERMISSION_REGISTRY.declarations).issubset(
        app.state.bound_route_permissions
    )
    assert len(app.state.bound_route_permissions) >= len(
        DEFAULT_ROUTE_PERMISSION_REGISTRY.declarations
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "allowed_permissions"),
    (
        (None, frozenset()),
        (AdminRole.PLATFORM_READER, frozenset({AdminPermission.READ})),
        (
            AdminRole.PLATFORM_OPERATOR,
            frozenset({AdminPermission.READ, AdminPermission.WRITE}),
        ),
        (AdminRole.PLATFORM_ADMIN, frozenset(AdminPermission)),
    ),
)
async def test_every_guarded_route_enforces_the_401_403_role_matrix(
    role: AdminRole | None,
    allowed_permissions: frozenset[AdminPermission],
) -> None:
    """Prove every shipped guard declaration has an executable auth outcome.

    `test_default_manifest_covers_every_shipped_admin_and_plugin_route` binds
    the same declarations to the real routers.  This test then drives every
    declaration through the production guard for anonymous, reader, operator,
    and platform-admin principals, without reaching endpoint side effects.
    """

    principal = (
        None
        if role is None
        else Principal(
            subject=role.value,
            roles=(role.value,),
            tenant_ids=("*",),
            auth_kind="test",
        )
    )
    shipped_declarations = tuple(
        _shipped_control_plane_app().state.bound_route_permissions
    )
    guarded = tuple(
        declaration
        for declaration in shipped_declarations
        if declaration.enforcement is RoutePermissionEnforcement.ADMIN_GUARD
    )
    assert guarded

    transport = httpx.ASGITransport(
        app=_permission_matrix_app(principal, shipped_declarations)
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        for declaration in guarded:
            response = await client.request(
                declaration.method,
                _concrete_path(declaration.path),
            )
            if role is None:
                assert response.status_code == 401, declaration.key
                assert response.json() == {"detail": "not_authenticated"}
            elif declaration.permission in allowed_permissions:
                assert response.status_code == 200, declaration.key
            else:
                assert response.status_code == 403, declaration.key
                assert response.json() == {"detail": "admin_permission_denied"}


def test_non_guard_auth_routes_are_explicit_and_bounded() -> None:
    """Keep login/session exceptions out of the generic admin-guard surface."""

    exceptional = {
        declaration.key: declaration.enforcement
        for declaration in _shipped_control_plane_app().state.bound_route_permissions
        if declaration.enforcement is not RoutePermissionEnforcement.ADMIN_GUARD
    }
    assert exceptional == {
        ("DELETE", "/v1/admin/auth/session"): (
            RoutePermissionEnforcement.PUBLIC_SESSION_CLEAR
        ),
        ("GET", "/v1/admin/auth/me"): RoutePermissionEnforcement.AUTH_HANDLER,
        ("GET", "/v1/admin/auth/session"): RoutePermissionEnforcement.AUTH_HANDLER,
        ("POST", "/v1/admin/auth/session"): RoutePermissionEnforcement.AUTH_HANDLER,
    }
