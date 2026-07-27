from __future__ import annotations

import json
from fnmatch import fnmatch

import httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.channel import ChannelTarget
from app.common.config import Settings
from app.container import Container, OutboundContainer, SchedulerContainer
from app.infra.runtime_schema import (
    RUNTIME_SCHEMA_COMPATIBILITY_LEVEL,
    RUNTIME_SCHEMA_REVISION,
)
from app.main import (
    _build_outbound_container,
    _build_plugin_runtime_dependency,
    _build_scheduler_container,
    _dependency_startup_errors,
    _enforce_startup_settings,
    _probe_qdrant,
    _probe_worker_heartbeats,
    _readiness_payload,
    _setup_frontend_cors,
    _setup_legacy_api_deprecation_headers,
    _validate_startup_settings,
    create_app,
)


@pytest.mark.parametrize("app_env", ["prod", "production", "staging", "qa"])
def test_frontend_cors_production_like_environments_never_reflect_unknown_origins(
    app_env: str,
) -> None:
    app = FastAPI()

    @app.get("/probe")
    async def probe() -> dict[str, bool]:
        return {"ok": True}

    _setup_frontend_cors(
        app,
        Settings(
            app_env=app_env,
            frontend_cors_origins="https://console.example.com",
        ),
    )
    client = TestClient(app)

    denied = client.get("/probe", headers={"Origin": "https://evil.example"})
    allowed = client.get("/probe", headers={"Origin": "https://console.example.com"})

    assert "access-control-allow-origin" not in denied.headers
    assert allowed.headers["access-control-allow-origin"] == "https://console.example.com"
    assert allowed.headers["access-control-allow-credentials"] == "true"


def test_plugin_router_dependency_enforces_path_scope() -> None:
    class _Registry:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str]] = []

        async def global_execution_allowed(self, owner: str) -> bool:
            raise AssertionError(f"scope-aware request used global gate for {owner}")

        async def scope_execution_allowed(
            self,
            owner: str,
            *,
            tenant_id: str,
            session_id: str,
        ) -> bool:
            self.calls.append((owner, tenant_id, session_id))
            return session_id != "disabled-room"

    registry = _Registry()
    app = FastAPI()

    @app.post(
        "/action/{tenant_id}/{session_id}",
        dependencies=[
            Depends(
                _build_plugin_runtime_dependency(
                    registry,  # type: ignore[arg-type]
                    "demo",
                )
            )
        ],
    )
    async def action(tenant_id: str, session_id: str) -> dict[str, str]:
        return {"tenant_id": tenant_id, "session_id": session_id}

    client = TestClient(app)
    assert client.post("/action/tenant-a/room-a").status_code == 200
    denied = client.post("/action/tenant-a/disabled-room")

    assert denied.status_code == 503
    assert denied.json()["detail"] == "plugin_runtime_disabled"
    assert registry.calls == [
        ("demo", "tenant-a", "room-a"),
        ("demo", "tenant-a", "disabled-room"),
    ]


def test_plugin_router_dependency_gates_every_json_bulk_scope_without_consuming_body() -> None:
    class _Registry:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str]] = []

        async def global_execution_allowed(self, owner: str) -> bool:
            raise AssertionError(f"scoped JSON request used global gate for {owner}")

        async def scope_execution_allowed(
            self,
            owner: str,
            *,
            tenant_id: str,
            session_id: str,
        ) -> bool:
            self.calls.append((owner, tenant_id, session_id))
            return True

    registry = _Registry()
    app = FastAPI()

    @app.post(
        "/bulk",
        dependencies=[
            Depends(
                _build_plugin_runtime_dependency(
                    registry,  # type: ignore[arg-type]
                    "demo",
                )
            )
        ],
    )
    async def bulk(payload: list[dict[str, str]]) -> dict[str, object]:
        return {"items": payload}

    payload = [
        {"tenant_id": "tenant-a", "session_id": "room-a"},
        {"tenant_id": "tenant-b", "session_id": "room-b"},
    ]
    response = TestClient(app).post("/bulk", json=payload)

    assert response.status_code == 200
    assert response.json() == {"items": payload}
    assert set(registry.calls) == {
        ("demo", "tenant-a", "room-a"),
        ("demo", "tenant-b", "room-b"),
    }


def test_plugin_router_dependency_gates_plural_session_ids() -> None:
    class _Registry:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def global_execution_allowed(self, _owner: str) -> bool:
            raise AssertionError("plural scopes must not use the global gate")

        async def scope_execution_allowed(
            self,
            _owner: str,
            *,
            tenant_id: str,
            session_id: str,
        ) -> bool:
            self.calls.append((tenant_id, session_id))
            return session_id != "disabled-room"

    registry = _Registry()
    app = FastAPI()

    @app.post(
        "/bulk",
        dependencies=[Depends(_build_plugin_runtime_dependency(registry, "demo"))],  # type: ignore[arg-type]
    )
    async def bulk(body: dict[str, object]) -> dict[str, object]:
        return body

    response = TestClient(app).post(
        "/bulk",
        json={
            "tenant_id": "tenant-a",
            "session_ids": ["room-a", "disabled-room"],
        },
    )

    assert response.status_code == 503
    assert ("tenant-a", "") not in registry.calls
    assert ("tenant-a", "room-a") in registry.calls
    assert ("tenant-a", "disabled-room") in registry.calls


