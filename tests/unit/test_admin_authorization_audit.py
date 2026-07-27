from __future__ import annotations

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from pydantic import BaseModel

from app.admin.audit import (
    ADMIN_AUDIT_WRITE_FAILURES,
    AdminAuditEvent,
    install_admin_audit_middleware,
    set_admin_audit_context,
)
from app.admin.authorization import (
    AdminPermission,
    AdminRole,
    Principal,
    build_admin_authorization_dependency,
    permissions_for_roles,
    required_admin_permission,
)
from app.common.config import Settings


class _CaptureSink:
    def __init__(self) -> None:
        self.events: list[AdminAuditEvent] = []

    async def write(self, event: AdminAuditEvent) -> None:
        self.events.append(event)


class _FailingSink:
    async def write(self, event: AdminAuditEvent) -> None:
        _ = event
        raise RuntimeError("bearer-token-and-private-body-must-not-leak")


def _settings(*, app_env: str = "test") -> Settings:
    return Settings(
        app_env=app_env,
        admin_bearer_token="unit_admin_token",
        admin_session_cookie_secure=app_env == "prod",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )


def _principal(
    role: AdminRole,
    *,
    auth_kind: str = "test",
    tenant_ids: tuple[str, ...] = ("tenant-a",),
    group_ids: tuple[str, ...] = (),
) -> Principal:
    return Principal(
        subject=role.value,
        roles=(role.value,),
        tenant_ids=tenant_ids,
        auth_kind=auth_kind,
        group_ids=group_ids,
    )


class _TenantMutation(BaseModel):
    tenant_id: str
    enabled: bool = True


def _app(
    sink: _CaptureSink | _FailingSink,
    *,
    settings: Settings | None = None,
    principal: Principal | None = None,
) -> FastAPI:
    configured = settings or _settings()

    authentication_dependency = None
    if principal is not None:

        async def injected_principal() -> Principal:
            return principal

        authentication_dependency = injected_principal

    guard = build_admin_authorization_dependency(
        configured,
        authentication_dependency=authentication_dependency,
    )
    router = APIRouter(
        prefix="/v1/admin",
        dependencies=[Depends(guard)],
    )

    @router.get("/resources/{resource_id}")
    async def get_resource(resource_id: str) -> dict[str, str]:
        return {"resource_id": resource_id}

    @router.post("/resources/{resource_id}")
    async def update_resource(
        resource_id: str,
        payload: dict[str, object],
        tenant_id: str | None = None,
    ) -> dict[str, object]:
        return {"resource_id": resource_id, "updated": bool(payload), "tenant_id": tenant_id}

    @router.delete("/resources/{resource_id}")
    async def delete_resource(resource_id: str) -> dict[str, str]:
        return {"deleted": resource_id}

    @router.post("/tenant-resources")
    async def update_tenant_resource(payload: _TenantMutation) -> dict[str, object]:
        return payload.model_dump()

    @router.post("/semantic")
    async def semantic_mutation(request: Request) -> dict[str, bool]:
        set_admin_audit_context(
            request,
            target_type="semantic_resource",
            tenant_id="customer@example.test",
            session_id="private-group-id",
            user_id="person@example.test",
            before_state={"enabled": False},
            after_state={"enabled": True},
            policy_version=7,
            reason="policy_updated",
        )
        return {"updated": True}

    @router.get("/tenants/{tenant_id}/groups/{session_id}")
    async def get_group_resource(tenant_id: str, session_id: str) -> dict[str, str]:
        return {"tenant_id": tenant_id, "session_id": session_id}

    @router.post("/crash")
    async def crash() -> None:
        raise RuntimeError("private-database-error")

    plugin_router = APIRouter(
        prefix="/plugins/demo",
        dependencies=[Depends(guard)],
    )

    @plugin_router.post("/config/{session_id:path}")
    async def update_plugin_config(session_id: str) -> dict[str, str]:
        return {"session_id": session_id}

    app = FastAPI()
    install_admin_audit_middleware(app, configured, sink=sink)
    app.include_router(router)
    app.include_router(plugin_router)
    return app


def test_role_permission_model_is_explicit_and_cumulative() -> None:
    assert permissions_for_roles([AdminRole.PLATFORM_READER.value]) == {AdminPermission.READ}
    assert permissions_for_roles([AdminRole.PLATFORM_OPERATOR.value]) == {
        AdminPermission.READ,
        AdminPermission.WRITE,
    }
    assert permissions_for_roles([AdminRole.PLATFORM_ADMIN.value]) == set(AdminPermission)
    assert permissions_for_roles([AdminRole.TENANT_ADMIN.value]) == set(AdminPermission)
    for role in (
        AdminRole.GROUP_OPERATOR,
        AdminRole.MODERATOR,
        AdminRole.REVIEWER,
        AdminRole.SERVICE_ACCOUNT,
    ):
        assert permissions_for_roles([role.value]) == {
            AdminPermission.READ,
            AdminPermission.WRITE,
        }
    assert permissions_for_roles([AdminRole.OBSERVER.value]) == {AdminPermission.READ}
    assert permissions_for_roles(["unknown_role"]) == frozenset()


