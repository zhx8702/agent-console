from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI, Request

from app.admin import kb_router
from app.admin.authorization import AdminRole, Principal
from app.admin.capabilities import build_tenant_capabilities
from app.common.config import Settings
from app.plugin.base import PluginMeta


class _Plugin:
    def __init__(
        self,
        name: str,
        *,
        version: str = "1.0.0",
        dependencies: list[str] | None = None,
    ) -> None:
        self.meta = PluginMeta(
            name=name,
            version=version,
            description=f"{name} plugin",
            dependencies=list(dependencies or []),
        )


class _Registry:
    def __init__(
        self,
        plugins: list[_Plugin],
        *,
        active: set[str] | None = None,
        failures: dict[str, str] | None = None,
    ) -> None:
        self._plugins = {plugin.meta.name: plugin for plugin in plugins}
        self._active = set(active if active is not None else self._plugins)
        self._failures = dict(failures or {})

    @property
    def loaded_plugins(self) -> dict[str, _Plugin]:
        return dict(self._plugins)

    @property
    def summary(self) -> list[dict[str, str]]:
        return [
            {
                "name": plugin.meta.name,
                "version": plugin.meta.version,
                "description": plugin.meta.description,
            }
            for plugin in self._plugins.values()
            if plugin.meta.name in self._active
        ]

    @property
    def initialization_failures(self) -> dict[str, str]:
        return dict(self._failures)

    def all_permissions(self) -> dict[str, set[str]]:
        return {
            name: {"admin_api", "storage:shared"}
            for name in self._active
        }

    def all_api_routers(self) -> list[tuple[str, object]]:
        return [(name, object()) for name in sorted(self._active)]


class _ScopeManager:
    def __init__(self, items: list[dict[str, object]] | None = None) -> None:
        self.items = list(items or [])

    async def scope_states(self, **_kwargs) -> dict[str, object]:
        return {"items": list(self.items)}


class _FailingScopeManager:
    async def scope_states(self, **_kwargs) -> dict[str, object]:
        raise RuntimeError("scope store unavailable")


class _ConnectionStore:
    def __init__(self, items: list[dict[str, object]]) -> None:
        self.items = items

    async def list(self, tenant_id: str) -> list[dict[str, object]]:
        return [item for item in self.items if item.get("tenant_id") == tenant_id]


def _principal(role: AdminRole = AdminRole.PLATFORM_ADMIN) -> Principal:
    return Principal(
        subject="admin-user",
        roles=(role.value,),
        tenant_ids=("demo",),
        auth_kind="session",
    )


def _group_principal(role: AdminRole = AdminRole.GROUP_OPERATOR) -> Principal:
    return Principal(
        subject="group-user",
        roles=(role.value,),
        tenant_ids=("demo",),
        group_ids=("group-1",),
        auth_kind="session",
    )


def _capability(payload: dict[str, object], capability_id: str) -> dict[str, object]:
    return next(
        item
        for item in payload["capabilities"]
        if item["id"] == capability_id
    )