def test_plugin_router_dependency_rejects_scope_walk_truncation() -> None:
    class _Registry:
        async def global_execution_allowed(self, owner: str) -> bool:
            raise AssertionError(f"complex scoped request used global gate for {owner}")

        async def scope_execution_allowed(self, *_args, **_kwargs) -> bool:
            raise AssertionError("no partial scope set may be authorized")

    app = FastAPI()

    @app.post(
        "/bulk",
        dependencies=[
            Depends(
                _build_plugin_runtime_dependency(
                    _Registry(),  # type: ignore[arg-type]
                    "demo",
                )
            )
        ],
    )
    async def bulk(payload: list[dict[str, str]]) -> dict[str, int]:
        return {"count": len(payload)}

    response = TestClient(app).post(
        "/bulk",
        json=[
            {"tenant_id": "tenant-a", "session_id": f"room-{index}"}
            for index in range(300)
        ],
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "plugin_scope_payload_too_complex"


@pytest.mark.parametrize(
    ("url", "payload"),
    [
        ("/bulk?tenant_id=tenant-a", {"session_id": "disabled-room"}),
        (
            "/bulk",
            {
                "tenant_id": "tenant-a",
                "children": [{"session_id": "disabled-room"}],
            },
        ),
    ],
)
def test_plugin_router_dependency_inherits_tenant_for_nested_session_scope(
    url: str,
    payload: dict[str, object],
) -> None:
    class _Registry:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def global_execution_allowed(self, owner: str) -> bool:
            raise AssertionError(f"scoped request used global gate for {owner}")

        async def scope_execution_allowed(
            self,
            _owner: str,
            *,
            tenant_id: str,
            session_id: str,
        ) -> bool:
            self.calls.append((tenant_id, session_id))
            return (tenant_id, session_id) != ("tenant-a", "disabled-room")

    registry = _Registry()
    app = FastAPI()

    @app.post(
        "/bulk",
        dependencies=[
            Depends(
                _build_plugin_runtime_dependency(
                    registry,  # type: ignore[arg-type]
                    "demo",
                )
            )
        ],
    )
    async def bulk(body: dict[str, object]) -> dict[str, object]:
        return body

    response = TestClient(app).post(url, json=payload)

    assert response.status_code == 503
    assert ("tenant-a", "disabled-room") in registry.calls


def test_plugin_router_dependency_child_tenant_overrides_parent() -> None:
    class _Registry:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def global_execution_allowed(self, _owner: str) -> bool:
            raise AssertionError("scoped request used global gate")

        async def scope_execution_allowed(
            self,
            _owner: str,
            *,
            tenant_id: str,
            session_id: str,
        ) -> bool:
            self.calls.append((tenant_id, session_id))
            return True

    registry = _Registry()
    app = FastAPI()

    @app.post(
        "/bulk",
        dependencies=[Depends(_build_plugin_runtime_dependency(registry, "demo"))],  # type: ignore[arg-type]
    )
    async def bulk(body: dict[str, object]) -> dict[str, object]:
        return body

    response = TestClient(app).post(
        "/bulk",
        json={
            "tenant_id": "tenant-a",
            "children": [{"tenant_id": "tenant-b", "session_id": "room-b"}],
        },
    )

    assert response.status_code == 200
    assert ("tenant-b", "room-b") in registry.calls


def test_plugin_router_dependency_rejects_session_without_tenant() -> None:
    class _Registry:
        async def global_execution_allowed(self, _owner: str) -> bool:
            raise AssertionError("ambiguous scope used global gate")

        async def scope_execution_allowed(self, *_args, **_kwargs) -> bool:
            raise AssertionError("ambiguous scope reached owner gate")

    app = FastAPI()

    @app.post(
        "/bulk",
        dependencies=[Depends(_build_plugin_runtime_dependency(_Registry(), "demo"))],  # type: ignore[arg-type]
    )
    async def bulk(body: dict[str, object]) -> dict[str, object]:
        return body

    response = TestClient(app).post(
        "/bulk",
        json={"session_id": "room-without-tenant"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "plugin_scope_tenant_required"


def test_plugin_router_dependency_gates_distinct_path_and_query_tenants() -> None:
    class _Registry:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def global_execution_allowed(self, _owner: str) -> bool:
            raise AssertionError("scoped request used global gate")

        async def scope_execution_allowed(
            self,
            _owner: str,
            *,
            tenant_id: str,
            session_id: str,
        ) -> bool:
            self.calls.append((tenant_id, session_id))
            return True

    registry = _Registry()
    app = FastAPI()

    @app.post(
        "/action/{tenant_id}",
        dependencies=[Depends(_build_plugin_runtime_dependency(registry, "demo"))],  # type: ignore[arg-type]
    )
    async def action(tenant_id: str) -> dict[str, str]:
        return {"tenant_id": tenant_id}

    response = TestClient(app).post(
        "/action/path-tenant?tenant_id=query-tenant"
    )

    assert response.status_code == 200
    assert set(registry.calls) == {
        ("path-tenant", ""),
        ("query-tenant", ""),
    }


def test_plugin_router_dependency_rejects_ambiguous_query_scope() -> None:
    class _Registry:
        async def global_execution_allowed(self, _owner: str) -> bool:
            raise AssertionError("ambiguous scope used global gate")

        async def scope_execution_allowed(self, *_args, **_kwargs) -> bool:
            raise AssertionError("ambiguous scope reached owner gate")

    app = FastAPI()

    @app.post(
        "/bulk",
        dependencies=[Depends(_build_plugin_runtime_dependency(_Registry(), "demo"))],  # type: ignore[arg-type]
    )
    async def bulk() -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app)
    response = client.post(
        "/bulk?tenant_id=tenant-a&tenant_id=tenant-b"
    )
    mixed_empty = client.post(
        "/bulk?tenant_id=tenant-a&tenant_id="
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "plugin_scope_query_ambiguous"
    assert mixed_empty.status_code == 400
    assert mixed_empty.json()["detail"] == "plugin_scope_query_ambiguous"


def test_plugin_router_dependency_inherits_explicit_plugin_default_tenant() -> None:
    class _Registry:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def api_default_tenant_id(self, owner: str) -> str:
            assert owner == "wxbot"
            return "tenant-default"

        async def global_execution_allowed(self, _owner: str) -> bool:
            raise AssertionError("default-tenant API fell back to global gate")

        async def scope_execution_allowed(
            self,
            _owner: str,
            *,
            tenant_id: str,
            session_id: str,
        ) -> bool:
            self.calls.append((tenant_id, session_id))
            return True

    registry = _Registry()
    app = FastAPI()

    @app.get(
        "/groups/{session_id}",
        dependencies=[
            Depends(_build_plugin_runtime_dependency(registry, "wxbot"))  # type: ignore[arg-type]
        ],
    )
    async def group(session_id: str) -> dict[str, str]:
        return {"session_id": session_id}

    @app.get(
        "/status",
        dependencies=[
            Depends(_build_plugin_runtime_dependency(registry, "wxbot"))  # type: ignore[arg-type]
        ],
    )
    async def status() -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app)
    group_response = client.get("/groups/group-a")
    status_response = client.get("/status")

    assert group_response.status_code == 200
    assert status_response.status_code == 200
    assert registry.calls == [
        ("tenant-default", "group-a"),
        ("tenant-default", ""),
    ]


def test_plugin_router_dependency_gates_json_body_without_content_type() -> None:
    class _Registry:
        def api_default_tenant_id(self, _owner: str) -> str:
            return "tenant-default"

        async def global_execution_allowed(self, _owner: str) -> bool:
            raise AssertionError("JSON body scope used global gate")

        async def scope_execution_allowed(
            self,
            _owner: str,
            *,
            tenant_id: str,
            session_id: str,
        ) -> bool:
            return (tenant_id, session_id) != ("tenant-default", "disabled-room")

    app = FastAPI()

    @app.post(
        "/action",
        dependencies=[
            Depends(
                _build_plugin_runtime_dependency(  # type: ignore[arg-type]
                    _Registry(),
                    "wxbot",
                )
            )
        ],
    )
    async def action(payload: dict[str, str]) -> dict[str, str]:
        return payload

    response = TestClient(app).post(
        "/action",
        content=json.dumps({"session_id": "disabled-room"}),
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "plugin_runtime_disabled"


@pytest.mark.asyncio
async def test_qdrant_probe_checks_collection_and_query_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, object | None]] = []

    class _Client:
        def __init__(self, **_kwargs: object) -> None:
            async def respond(request: httpx.Request) -> httpx.Response:
                request_json = (
                    json.loads(request.content.decode("utf-8"))
                    if request.content
                    else None
                )
                calls.append((request.method, str(request.url), request_json))
                if request.url.path == "/healthz":
                    return httpx.Response(200, json={})
                if request.url.path == "/collections":
                    return httpx.Response(
                        200,
                        json={"result": {"collections": [{"name": "memory"}]}},
                    )
                return httpx.Response(200, json={"result": {"count": 0}})

            self._transport = httpx.MockTransport(respond)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            await self._transport.aclose()

    monkeypatch.setattr("app.workers.readiness.httpx.AsyncClient", _Client)
    monkeypatch.setattr(
        "app.main.get_settings",
        lambda: Settings(
            app_env="test",
            memory_vector_index_enabled=True,
            memory_vector_index_strict_startup_check=True,
            memory_vector_collection="memory",
        ),
    )

    assert await _probe_qdrant("http://qdrant:6333") is True
    assert calls == [
        ("GET", "http://qdrant:6333/healthz", None),
        ("GET", "http://qdrant:6333/collections", None),
        (
            "POST",
            "http://qdrant:6333/collections/memory/points/count",
            {"exact": False},
        ),
    ]


def _patch_ready_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def always_ok() -> bool:
        return True

    async def qdrant_ok(_url: str) -> bool:
        return True

    monkeypatch.setattr("app.main._probe_redis", always_ok)
    monkeypatch.setattr("app.main._probe_db", always_ok)
    monkeypatch.setattr("app.main._probe_qdrant", qdrant_ok)


def test_validate_startup_settings_prod_rejects_default_secrets() -> None:
    settings = Settings(
        app_env="prod",
        outbound_hmac_secret="change_me",
        admin_bearer_token="admin_dev_token",
        llm_provider="fake",
        llm_embed_provider="fake",
        tenant_demo_secret="demo_secret",
    )

    errors = _validate_startup_settings(settings)

    assert "OUTBOUND_HMAC_SECRET must be changed in prod" in errors
    assert "ADMIN_BEARER_TOKEN must be changed in prod" in errors
    assert "LLM_PROVIDER=fake is not allowed in prod" in errors
    assert "LLM_EMBED_PROVIDER=fake is not allowed in prod" in errors
    assert "TENANT_DEMO_SECRET must be changed in prod" in errors


def test_enforce_startup_settings_requires_openai_api_key() -> None:
    settings = Settings(
        app_env="test",
        llm_provider="openai",
        openai_api_key=None,
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        _enforce_startup_settings(settings)


def test_validate_startup_settings_allows_fake_embed_when_knowledge_disabled() -> None:
    settings = Settings(
        app_env="prod",
        llm_provider="openai",
        openai_api_key="sk-test",
        llm_embed_provider="fake",
        knowledge_features_enabled=False,
        outbound_hmac_secret="prod_outbound_secret",
        admin_bearer_token="prod_admin_token",
        admin_session_signing_secret="prod_admin_session_signing_secret_32_chars",
        media_id_signing_secret="prod_media_id_signing_secret_32_chars_long",
        admin_session_cookie_secure=True,
        tenant_demo_secret="prod_demo_secret",
        wxbot_api_token="prod_wxbot_service_token",
        orchestrator_flow_runtime_enabled=True,
        orchestrator_flow_runtime_name="auto",
        orchestrator_flow_runtime_allowed_names="auto",
        orchestrator_flow_runtime_allow_target_flows=True,
        orchestrator_flow_effect_commit_backend="redis",
        orchestrator_flow_effect_handlers_enabled=True,
        orchestrator_flow_effect_log_backend="postgres",
        orchestrator_flow_effect_log_failure_policy="fail_closed",
        readiness_required_worker_roles="inbound,outbound,scheduler",
    )

    errors = _validate_startup_settings(settings)

    assert errors == []


@pytest.mark.parametrize(
    ("configured_roles", "expected_error"),
    [
        (
            "inbound,outboud,scheduler",
            "READINESS_REQUIRED_WORKER_ROLES contains unknown roles: outboud",
        ),
        (
            "inbound,outbound,inbound,scheduler",
            "READINESS_REQUIRED_WORKER_ROLES contains duplicate roles: inbound",
        ),
        (
            "inbound,outbound",
            "READINESS_REQUIRED_WORKER_ROLES must include core roles: scheduler",
        ),
        (
            "",
            "READINESS_REQUIRED_WORKER_ROLES must include core roles: inbound, outbound, scheduler",
        ),
    ],
)
def test_validate_startup_settings_prod_api_rejects_invalid_worker_contract(
    configured_roles: str,
    expected_error: str,
) -> None:
    settings = Settings(
        app_env="prod",
        app_process_role="api",
        readiness_required_worker_roles=configured_roles,
    )

    assert expected_error in _validate_startup_settings(settings)


def test_readiness_worker_roles_are_normalized_and_deduplicated() -> None:
    settings = Settings(
        app_env="test",
        readiness_required_worker_roles=(
            " inbound,OUTBOUND,inbound,unknown,scheduler "
        ),
    )

    assert settings.resolved_readiness_required_worker_roles == [
        "inbound",
        "outbound",
        "scheduler",
    ]


def test_validate_startup_settings_separates_mutable_and_builtin_plugin_roots() -> None:
    settings = Settings(
        app_env="dev",
        llm_provider="fake",
        plugin_dynamic_mutations_enabled=True,
        plugin_install_dir="plugins",
    )

    assert (
        "PLUGIN_INSTALL_DIR must be separate from the trusted built-in plugins directory"
        in _validate_startup_settings(settings)
    )


def test_validate_startup_settings_prod_requires_target_flow_cutover() -> None:
    settings = Settings(
        app_env="prod",
        llm_provider="openai",
        openai_api_key="sk-test",
        llm_embed_provider="fake",
        knowledge_features_enabled=False,
        outbound_hmac_secret="prod_outbound_secret",
        admin_bearer_token="prod_admin_token",
        admin_session_cookie_secure=True,
        tenant_demo_secret="prod_demo_secret",
        wxbot_api_token="prod_wxbot_service_token",
    )

    errors = _validate_startup_settings(settings)

    assert "ORCHESTRATOR_FLOW_RUNTIME_ENABLED must be enabled in prod" in errors
    assert "ORCHESTRATOR_FLOW_RUNTIME_NAME must be auto in prod" in errors
    assert "ORCHESTRATOR_FLOW_RUNTIME_ALLOW_TARGET_FLOWS must be enabled in prod" in errors
    assert "ORCHESTRATOR_FLOW_EFFECT_COMMIT_BACKEND must be redis in prod" in errors
    assert "ORCHESTRATOR_FLOW_EFFECT_HANDLERS_ENABLED must be enabled in prod" in errors
    assert "ORCHESTRATOR_FLOW_EFFECT_LOG_BACKEND must be postgres in prod" in errors


def test_validate_startup_settings_prod_forbids_compatible_flow_fallback() -> None:
    settings = Settings(
        app_env="prod",
        llm_provider="openai",
        openai_api_key="sk-test",
        llm_embed_provider="fake",
        knowledge_features_enabled=False,
        outbound_hmac_secret="prod_outbound_secret",
        admin_bearer_token="prod_admin_token",
        admin_session_signing_secret="prod_admin_session_signing_secret_32_chars",
        media_id_signing_secret="prod_media_id_signing_secret_32_chars_long",
        admin_session_cookie_secure=True,
        tenant_demo_secret="prod_demo_secret",
        wxbot_api_token="prod_wxbot_service_token",
        orchestrator_flow_runtime_enabled=True,
        orchestrator_flow_runtime_name="auto",
        orchestrator_flow_runtime_allowed_names="auto",
        orchestrator_flow_runtime_allow_target_flows=True,
        orchestrator_flow_runtime_allow_compatible_fallback=True,
        orchestrator_flow_effect_commit_backend="redis",
        orchestrator_flow_effect_handlers_enabled=True,
        orchestrator_flow_effect_log_backend="postgres",
        orchestrator_flow_effect_log_failure_policy="fail_closed",
        readiness_required_worker_roles="inbound,outbound,scheduler",
    )

    assert (
        "ORCHESTRATOR_FLOW_RUNTIME_ALLOW_COMPATIBLE_FALLBACK must be disabled in prod"
        in _validate_startup_settings(settings)
    )


def test_validate_startup_settings_prod_rejects_compose_development_secrets() -> None:
    settings = Settings(
        app_env="prod",
        llm_provider="openai",
        openai_api_key="sk-test",
        llm_embed_provider="openai",
        outbound_hmac_secret="compose_dev_outbound_secret",
        admin_bearer_token="compose_dev_admin_token",
        admin_session_cookie_secure=False,
        tenant_demo_secret="compose_dev_tenant_secret",
    )

    errors = _validate_startup_settings(settings)

    assert "OUTBOUND_HMAC_SECRET must be changed in prod" in errors
    assert "ADMIN_BEARER_TOKEN must be changed in prod" in errors
    assert (
        "ADMIN_SESSION_SIGNING_SECRET must be an independent 32+ character secret in prod"
        in errors
    )
    assert (
        "MEDIA_ID_SIGNING_SECRET must be an independent 32+ character secret in prod"
        in errors
    )
    assert "TENANT_DEMO_SECRET must be changed in prod" in errors
    assert "ADMIN_SESSION_COOKIE_SECURE must be enabled in prod" in errors


def test_validate_startup_settings_prod_requires_explicit_agent_tool_policy() -> None:
    settings = Settings(
        app_env="prod",
        llm_provider="openai",
        openai_api_key="sk-test",
        llm_embed_provider="openai",
        outbound_hmac_secret="prod_outbound_secret",
        admin_bearer_token="prod_admin_token",
        admin_session_cookie_secure=True,
        tenant_demo_secret="prod_demo_secret",
        agent_tools_require_explicit_policy=False,
        wxbot_api_token="prod_wxbot_service_token",
    )

    errors = _validate_startup_settings(settings)

    assert "AGENT_TOOLS_REQUIRE_EXPLICIT_POLICY must be enabled in prod" in errors


def test_validate_startup_settings_prod_api_does_not_require_wxbot_service_token() -> None:
    settings = Settings(
        app_env="prod",
        app_process_role="api",
        llm_provider="openai",
        openai_api_key="sk-test",
        llm_embed_provider="openai",
        outbound_hmac_secret="prod_outbound_secret",
        admin_bearer_token="prod_admin_token",
        admin_session_cookie_secure=True,
        tenant_demo_secret="prod_demo_secret",
        wxbot_api_token="",
    )

    assert not any(
        "WXBOT_API_TOKEN" in error for error in _validate_startup_settings(settings)
    )


def test_validate_startup_settings_prod_wxbot_bridge_allows_tokenless_sdk() -> None:
    settings = Settings(
        app_env="prod",
        app_process_role="wxbot_bridge",
        llm_provider="openai",
        openai_api_key="sk-test",
        llm_embed_provider="openai",
        outbound_hmac_secret="prod_outbound_secret",
        admin_bearer_token="prod_admin_token",
        admin_session_cookie_secure=True,
        tenant_demo_secret="prod_demo_secret",
        wxbot_api_token="",
    )

    assert not any(
        "WXBOT_API_TOKEN" in error for error in _validate_startup_settings(settings)
    )


def test_validate_startup_settings_managed_bridge_resolves_connection_secret_at_runtime() -> None:
    settings = Settings(
        app_env="prod",
        app_process_role="wxbot_bridge",
        channel_connection_id="wechat-main",
        wxbot_api_token="",
    )

    assert not any(
        "WXBOT_API_TOKEN" in error for error in _validate_startup_settings(settings)
    )


def test_dependency_startup_errors_fail_closed_by_process_role() -> None:
    api = Settings(
        app_env="dev",
        app_process_role="api",
        knowledge_features_enabled=True,
    )
    assert _dependency_startup_errors(
        api,
        redis_ok=True,
        db_ok=False,
        qdrant_ok=False,
    ) == []

    inbound = api.model_copy(update={"app_process_role": "inbound"})
    assert _dependency_startup_errors(
        inbound,
        redis_ok=True,
        db_ok=False,
        qdrant_ok=True,
    ) == ["db_unreachable"]
    assert _dependency_startup_errors(
        inbound,
        redis_ok=True,
        db_ok=True,
        qdrant_ok=False,
    ) == ["qdrant_unreachable"]

    scheduler = api.model_copy(update={"app_process_role": "scheduler"})
    assert _dependency_startup_errors(
        scheduler,
        redis_ok=False,
        db_ok=False,
        qdrant_ok=True,
    ) == ["redis_unreachable", "db_unreachable"]
    assert _dependency_startup_errors(
        scheduler,
        redis_ok=True,
        db_ok=True,
        qdrant_ok=False,
    ) == ["qdrant_unreachable"]

    outbound = api.model_copy(update={"app_process_role": "outbound"})
    assert _dependency_startup_errors(
        outbound,
        redis_ok=True,
        db_ok=False,
        qdrant_ok=True,
    ) == ["db_unreachable"]


def test_outbound_startup_does_not_require_inference_credentials() -> None:
    settings = Settings(
        app_env="test",
        app_process_role="outbound",
        llm_provider="openai",
        openai_api_key=None,
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )

    assert _validate_startup_settings(settings) == []


def test_wxbot_bridge_startup_does_not_require_inference_credentials() -> None:
    settings = Settings(
        app_env="test",
        app_process_role="wxbot_bridge",
        llm_provider="openai",
        openai_api_key=None,
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )

    assert _validate_startup_settings(settings) == []


def test_prod_outbound_is_not_coupled_to_api_only_secrets() -> None:
    settings = Settings(
        app_env="prod",
        app_process_role="outbound",
        llm_provider="fake",
        llm_embed_provider="fake",
        outbound_hmac_secret="prod_outbound_secret",
        admin_bearer_token="admin_dev_token",
        admin_session_cookie_secure=False,
        tenant_demo_secret="demo_secret",
        agent_tools_require_explicit_policy=False,
        wxbot_api_token="prod_wxbot_service_token",
    )

    assert _validate_startup_settings(settings) == []


@pytest.mark.asyncio
async def test_outbound_container_skips_llm_plugins_and_vector_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        app_env="test",
        app_process_role="outbound",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )

    async def always_ok() -> bool:
        return True

    async def schema_ok(*args, **kwargs) -> None:
        return None

    class FakeRedis:
        pass

    def forbidden(*args, **kwargs):
        raise AssertionError("unrelated inference dependency was initialized")

    monkeypatch.setattr("app.main.get_redis", lambda: FakeRedis())
    monkeypatch.setattr("app.main._probe_redis", always_ok)
    monkeypatch.setattr("app.main._probe_db", always_ok)
    monkeypatch.setattr("app.main.get_engine", lambda: object())
    monkeypatch.setattr("app.main.verify_runtime_schema", schema_ok)
    monkeypatch.setattr("app.main.build_llm_service", forbidden)
    monkeypatch.setattr("app.main.QdrantVectorStore", forbidden)
    monkeypatch.setattr("app.main.PluginRegistry", forbidden)

    container = await _build_outbound_container(settings)
    try:
        assert isinstance(container, OutboundContainer)
        assert container.dispatcher is not None
        assert getattr(container.http_client, "_trust_env", None) is False
        assert not hasattr(container, "plugin_registry")
        assert not hasattr(container, "llm_service")
        assert not hasattr(container, "vector_store")
    finally:
        await container.http_client.aclose()


@pytest.mark.asyncio
async def test_scheduler_container_skips_request_bus_and_egress_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        app_env="test",
        app_process_role="scheduler",
        knowledge_features_enabled=False,
        draw_task_recovery_enabled=False,
        draw_task_queue_worker_enabled=False,
        wxbot_group_summary_enabled=False,
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    seen: dict[str, object] = {}

    async def always_ok() -> bool:
        return True

    async def schema_ok(*_args, **_kwargs) -> None:
        return None

    async def runtime_config(current: Settings):
        return type("ResolvedRuntimeConfig", (), {"settings": current})()

    class FakeAgentStore:
        def __init__(self, _settings: Settings) -> None:
            pass

        async def ensure_tables(self) -> None:
            return None

    class FakeRegistry:
        def __init__(self, _state_store: object) -> None:
            self.loaded_plugins: dict[str, object] = {}

        def discover_directory(
            self,
            _path: object,
            *,
            trusted_builtin: bool = True,
        ) -> int:
            _ = trusted_builtin
            return 0

        def discover_entrypoints(self) -> int:
            return 0

        async def reconcile_state(self) -> None:
            return None

        async def initialize_all(self, ctx: object) -> None:
            seen["plugin_container"] = ctx.container  # type: ignore[attr-defined]

        async def session_execution_allowed(self, *_args, **_kwargs) -> bool:
            return True

        async def scope_execution_allowed(
            self,
            owner: str,
            *,
            tenant_id: str,
            session_id: str = "",
        ) -> bool:
            seen["channel_scope_call"] = (owner, tenant_id, session_id)
            return True

    class FakeChannelRegistry:
        def __init__(self, *, owner_gate: object) -> None:
            seen["channel_owner_gate"] = owner_gate

    class FakeAgentCapability:
        def set_tool_owner_gate(self, gate: object) -> None:
            seen["tool_owner_gate"] = gate

    def forbidden(*_args, **_kwargs):
        raise AssertionError("unrelated runtime dependency was initialized")

    monkeypatch.setattr("app.main.get_redis", lambda: object())
    monkeypatch.setattr("app.main._probe_redis", always_ok)
    monkeypatch.setattr("app.main._probe_db", always_ok)
    monkeypatch.setattr("app.main._probe_qdrant", forbidden)
    monkeypatch.setattr("app.main.get_engine", lambda: object())
    monkeypatch.setattr("app.main.verify_runtime_schema", schema_ok)
    monkeypatch.setattr("app.main.load_runtime_llm_config", runtime_config)
    monkeypatch.setattr("app.main.build_llm_service", lambda _settings: object())
    monkeypatch.setattr("app.main.AgentStore", FakeAgentStore)
    monkeypatch.setattr("app.main.AgentToolRegistry", lambda: object())
    monkeypatch.setattr("app.main.BillingCoordinator", lambda: object())
    monkeypatch.setattr("app.main.ChannelRegistry", FakeChannelRegistry)
    monkeypatch.setattr(
        "app.main.AgentCapabilityEngine",
        lambda *_args, **_kwargs: FakeAgentCapability(),
    )
    monkeypatch.setattr("app.main.PluginStateStore", lambda: object())
    monkeypatch.setattr("app.main.PluginRegistry", FakeRegistry)
    monkeypatch.setattr("app.main.PluginManager", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("app.main.get_session_factory", lambda: object())
    monkeypatch.setattr("app.main.SocialPolicyStore", lambda _factory: object())
    monkeypatch.setattr("app.main.build_preprocessor", forbidden)
    monkeypatch.setattr("app.main.build_rule_router", forbidden)
    monkeypatch.setattr("app.main.build_safety", forbidden)
    monkeypatch.setattr("app.main.build_postprocessor", forbidden)
    monkeypatch.setattr("app.main.SessionManager", forbidden)
    monkeypatch.setattr("app.main.RedisStreamBus", forbidden)
    monkeypatch.setattr("app.main.OutboundDispatcher", forbidden)

    container = await _build_scheduler_container(settings)

    assert isinstance(container, SchedulerContainer)
    assert seen["plugin_container"] is container
    channel_owner_gate = seen["channel_owner_gate"]
    assert callable(channel_owner_gate)
    assert await channel_owner_gate(
        "wxbot",
        ChannelTarget(
            tenant_id="tenant-a",
            channel="wechat",
            session_id="room-a",
        ),
    )
    assert seen["channel_scope_call"] == ("wxbot", "tenant-a", "room-a")
    assert not hasattr(container, "bus")
    assert not hasattr(container, "orchestrator")
    assert not hasattr(container, "preprocessor")
    assert not hasattr(container, "dispatcher")


def test_dependency_startup_errors_forbid_production_fallbacks() -> None:
    settings = Settings(
        app_env="prod",
        app_process_role="api",
        knowledge_features_enabled=True,
        llm_provider="openai",
        llm_embed_provider="openai",
        openai_api_key="sk-test",
        outbound_hmac_secret="prod_outbound_secret",
        admin_bearer_token="prod_admin_token",
        admin_session_cookie_secure=True,
        tenant_demo_secret="prod_demo_secret",
        wxbot_api_token="prod_wxbot_service_token",
    )

    assert _dependency_startup_errors(
        settings,
        redis_ok=True,
        db_ok=False,
        qdrant_ok=False,
    ) == ["db_unreachable", "qdrant_unreachable"]


@pytest.mark.asyncio
async def test_readiness_payload_prod_rejects_memory_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = Container()
    container.__dict__.update(
        _vector_backend="memory",
        _persistence_backend="memory",
    )
    settings = Settings(
        app_env="prod",
        llm_provider="openai",
        llm_embed_provider="openai",
        openai_api_key="sk-test",
        outbound_hmac_secret="prod_outbound_secret",
        admin_bearer_token="prod_admin_token",
        admin_session_cookie_secure=True,
        tenant_demo_secret="prod_demo_secret",
        wxbot_api_token="prod_wxbot_service_token",
    )

    async def always_ok() -> bool:
        return True

    async def qdrant_ok(_url: str) -> bool:
        return True

    monkeypatch.setattr("app.main._probe_redis", always_ok)
    monkeypatch.setattr("app.main._probe_db", always_ok)
    monkeypatch.setattr("app.main._probe_qdrant", qdrant_ok)

    status_code, payload = await _readiness_payload(container, settings)

    assert status_code == 503
    assert payload["status"] == "not_ready"
    assert "persistence_fallback_memory" in payload["errors"]
    assert "vector_store_fallback_memory" in payload["errors"]


@pytest.mark.asyncio
async def test_readiness_payload_prod_reports_qdrant_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = Container()
    container.__dict__.update(
        _vector_backend="qdrant",
        _persistence_backend="postgres",
    )
    settings = Settings(
        app_env="prod",
        llm_provider="openai",
        llm_embed_provider="openai",
        openai_api_key="sk-test",
        outbound_hmac_secret="prod_outbound_secret",
        admin_bearer_token="prod_admin_bearer_token",
        admin_session_signing_secret="prod_admin_session_signing_secret_32_chars",
        media_id_signing_secret="prod_media_id_signing_secret_32_chars_long",
        admin_session_cookie_secure=True,
        tenant_demo_secret="prod_demo_secret",
        wxbot_api_token="prod_wxbot_service_token",
        orchestrator_flow_runtime_enabled=True,
        orchestrator_flow_runtime_name="auto",
        orchestrator_flow_runtime_allowed_names="auto",
        orchestrator_flow_runtime_allow_target_flows=True,
        orchestrator_flow_effect_commit_backend="redis",
        orchestrator_flow_effect_handlers_enabled=True,
        orchestrator_flow_effect_log_backend="postgres",
        orchestrator_flow_effect_log_failure_policy="fail_closed",
        readiness_required_worker_roles="inbound,outbound,scheduler",
    )

    async def redis_ok() -> bool:
        return True

    async def db_ok() -> bool:
        return True

    async def qdrant_down(_url: str) -> bool:
        return False

    async def workers_ok(_settings: Settings) -> dict[str, bool]:
        return {"inbound": True, "outbound": True, "scheduler": True}

    monkeypatch.setattr("app.main._probe_redis", redis_ok)
    monkeypatch.setattr("app.main._probe_db", db_ok)
    monkeypatch.setattr("app.main._probe_qdrant", qdrant_down)
    monkeypatch.setattr("app.main._probe_worker_heartbeats", workers_ok)

    status_code, payload = await _readiness_payload(container, settings)

    assert status_code == 503
    assert payload["status"] == "not_ready"
    assert payload["errors"] == ["qdrant_unreachable"]


@pytest.mark.asyncio
async def test_readiness_payload_test_env_tolerates_memory_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = Container()
    container.__dict__.update(
        _vector_backend="memory",
        _persistence_backend="memory",
    )
    settings = Settings(
        app_env="test",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )

    async def redis_ok() -> bool:
        return True

    async def db_down() -> bool:
        return False

    async def qdrant_down(_url: str) -> bool:
        return False

    monkeypatch.setattr("app.main._probe_redis", redis_ok)
    monkeypatch.setattr("app.main._probe_db", db_down)
    monkeypatch.setattr("app.main._probe_qdrant", qdrant_down)

    status_code, payload = await _readiness_payload(container, settings)

    assert status_code == 200
    assert payload["status"] == "ready"
    assert payload["errors"] == []


@pytest.mark.asyncio
async def test_readiness_payload_strict_memory_vector_requires_qdrant_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = Container()
    container.__dict__.update(
        _vector_backend="memory",
        _persistence_backend="memory",
    )
    settings = Settings(
        app_env="test",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
        memory_vector_index_enabled=True,
        memory_vector_index_strict_startup_check=True,
    )

    async def redis_ok() -> bool:
        return True

    async def db_ok() -> bool:
        return True

    async def qdrant_down(_url: str) -> bool:
        return False

    monkeypatch.setattr("app.main._probe_redis", redis_ok)
    monkeypatch.setattr("app.main._probe_db", db_ok)
    monkeypatch.setattr("app.main._probe_qdrant", qdrant_down)

    status_code, payload = await _readiness_payload(container, settings)

    assert status_code == 503
    assert payload["status"] == "not_ready"
    assert payload["errors"] == [
        "memory_vector_qdrant_unreachable",
        "memory_vector_qdrant_not_active_backend",
    ]


@pytest.mark.asyncio
async def test_readiness_payload_requires_redis_in_every_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = Container()
    container.__dict__.update(
        _vector_backend="qdrant",
        _persistence_backend="postgres",
    )
    settings = Settings(
        app_env="test",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )

    async def redis_down() -> bool:
        return False

    async def always_ok() -> bool:
        return True

    async def qdrant_ok(_url: str) -> bool:
        return True

    monkeypatch.setattr("app.main._probe_redis", redis_down)
    monkeypatch.setattr("app.main._probe_db", always_ok)
    monkeypatch.setattr("app.main._probe_qdrant", qdrant_ok)

    status_code, payload = await _readiness_payload(container, settings)

    assert status_code == 503
    assert payload["status"] == "not_ready"
    assert payload["errors"] == ["redis_unreachable"]


@pytest.mark.asyncio
async def test_readiness_payload_requires_configured_worker_heartbeats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = Container()
    container.__dict__.update(
        _vector_backend="qdrant",
        _persistence_backend="postgres",
    )
    settings = Settings(
        app_env="test",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
        readiness_required_worker_roles="inbound,outbound,scheduler",
    )

    async def always_ok() -> bool:
        return True

    async def qdrant_ok(_url: str) -> bool:
        return True

    async def worker_status(_settings: Settings) -> dict[str, bool]:
        return {
            "inbound": True,
            "outbound": False,
            "scheduler": True,
        }

    monkeypatch.setattr("app.main._probe_redis", always_ok)
    monkeypatch.setattr("app.main._probe_db", always_ok)
    monkeypatch.setattr("app.main._probe_qdrant", qdrant_ok)
    monkeypatch.setattr("app.main._probe_worker_heartbeats", worker_status)

    status_code, payload = await _readiness_payload(container, settings)

    assert status_code == 503
    assert payload["errors"] == ["worker_outbound_heartbeat_missing"]
    assert payload["checks"]["workers"]["inbound"] == {"ok": True}
    assert payload["checks"]["workers"]["outbound"] == {"ok": False}


@pytest.mark.asyncio
async def test_worker_heartbeat_probe_rejects_old_or_malformed_schema_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = "test:worker:heartbeat"

    class _Redis:
        def __init__(self) -> None:
            self.values = {
                f"{prefix}:inbound:worker-old:token": json.dumps(
                    {
                        "role": "inbound",
                        "state": "ready",
                        "schema_revision": "0035_plugin_lifecycle_global_index",
                        "schema_compatibility": RUNTIME_SCHEMA_COMPATIBILITY_LEVEL - 1,
                    }
                ),
                f"{prefix}:outbound:worker-bad:token": "not-json",
                f"{prefix}:scheduler:worker-current:token": json.dumps(
                    {
                        "role": "scheduler",
                        "state": "ready",
                        "schema_revision": RUNTIME_SCHEMA_REVISION,
                        "schema_compatibility": RUNTIME_SCHEMA_COMPATIBILITY_LEVEL,
                    }
                ),
            }

        async def scan_iter(self, *, match: str, count: int):
            assert count == 20
            for key in self.values:
                if fnmatch(key, match):
                    yield key

        async def ttl(self, key: str) -> int:
            _ = key
            return 10

        async def get(self, key: str) -> str:
            return self.values[key]

    monkeypatch.setattr("app.main.get_redis", lambda: _Redis())
    settings = Settings(
        app_env="test",
        worker_heartbeat_key_prefix=prefix,
        readiness_required_worker_roles="inbound,outbound,scheduler",
    )

    assert await _probe_worker_heartbeats(settings) == {
        "inbound": False,
        "outbound": False,
        "scheduler": True,
    }


def test_legacy_api_paths_advertise_api_prefix_successor() -> None:
    app = FastAPI()
    _setup_legacy_api_deprecation_headers(app)

    @app.get("/v1/example")
    async def example() -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app)
    legacy = client.get("/v1/example")
    current = client.get(
        "/v1/example",
        headers={"X-Agent-Console-Api-Prefix": "/api"},
    )

    assert legacy.headers["Deprecation"] == "true"
    assert legacy.headers["Sunset"] == "Sun, 31 Jan 2027 00:00:00 GMT"
    assert legacy.headers["Link"] == '</api/v1/example>; rel="successor-version"'
    assert "Deprecation" not in current.headers


@pytest.mark.asyncio
async def test_readiness_payload_reports_blocked_flow_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = Container()
    container.__dict__.update(
        _vector_backend="qdrant",
        _persistence_backend="postgres",
    )
    settings = Settings(
        app_env="test",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
        orchestrator_flow_runtime_enabled=True,
        orchestrator_flow_runtime_name="auto",
        orchestrator_flow_runtime_allowed_names="auto",
        orchestrator_flow_runtime_allow_target_flows=False,
    )

    async def always_ok() -> bool:
        return True

    async def qdrant_ok(_url: str) -> bool:
        return True

    monkeypatch.setattr("app.main._probe_redis", always_ok)
    monkeypatch.setattr("app.main._probe_db", always_ok)
    monkeypatch.setattr("app.main._probe_qdrant", qdrant_ok)

    status_code, payload = await _readiness_payload(container, settings)

    assert status_code == 503
    assert payload["status"] == "not_ready"
    assert payload["checks"]["flow_runtime"] == {
        "enabled": True,
        "name": "auto",
        "allowed_names": ["auto"],
        "allow_target_flows": False,
        "allow_compatible_fallback": False,
        "allowed": False,
        "reason": "auto_flow_not_allowed",
    }
    assert payload["errors"] == ["flow_runtime_auto_flow_not_allowed"]


@pytest.mark.asyncio
async def test_readiness_payload_reports_invalid_flow_effect_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = Container()
    container.__dict__.update(
        _vector_backend="qdrant",
        _persistence_backend="postgres",
    )
    settings = Settings(
        app_env="test",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
        orchestrator_flow_runtime_enabled=True,
        orchestrator_flow_effect_commit_backend="bogus",
        orchestrator_flow_effect_log_backend="none",
        orchestrator_flow_effect_log_failure_policy="fail_closed",
    )

    async def always_ok() -> bool:
        return True

    async def qdrant_ok(_url: str) -> bool:
        return True

    monkeypatch.setattr("app.main._probe_redis", always_ok)
    monkeypatch.setattr("app.main._probe_db", always_ok)
    monkeypatch.setattr("app.main._probe_qdrant", qdrant_ok)

    status_code, payload = await _readiness_payload(container, settings)

    assert status_code == 503
    assert payload["checks"]["flow_effect_commit"] == {
        "backend": "bogus",
        "allowed": False,
        "reason": "unsupported_backend",
        "ttl_seconds": 604800,
        "key_prefix": "cs:flow:effect",
        "stream": "cs:flow:effects",
        "handlers_enabled": False,
        "handler_allowlist": [],
        "handler_mode": "off",
        "handlers_commit_backend_safe": False,
        "log_backend": "none",
        "log_failure_policy": "fail_closed",
    }
    assert payload["errors"] == ["flow_effect_commit_unsupported_backend"]


@pytest.mark.asyncio
async def test_readiness_payload_reports_blocked_target_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = Container()
    container.__dict__.update(
        _vector_backend="qdrant",
        _persistence_backend="postgres",
    )
    settings = Settings(
        app_env="test",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
        orchestrator_flow_runtime_enabled=True,
        orchestrator_flow_runtime_name="default_group_channel_flow",
        orchestrator_flow_runtime_allowed_names="default_group_channel_flow",
        orchestrator_flow_runtime_allow_target_flows=False,
    )
    _patch_ready_probes(monkeypatch)

    status_code, payload = await _readiness_payload(container, settings)

    assert status_code == 503
    assert payload["checks"]["flow_runtime"]["allowed"] is False
    assert payload["checks"]["flow_runtime"]["reason"] == "target_flow_not_allowed"
    assert payload["errors"] == ["flow_runtime_target_flow_not_allowed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        (
            {
                "orchestrator_flow_effect_commit_backend": "redis",
                "orchestrator_flow_effect_log_backend": "bogus",
            },
            "unsupported_log_backend",
        ),
        (
            {
                "orchestrator_flow_effect_commit_backend": "redis",
                "orchestrator_flow_effect_log_backend": "postgres",
                "orchestrator_flow_effect_log_failure_policy": "bogus",
            },
            "unsupported_log_failure_policy",
        ),
    ],
)
async def test_readiness_payload_reports_invalid_flow_effect_log_config(
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, str],
    reason: str,
) -> None:
    container = Container()
    container.__dict__.update(
        _vector_backend="qdrant",
        _persistence_backend="postgres",
    )
    settings = Settings(
        app_env="test",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
        orchestrator_flow_runtime_enabled=True,
        **kwargs,
    )
    _patch_ready_probes(monkeypatch)

    status_code, payload = await _readiness_payload(container, settings)

    assert status_code == 503
    assert payload["checks"]["flow_effect_commit"]["allowed"] is False
    assert payload["checks"]["flow_effect_commit"]["reason"] == reason
    assert payload["errors"] == [f"flow_effect_commit_{reason}"]