def test_tenant_scoped_roles_cannot_turn_wildcard_into_platform_scope() -> None:
    principal = Principal(
        subject="tenant-admin",
        roles=(AdminRole.TENANT_ADMIN.value,),
        tenant_ids=("*", "tenant-a"),
        auth_kind="oidc",
    )

    assert principal.has_global_tenant_scope is False
    assert principal.allows_tenant("tenant-a") is True
    assert principal.allows_tenant("tenant-b") is False


def test_group_scoped_roles_require_an_explicit_authenticated_group_claim() -> None:
    principal = _principal(
        AdminRole.GROUP_OPERATOR,
        group_ids=("tenant-a:allowed@chatroom",),
    )

    assert principal.requires_explicit_group_scope is True
    assert principal.allows_group("tenant-a", "allowed@chatroom") is True
    assert principal.allows_group("tenant-a", "other@chatroom") is False
    assert principal.allows_group("tenant-b", "allowed@chatroom") is False


@pytest.mark.asyncio
async def test_core_admin_routes_enforce_tenant_and_group_scope() -> None:
    sink = _CaptureSink()
    principal = _principal(
        AdminRole.GROUP_OPERATOR,
        group_ids=("tenant-a:allowed@chatroom",),
    )
    transport = httpx.ASGITransport(app=_app(sink, principal=principal))

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        allowed = await client.get(
            "/v1/admin/tenants/tenant-a/groups/allowed@chatroom"
        )
        wrong_group = await client.get(
            "/v1/admin/tenants/tenant-a/groups/other@chatroom"
        )
        wrong_tenant = await client.get(
            "/v1/admin/tenants/tenant-b/groups/allowed@chatroom"
        )
        body_cross_tenant = await client.post(
            "/v1/admin/tenant-resources",
            json={"tenant_id": "tenant-b", "enabled": True},
        )

    assert allowed.status_code == 200
    assert wrong_group.status_code == 403
    assert wrong_group.json() == {"detail": "group_scope_forbidden"}
    assert wrong_tenant.status_code == 403
    assert wrong_tenant.json() == {"detail": "tenant_scope_forbidden"}
    assert body_cross_tenant.status_code == 403
    assert body_cross_tenant.json() == {"detail": "tenant_scope_forbidden"}


def test_request_policy_distinguishes_read_write_and_danger() -> None:
    assert required_admin_permission("GET", "/v1/admin/faqs") is AdminPermission.READ
    assert required_admin_permission("POST", "/v1/admin/faqs") is AdminPermission.WRITE
    assert (
        required_admin_permission("POST", "/v1/admin/runtime/llm-config") is AdminPermission.DANGER
    )
    assert required_admin_permission("POST", "/plugins/credits/transfer") is AdminPermission.DANGER
    assert (
        required_admin_permission("POST", "/plugins/amap/admin/config")
        is AdminPermission.DANGER
    )
    assert (
        required_admin_permission(
            "POST",
            "/plugins/wxbot/admin/sdk/query/read",
        )
        is AdminPermission.DANGER
    )
    assert (
        required_admin_permission(
            "POST",
            "/plugins/repeater/config/{tenant_id}/{session_id:path}",
        )
        is AdminPermission.DANGER
    )
    assert (
        required_admin_permission("POST", "/v1/admin/plugins/demo/scopes") is AdminPermission.DANGER
    )
    assert (
        required_admin_permission("POST", "/plugins/wxbot/admin/reports/send")
        is AdminPermission.DANGER
    )
    assert required_admin_permission("DELETE", "/plugins/memory/items/42") is AdminPermission.DANGER


@pytest.mark.asyncio
async def test_platform_admin_keeps_read_write_and_danger_compatibility() -> None:
    sink = _CaptureSink()
    transport = httpx.ASGITransport(app=_app(sink))
    headers = {"Authorization": "Bearer unit_admin_token"}

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        read = await client.get("/v1/admin/resources/item-1", headers=headers)
        write = await client.post(
            "/v1/admin/resources/item-1",
            headers=headers,
            json={"enabled": True},
        )
        danger = await client.delete("/v1/admin/resources/item-1", headers=headers)

    assert [read.status_code, write.status_code, danger.status_code] == [200, 200, 200]


