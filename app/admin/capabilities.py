"""Tenant-aware capability inventory for the administrative console.

The registry intentionally reports what is wired and enabled without probing
tenant data through plugin-specific stores.  This keeps the endpoint read-only,
fast, and honest about the distinction between an available control surface and
an external dependency that still needs an operator check.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from packaging.version import InvalidVersion, Version

from app.admin.authorization import AdminPermission, Principal
from app.common.config import Settings
from app.llm.service import validate_llm_settings
from app.orchestrator.flow_runtime_config import build_flow_runtime_config_payload
from app.plugin.dependencies import PluginDependencyError, parse_plugin_dependency

CapabilityHealth = Literal["ready", "action_required", "blocked", "degraded"]

_KNOWN_PLUGIN_ROUTES: dict[str, str] = {
    "amap": "/amap",
    "commands": "/commands",
    "credits": "/credits",
    "memory": "/memory",
    "moderation": "/moderation",
    "persona_extract": "/persona",
    "repeater": "/repeater",
    "wxbot": "/channels?adapter=wechat-sdk",
    "tibo_reset": "/plugins?plugin=tibo_reset",
}

_WECHAT_ADAPTER_ID = "wechat-sdk"
_CONNECTION_EVIDENCE_MAX_AGE = timedelta(hours=24)
_CONNECTION_EVIDENCE_FUTURE_SKEW = timedelta(minutes=5)

_PLUGIN_LABELS = {
    "amap": "高德地图",
    "commands": "命令中心",
    "credits": "积分运营",
    "draw": "群聊绘图",
    "group_activity": "群聊主动参与",
    "memory": "用户记忆",
    "moderation": "内容审核",
    "persona_extract": "回复风格",
    "repeater": "复读机",
    "tibo_reset": "Tibo Reset",
    "wxbot": "微信 SDK 适配器",
}

_NAVIGATION_SPECS: tuple[tuple[str, str, AdminPermission, bool], ...] = (
    ("/", "core.overview", AdminPermission.READ, True),
    ("/llm", "runtime.llm", AdminPermission.DANGER, False),
    ("/plugins", "plugins.management", AdminPermission.DANGER, False),
    ("/plugins/marketplace", "plugins.marketplace", AdminPermission.DANGER, False),
    ("/channels", "messages.channels", AdminPermission.WRITE, False),
    ("/amap", "plugin.amap", AdminPermission.WRITE, False),
    ("/commands", "plugin.commands", AdminPermission.WRITE, False),
    ("/playground", "messages.playground", AdminPermission.WRITE, True),
    ("/queues", "messages.queues", AdminPermission.READ, False),
    ("/knowledge", "knowledge.management", AdminPermission.WRITE, True),
    ("/memory", "plugin.memory", AdminPermission.WRITE, True),
    ("/relationship-graph", "plugin.memory", AdminPermission.READ, True),
    ("/credits", "plugin.credits", AdminPermission.WRITE, True),
    ("/moderation", "plugin.moderation", AdminPermission.WRITE, True),
    ("/persona", "plugin.persona_extract", AdminPermission.WRITE, True),
    ("/repeater", "plugin.repeater", AdminPermission.WRITE, True),
    ("/group-behavior", "social.group_behavior", AdminPermission.WRITE, True),
    ("/dlq", "operations.dlq", AdminPermission.DANGER, False),
)


def tenant_scope_allowed(principal: Principal, tenant_id: str) -> bool:
    """Return whether an authenticated principal may inspect a tenant."""

    return principal.allows_tenant(tenant_id)


@dataclass(frozen=True, slots=True)
class _MessageConnectionEvidence:
    configured_ids: frozenset[str] = frozenset()
    verified_ids: frozenset[str] = frozenset()
    bidirectional_ids: frozenset[str] = frozenset()
    required_ids: frozenset[str] = frozenset()
    adapter_by_connection: tuple[tuple[str, str], ...] = ()
    lookup_failed: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.configured_ids)

    @property
    def verified(self) -> bool:
        return bool(self.verified_ids)

    @property
    def bidirectional(self) -> bool:
        return bool(self.bidirectional_ids)

    @property
    def required_connections_ready(self) -> bool:
        return self.required_ids.issubset(self.verified_ids)

    def adapter_state(self, adapter_id: str) -> str:
        normalized = str(adapter_id or "").strip().lower()
        adapter_map = dict(self.adapter_by_connection)
        matching = {
            connection_id
            for connection_id in self.configured_ids
            if adapter_map.get(connection_id) == normalized
        }
        if matching.intersection(self.verified_ids):
            return "verified"
        return "configured" if matching else "unverified"


async def _message_connection_evidence(
    *,
    tenant_id: str,
    connection_store: Any | None,
) -> _MessageConnectionEvidence:
    """Derive connection readiness from durable state and observed message flow.

    A successful stored probe is authoritative.  Durable inbound acceptance
    and final outbound-delivery timestamps for the same connection are a
    stronger, user-visible round-trip signal.  Merely having a worker or a
    queued outbound message is intentionally not enough.
    """

    connections: list[Any] = []
    lookup_failed = False
    list_connections = getattr(connection_store, "list", None)
    if callable(list_connections):
        try:
            value = await list_connections(tenant_id)
            connections = list(value or [])
        except Exception:
            lookup_failed = True

    configured_ids: set[str] = set()
    verified_ids: set[str] = set()
    required_ids: set[str] = set()
    bidirectional_ids: set[str] = set()
    adapter_by_connection: dict[str, str] = {}
    for connection in connections:
        connection_id = _item_text(connection, "connection_id")
        if not connection_id:
            continue
        configured_ids.add(connection_id)
        adapter_by_connection[connection_id] = _item_text(connection, "adapter_id").lower()
        if _item_bool(connection, "required_for_launch"):
            required_ids.add(connection_id)
        last_probed_at = _item_value(connection, "last_probed_at")
        effective_state = _item_text(connection, "effective_state").lower()
        if (
            _connection_evidence_is_recent(last_probed_at)
            and not _item_text(connection, "last_error_code")
            and effective_state in {"enabled", "ready"}
        ):
            verified_ids.add(connection_id)
        if (
            _connection_evidence_is_recent(_item_value(connection, "last_inbound_at"))
            and _connection_evidence_is_recent(
                _item_value(connection, "last_outbound_delivered_at")
            )
        ):
            bidirectional_ids.add(connection_id)
            verified_ids.add(connection_id)

    return _MessageConnectionEvidence(
        configured_ids=frozenset(configured_ids),
        verified_ids=frozenset(verified_ids),
        bidirectional_ids=frozenset(bidirectional_ids),
        required_ids=frozenset(required_ids),
        adapter_by_connection=tuple(sorted(adapter_by_connection.items())),
        lookup_failed=lookup_failed,
    )


def _item_text(item: Any, field: str) -> str:
    value = _item_value(item, field)
    return str(value or "").strip()


def _item_bool(item: Any, field: str) -> bool:
    value = _item_value(item, field)
    return bool(value)


def _item_value(item: Any, field: str) -> Any:
    return item.get(field) if isinstance(item, Mapping) else getattr(item, field, None)


def _connection_evidence_is_recent(value: Any) -> bool:
    if isinstance(value, datetime):
        observed_at = value
    else:
        text = str(value or "").strip()
        if not text:
            return False
        try:
            observed_at = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return False
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    return (
        now - _CONNECTION_EVIDENCE_MAX_AGE
        <= observed_at.astimezone(UTC)
        <= now + _CONNECTION_EVIDENCE_FUTURE_SKEW
    )


async def build_tenant_capabilities(
    *,
    tenant_id: str,
    principal: Principal,
    settings: Settings,
    plugin_registry: Any | None,
    plugin_manager: Any | None,
    faq_store: Any | None,
    kb_service: Any | None,
    dlq_service: Any | None,
    stream_service: Any | None,
    orchestrator: Any | None,
    connection_store: Any | None = None,
) -> dict[str, Any]:
    """Build the server-authoritative capability and launch inventory."""

    connection_evidence = await _message_connection_evidence(
        tenant_id=tenant_id,
        connection_store=connection_store,
    )
    scope_states, scope_error = await _tenant_plugin_scope_states(
        tenant_id,
        plugin_manager,
    )
    capabilities = _core_capabilities(
        tenant_id=tenant_id,
        settings=settings,
        plugin_registry=plugin_registry,
        plugin_manager=plugin_manager,
        faq_store=faq_store,
        kb_service=kb_service,
        dlq_service=dlq_service,
        stream_service=stream_service,
        orchestrator=orchestrator,
        connection_evidence=connection_evidence,
    )
    capabilities.extend(
        _plugin_capabilities(
            tenant_id=tenant_id,
            settings=settings,
            plugin_registry=plugin_registry,
            scope_states=scope_states,
            scope_error=scope_error,
            connection_evidence=connection_evidence,
        )
    )
    wxbot_capability = next(
        (item for item in capabilities if item.get("id") == "plugin.wxbot"),
        None,
    )
    if wxbot_capability is not None:
        capabilities.append(
            {
                **wxbot_capability,
                "id": "social.group_behavior",
                "label": "群参与与行为",
                "entry_route": "/group-behavior",
                "permissions": [AdminPermission.WRITE.value],
                "source": "derived",
            }
        )
    capability_by_id = {item["id"]: item for item in capabilities}
    navigation = _navigation_payload(principal, capability_by_id)
    onboarding = _onboarding_payload(
        tenant_id=tenant_id,
        plugin_registry=plugin_registry,
        stream_service=stream_service,
        orchestrator=orchestrator,
        capability_by_id=capability_by_id,
        connection_evidence=connection_evidence,
    )
    attention_count = sum(item["health"] != "ready" for item in capabilities)
    overall_state: CapabilityHealth = (
        "blocked"
        if onboarding["state"] == "blocked"
        else "degraded"
        if scope_error or any(item["health"] == "degraded" for item in capabilities)
        else "action_required"
        if attention_count
        else "ready"
    )
    payload = {
        "schema_version": "1.0",
        "tenant_id": tenant_id,
        "state": overall_state,
        "access": {
            "subject": principal.subject,
            "roles": list(principal.roles),
            "tenant_ids": list(principal.tenant_ids),
            "permissions": sorted(permission.value for permission in principal.permissions),
            "scope": "group" if principal.requires_explicit_group_scope else "tenant",
        },
        "capabilities": capabilities,
        "navigation": navigation,
        "onboarding": onboarding,
        "message_flow_runtime": build_flow_runtime_config_payload(settings),
        "summary": {
            "total": len(capabilities),
            "ready": sum(item["health"] == "ready" for item in capabilities),
            "attention": attention_count,
            "visible_navigation": sum(bool(item["visible"]) for item in navigation),
        },
    }
    if principal.requires_explicit_group_scope:
        return _group_scoped_capability_payload(payload)
    return payload


def _group_scoped_capability_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove tenant-wide diagnostics from the group-operator console view.

    Group-scoped actors need server-authoritative navigation and a truthful
    availability signal for the pages they may use.  They do not need provider
    validation errors, service topology, plugin versions, SDK addresses, or
    administrator recovery links.  Keeping that distinction here prevents the
    otherwise safe capabilities collection endpoint from becoming a tenant-wide
    diagnostics side channel.
    """

    eligible_ids = {
        capability_id
        for _path, capability_id, _permission, group_scoped in _NAVIGATION_SPECS
        if group_scoped
    }
    capabilities: list[dict[str, Any]] = []
    for item in payload["capabilities"]:
        if item.get("id") not in eligible_ids:
            continue
        available = bool(item.get("enabled")) and bool(item.get("available"))
        capabilities.append(
            {
                "id": item.get("id"),
                "label": item.get("label"),
                "category": item.get("category"),
                "enabled": bool(item.get("enabled")),
                "available": bool(item.get("available")),
                "health": "ready" if available else "blocked",
                "status_reason": "available_for_group_scope" if available else "unavailable_for_group_scope",
                "dependencies": [],
                "recovery_actions": [],
                "source": "scoped_view",
                "plugin": item.get("plugin"),
                "permissions": [],
                "entry_route": item.get("entry_route", ""),
            }
        )

    navigation = [
        item
        for item in payload["navigation"]
        if item.get("capability_id") in eligible_ids
    ]
    group_behavior = next(
        (item for item in capabilities if item.get("id") == "social.group_behavior"),
        None,
    )
    group_behavior_ready = bool(group_behavior and group_behavior.get("available"))
    onboarding_steps = [
        _onboarding_step(
            "group_scope",
            label="确认授权群聊",
            description="当前身份只可查看和管理凭据中明确授权的群聊。",
            state="ready",
            dependencies=[],
            recovery_actions=[],
        ),
        _onboarding_step(
            "participation_policy",
            label="复核群参与策略",
            description="选择授权群后，复核机器人参与、隐私和人工接管边界。",
            state="action_required" if group_behavior_ready else "blocked",
            dependencies=[],
            recovery_actions=(
                [
                    _action(
                        "configure",
                        "打开群参与与行为",
                        "/group-behavior",
                        requires_admin=False,
                    )
                ]
                if group_behavior_ready
                else []
            ),
        ),
    ]
    attention_count = sum(item["health"] != "ready" for item in capabilities)
    scoped_state: CapabilityHealth = (
        "blocked"
        if not group_behavior_ready
        else "action_required"
        if attention_count
        else "ready"
    )
    scoped_payload = dict(payload)
    scoped_payload.pop("message_flow_runtime", None)
    return {
        **scoped_payload,
        "state": scoped_state,
        "capabilities": capabilities,
        "navigation": navigation,
        "onboarding": {
            "state": "blocked" if not group_behavior_ready else "action_required",
            "steps": onboarding_steps,
        },
        "summary": {
            "total": len(capabilities),
            "ready": sum(item["health"] == "ready" for item in capabilities),
            "attention": attention_count,
            "visible_navigation": sum(bool(item["visible"]) for item in navigation),
        },
    }


