"""Agent-tool, event-subscription, and member-setting admin routes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from app.admin.audit import set_admin_audit_context
from app.agent.scopes import DEFAULT_AGENT_SCOPE, normalize_agent_scope
from plugins.wxbot.router import (
    WxbotAgentSessionPolicyRequest,
    WxbotEventSubscriptionRequest,
    WxbotGroupMemberSettingsRequest,
    _AdminEffectOutcome,
    _agent_policy_config,
    _agent_tool_catalog,
    _client_safe_media_payload,
    _event_subscription_config,
    _execute_admin_mutation,
    _group_member_settings_config,
    _mutation_audit_summary,
    _observe_admin_resource,
    _request_trace_id,
    _require_admin,
    _require_default_tenant_admin,
    _require_session_admin,
    _require_verified_group,
    _require_wxbot_scope_execution,
    _required_idempotency_key,
    _required_version_if_match,
    _sdk_request,
    _set_no_store_etag,
    _version_etag,
)


def register_agent_event_routes(
    router: APIRouter,
    *,
    store: Any,
    bridge: Any,
    container: Any,
    agent_store: Any,
    scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None = None,
) -> None:
    @router.get("/admin/agent-tools/catalog")
    async def get_agent_tool_catalog(request: Request, scope: str = DEFAULT_AGENT_SCOPE):
        _require_admin(store, request)
        normalized_scope = normalize_agent_scope(scope)
        items = _agent_tool_catalog(container, normalized_scope)
        registry = getattr(container, "agent_tool_registry", None) or getattr(
            container, "_agent_tool_registry", None
        )
        scopes = registry.scopes() if registry is not None else ([scope] if items else [])
        return {
            "scope": normalized_scope,
            "scopes": scopes,
            "items": items,
            "count": len(items),
        }

    @router.get("/admin/agent-tools/policy/{tenant_id}/{session_id:path}")
    async def get_agent_tool_policy(
        tenant_id: str,
        session_id: str,
        request: Request,
        response: Response,
        scope: str = DEFAULT_AGENT_SCOPE,
    ):
        tenant, session, _principal = _require_session_admin(
            store,
            request,
            tenant_id,
            session_id,
        )
        normalized_scope = normalize_agent_scope(scope)
        catalog_items = _agent_tool_catalog(container, normalized_scope)
        available_tools = [item["name"] for item in catalog_items]
        if agent_store is None:
            policy = {
                "tenant_id": tenant,
                "session_id": session,
                "enabled": False,
                "policy_configured": False,
                "allowed_tools": [],
                "available_tools": available_tools,
                "effective_tools": [],
                "inherits_default_tools": False,
                "denial_reason": "policy_store_unavailable",
                "scope": normalized_scope,
                "unsupported": True,
            }
        else:
            policy = await agent_store.get_session_policy(
                tenant,
                session,
                scope=normalized_scope,
                available_tools=available_tools,
            )
        policy["scope"] = normalized_scope
        version = await _observe_admin_resource(
            store,
            tenant,
            f"agent-tools-policy:{normalized_scope}:{session}",
            resource_kind="agent_tools_policy",
            state_payload=_agent_policy_config(policy),
        )
        policy["version"] = version
        _set_no_store_etag(response, _version_etag(version))
        return policy

    @router.post("/admin/agent-tools/policy/{tenant_id}/{session_id:path}")
    async def set_agent_tool_policy(
        tenant_id: str,
        session_id: str,
        body: WxbotAgentSessionPolicyRequest,
        request: Request,
        response: Response,
        scope: str = DEFAULT_AGENT_SCOPE,
    ):
        tenant, session, _principal = _require_session_admin(
            store,
            request,
            tenant_id,
            session_id,
        )
        if agent_store is None:
            raise HTTPException(503, "agent store unavailable")
        if body.enabled is None and body.allowed_tools is None:
            raise HTTPException(400, "no_mutable_fields")
        expected_version = _required_version_if_match(request)
        normalized_scope = normalize_agent_scope(scope)
        available_tools = [
            item["name"] for item in _agent_tool_catalog(container, normalized_scope)
        ]
        before = await agent_store.get_session_policy(
            tenant,
            session,
            scope=normalized_scope,
            available_tools=available_tools,
        )
        before["scope"] = normalized_scope
        before_config = _agent_policy_config(before)
        await _observe_admin_resource(
            store,
            tenant,
            f"agent-tools-policy:{normalized_scope}:{session}",
            resource_kind="agent_tools_policy",
            state_payload=before_config,
        )
        desired_config = {
            "enabled": before_config["enabled"] if body.enabled is None else bool(body.enabled),
            "allowed_tools": (
                before_config["allowed_tools"]
                if body.allowed_tools is None
                else sorted({str(item) for item in body.allowed_tools if str(item).strip()})
            ),
            "scope": normalized_scope,
        }
        recovery_response = {
            "tenant_id": tenant,
            "session_id": session,
            **desired_config,
            "available_tools": available_tools,
        }

        async def effect() -> _AdminEffectOutcome:
            policy = await agent_store.set_session_policy(
                tenant,
                session,
                scope=normalized_scope,
                enabled=body.enabled,
                allowed_tools=body.allowed_tools,
                available_tools=available_tools,
            )
            policy["scope"] = normalized_scope
            return _AdminEffectOutcome(policy, _agent_policy_config(policy))

        result, version, _replayed = await _execute_admin_mutation(
            store,
            request,
            tenant_id=tenant,
            operation="agent_tools_policy_update",
            resource_key=f"agent-tools-policy:{normalized_scope}:{session}",
            request_payload=desired_config,
            expected_version=expected_version,
            desired_state=desired_config,
            recovery_response=recovery_response,
            effect=effect,
        )
        if isinstance(result, dict) and version is not None:
            result["version"] = version
        if version is not None:
            _set_no_store_etag(response, _version_etag(version))
        set_admin_audit_context(
            request,
            target_type="wxbot_agent_tools_policy",
            tenant_id=tenant,
            session_id=session,
            before_state={
                "enabled": bool(before_config["enabled"]),
                "allowed_tool_count": len(before_config["allowed_tools"]),
            },
            after_state={
                "enabled": bool(desired_config["enabled"]),
                "allowed_tool_count": len(desired_config["allowed_tools"]),
            },
            policy_version=int(version or 0),
            trace_id=_request_trace_id(request),
            reason="conditional_agent_tool_policy_update",
        )
        return result

    @router.get("/admin/agent-tools/audit")
    async def list_agent_tool_audit(
        tenant_id: str,
        request: Request,
        session_id: str = "",
        scope: str = "",
        tool_name: str = "",
        trace_id: str = "",
        limit: int = 50,
    ):
        _require_admin(store, request)
        if agent_store is None:
            return {"items": [], "count": 0, "unsupported": True}
        items = await agent_store.list_tool_audits(
            tenant_id,
            session_id=session_id,
            scope=normalize_agent_scope(scope) if scope else "",
            tool_name=tool_name,
            trace_id=trace_id,
            limit=limit,
        )
        return {"items": items, "count": len(items)}

    @router.get("/admin/event-subscriptions")
    async def list_event_subscriptions(
        request: Request,
        response: Response,
        event_type: str = "",
        session_id: str = "",
    ):
        tenant_id = _require_default_tenant_admin(store, request)
        params: dict[str, Any] = {}
        if event_type.strip():
            params["event_type"] = event_type.strip()
        if session_id.strip():
            params["session_id"] = session_id.strip()
        payload = await _sdk_request(
            store,
            bridge,
            "GET",
            "/event-subscriptions",
            params=params or None,
        )
        items = payload.get("items")
        if items is None:
            items = payload.get("subscriptions", [])
        resource_payload = (
            await _sdk_request(store, bridge, "GET", "/event-subscriptions")
            if params
            else payload
        )
        resource_items = resource_payload.get(
            "items",
            resource_payload.get("subscriptions", []),
        )
        version = await _observe_admin_resource(
            store,
            tenant_id,
            "event-subscriptions",
            resource_kind="event_subscriptions",
            state_payload=_event_subscription_config(resource_items),
        )
        _set_no_store_etag(response, _version_etag(version))
        return {"items": items, "count": payload.get("count", len(items)), "version": version}

    @router.post("/admin/event-subscriptions")
    async def upsert_event_subscription(
        body: WxbotEventSubscriptionRequest,
        request: Request,
        response: Response,
    ):
        tenant_id = _require_default_tenant_admin(store, request)
        expected_version = _required_version_if_match(request)
        if body.session_id.strip():
            await _require_verified_group(
                store,
                bridge,
                tenant_id=tenant_id,
                session_id=body.session_id,
            )
        before_payload = await _sdk_request(store, bridge, "GET", "/event-subscriptions")
        before_items = before_payload.get("items", before_payload.get("subscriptions", []))
        before_config = _event_subscription_config(before_items)
        await _observe_admin_resource(
            store,
            tenant_id,
            "event-subscriptions",
            resource_kind="event_subscriptions",
            state_payload=before_config,
        )
        intent = body.model_dump()

        async def effect() -> _AdminEffectOutcome:
            await _require_wxbot_scope_execution(
                scope_execution_allowed,
                tenant_id=tenant_id,
                session_id=body.session_id,
            )
            sdk_result = await _sdk_request(
                store,
                bridge,
                "POST",
                "/event-subscriptions",
                json_body=intent,
                request_headers={"Idempotency-Key": _required_idempotency_key(request)},
            )
            after_payload = await _sdk_request(store, bridge, "GET", "/event-subscriptions")
            after_items = after_payload.get("items", after_payload.get("subscriptions", []))
            return _AdminEffectOutcome(sdk_result, _event_subscription_config(after_items))

        result, version, _replayed = await _execute_admin_mutation(
            store,
            request,
            tenant_id=tenant_id,
            operation="event_subscription_upsert",
            resource_key="event-subscriptions",
            request_payload=intent,
            expected_version=expected_version,
            recovery_response={"subscription": intent, "saved": True},
            effect=effect,
        )
        if version is not None:
            _set_no_store_etag(response, _version_etag(version))
        set_admin_audit_context(
            request,
            target_type="wxbot_event_subscription",
            tenant_id=tenant_id,
            session_id=body.session_id,
            before_state=_mutation_audit_summary(
                operation="upsert",
                affected_count=len(before_config),
            ),
            after_state=_mutation_audit_summary(
                operation="upsert",
                affected_count=len(before_config) + 1,
                enabled=body.enabled,
            ),
            policy_version=int(version or 0),
            trace_id=_request_trace_id(request),
            reason="conditional_event_subscription_update",
        )
        return result

    @router.delete("/admin/event-subscriptions/{subscription_id}")
    async def delete_event_subscription(
        subscription_id: int,
        request: Request,
        response: Response,
    ):
        tenant_id = _require_default_tenant_admin(store, request)
        expected_version = _required_version_if_match(request)
        before_payload = await _sdk_request(store, bridge, "GET", "/event-subscriptions")
        before_items = before_payload.get("items", before_payload.get("subscriptions", []))
        before_config = _event_subscription_config(before_items)
        await _observe_admin_resource(
            store,
            tenant_id,
            "event-subscriptions",
            resource_kind="event_subscriptions",
            state_payload=before_config,
        )
        selected = next(
            (
                item
                for item in before_items
                if isinstance(item, dict) and int(item.get("id") or 0) == subscription_id
            ),
            None,
        )
        if selected is None:
            raise HTTPException(404, "event_subscription_not_found")
        selected_session = str((selected or {}).get("session_id") or "")
        await _require_wxbot_scope_execution(
            scope_execution_allowed,
            tenant_id=tenant_id,
            session_id=selected_session,
        )
        if selected_session:
            await _require_verified_group(
                store,
                bridge,
                tenant_id=tenant_id,
                session_id=selected_session,
            )

        async def effect() -> _AdminEffectOutcome:
            await _require_wxbot_scope_execution(
                scope_execution_allowed,
                tenant_id=tenant_id,
                session_id=selected_session,
            )
            sdk_result = await _sdk_request(
                store,
                bridge,
                "DELETE",
                f"/event-subscriptions/{subscription_id}",
                request_headers={"Idempotency-Key": _required_idempotency_key(request)},
            )
            after_payload = await _sdk_request(store, bridge, "GET", "/event-subscriptions")
            after_items = after_payload.get("items", after_payload.get("subscriptions", []))
            return _AdminEffectOutcome(sdk_result, _event_subscription_config(after_items))

        result, version, _replayed = await _execute_admin_mutation(
            store,
            request,
            tenant_id=tenant_id,
            operation="event_subscription_delete",
            resource_key="event-subscriptions",
            request_payload={"subscription_id": int(subscription_id)},
            expected_version=expected_version,
            recovery_response={"deleted": True, "id": subscription_id},
            effect=effect,
        )
        if version is not None:
            _set_no_store_etag(response, _version_etag(version))
        set_admin_audit_context(
            request,
            target_type="wxbot_event_subscription",
            tenant_id=tenant_id,
            session_id=selected_session,
            before_state=_mutation_audit_summary(
                operation="delete",
                affected_count=len(before_config),
            ),
            after_state=_mutation_audit_summary(
                operation="delete",
                affected_count=max(0, len(before_config) - 1),
            ),
            policy_version=int(version or 0),
            trace_id=_request_trace_id(request),
            reason="conditional_event_subscription_delete",
        )
        return result

    @router.get("/admin/group-members/settings/{session_id:path}")
    async def get_group_member_settings(
        session_id: str,
        request: Request,
        response: Response,
    ):
        tenant_id = _require_default_tenant_admin(store, request)
        await _require_verified_group(
            store,
            bridge,
            tenant_id=tenant_id,
            session_id=session_id,
        )
        payload = await _sdk_request(
            store,
            bridge,
            "GET",
            f"/group-members/settings/{session_id}",
        )
        safe_payload = _client_safe_media_payload(payload, store, tenant_id=tenant_id)
        version = await _observe_admin_resource(
            store,
            tenant_id,
            f"group-member-settings:{session_id}",
            resource_kind="group_member_settings",
            state_payload=_group_member_settings_config(safe_payload),
        )
        safe_payload["version"] = version
        _set_no_store_etag(response, _version_etag(version))
        return safe_payload

    @router.post("/admin/group-members/settings/{session_id:path}")
    async def set_group_member_settings(
        session_id: str,
        body: WxbotGroupMemberSettingsRequest,
        request: Request,
        response: Response,
    ):
        tenant_id = _require_default_tenant_admin(store, request)
        expected_version = _required_version_if_match(request)
        await _require_verified_group(
            store,
            bridge,
            tenant_id=tenant_id,
            session_id=session_id,
        )
        before = await _sdk_request(
            store,
            bridge,
            "GET",
            f"/group-members/settings/{session_id}",
        )
        before_config = _group_member_settings_config(before)
        await _observe_admin_resource(
            store,
            tenant_id,
            f"group-member-settings:{session_id}",
            resource_kind="group_member_settings",
            state_payload=before_config,
        )
        body_payload = body.model_dump(exclude_none=True)
        desired_public = {
            "session_id": session_id,
            "welcome_enabled": (
                bool(before.get("welcome_enabled"))
                if body.welcome_enabled is None
                else body.welcome_enabled
            ),
            "welcome_template": (
                str(before.get("welcome_template") or "")
                if body.welcome_template is None
                else body.welcome_template
            ),
            "welcome_mention": (
                bool(before.get("welcome_mention"))
                if body.welcome_mention is None
                else body.welcome_mention
            ),
        }
        desired_config = _group_member_settings_config(desired_public)

        async def effect() -> _AdminEffectOutcome:
            await _sdk_request(
                store,
                bridge,
                "POST",
                f"/group-members/settings/{session_id}",
                json_body=body_payload,
                request_headers={"Idempotency-Key": _required_idempotency_key(request)},
            )
            after = await _sdk_request(
                store,
                bridge,
                "GET",
                f"/group-members/settings/{session_id}",
            )
            return _AdminEffectOutcome(after, _group_member_settings_config(after))

        result, version, _replayed = await _execute_admin_mutation(
            store,
            request,
            tenant_id=tenant_id,
            operation="group_member_settings_update",
            resource_key=f"group-member-settings:{session_id}",
            request_payload=body_payload,
            expected_version=expected_version,
            desired_state=desired_config,
            recovery_response=desired_public,
            effect=effect,
        )
        if isinstance(result, dict) and version is not None:
            result["version"] = version
        if version is not None:
            _set_no_store_etag(response, _version_etag(version))
        set_admin_audit_context(
            request,
            target_type="wxbot_group_member_settings",
            tenant_id=tenant_id,
            session_id=session_id,
            before_state={
                "welcome_enabled": bool(before_config["welcome_enabled"]),
                "welcome_mention": bool(before_config["welcome_mention"]),
            },
            after_state={
                "welcome_enabled": bool(desired_config["welcome_enabled"]),
                "welcome_mention": bool(desired_config["welcome_mention"]),
            },
            policy_version=int(version or 0),
            trace_id=_request_trace_id(request),
            reason="conditional_group_member_settings_update",
        )
        return result