@pytest.mark.asyncio
async def test_capability_registry_reports_truthful_blockers_and_recovery_actions() -> None:
    registry = _Registry(
        [
            _Plugin("wxbot", version="0.2.0"),
            _Plugin("tibo_reset", dependencies=["wxbot>=0.2.0"]),
        ]
    )
    settings = Settings(
        app_env="test",
        llm_provider="openai",
        llm_embed_provider="openai",
        openai_api_key=None,
    )

    payload = await build_tenant_capabilities(
        tenant_id="demo",
        principal=_principal(),
        settings=settings,
        plugin_registry=registry,
        plugin_manager=_ScopeManager(),
        faq_store=object(),
        kb_service=object(),
        dlq_service=object(),
        stream_service=object(),
        orchestrator=object(),
    )

    llm = _capability(payload, "runtime.llm")
    assert llm["enabled"] is True
    assert llm["available"] is True
    assert llm["health"] == "action_required"
    assert llm["dependencies"][0]["state"] == "action_required"
    assert llm["recovery_actions"] == [
        {
            "type": "configure",
            "label": "修复模型配置",
            "target": "/llm",
            "requires_admin": True,
        }
    ]

    wxbot = _capability(payload, "plugin.wxbot")
    assert wxbot["enabled"] is True
    assert wxbot["available"] is True
    assert wxbot["health"] == "action_required"
    assert wxbot["status_reason"] == "adapter_available_connection_unverified"
    assert wxbot["connection_state"] == "unverified"
    assert wxbot["required_for_core"] is False
    assert wxbot["adapter_id"] == "wechat-sdk"
    assert wxbot["entry_route"] == "/channels?adapter=wechat-sdk"
    assert wxbot["source"] == "plugin_registry"
    assert wxbot["permissions"] == ["admin_api", "storage:shared"]
    assert payload["message_flow_runtime"]["enabled"] is False
    assert "session_coverage" not in payload["message_flow_runtime"]

    missing_amap = _capability(payload, "plugin.amap")
    assert missing_amap["enabled"] is False
    assert missing_amap["available"] is False
    assert missing_amap["health"] == "blocked"
    assert missing_amap["dependencies"] == [
        {
            "id": "plugin:amap",
            "required": True,
            "state": "blocked",
            "reason": "plugin_not_loaded",
        }
    ]
    assert missing_amap["recovery_actions"][0]["type"] == "install"
    assert missing_amap["recovery_actions"][0]["target"] == "/plugins/marketplace?plugin=amap"

    assert [step["id"] for step in payload["onboarding"]["steps"]] == [
        "dependencies",
        "llm",
        "message_channel",
        "connection_probe",
        "participation_policy",
        "test",
        "launch",
    ]
    assert payload["onboarding"]["steps"][1]["state"] == "action_required"
    assert payload["onboarding"]["steps"][5]["state"] == "blocked"


@pytest.mark.asyncio
async def test_core_onboarding_and_navigation_do_not_require_wechat_connection() -> None:
    payload = await build_tenant_capabilities(
        tenant_id="demo",
        principal=_principal(),
        settings=Settings(app_env="test"),
        plugin_registry=_Registry([]),
        plugin_manager=_ScopeManager(),
        faq_store=object(),
        kb_service=object(),
        dlq_service=object(),
        stream_service=object(),
        orchestrator=object(),
    )

    channels = _capability(payload, "messages.channels")
    assert channels["label"] == "消息平台连接"
    assert channels["available"] is True
    assert channels["health"] == "action_required"
    assert channels["connection_state"] == "unverified"
    assert channels["required_for_core"] is False

    navigation = {item["path"]: item for item in payload["navigation"]}
    assert navigation["/channels"]["visible"] is True
    assert "/wxbot" not in navigation

    steps = {item["id"]: item for item in payload["onboarding"]["steps"]}
    assert steps["dependencies"]["state"] == "ready"
    assert steps["message_channel"]["state"] == "action_required"
    assert steps["connection_probe"]["state"] == "action_required"
    assert steps["launch"]["state"] == "ready"
    assert steps["launch"]["dependencies"][0]["state"] == "ready"
    assert "plugin:wxbot" not in str(payload["onboarding"])
    assert all(
        action["target"].startswith("/channels")
        for step in payload["onboarding"]["steps"][2:]
        for action in step["recovery_actions"]
    )