def _dependency(
    dependency_id: str,
    *,
    required: bool,
    state: CapabilityHealth,
    reason: str,
) -> dict[str, Any]:
    return {
        "id": dependency_id,
        "required": required,
        "state": state,
        "reason": reason,
    }


def _action(
    action_type: Literal["retry", "configure", "install", "contact_admin"],
    label: str,
    target: str,
    *,
    requires_admin: bool,
) -> dict[str, Any]:
    return {
        "type": action_type,
        "label": label,
        "target": target,
        "requires_admin": requires_admin,
    }


def _capability(
    capability_id: str,
    *,
    label: str,
    category: str,
    enabled: bool,
    available: bool,
    health: CapabilityHealth,
    status_reason: str,
    dependencies: list[dict[str, Any]] | None = None,
    recovery_actions: list[dict[str, Any]] | None = None,
    source: str,
    plugin: str | None = None,
    permissions: Iterable[str] = (),
    entry_route: str = "",
) -> dict[str, Any]:
    return {
        "id": capability_id,
        "label": label,
        "category": category,
        "enabled": enabled,
        "available": available,
        "health": health,
        "status_reason": status_reason,
        "dependencies": dependencies or [],
        "recovery_actions": recovery_actions or [],
        "source": source,
        "plugin": plugin,
        "permissions": sorted({str(item) for item in permissions if str(item)}),
        "entry_route": entry_route,
    }


