from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI, Request

from app.admin.auth_router import (
    authenticate_admin_request,
    build_admin_auth_dependency,
    build_admin_auth_router,
)
from app.common.config import Settings


def _settings(token: str = "unit_admin_token") -> Settings:
    return Settings(
        app_env="test",
        admin_bearer_token=token,
        admin_session_cookie_secure=False,
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )


def _app(token: str = "unit_admin_token") -> FastAPI:
    settings = _settings(token)
    app = FastAPI()
    app.include_router(build_admin_auth_router(settings))
    return app


def _app_with_protected_plugin(token: str = "unit_admin_token") -> FastAPI:
    settings = _settings(token)
    router = APIRouter()

    @router.get("/state")
    async def get_state() -> dict[str, bool]:
        return {"ok": True}

    app = FastAPI()
    app.include_router(build_admin_auth_router(settings))
    app.include_router(
        router,
        prefix="/plugins/example",
        dependencies=[Depends(build_admin_auth_dependency(settings))],
    )
    return app


def _app_with_imperative_plugin_auth(token: str = "unit_admin_token") -> FastAPI:
    settings = _settings(token)
    app = FastAPI()
    app.include_router(build_admin_auth_router(settings))

    @app.get("/plugins/example/imperative")
    async def get_imperative_state(request: Request) -> dict[str, str]:
        principal = authenticate_admin_request(request, settings)
        return {"auth_kind": principal.auth_kind}

    return app


def _delegated_app(*, group_ids: list[str] | None = None) -> tuple[FastAPI, Settings]:
    token = "group-operator-secret"
    settings = Settings(
        app_env="test",
        admin_bearer_token="",
        admin_session_signing_secret="unit-session-signing-secret",
        admin_principal_tokens_json=json.dumps(
            [
                {
                    "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                    "subject": "group-operator-a",
                    "roles": ["group_operator"],
                    "tenant_ids": ["tenant-a"],
                    "group_ids": group_ids if group_ids is not None else ["tenant-a:room@chatroom"],
                }
            ]
        ),
        admin_session_cookie_secure=False,
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    app = FastAPI()
    app.include_router(build_admin_auth_router(settings))
    return app, settings


@pytest.mark.asyncio
async def test_admin_auth_session_requires_bearer() -> None:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/v1/admin/auth/session")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["detail"] == "missing_admin_bearer"


@pytest.mark.asyncio
async def test_admin_auth_session_rejects_invalid_bearer() -> None:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/v1/admin/auth/session",
            headers={"Authorization": "Bearer wrong-token"},
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "invalid_admin_bearer"


@pytest.mark.asyncio
async def test_admin_auth_session_accepts_valid_bearer() -> None:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            "/v1/admin/auth/session",
            headers={"Authorization": "Bearer unit_admin_token"},
        )

    assert response.status_code == 200
    assert response.json() == {"authenticated": True}


@pytest.mark.asyncio
async def test_admin_auth_exchange_sets_http_only_session_cookie() -> None:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/v1/admin/auth/session",
            headers={"Authorization": "Bearer unit_admin_token"},
        )
        session = await client.get("/v1/admin/auth/me")

    assert login.status_code == 200
    set_cookie = login.headers["set-cookie"]
    assert "agent_console_admin_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=strict" in set_cookie
    assert session.status_code == 200
    assert session.json() == {
        "authenticated": True,
        "subject": "admin",
        "roles": ["platform_admin"],
        "tenant_ids": ["*"],
        "group_ids": ["*"],
        "default_tenant_id": "default",
        "access_scope": "tenant",
        "auth_kind": "session",
    }


@pytest.mark.asyncio
async def test_delegated_group_operator_claims_are_issued_into_session() -> None:
    app, settings = _delegated_app()
    assert "group-operator-secret" not in settings.admin_principal_tokens_json
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        login = await client.post(
            "/v1/admin/auth/session",
            headers={"Authorization": "Bearer group-operator-secret"},
        )
        principal = await client.get("/v1/admin/auth/me")

    assert login.status_code == 200
    assert principal.status_code == 200
    assert principal.json() == {
        "authenticated": True,
        "subject": "group-operator-a",
        "roles": ["group_operator"],
        "tenant_ids": ["tenant-a"],
        "group_ids": ["tenant-a:room@chatroom"],
        "default_tenant_id": "tenant-a",
        "access_scope": "group",
        "auth_kind": "session",
    }


@pytest.mark.asyncio
async def test_delegated_group_role_without_group_scope_fails_closed() -> None:
    app, _settings = _delegated_app(group_ids=[])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/admin/auth/session",
            headers={"Authorization": "Bearer group-operator-secret"},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "admin_principal_config_invalid"


@pytest.mark.asyncio
async def test_admin_auth_logout_invalidates_browser_cookie() -> None:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        await client.post(
            "/v1/admin/auth/session",
            headers={"Authorization": "Bearer unit_admin_token"},
        )
        logout = await client.delete("/v1/admin/auth/session")
        session = await client.get("/v1/admin/auth/session")

    assert logout.status_code == 204
    assert session.status_code == 401


@pytest.mark.asyncio
async def test_plugin_route_is_denied_by_default_and_accepts_session_cookie() -> None:
    transport = httpx.ASGITransport(app=_app_with_protected_plugin())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        unauthenticated = await client.get("/plugins/example/state")
        await client.post(
            "/v1/admin/auth/session",
            headers={"Authorization": "Bearer unit_admin_token"},
        )
        authenticated = await client.get("/plugins/example/state")

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 200
    assert authenticated.json() == {"ok": True}


@pytest.mark.asyncio
async def test_plugin_imperative_auth_accepts_browser_session_cookie() -> None:
    transport = httpx.ASGITransport(app=_app_with_imperative_plugin_auth())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        unauthenticated = await client.get("/plugins/example/imperative")
        await client.post(
            "/v1/admin/auth/session",
            headers={"Authorization": "Bearer unit_admin_token"},
        )
        authenticated = await client.get("/plugins/example/imperative")

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 200
    assert authenticated.json() == {"auth_kind": "session"}