@pytest.mark.asyncio
async def test_readiness_payload_allows_redis_commit_with_postgres_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = Container()
    container.__dict__.update(
        _vector_backend="qdrant",
        _persistence_backend="postgres",
    )
    settings = Settings(
        app_env="test",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
        orchestrator_flow_runtime_enabled=True,
        orchestrator_flow_effect_commit_backend="redis",
        orchestrator_flow_effect_log_backend="postgres",
        orchestrator_flow_effect_log_failure_policy="fail_closed",
    )
    _patch_ready_probes(monkeypatch)

    status_code, payload = await _readiness_payload(container, settings)

    assert status_code == 200
    assert payload["checks"]["flow_effect_commit"]["allowed"] is True
    assert payload["checks"]["flow_effect_commit"]["reason"] == "allowed"
    assert payload["checks"]["flow_effect_commit"]["handlers_commit_backend_safe"] is True
    assert payload["errors"] == []


@pytest.mark.asyncio
async def test_readiness_payload_rejects_handlers_without_safe_commit_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = Container()
    container.__dict__.update(
        _vector_backend="qdrant",
        _persistence_backend="postgres",
    )
    settings = Settings(
        app_env="test",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
        orchestrator_flow_runtime_enabled=True,
        orchestrator_flow_effect_commit_backend="memory",
        orchestrator_flow_effect_handlers_enabled=True,
        orchestrator_flow_effect_log_backend="none",
        orchestrator_flow_effect_log_failure_policy="fail_closed",
    )
    _patch_ready_probes(monkeypatch)

    status_code, payload = await _readiness_payload(container, settings)

    assert status_code == 503
    assert payload["checks"]["flow_effect_commit"] == {
        "backend": "memory",
        "allowed": False,
        "reason": "handlers_require_redis_commit_backend",
        "ttl_seconds": 604800,
        "key_prefix": "cs:flow:effect",
        "stream": "cs:flow:effects",
        "handlers_enabled": True,
        "handler_allowlist": [],
        "handler_mode": "all",
        "handlers_commit_backend_safe": False,
        "log_backend": "none",
        "log_failure_policy": "fail_closed",
    }
    assert payload["errors"] == [
        "flow_effect_commit_handlers_require_redis_commit_backend"
    ]