def _service_capability(
    capability_id: str,
    *,
    label: str,
    category: str,
    service_id: str,
    service_available: bool,
    entry_route: str,
    recovery_label: str,
    permission: AdminPermission,
) -> dict[str, Any]:
    dependency = _dependency(
        service_id,
        required=True,
        state="ready" if service_available else "blocked",
        reason="service_registered" if service_available else "service_not_registered",
    )
    return _capability(
        capability_id,
        label=label,
        category=category,
        enabled=True,
        available=service_available,
        health="ready" if service_available else "blocked",
        status_reason="service_registered" if service_available else "service_not_registered",
        dependencies=[dependency],
        recovery_actions=(
            []
            if service_available
            else [
                _action(
                    "contact_admin",
                    recovery_label,
                    entry_route or "/",
                    requires_admin=True,
                )
            ]
        ),
        source="container",
        permissions=[permission.value],
        entry_route=entry_route,
    )


def _core_capabilities(
    *,
    tenant_id: str,
    settings: Settings,
    plugin_registry: Any | None,
    plugin_manager: Any | None,
    faq_store: Any | None,
    kb_service: Any | None,
    dlq_service: Any | None,
    stream_service: Any | None,
    orchestrator: Any | None,
    connection_evidence: _MessageConnectionEvidence,
) -> list[dict[str, Any]]:
    llm_errors = validate_llm_settings(settings)
    llm_dependency = _dependency(
        "llm_configuration",
        required=True,
        state="ready" if not llm_errors else "action_required",
        reason="llm_configuration_valid" if not llm_errors else "; ".join(llm_errors),
    )
    knowledge_available = faq_store is not None or kb_service is not None
    capabilities = [
        _capability(
            "core.overview",
            label="系统概览",
            category="system",
            enabled=True,
            available=True,
            health="ready",
            status_reason="admin_router_mounted",
            source="core",
            permissions=[AdminPermission.READ.value],
            entry_route="/",
        ),
        _capability(
            "runtime.llm",
            label="LLM 配置",
            category="setup",
            enabled=True,
            available=True,
            health="ready" if not llm_errors else "action_required",
            status_reason="llm_configuration_valid" if not llm_errors else "llm_configuration_invalid",
            dependencies=[llm_dependency],
            recovery_actions=(
                []
                if not llm_errors
                else [_action("configure", "修复模型配置", "/llm", requires_admin=True)]
            ),
            source="settings",
            permissions=[AdminPermission.DANGER.value],
            entry_route="/llm",
        ),
        _service_capability(
            "plugins.management",
            label="插件管理",
            category="system",
            service_id="plugin_manager",
            service_available=plugin_manager is not None and plugin_registry is not None,
            entry_route="/plugins",
            recovery_label="联系平台管理员检查插件服务装配",
            permission=AdminPermission.DANGER,
        ),
        _service_capability(
            "plugins.marketplace",
            label="插件市场",
            category="system",
            service_id="plugin_manager",
            service_available=plugin_manager is not None and plugin_registry is not None,
            entry_route="/plugins/marketplace",
            recovery_label="联系平台管理员检查插件服务装配",
            permission=AdminPermission.DANGER,
        ),
        _capability(
            "messages.channels",
            label="消息平台连接",
            category="setup",
            enabled=True,
            available=plugin_registry is not None,
            health=(
                "ready"
                if plugin_registry is not None and connection_evidence.verified
                else "action_required"
                if plugin_registry is not None
                else "blocked"
            ),
            status_reason=(
                "connection_verified_by_probe_or_message_flow"
                if plugin_registry is not None and connection_evidence.verified
                else "connection_configured_but_unverified"
                if plugin_registry is not None and connection_evidence.configured
                else "connection_store_unavailable"
                if plugin_registry is not None and connection_evidence.lookup_failed
                else "connection_not_configured"
                if plugin_registry is not None
                else "adapter_registry_not_available"
            ),
            dependencies=[
                _dependency(
                    "adapter_registry",
                    required=True,
                    state="ready" if plugin_registry is not None else "blocked",
                    reason=(
                        "adapter_catalog_available"
                        if plugin_registry is not None
                        else "adapter_catalog_unavailable"
                    ),
                )
            ],
            recovery_actions=(
                []
                if connection_evidence.verified
                else [
                    _action(
                        "configure",
                        "管理消息平台连接",
                        "/channels",
                        requires_admin=True,
                    )
                ]
            ),
            source="channel_adapter_registry",
            permissions=[AdminPermission.WRITE.value],
            entry_route="/channels",
        ),
        _service_capability(
            "messages.playground",
            label="消息入口测试",
            category="operations",
            service_id="message_orchestrator",
            service_available=orchestrator is not None,
            entry_route="/playground",
            recovery_label="联系平台管理员检查消息编排服务",
            permission=AdminPermission.WRITE,
        ),
        _service_capability(
            "messages.queues",
            label="消息队列",
            category="operations",
            service_id="stream_admin_service",
            service_available=stream_service is not None,
            entry_route="/queues",
            recovery_label="联系平台管理员检查消息流服务",
            permission=AdminPermission.READ,
        ),
        _service_capability(
            "knowledge.management",
            label="FAQ / 知识库",
            category="knowledge",
            service_id="knowledge_backend",
            service_available=knowledge_available,
            entry_route="/knowledge",
            recovery_label="联系平台管理员检查知识服务",
            permission=AdminPermission.WRITE,
        ),
        _service_capability(
            "operations.dlq",
            label="失败消息恢复",
            category="operations",
            service_id="dlq_admin_service",
            service_available=dlq_service is not None,
            entry_route="/dlq",
            recovery_label="联系平台管理员检查 DLQ 管理服务",
            permission=AdminPermission.DANGER,
        ),
    ]
    for item in capabilities:
        item["tenant_id"] = tenant_id
        if item["id"] == "messages.channels":
            item["connection_state"] = (
                "verified"
                if connection_evidence.verified
                else "configured"
                if connection_evidence.configured
                else "unverified"
            )
            item["required_for_core"] = False
    return capabilities