@pytest.mark.asyncio
async def test_configured_connection_completes_add_step_and_probe_updates_readiness() -> None:
    base_connection = {
        "tenant_id": "demo",
        "connection_id": "wechat-primary",
        "adapter_id": "wechat-sdk",
        "required_for_launch": False,
        "effective_state": "unverified",
        "last_probed_at": None,
        "last_error_code": "",
        "last_inbound_at": None,
        "last_outbound_delivered_at": None,
    }
    payload = await build_tenant_capabilities(
        tenant_id="demo",
        principal=_principal(),
        settings=Settings(app_env="test"),
        plugin_registry=_Registry([_Plugin("wxbot")]),
        plugin_manager=_ScopeManager(),
        faq_store=object(),
        kb_service=object(),
        dlq_service=object(),
        stream_service=object(),
        orchestrator=object(),
        connection_store=_ConnectionStore([base_connection]),
    )

    channels = _capability(payload, "messages.channels")
    steps = {item["id"]: item for item in payload["onboarding"]["steps"]}
    assert channels["connection_state"] == "configured"
    assert steps["message_channel"]["state"] == "ready"
    assert steps["connection_probe"]["state"] == "action_required"
    assert steps["test"]["optional"] is True
    assert steps["launch"]["state"] == "ready"

    probed = await build_tenant_capabilities(
        tenant_id="demo",
        principal=_principal(),
        settings=Settings(app_env="test"),
        plugin_registry=_Registry([_Plugin("wxbot")]),
        plugin_manager=_ScopeManager(),
        faq_store=object(),
        kb_service=object(),
        dlq_service=object(),
        stream_service=object(),
        orchestrator=object(),
        connection_store=_ConnectionStore(
            [
                {
                    **base_connection,
                    "effective_state": "enabled",
                    "last_probed_at": datetime.now(UTC).isoformat(),
                }
            ]
        ),
    )
    probed_channels = _capability(probed, "messages.channels")
    probed_wxbot = _capability(probed, "plugin.wxbot")
    probed_steps = {item["id"]: item for item in probed["onboarding"]["steps"]}
    assert probed_channels["connection_state"] == "verified"
    assert probed_channels["health"] == "ready"
    assert probed_wxbot["connection_state"] == "verified"
    assert probed_wxbot["health"] == "ready"
    assert probed_steps["connection_probe"]["state"] == "ready"


@pytest.mark.asyncio
async def test_observed_round_trip_replaces_probe_and_optional_send_test() -> None:
    payload = await build_tenant_capabilities(
        tenant_id="demo",
        principal=_principal(),
        settings=Settings(app_env="test"),
        plugin_registry=_Registry([_Plugin("wxbot")]),
        plugin_manager=_ScopeManager(),
        faq_store=object(),
        kb_service=object(),
        dlq_service=object(),
        stream_service=object(),
        orchestrator=object(),
        connection_store=_ConnectionStore(
            [
                {
                    "tenant_id": "demo",
                    "connection_id": "wechat-primary",
                    "adapter_id": "wechat-sdk",
                    "required_for_launch": True,
                    "effective_state": "unverified",
                    "last_probed_at": None,
                    "last_error_code": "",
                    "last_inbound_at": datetime.now(UTC).isoformat(),
                    "last_outbound_delivered_at": datetime.now(UTC).isoformat(),
                }
            ]
        ),
    )

    steps = {item["id"]: item for item in payload["onboarding"]["steps"]}
    assert steps["connection_probe"]["state"] == "ready"
    assert steps["connection_probe"]["dependencies"][0]["reason"] == (
        "bidirectional_message_flow_observed"
    )
    assert steps["participation_policy"]["state"] == "ready"
    assert steps["test"]["state"] == "ready"
    assert steps["test"]["recovery_actions"] == []
    assert steps["launch"]["state"] == "ready"


@pytest.mark.asyncio
async def test_wechat_adapter_recovery_keeps_generic_and_extension_routes() -> None:
    payload = await build_tenant_capabilities(
        tenant_id="demo",
        principal=_principal(),
        settings=Settings(app_env="test"),
        plugin_registry=_Registry([_Plugin("wxbot")]),
        plugin_manager=_ScopeManager(),
        faq_store=None,
        kb_service=None,
        dlq_service=None,
        stream_service=object(),
        orchestrator=object(),
    )

    wxbot = _capability(payload, "plugin.wxbot")
    targets = {item["target"] for item in wxbot["recovery_actions"]}
    assert "/channels?adapter=wechat-sdk" in targets
    assert "/wxbot" in targets
    assert wxbot["extension_route"] == "/wxbot"


