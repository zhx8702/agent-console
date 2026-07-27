from __future__ import annotations

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI, Query
from pydantic import BaseModel, ConfigDict

from app.admin.authorization import (
    AdminRole,
    Principal,
    build_admin_authorization_dependency,
)


class _TenantPayload(BaseModel):
    tenant_id: str
    message: str


class _GlobalPayload(BaseModel):
    action: str


class _IgnoringPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: str


class _OptionalTenantPayload(BaseModel):
    tenant_id: str | None = None
    action: str


def _principal(
    *tenant_ids: str,
    role: AdminRole = AdminRole.PLATFORM_OPERATOR,
    group_ids: tuple[str, ...] = (),
) -> Principal:
    return Principal(
        subject="operator",
        roles=(role.value,),
        tenant_ids=tuple(tenant_ids),
        auth_kind="test",
        group_ids=group_ids,
    )


def _app(principal: Principal) -> FastAPI:
    async def authenticate() -> Principal:
        return principal

    guard = build_admin_authorization_dependency(
        authentication_dependency=authenticate,
    )
    router = APIRouter(
        prefix="/plugins/demo",
        dependencies=[Depends(guard)],
    )

    @router.get("/path/{tenant_id}")
    async def path_tenant(tenant_id: str) -> dict[str, str]:
        return {"tenant_id": tenant_id}

    @router.get("/query")
    async def query_tenant(tenant_id: str = Query(...)) -> dict[str, str]:
        return {"tenant_id": tenant_id}

    @router.post("/body")
    async def body_tenant(payload: _TenantPayload) -> dict[str, str]:
        return payload.model_dump()

    @router.post("/mixed/{tenant_id}")
    async def mixed_tenant(tenant_id: str, payload: _TenantPayload) -> dict[str, str]:
        return {"path": tenant_id, "body": payload.tenant_id}

    @router.post("/global")
    async def global_action(payload: _GlobalPayload) -> dict[str, str]:
        return payload.model_dump()

    @router.post("/ignored-body")
    async def ignored_body(payload: _IgnoringPayload) -> dict[str, str]:
        return payload.model_dump()

    @router.post("/optional-body")
    async def optional_body(payload: _OptionalTenantPayload) -> dict[str, object]:
        return payload.model_dump()

    @router.get("/optional-query")
    async def optional_query(tenant_id: str | None = None) -> dict[str, str | None]:
        return {"tenant_id": tenant_id}

    @router.get("/opaque/{resource_id}")
    async def opaque_resource(resource_id: str) -> dict[str, str]:
        return {"resource_id": resource_id}

    catalog = APIRouter(
        prefix="/plugins/commands",
        dependencies=[Depends(guard)],
    )

    @catalog.get("/catalog")
    async def command_catalog() -> dict[str, list[object]]:
        return {"items": []}

    repeater = APIRouter(
        prefix="/plugins/repeater",
        dependencies=[Depends(guard)],
    )

    @repeater.post("/config/{tenant_id}/{session_id:path}")
    async def repeater_config(tenant_id: str, session_id: str) -> dict[str, str]:
        return {"tenant_id": tenant_id, "session_id": session_id}

    amap = APIRouter(
        prefix="/plugins/amap",
        dependencies=[Depends(guard)],
    )

    @amap.post("/admin/config")
    async def amap_global_config(payload: _GlobalPayload) -> dict[str, str]:
        return payload.model_dump()

    wxbot = APIRouter(
        prefix="/plugins/wxbot",
        dependencies=[Depends(guard)],
    )

    @wxbot.get("/bridge/status")
    async def wxbot_bridge_status() -> dict[str, bool]:
        return {"ok": True}

    @wxbot.get("/admin/reports/subscriptions")
    async def wxbot_report_subscriptions() -> dict[str, list[object]]:
        return {"items": []}

    @wxbot.get("/admin/roster/groups")
    async def wxbot_group_roster() -> dict[str, list[object]]:
        return {"sessions": []}

    @wxbot.get("/admin/sessions")
    async def wxbot_sessions() -> dict[str, list[object]]:
        return {"sessions": []}

    @wxbot.post("/admin/tenants/{tenant_id}/groups/{session_id:path}/simulate-inbound")
    async def wxbot_simulate(tenant_id: str, session_id: str) -> dict[str, str]:
        return {"tenant_id": tenant_id, "session_id": session_id}

    app = FastAPI()
    app.include_router(router)
    app.include_router(catalog)
    app.include_router(repeater)
    app.include_router(amap)
    app.include_router(wxbot)
    return app