async def _tenant_plugin_scope_states(
    tenant_id: str,
    plugin_manager: Any | None,
) -> tuple[dict[str, bool], str]:
    scope_states = getattr(plugin_manager, "scope_states", None)
    if not callable(scope_states):
        return {}, ""
    try:
        payload = await scope_states(
            tenant_id=tenant_id,
            session_id=None,
            plugin_name="",
        )
    except Exception as exc:
        return {}, (str(exc).strip() or type(exc).__name__)[:300]
    overrides: dict[str, bool] = {}
    for item in payload.get("items", []) if isinstance(payload, Mapping) else []:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("session_id") or "").strip():
            continue
        name = str(item.get("plugin_name") or item.get("name") or "").strip()
        if name:
            overrides[name] = bool(item.get("enabled"))
    return overrides, ""


def _plugin_secret_missing(name: str, settings: Settings) -> bool:
    if name == "tibo_reset":
        return not str(getattr(settings, "tibo_reset_api_url", "") or "").strip()
    return False


def _plugin_capabilities(
    *,
    tenant_id: str,
    settings: Settings,
    plugin_registry: Any | None,
    scope_states: Mapping[str, bool],
    scope_error: str,
    connection_evidence: _MessageConnectionEvidence,
) -> list[dict[str, Any]]:
    loaded = _loaded_plugin_metadata(plugin_registry)
    active_names = _active_plugin_names(plugin_registry)
    failures = dict(getattr(plugin_registry, "initialization_failures", {}) or {})
    names = sorted(set(loaded).union(_KNOWN_PLUGIN_ROUTES))
    result: list[dict[str, Any]] = []
    for name in names:
        metadata = loaded.get(name)
        globally_active = name in active_names
        tenant_enabled = scope_states.get(name, True)
        dependencies = _plugin_dependency_payloads(
            name=name,
            metadata=metadata,
            loaded=loaded,
            active_names=active_names,
            scope_states=scope_states,
        )
        if scope_error:
            dependencies.append(
                _dependency(
                    "tenant_plugin_scope",
                    required=False,
                    state="degraded",
                    reason=f"tenant_scope_lookup_failed: {scope_error}",
                )
            )
        required_blocked = any(
            item["required"] and item["state"] == "blocked" for item in dependencies
        )
        failure = str(failures.get(name) or "").strip()
        secret_missing = _plugin_secret_missing(name, settings)
        enabled = bool(metadata) and globally_active and tenant_enabled
        available = enabled and not failure and not required_blocked
        if metadata is None:
            health: CapabilityHealth = "blocked"
            status_reason = "plugin_not_loaded"
        elif failure:
            health = "blocked"
            status_reason = f"plugin_initialization_failed: {failure[:300]}"
        elif required_blocked:
            health = "blocked"
            status_reason = "required_plugin_dependency_unavailable"
        elif not globally_active:
            health = "action_required"
            status_reason = "plugin_not_active"
        elif secret_missing:
            health = "action_required"
            status_reason = "plugin_not_configured"
        elif not tenant_enabled:
            health = "action_required"
            status_reason = "plugin_disabled_for_tenant"
        elif scope_error:
            health = "degraded"
            status_reason = "tenant_scope_state_unavailable"
        else:
            health = "ready"
            status_reason = "plugin_active_for_tenant"

        adapter_connection_state = connection_evidence.adapter_state(_WECHAT_ADAPTER_ID)
        if (
            name == "wxbot"
            and available
            and health == "ready"
            and adapter_connection_state != "verified"
        ):
            # Loaded code means the adapter is supported; it says nothing
            # about SDK transport, authorization or bot identity.  Connection
            # health is owned by the channel-connections resource.
            health = "action_required"
            status_reason = (
                "adapter_connection_configured_unverified"
                if adapter_connection_state == "configured"
                else "adapter_available_connection_unverified"
            )
        elif name == "wxbot" and available and health == "ready":
            status_reason = "adapter_connection_verified"

        actions = _plugin_recovery_actions(
            name=name,
            metadata=metadata,
            globally_active=globally_active,
            tenant_enabled=tenant_enabled,
            failure=failure,
            secret_missing=secret_missing,
            dependencies=dependencies,
            scope_error=scope_error,
        )
        if name == "wxbot" and metadata is not None and globally_active:
            actions.extend(
                [
                    _action(
                        "configure",
                        "管理微信 SDK 连接",
                        f"/channels?adapter={_WECHAT_ADAPTER_ID}",
                        requires_admin=True,
                    ),
                    _action(
                        "configure",
                        "打开微信高级设置",
                        "/wxbot",
                        requires_admin=True,
                    ),
                ]
            )
            actions = _dedupe_actions(actions)
        permissions = metadata.get("permissions", []) if metadata else []
        result.append(
            _capability(
                f"plugin.{name}",
                label=_PLUGIN_LABELS.get(name, str(metadata.get("description") or name) if metadata else name),
                category="plugin",
                enabled=enabled,
                available=available,
                health=health,
                status_reason=status_reason,
                dependencies=dependencies,
                recovery_actions=actions,
                source="plugin_registry",
                plugin=name,
                permissions=permissions,
                entry_route=_KNOWN_PLUGIN_ROUTES.get(name, "/plugins"),
            )
        )
        result[-1]["tenant_id"] = tenant_id
        if metadata:
            result[-1]["version"] = metadata.get("version", "")
            result[-1]["description"] = metadata.get("description", "")
        if name == "wxbot":
            result[-1].update(
                {
                    "adapter_id": _WECHAT_ADAPTER_ID,
                    "connection_state": adapter_connection_state,
                    "required_for_core": False,
                    "extension_route": "/wxbot",
                }
            )
    return result