@pytest.mark.asyncio
async def test_optional_scope_lookup_failure_degrades_but_does_not_hide_plugin() -> None:
    registry = _Registry([_Plugin("wxbot")])

    payload = await build_tenant_capabilities(
        tenant_id="demo",
        principal=_principal(),
        settings=Settings(app_env="test"),
        plugin_registry=registry,
        plugin_manager=_FailingScopeManager(),
        faq_store=None,
        kb_service=None,
        dlq_service=None,
        stream_service=None,
        orchestrator=object(),
    )

    wxbot = _capability(payload, "plugin.wxbot")
    assert wxbot["enabled"] is True
    assert wxbot["available"] is True
    assert wxbot["health"] == "degraded"
    scope_dependency = next(
        item for item in wxbot["dependencies"] if item["id"] == "tenant_plugin_scope"
    )
    assert scope_dependency["required"] is False
    assert scope_dependency["state"] == "degraded"
    assert any(action["type"] == "retry" for action in wxbot["recovery_actions"])
    channel_navigation = next(
        item for item in payload["navigation"] if item["path"] == "/channels"
    )
    assert channel_navigation["visible"] is True


@pytest.mark.asyncio
async def test_explicit_tenant_scope_disable_hides_plugin_and_reports_recovery() -> None:
    registry = _Registry([_Plugin("wxbot")])
    manager = _ScopeManager(
        [
            {
                "tenant_id": "demo",
                "session_id": "",
                "plugin_name": "wxbot",
                "enabled": False,
            },
            {
                "tenant_id": "demo",
                "session_id": "group-1",
                "plugin_name": "wxbot",
                "enabled": True,
            },
        ]
    )

    payload = await build_tenant_capabilities(
        tenant_id="demo",
        principal=_principal(),
        settings=Settings(app_env="test"),
        plugin_registry=registry,
        plugin_manager=manager,
        faq_store=object(),
        kb_service=object(),
        dlq_service=object(),
        stream_service=object(),
        orchestrator=object(),
    )

    wxbot = _capability(payload, "plugin.wxbot")
    assert wxbot["enabled"] is False
    assert wxbot["available"] is False
    assert wxbot["health"] == "action_required"
    assert wxbot["status_reason"] == "plugin_disabled_for_tenant"
    assert wxbot["recovery_actions"][0]["target"] == "/plugins?plugin=wxbot"
    navigation = next(
        item for item in payload["navigation"] if item["path"] == "/channels"
    )
    assert navigation == {
        "path": "/channels",
        "capability_id": "messages.channels",
        "required_permission": "admin:write",
        "visible": True,
        "reason": "visible",
    }


@pytest.mark.asyncio
async def test_navigation_is_filtered_by_rbac_and_capability_availability() -> None:
    registry = _Registry([_Plugin("memory"), _Plugin("wxbot")])

    payload = await build_tenant_capabilities(
        tenant_id="demo",
        principal=_principal(AdminRole.PLATFORM_READER),
        settings=Settings(app_env="test"),
        plugin_registry=registry,
        plugin_manager=_ScopeManager(),
        faq_store=object(),
        kb_service=object(),
        dlq_service=object(),
        stream_service=object(),
        orchestrator=object(),
    )

    navigation = {item["path"]: item for item in payload["navigation"]}
    assert navigation["/"]["visible"] is True
    assert navigation["/queues"]["visible"] is True
    assert navigation["/relationship-graph"]["visible"] is True
    assert navigation["/llm"]["visible"] is False
    assert navigation["/llm"]["reason"] == "permission_denied"
    assert navigation["/channels"]["visible"] is False
    assert navigation["/channels"]["reason"] == "permission_denied"
    assert navigation["/amap"]["visible"] is False


