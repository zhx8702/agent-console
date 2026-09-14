"""Admin CRUD for per-group inbound webhooks."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field

from app.admin.audit import set_admin_audit_context
from app.channel.identity import LEGACY_WXBOT_CONNECTION_ID
from app.common.config import get_settings
from app.common.request_models import StrictRequestModel
from plugins.wxbot.group_webhook import (
    create_group_webhook,
    delete_group_webhook,
    list_group_webhooks,
    public_webhook_document,
    revoke_group_webhook,
    rotate_group_webhook,
    webhook_public_path,
    webhook_public_url,
)
from plugins.wxbot.router import (
    _mutation_audit_summary,
    _request_trace_id,
    _require_session_admin,
    _require_tenant_admin,
    _require_verified_group,
    _require_wxbot_scope_execution,
)


class WxbotGroupWebhookCreateRequest(StrictRequestModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=256)
    session_name: str = Field(default="", max_length=256)
    connection_id: str = Field(default="", max_length=64)


def _request_public_origin(request: Request) -> str:
    configured = str(getattr(get_settings(), "wxbot_group_webhook_public_origin", "") or "").strip()
    if configured:
        return configured.rstrip("/")
    forwarded_proto = str(request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    forwarded_host = str(request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    if forwarded_host:
        scheme = forwarded_proto or request.url.scheme or "https"
        return f"{scheme}://{forwarded_host}"
    return str(request.base_url)


def _created_payload(
    request: Request,
    record: Any,
    token: str,
) -> dict[str, Any]:
    document = public_webhook_document(record)
    document["token"] = token
    prefix = str(request.headers.get("x-agent-console-api-prefix") or "/api").strip() or "/api"
    document["path"] = webhook_public_path(token, api_prefix=prefix)
    document["url"] = webhook_public_url(
        _request_public_origin(request),
        token,
        api_prefix=prefix,
    )
    document["example"] = {
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "body": {"text": "hello", "message_id": "optional-unique-id"},
    }
    return document


def register_group_webhook_routes(
    router: APIRouter,
    *,
    store: Any,
    bridge: Any,
    scope_execution_allowed: Any = None,
) -> None:
    @router.get("/admin/group-webhooks")
    async def list_webhooks(tenant_id: str, request: Request) -> dict[str, Any]:
        _require_tenant_admin(store, request, tenant_id)
        items = [
            public_webhook_document(item) for item in await list_group_webhooks(tenant_id)
        ]
        return {"items": items, "count": len(items)}

    @router.post("/admin/group-webhooks")
    async def create_webhook(
        body: WxbotGroupWebhookCreateRequest,
        request: Request,
    ) -> dict[str, Any]:
        tenant, session, principal = _require_session_admin(
            store,
            request,
            body.tenant_id,
            body.session_id,
            group_required=True,
        )
        connection_id = (
            str(body.connection_id or "").strip()
            or str(getattr(store.settings, "channel_connection_id", "") or "").strip()
            or LEGACY_WXBOT_CONNECTION_ID
        )
        roster = await _require_verified_group(
            store,
            bridge,
            tenant_id=tenant,
            session_id=session,
        )
        await _require_wxbot_scope_execution(
            scope_execution_allowed,
            tenant_id=tenant,
            session_id=session,
        )
        try:
            record, token = await create_group_webhook(
                tenant_id=tenant,
                connection_id=connection_id,
                session_id=session,
                session_name=body.session_name
                or str(roster.get("session_name") or ""),
                created_by=str(getattr(principal, "subject", "") or ""),
            )
        except ValueError as exc:
            detail = str(exc)
            status_code = 409 if detail == "group_webhook_exists" else 400
            raise HTTPException(status_code, detail) from exc
        payload = _created_payload(request, record, token)
        set_admin_audit_context(
            request,
            target_type="wxbot_group_webhook",
            tenant_id=tenant,
            session_id=session,
            after_state=_mutation_audit_summary(
                operation="group_webhook_create",
                affected_count=1,
            ),
            trace_id=_request_trace_id(request),
            reason="create_group_webhook",
        )
        return payload

    @router.post("/admin/group-webhooks/{webhook_id}/rotate")
    async def rotate_webhook(webhook_id: str, request: Request) -> dict[str, Any]:
        from plugins.wxbot.group_webhook import get_group_webhook

        record = await get_group_webhook(webhook_id)
        if record is None or not record.active:
            raise HTTPException(404, "group_webhook_not_found")
        _require_session_admin(
            store,
            request,
            record.tenant_id,
            record.external_session_id,
            group_required=True,
        )
        try:
            updated, token = await rotate_group_webhook(webhook_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        payload = _created_payload(request, updated, token)
        set_admin_audit_context(
            request,
            target_type="wxbot_group_webhook",
            tenant_id=updated.tenant_id,
            session_id=updated.external_session_id,
            after_state=_mutation_audit_summary(
                operation="group_webhook_rotate",
                affected_count=1,
            ),
            trace_id=_request_trace_id(request),
            reason="rotate_group_webhook",
        )
        return payload

    @router.delete("/admin/group-webhooks/{webhook_id}")
    async def delete_webhook(webhook_id: str, request: Request) -> dict[str, Any]:
        from plugins.wxbot.group_webhook import get_group_webhook

        record = await get_group_webhook(webhook_id)
        if record is None:
            raise HTTPException(404, "group_webhook_not_found")
        _require_session_admin(
            store,
            request,
            record.tenant_id,
            record.external_session_id,
            group_required=True,
        )
        if record.active:
            try:
                updated = await revoke_group_webhook(webhook_id)
            except ValueError as exc:
                raise HTTPException(404, str(exc)) from exc
            set_admin_audit_context(
                request,
                target_type="wxbot_group_webhook",
                tenant_id=updated.tenant_id,
                session_id=updated.external_session_id,
                after_state=_mutation_audit_summary(
                    operation="group_webhook_revoke",
                    affected_count=1,
                ),
                trace_id=_request_trace_id(request),
                reason="revoke_group_webhook",
            )
            return public_webhook_document(updated)
        try:
            deleted = await delete_group_webhook(webhook_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        set_admin_audit_context(
            request,
            target_type="wxbot_group_webhook",
            tenant_id=deleted.tenant_id,
            session_id=deleted.external_session_id,
            after_state=_mutation_audit_summary(
                operation="group_webhook_delete",
                affected_count=1,
            ),
            trace_id=_request_trace_id(request),
            reason="delete_group_webhook",
        )
        return {"deleted": True, "webhook_id": deleted.webhook_id}