def _loaded_plugin_metadata(plugin_registry: Any | None) -> dict[str, dict[str, Any]]:
    if plugin_registry is None:
        return {}
    permissions_by_name: dict[str, set[str]] = {}
    collect_permissions = getattr(plugin_registry, "all_permissions", None)
    if callable(collect_permissions):
        try:
            permissions_by_name = collect_permissions()
        except Exception:
            permissions_by_name = {}
    result: dict[str, dict[str, Any]] = {}
    loaded_plugins = getattr(plugin_registry, "loaded_plugins", {}) or {}
    for name, plugin in loaded_plugins.items():
        meta = getattr(plugin, "meta", None)
        result[str(name)] = {
            "version": str(getattr(meta, "version", "") or ""),
            "description": str(getattr(meta, "description", "") or ""),
            "dependencies": list(getattr(meta, "dependencies", []) or []),
            "permissions": sorted(permissions_by_name.get(str(name), set())),
        }
    for item in getattr(plugin_registry, "summary", []) or []:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        result.setdefault(
            name,
            {
                "version": str(item.get("version") or ""),
                "description": str(item.get("description") or ""),
                "dependencies": [],
                "permissions": sorted(permissions_by_name.get(name, set())),
            },
        )
    return result


def _active_plugin_names(plugin_registry: Any | None) -> set[str]:
    if plugin_registry is None:
        return set()
    return {
        str(item.get("name") or "")
        for item in getattr(plugin_registry, "summary", []) or []
        if isinstance(item, Mapping) and str(item.get("name") or "").strip()
    }