@pytest.mark.asyncio
async def test_reader_and_operator_are_limited_by_permission_dependencies() -> None:
    reader_sink = _CaptureSink()
    reader_transport = httpx.ASGITransport(
        app=_app(
            reader_sink,
            principal=_principal(AdminRole.PLATFORM_READER, tenant_ids=("*",)),
        )
    )
    async with httpx.AsyncClient(
        transport=reader_transport,
        base_url="http://testserver",
    ) as client:
        reader_get = await client.get("/v1/admin/resources/item-1")
        reader_post = await client.post(
            "/v1/admin/resources/item-1",
            json={"enabled": True},
        )

    operator_sink = _CaptureSink()
    operator_transport = httpx.ASGITransport(
        app=_app(
            operator_sink,
            principal=_principal(AdminRole.PLATFORM_OPERATOR, tenant_ids=("*",)),
        )
    )
    async with httpx.AsyncClient(
        transport=operator_transport,
        base_url="http://testserver",
    ) as client:
        operator_post = await client.post(
            "/v1/admin/resources/item-1",
            json={"enabled": True},
        )
        operator_delete = await client.delete("/v1/admin/resources/item-1")

    assert reader_get.status_code == 200
    assert reader_post.status_code == 403
    assert reader_post.json() == {"detail": "admin_permission_denied"}
    assert operator_post.status_code == 200
    assert operator_delete.status_code == 403
    assert operator_sink.events[-1].permission == AdminPermission.DANGER.value
    assert operator_sink.events[-1].outcome == "denied"


@pytest.mark.asyncio
async def test_policy_uses_route_template_instead_of_sensitive_path_values() -> None:
    sink = _CaptureSink()
    transport = httpx.ASGITransport(
        app=_app(
            sink,
            principal=_principal(
                AdminRole.PLATFORM_OPERATOR,
                tenant_ids=("*",),
            ),
        )
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/plugins/demo/config/private/clear")

    assert response.status_code == 200
    event = sink.events[0]
    assert event.permission == AdminPermission.WRITE.value
    assert event.route == "/plugins/demo/config/{session_id:path}"
    assert "private/clear" not in repr(event.as_dict())


@pytest.mark.asyncio
async def test_plugin_tenant_scope_rejection_is_audited() -> None:
    sink = _CaptureSink()
    transport = httpx.ASGITransport(
        app=_app(
            sink,
            principal=_principal(AdminRole.PLATFORM_OPERATOR),
        )
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/plugins/demo/config/private-session")

    assert response.status_code == 403
    assert response.json() == {"detail": "tenant_scope_forbidden"}
    event = sink.events[0]
    assert event.route == "/plugins/demo/config/{session_id:path}"
    assert event.permission == AdminPermission.WRITE.value
    assert event.outcome == "denied"
    assert "private-session" not in repr(event.as_dict())


@pytest.mark.asyncio
async def test_mutation_audit_uses_route_template_and_excludes_credentials_and_body() -> None:
    sink = _CaptureSink()
    transport = httpx.ASGITransport(app=_app(sink))
    token = "unit_admin_token"
    private_value = "person@example.test"
    private_query = "query-person@example.test"
    headers = {
        "Authorization": f"Bearer {token}",
        # Untrusted correlation headers must not be copied into audit logs.
        "X-Request-ID": token,
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            f"/v1/admin/resources/private-person-id?contact={private_query}",
            headers=headers,
            json={"email": private_value},
        )
        await client.get("/v1/admin/resources/private-person-id", headers=headers)

    assert response.status_code == 200
    assert response.headers["x-request-id"].startswith("admin_")
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.actor == "admin"
    assert event.tenant_id == "*"
    assert event.method == "POST"
    assert event.route == "/v1/admin/resources/{resource_id}"
    assert event.status == 200
    assert event.request_id == response.headers["x-request-id"]
    assert event.source == "bearer"
    assert event.permission == AdminPermission.WRITE.value
    assert event.outcome == "success"
    serialized = repr(event.as_dict())
    assert token not in serialized
    assert private_value not in serialized
    assert private_query not in serialized
    assert "private-person-id" not in serialized


@pytest.mark.asyncio
async def test_auth_rejection_is_audited_without_recording_invalid_token() -> None:
    sink = _CaptureSink()
    transport = httpx.ASGITransport(app=_app(sink))

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/admin/resources/item-1",
            headers={"Authorization": "Bearer invalid-private-token"},
            json={"secret": "private-body"},
        )

    assert response.status_code == 403
    event = sink.events[0]
    assert event.actor == "anonymous"
    assert event.source == "bearer"
    assert event.status == 403
    assert event.outcome == "denied"
    assert "invalid-private-token" not in repr(event.as_dict())
    assert "private-body" not in repr(event.as_dict())


@pytest.mark.asyncio
async def test_actor_and_tenant_are_pseudonymized_when_claims_may_contain_pii() -> None:
    sink = _CaptureSink()
    principal = Principal(
        subject="operator@example.test",
        roles=(AdminRole.PLATFORM_OPERATOR.value,),
        tenant_ids=("customer@example.test",),
        auth_kind="custom-idp-with-private-label",
    )
    transport = httpx.ASGITransport(app=_app(sink, principal=principal))

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/admin/resources/item-1?tenant_id=customer%40example.test",
            json={"secret": "private-body"},
        )

    assert response.status_code == 200
    event = sink.events[0]
    assert event.actor.startswith("actor_")
    assert event.tenant_id.startswith("tenant_")
    assert event.source == "authenticated"
    serialized = repr(event.as_dict())
    assert "operator@example.test" not in serialized
    assert "customer@example.test" not in serialized