@pytest.mark.asyncio
async def test_scoped_principal_can_use_declared_path_query_and_body_tenants() -> None:
    transport = httpx.ASGITransport(app=_app(_principal("tenant-a")))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        path = await client.get("/plugins/demo/path/tenant-a")
        query = await client.get("/plugins/demo/query", params={"tenant_id": "tenant-a"})
        body = await client.post(
            "/plugins/demo/body",
            json={"tenant_id": "tenant-a", "message": "body-remains-readable"},
        )

    assert [path.status_code, query.status_code, body.status_code] == [200, 200, 200]
    assert body.json() == {
        "tenant_id": "tenant-a",
        "message": "body-remains-readable",
    }


@pytest.mark.asyncio
async def test_scope_guard_rejects_mismatched_or_conflicting_declared_tenants() -> None:
    transport = httpx.ASGITransport(app=_app(_principal("tenant-a")))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        path = await client.get("/plugins/demo/path/tenant-b")
        query = await client.get("/plugins/demo/query", params={"tenant_id": "tenant-b"})
        body = await client.post(
            "/plugins/demo/body",
            json={"tenant_id": "tenant-b", "message": "no"},
        )
        mixed = await client.post(
            "/plugins/demo/mixed/tenant-a",
            json={"tenant_id": "tenant-b", "message": "no"},
        )
        duplicate_query = await client.get(
            "/plugins/demo/query",
            params=[("tenant_id", "tenant-a"), ("tenant_id", "tenant-b")],
        )

    for response in (path, query, body, mixed, duplicate_query):
        assert response.status_code == 403
        assert response.json() == {"detail": "tenant_scope_forbidden"}


@pytest.mark.asyncio
async def test_ignored_query_or_json_keys_cannot_manufacture_tenant_context() -> None:
    transport = httpx.ASGITransport(app=_app(_principal("tenant-a")))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        ignored_query = await client.get(
            "/plugins/demo/opaque/resource-1",
            params={"tenant_id": "tenant-a"},
        )
        ignored_body = await client.post(
            "/plugins/demo/ignored-body",
            json={"action": "global", "tenant_id": "tenant-a"},
        )

    assert ignored_query.status_code == 403
    assert ignored_body.status_code == 403


@pytest.mark.asyncio
async def test_tenantless_global_actions_require_explicit_star_scope() -> None:
    scoped_transport = httpx.ASGITransport(app=_app(_principal("tenant-a")))
    empty_transport = httpx.ASGITransport(app=_app(_principal()))
    global_transport = httpx.ASGITransport(app=_app(_principal("*")))

    async with httpx.AsyncClient(transport=scoped_transport, base_url="http://test") as client:
        scoped = await client.post("/plugins/demo/global", json={"action": "rotate"})
    async with httpx.AsyncClient(transport=empty_transport, base_url="http://test") as client:
        empty = await client.get("/plugins/demo/path/tenant-a")
    async with httpx.AsyncClient(transport=global_transport, base_url="http://test") as client:
        global_action = await client.post(
            "/plugins/demo/global",
            json={"action": "rotate"},
        )
        arbitrary_tenant = await client.get("/plugins/demo/path/tenant-z")

    assert scoped.status_code == 403
    assert empty.status_code == 403
    assert global_action.status_code == 200
    assert arbitrary_tenant.status_code == 200


@pytest.mark.asyncio
async def test_known_global_side_effect_requires_star_even_with_path_tenant() -> None:
    scoped_admin = _principal("tenant-a", role=AdminRole.PLATFORM_ADMIN)
    global_admin = _principal("*", role=AdminRole.PLATFORM_ADMIN)
    scoped_transport = httpx.ASGITransport(app=_app(scoped_admin))
    global_transport = httpx.ASGITransport(app=_app(global_admin))

    async with httpx.AsyncClient(transport=scoped_transport, base_url="http://test") as client:
        scoped = await client.post("/plugins/repeater/config/tenant-a/group-1")
    async with httpx.AsyncClient(transport=global_transport, base_url="http://test") as client:
        global_response = await client.post(
            "/plugins/repeater/config/tenant-a/group-1"
        )

    assert scoped.status_code == 403
    assert scoped.json() == {"detail": "tenant_scope_forbidden"}
    assert global_response.status_code == 200