def _plugin_dependency_payloads(
    *,
    name: str,
    metadata: Mapping[str, Any] | None,
    loaded: Mapping[str, Mapping[str, Any]],
    active_names: set[str],
    scope_states: Mapping[str, bool],
) -> list[dict[str, Any]]:
    if metadata is None:
        return [
            _dependency(
                f"plugin:{name}",
                required=True,
                state="blocked",
                reason="plugin_not_loaded",
            )
        ]
    result: list[dict[str, Any]] = []
    for raw in metadata.get("dependencies", []) or []:
        try:
            spec = parse_plugin_dependency(str(raw), owner=name)
        except PluginDependencyError as exc:
            result.append(
                _dependency(
                    f"plugin:{raw}",
                    required=True,
                    state="blocked",
                    reason=str(exc),
                )
            )
            continue
        dependency = loaded.get(spec.name)
        state: CapabilityHealth = "ready"
        reason = "dependency_active"
        if dependency is None:
            state = "blocked"
            reason = "dependency_not_loaded"
        elif spec.name not in active_names:
            state = "blocked"
            reason = "dependency_not_active"
        elif not scope_states.get(spec.name, True):
            state = "blocked"
            reason = "dependency_disabled_for_tenant"
        elif spec.minimum_version is not None:
            try:
                actual = Version(str(dependency.get("version") or ""))
            except InvalidVersion:
                state = "blocked"
                reason = "dependency_version_invalid"
            else:
                if actual < spec.minimum_version:
                    state = "blocked"
                    reason = f"dependency_version_too_old:{actual}<{spec.minimum_version}"
        result.append(
            _dependency(
                f"plugin:{spec.label}",
                required=True,
                state=state,
                reason=reason,
            )
        )
    return result