@pytest.mark.asyncio
async def test_handler_audit_context_pseudonymizes_body_scopes_and_keeps_semantic_diff() -> None:
    sink = _CaptureSink()
    transport = httpx.ASGITransport(app=_app(sink))

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/admin/semantic",
            headers={"Authorization": "Bearer unit_admin_token"},
        )

    assert response.status_code == 200
    event = sink.events[0]
    assert event.target_type == "semantic_resource"
    assert event.tenant_id.startswith("tenant_")
    assert event.session_id.startswith("session_")
    assert event.user_id.startswith("user_")
    assert event.before_state == {"enabled": False}
    assert event.after_state == {"enabled": True}
    assert event.policy_version == 7
    assert event.reason == "policy_updated"
    serialized = repr(event.as_dict())
    assert "customer@example.test" not in serialized
    assert "private-group-id" not in serialized
    assert "person@example.test" not in serialized


@pytest.mark.asyncio
async def test_unhandled_mutation_exception_is_audited_without_error_detail() -> None:
    sink = _CaptureSink()
    transport = httpx.ASGITransport(app=_app(sink), raise_app_exceptions=False)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/admin/crash",
            headers={"Authorization": "Bearer unit_admin_token"},
        )

    assert response.status_code == 500
    event = sink.events[0]
    assert event.route == "/v1/admin/crash"
    assert event.status == 500
    assert event.outcome == "error"
    assert "private-database-error" not in repr(event.as_dict())


@pytest.mark.asyncio
async def test_unmatched_admin_mutation_is_audited_without_raw_path() -> None:
    sink = _CaptureSink()
    transport = httpx.ASGITransport(app=_app(sink))

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/v1/admin/not-found/private-person-id")

    assert response.status_code == 404
    event = sink.events[0]
    assert event.route == "/v1/admin/<unmatched>"
    assert event.status == 404
    assert event.outcome == "rejected"
    assert "private-person-id" not in repr(event.as_dict())


@pytest.mark.asyncio
async def test_production_audit_sink_failure_blocks_mutation_before_handler() -> None:
    settings = _settings(app_env="prod")
    counter = ADMIN_AUDIT_WRITE_FAILURES.labels(environment="prod")
    before = counter._value.get()
    transport = httpx.ASGITransport(app=_app(_FailingSink(), settings=settings))

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/admin/resources/item-1",
            headers={"Authorization": "Bearer unit_admin_token"},
            json={"secret": "private-body"},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "admin_audit_unavailable"}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"].startswith("admin_")
    assert counter._value.get() == before + 1
    assert "bearer-token" not in response.text
    assert "private-body" not in response.text


@pytest.mark.asyncio
async def test_production_mutation_has_durable_attempt_and_semantic_completion() -> None:
    sink = _CaptureSink()
    settings = _settings(app_env="prod")
    transport = httpx.ASGITransport(app=_app(sink, settings=settings))

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/admin/semantic",
            headers={"Authorization": "Bearer unit_admin_token"},
        )

    assert response.status_code == 200
    assert len(sink.events) == 2
    attempt, completion = sink.events
    assert attempt.route == "/v1/admin/semantic"
    assert attempt.outcome == "pending"
    assert attempt.reason == "mutation_attempt_pending_completion"
    assert attempt.request_id == completion.request_id == response.headers["x-request-id"]
    assert completion.outcome == "success"
    assert completion.before_state == {"enabled": False}
    assert completion.after_state == {"enabled": True}