@pytest.mark.asyncio
async def test_group_scoped_capabilities_hide_tenant_diagnostics_and_global_navigation() -> None:
    registry = _Registry(
        [_Plugin("wxbot", version="9.9.9"), _Plugin("memory")],
        failures={"amap": "private platform stack trace"},
    )
    settings = Settings(
        app_env="test",
        wxbot_sdk_url="http://internal-wxbot:8066",
        llm_provider="openai",
        openai_api_key=None,
    )

    payload = await build_tenant_capabilities(
        tenant_id="demo",
        principal=_group_principal(),
        settings=settings,
        plugin_registry=registry,
        plugin_manager=_ScopeManager(),
        faq_store=object(),
        kb_service=object(),
        dlq_service=object(),
        stream_service=object(),
        orchestrator=object(),
    )

    assert payload["access"]["scope"] == "group"
    assert "message_flow_runtime" not in payload
    navigation = {item["path"]: item for item in payload["navigation"]}
    assert navigation["/"]["visible"] is True
    assert navigation["/group-behavior"]["visible"] is True
    assert navigation["/memory"]["visible"] is True
    assert "/wxbot" not in navigation
    assert "/plugins" not in navigation
    assert "/queues" not in navigation
    assert "/llm" not in navigation

    serialized = str(payload)
    assert "internal-wxbot" not in serialized
    assert "private platform stack trace" not in serialized
    assert "9.9.9" not in serialized
    assert _capability(payload, "social.group_behavior")["entry_route"] == "/group-behavior"
    assert all(item["id"] != "plugin.wxbot" for item in payload["capabilities"])
    assert all(item["source"] == "scoped_view" for item in payload["capabilities"])
    assert all(not item["dependencies"] for item in payload["capabilities"])
    assert all(not item["permissions"] for item in payload["capabilities"])
    assert [step["id"] for step in payload["onboarding"]["steps"]] == [
        "group_scope",
        "participation_policy",
    ]


@pytest.mark.asyncio
async def test_capabilities_endpoint_enforces_authentication_and_tenant_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        app_env="test",
        admin_bearer_token="unit_admin_token",
    )
    normal_app = FastAPI()
    normal_app.include_router(kb_router.build_admin_router(None, None, settings))
    normal_transport = httpx.ASGITransport(app=normal_app)
    async with httpx.AsyncClient(
        transport=normal_transport,
        base_url="http://testserver",
    ) as client:
        missing_auth = await client.get("/v1/admin/tenants/demo/capabilities")
        wildcard_admin = await client.get(
            "/v1/admin/tenants/demo/capabilities",
            headers={"Authorization": "Bearer unit_admin_token"},
        )

    assert missing_auth.status_code == 401
    assert wildcard_admin.status_code == 200
    assert wildcard_admin.json()["tenant_id"] == "demo"

    scoped_principal = _principal(AdminRole.PLATFORM_READER)

    def fake_authorization_dependency(*_args, **_kwargs):
        async def authorize(request: Request) -> Principal:
            request.state.admin_principal = scoped_principal
            return scoped_principal

        return authorize

    monkeypatch.setattr(
        kb_router,
        "build_admin_authorization_dependency",
        fake_authorization_dependency,
    )
    scoped_app = FastAPI()
    scoped_app.include_router(kb_router.build_admin_router(None, None, settings))
    scoped_transport = httpx.ASGITransport(app=scoped_app)
    async with httpx.AsyncClient(
        transport=scoped_transport,
        base_url="http://testserver",
    ) as client:
        allowed = await client.get("/v1/admin/tenants/demo/capabilities")
        forbidden = await client.get("/v1/admin/tenants/other/capabilities")

    assert allowed.status_code == 200
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"] == "tenant_scope_forbidden"