def _plugin_recovery_actions(
    *,
    name: str,
    metadata: Mapping[str, Any] | None,
    globally_active: bool,
    tenant_enabled: bool,
    failure: str,
    secret_missing: bool,
    dependencies: list[dict[str, Any]],
    scope_error: str,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if metadata is None:
        actions.append(
            _action(
                "install",
                f"安装 {_PLUGIN_LABELS.get(name, name)}",
                f"/plugins/marketplace?plugin={name}",
                requires_admin=True,
            )
        )
    elif secret_missing and globally_active:
        actions.append(
            _action(
                "configure",
                f"配置 {_PLUGIN_LABELS.get(name, name)} 接口地址",
                f"/plugins?plugin={name}",
                requires_admin=True,
            )
        )
    elif failure or not globally_active or not tenant_enabled:
        actions.append(
            _action(
                "configure",
                f"检查 {_PLUGIN_LABELS.get(name, name)} 状态",
                f"/plugins?plugin={name}",
                requires_admin=True,
            )
        )
    for item in dependencies:
        if not item["required"] or item["state"] != "blocked":
            continue
        dependency_name = str(item["id"]).removeprefix("plugin:").split(">=", 1)[0]
        if dependency_name == name:
            continue
        actions.append(
            _action(
                "install" if item["reason"] == "dependency_not_loaded" else "configure",
                f"修复依赖 {dependency_name}",
                (
                    f"/plugins/marketplace?plugin={dependency_name}"
                    if item["reason"] == "dependency_not_loaded"
                    else f"/plugins?plugin={dependency_name}"
                ),
                requires_admin=True,
            )
        )
    if scope_error:
        actions.append(
            _action(
                "retry",
                "重试租户插件状态检查",
                "/",
                requires_admin=False,
            )
        )
    return _dedupe_actions(actions)


def _navigation_payload(
    principal: Principal,
    capability_by_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path, capability_id, permission, group_scoped in _NAVIGATION_SPECS:
        capability = capability_by_id.get(capability_id)
        enabled = bool(capability and capability.get("enabled"))
        available = bool(capability and capability.get("available"))
        group_scope_allowed = not principal.requires_explicit_group_scope or group_scoped
        allowed = principal.allows(permission) and group_scope_allowed
        visible = enabled and available and allowed
        reason = (
            "visible"
            if visible
            else "group_scope_denied"
            if not group_scope_allowed
            else "permission_denied"
            if not allowed
            else "capability_disabled"
            if capability and not enabled
            else "capability_unavailable"
        )
        items.append(
            {
                "path": path,
                "capability_id": capability_id,
                "required_permission": permission.value,
                "visible": visible,
                "reason": reason,
            }
        )
    return items


def _onboarding_payload(
    *,
    tenant_id: str,
    plugin_registry: Any | None,
    stream_service: Any | None,
    orchestrator: Any | None,
    capability_by_id: Mapping[str, Mapping[str, Any]],
    connection_evidence: _MessageConnectionEvidence,
) -> dict[str, Any]:
    endpoint = f"/v1/admin/tenants/{tenant_id}/capabilities"
    dependencies = [
        _dependency(
            "plugin_registry",
            required=True,
            state="ready" if plugin_registry is not None else "blocked",
            reason="plugin_registry_available" if plugin_registry is not None else "plugin_registry_missing",
        ),
        _dependency(
            "message_orchestrator",
            required=True,
            state="ready" if orchestrator is not None else "blocked",
            reason="orchestrator_available" if orchestrator is not None else "orchestrator_missing",
        ),
        _dependency(
            "stream_observability",
            required=False,
            state="ready" if stream_service is not None else "degraded",
            reason="stream_admin_available" if stream_service is not None else "stream_admin_missing",
        ),
    ]
    dependency_state = _dependency_health(dependencies)
    dependency_actions = (
        []
        if dependency_state == "ready"
        else [
            _action(
                "retry",
                "重新检查系统依赖",
                endpoint,
                requires_admin=False,
            ),
            _action(
                "contact_admin",
                "联系平台管理员检查服务装配",
                "/plugins",
                requires_admin=True,
            ),
        ]
    )
    llm = capability_by_id["runtime.llm"]
    channels = capability_by_id["messages.channels"]
    llm_blocked = llm["health"] in {"blocked", "action_required"}
    channels_blocked = not bool(channels.get("available"))
    connection_configured = connection_evidence.configured
    connection_verified = connection_evidence.verified
    message_flow_observed = connection_evidence.bidirectional
    connection_reason = (
        "bidirectional_message_flow_observed"
        if message_flow_observed
        else "connection_probe_succeeded"
        if connection_verified
        else "connection_status_requires_probe"
        if connection_configured
        else "connection_not_configured"
    )
    connection_dependency = _dependency(
        "message_platform_connection",
        required=True,
        state=(
            "blocked"
            if channels_blocked
            else "ready"
            if connection_verified
            else "action_required"
        ),
        reason=(
            "connection_control_surface_unavailable"
            if channels_blocked
            else connection_reason
        ),
    )
    configured_dependency = _dependency(
        "configured_message_platform_connection",
        required=True,
        state=(
            "blocked"
            if channels_blocked
            else "ready"
            if connection_configured
            else "action_required"
        ),
        reason=(
            "connection_control_surface_unavailable"
            if channels_blocked
            else "connection_configured"
            if connection_configured
            else "connection_not_configured"
        ),
    )
    participation_dependency = _dependency(
        "observed_participation_policy",
        required=True,
        state=(
            "ready"
            if message_flow_observed
            else "action_required"
            if not channels_blocked
            else "blocked"
        ),
        reason=(
            "participation_observed_in_message_flow"
            if message_flow_observed
            else "participation_policy_requires_review"
            if connection_verified
            else "verified_connection_required"
        ),
    )
    launch_blocked = bool(
        dependency_state == "blocked"
        or llm_blocked
        or not connection_evidence.required_connections_ready
    )
    steps = [
        _onboarding_step(
            "dependencies",
            label="确认系统依赖",
            description="确认插件注册、消息编排与观测服务已经装配。",
            state=dependency_state,
            dependencies=dependencies,
            recovery_actions=dependency_actions,
        ),
        _onboarding_step(
            "llm",
            label="配置 LLM",
            description="选择 Provider、模型与密钥，消除模型配置校验错误。",
            state=str(llm["health"]),
            dependencies=list(llm["dependencies"]),
            recovery_actions=(
                list(llm["recovery_actions"])
                or [_action("configure", "打开 LLM 配置", "/llm", requires_admin=True)]
            ),
        ),
        _onboarding_step(
            "message_channel",
            label="添加消息平台连接",
            description="选择消息平台适配器并创建租户连接；安装适配器不代表连接已认证。",
            state=(
                "blocked"
                if channels_blocked
                else "ready"
                if connection_configured
                else "action_required"
            ),
            dependencies=[*list(channels.get("dependencies", [])), configured_dependency],
            recovery_actions=(
                []
                if connection_configured
                else [
                    _action(
                        "configure",
                        "打开消息平台连接",
                        "/channels?onboarding=connect",
                        requires_admin=True,
                    )
                ]
            ),
        ),
        _onboarding_step(
            "connection_probe",
            label="验证连接",
            description="探测传输、认证与账号身份；已观察到同一连接正常收发消息时自动视为通过。",
            state=(
                "blocked"
                if channels_blocked
                else "ready"
                if connection_verified
                else "action_required"
            ),
            dependencies=[connection_dependency],
            recovery_actions=(
                []
                if connection_verified
                else [
                    _action(
                        "configure",
                        "探测消息平台连接",
                        "/channels?onboarding=probe",
                        requires_admin=True,
                    )
                ]
            ),
        ),
        _onboarding_step(
            "participation_policy",
            label="设置参与策略",
            description="为已验证连接选择参与范围、回复策略与人工接管边界。",
            state=(
                "ready"
                if message_flow_observed
                else "action_required"
                if not channels_blocked
                else "blocked"
            ),
            dependencies=[connection_dependency, participation_dependency],
            recovery_actions=(
                []
                if message_flow_observed
                else [
                    _action(
                        "configure",
                        "设置参与策略",
                        "/channels?onboarding=participation",
                        requires_admin=True,
                    )
                ]
            ),
        ),
        _onboarding_step(
            "test",
            label="发送测试消息",
            description="可选复测；已有正常收发记录时无需重复执行，也不影响上线。",
            state=(
                "ready"
                if message_flow_observed
                else "blocked"
                if channels_blocked or llm_blocked or not connection_verified
                else "action_required"
            ),
            dependencies=[
                _dependency(
                    "llm_configuration",
                    required=True,
                    state="blocked" if llm_blocked else "ready",
                    reason="llm_not_ready" if llm_blocked else "llm_ready",
                ),
                {**connection_dependency, "required": False},
            ],
            recovery_actions=(
                []
                if message_flow_observed
                else [
                    _action(
                        "configure",
                        "进入连接测试",
                        "/channels?onboarding=test",
                        requires_admin=True,
                    )
                ]
            ),
            optional=True,
        ),
        _onboarding_step(
            "launch",
            label="确认上线",
            description="核心平台可独立上线；只有标记为上线必需的消息连接会阻塞上线。",
            state="blocked" if launch_blocked else "ready",
            dependencies=[
                _dependency(
                    "launch_prerequisites",
                    required=True,
                    state=(
                        "blocked" if launch_blocked else "ready"
                    ),
                    reason=(
                        "launch_prerequisites_blocked"
                        if launch_blocked
                        else "launch_prerequisites_ready"
                    ),
                )
            ],
            recovery_actions=(
                [
                    _action(
                        "configure",
                        "复核平台与连接",
                        "/channels?onboarding=launch",
                        requires_admin=True,
                    )
                ]
                if launch_blocked
                else []
            ),
        ),
    ]
    required_steps = [step for step in steps if not step.get("optional")]
    state: CapabilityHealth = (
        "blocked"
        if any(step["state"] == "blocked" for step in required_steps)
        else "degraded"
        if any(step["state"] == "degraded" for step in required_steps)
        else "action_required"
        if any(step["state"] == "action_required" for step in required_steps)
        else "ready"
    )
    return {
        "state": state,
        "steps": steps,
    }


def _dependency_health(dependencies: list[dict[str, Any]]) -> CapabilityHealth:
    if any(item["required"] and item["state"] == "blocked" for item in dependencies):
        return "blocked"
    if any(not item["required"] and item["state"] != "ready" for item in dependencies):
        return "degraded"
    if any(item["state"] == "action_required" for item in dependencies):
        return "action_required"
    return "ready"


def _onboarding_step(
    step_id: str,
    *,
    label: str,
    description: str,
    state: str,
    dependencies: list[dict[str, Any]],
    recovery_actions: list[dict[str, Any]],
    optional: bool = False,
) -> dict[str, Any]:
    return {
        "id": step_id,
        "label": label,
        "description": description,
        "state": state,
        "dependencies": dependencies,
        "recovery_actions": _dedupe_actions(recovery_actions),
        "optional": optional,
    }


def _dedupe_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for action in actions:
        key = (str(action.get("type") or ""), str(action.get("target") or ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(action)
    return result