@pytest.mark.asyncio
async def test_group_scoped_principal_gets_only_filterable_collections_and_its_group() -> None:
    principal = _principal(
        "default",
        role=AdminRole.GROUP_OPERATOR,
        group_ids=("default:allowed@chatroom",),
    )
    transport = httpx.ASGITransport(app=_app(principal))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        roster = await client.get("/plugins/wxbot/admin/roster/groups")
        sessions = await client.get("/plugins/wxbot/admin/sessions")
        allowed = await client.post(
            "/plugins/wxbot/admin/tenants/default/groups/allowed@chatroom/simulate-inbound"
        )
        wrong_group = await client.post(
            "/plugins/wxbot/admin/tenants/default/groups/other@chatroom/simulate-inbound"
        )
        tenant_level = await client.get("/plugins/wxbot/admin/reports/subscriptions")

    assert roster.status_code == 200
    assert sessions.status_code == 200
    assert allowed.status_code == 200
    assert wrong_group.status_code == 403
    assert wrong_group.json() == {"detail": "group_scope_forbidden"}
    assert tenant_level.status_code == 403
    assert tenant_level.json() == {"detail": "group_scope_forbidden"}


@pytest.mark.asyncio
async def test_known_global_configuration_still_requires_danger_permission() -> None:
    operator_transport = httpx.ASGITransport(app=_app(_principal("*")))
    admin_transport = httpx.ASGITransport(
        app=_app(_principal("*", role=AdminRole.PLATFORM_ADMIN))
    )

    async with httpx.AsyncClient(transport=operator_transport, base_url="http://test") as client:
        operator = await client.post(
            "/plugins/amap/admin/config",
            json={"action": "rotate"},
        )
    async with httpx.AsyncClient(transport=admin_transport, base_url="http://test") as client:
        admin = await client.post(
            "/plugins/amap/admin/config",
            json={"action": "rotate"},
        )

    assert operator.status_code == 403
    assert operator.json() == {"detail": "admin_permission_denied"}
    assert admin.status_code == 200


@pytest.mark.asyncio
async def test_known_default_tenant_routes_use_configured_implicit_scope() -> None:
    allowed_transport = httpx.ASGITransport(app=_app(_principal("default")))
    denied_transport = httpx.ASGITransport(app=_app(_principal("tenant-a")))

    async with httpx.AsyncClient(transport=allowed_transport, base_url="http://test") as client:
        bridge = await client.get("/plugins/wxbot/bridge/status")
        reports = await client.get("/plugins/wxbot/admin/reports/subscriptions")
    async with httpx.AsyncClient(transport=denied_transport, base_url="http://test") as client:
        denied = await client.get("/plugins/wxbot/admin/reports/subscriptions")

    assert bridge.status_code == 200
    assert reports.status_code == 200
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_small_explicit_tenant_neutral_catalog_allowlist_remains_available() -> None:
    transport = httpx.ASGITransport(app=_app(_principal("tenant-a")))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/plugins/commands/catalog")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_normal_fastapi_validation_is_preserved_for_invalid_tenant_body() -> None:
    transport = httpx.ASGITransport(app=_app(_principal("tenant-a")))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        malformed = await client.post(
            "/plugins/demo/body",
            content="{not-json",
            headers={"Content-Type": "application/json"},
        )
        missing = await client.post(
            "/plugins/demo/body",
            json={"message": "missing tenant"},
        )
        missing_query = await client.get("/plugins/demo/query")

    assert malformed.status_code == 422
    assert missing.status_code == 422
    assert missing_query.status_code == 422


@pytest.mark.asyncio
async def test_explicit_blank_tenant_is_not_treated_as_global_context() -> None:
    transport = httpx.ASGITransport(app=_app(_principal("tenant-a")))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        query = await client.get("/plugins/demo/query", params={"tenant_id": ""})
        body = await client.post(
            "/plugins/demo/body",
            json={"tenant_id": " ", "message": "no"},
        )

    assert query.status_code == 403
    assert body.status_code == 403


@pytest.mark.asyncio
async def test_optional_tenant_must_be_selected_by_scoped_principal() -> None:
    transport = httpx.ASGITransport(app=_app(_principal("tenant-a")))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        omitted_query = await client.get("/plugins/demo/optional-query")
        selected_query = await client.get(
            "/plugins/demo/optional-query",
            params={"tenant_id": "tenant-a"},
        )
        omitted_body = await client.post(
            "/plugins/demo/optional-body",
            json={"action": "list-all"},
        )
        null_body = await client.post(
            "/plugins/demo/optional-body",
            json={"tenant_id": None, "action": "list-all"},
        )
        selected_body = await client.post(
            "/plugins/demo/optional-body",
            json={"tenant_id": "tenant-a", "action": "single"},
        )

    assert omitted_query.status_code == 403
    assert selected_query.status_code == 200
    assert omitted_body.status_code == 403
    assert null_body.status_code == 403
    assert selected_body.status_code == 200