def test_create_app_lifespan_runs_without_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeBus:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    class _FakeHTTPClient:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    fake_bus = _FakeBus()
    fake_http = _FakeHTTPClient()
    container = Container(bus=fake_bus)
    container.__dict__.update(
        _http_client=fake_http,
        _knowledge_features_enabled=False,
    )

    async def fake_build_container(settings: Settings | None = None) -> Container:
        _ = settings
        return container

    def fake_mount_routes(app: FastAPI, container_arg: Container) -> None:
        _ = container_arg

        @app.get("/healthz")
        async def healthz() -> dict[str, str]:
            return {"status": "ok"}

    async def fake_close_redis() -> None:
        return None

    async def fake_dispose_engine() -> None:
        return None

    monkeypatch.setattr("app.main.build_container", fake_build_container)
    monkeypatch.setattr("app.main._mount_routes", fake_mount_routes)
    monkeypatch.setattr("app.main.close_redis", fake_close_redis)
    monkeypatch.setattr("app.main.dispose_engine", fake_dispose_engine)

    with TestClient(create_app()) as client:
        resp = client.get("/healthz")

    assert resp.status_code == 200
    assert fake_bus.closed is True
    assert fake_http.closed is True


def test_create_app_enables_cors_for_frontend_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeBus:
        async def close(self) -> None:
            return None

    class _FakeHTTPClient:
        async def aclose(self) -> None:
            return None

    settings = Settings(
        frontend_cors_origins="http://127.0.0.1:5173,http://localhost:5173",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )
    container = Container(bus=_FakeBus())
    container.__dict__.update(
        _http_client=_FakeHTTPClient(),
        _knowledge_features_enabled=False,
    )

    async def fake_build_container(settings_arg: Settings | None = None) -> Container:
        assert settings_arg is settings
        return container

    def fake_mount_routes(app: FastAPI, container_arg: Container) -> None:
        _ = container_arg

        @app.get("/healthz")
        async def healthz() -> dict[str, str]:
            return {"status": "ok"}

    async def fake_close_redis() -> None:
        return None

    async def fake_dispose_engine() -> None:
        return None

    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    monkeypatch.setattr("app.main.build_container", fake_build_container)
    monkeypatch.setattr("app.main._mount_routes", fake_mount_routes)
    monkeypatch.setattr("app.main.close_redis", fake_close_redis)
    monkeypatch.setattr("app.main.dispose_engine", fake_dispose_engine)

    with TestClient(create_app()) as client:
        resp = client.get("/healthz", headers={"Origin": "http://127.0.0.1:5173"})

    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"


def test_worker_consumer_names_resolve_from_instance_id() -> None:
    settings = Settings(
        worker_instance_id="node-a-01",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )

    assert settings.resolved_inbound_worker_consumer_name == "inbound-node-a-01"
    assert settings.resolved_outbound_worker_consumer_name == "egress-node-a-01"


def test_worker_consumer_names_allow_explicit_overrides() -> None:
    settings = Settings(
        worker_instance_id="node-a-01",
        inbound_worker_consumer_name="custom-inbound",
        outbound_worker_consumer_name="custom-egress",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )

    assert settings.resolved_inbound_worker_consumer_name == "custom-inbound"
    assert settings.resolved_outbound_worker_consumer_name == "custom-egress"


def test_worker_runtime_settings_allow_explicit_overrides() -> None:
    settings = Settings(
        bus_consume_batch_size=32,
        bus_consume_block_ms=1500,
        worker_shutdown_timeout_seconds=12.5,
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )

    assert settings.bus_consume_batch_size == 32
    assert settings.bus_consume_block_ms == 1500
    assert settings.worker_shutdown_timeout_seconds == 12.5


def test_memory_llm_extraction_job_drain_max_claims_allows_canary_cap() -> None:
    settings = Settings(
        memory_llm_extraction_job_drain_max_claims=1,
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )

    assert settings.memory_llm_extraction_job_drain_max_claims == 1


def test_memory_llm_extraction_job_drain_max_claims_empty_is_unlimited() -> None:
    settings = Settings(
        memory_llm_extraction_job_drain_max_claims="",
        outbound_hmac_secret="test_secret",
        tenant_demo_secret="test_tenant_secret",
    )

    assert settings.memory_llm_extraction_job_drain_max_claims == 0
