from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, Response, status

from app.admin.audit import set_admin_audit_context
from app.commands import CommandRegistryService
from app.common.request_models import StrictRequestModel
from plugins.commands.store import CommandConfigVersionConflictError, CommandStore


class CommandConfigUpdate(StrictRequestModel):
    admin_user_ids_text: str | None = None
    user_commands_text: str | None = None
    admin_commands_text: str | None = None


def build_commands_router(store: CommandStore, service: CommandRegistryService) -> APIRouter:
    router = APIRouter()

    @router.get("/catalog")
    async def get_catalog():
        items = service.catalog()
        return {
            "items": items,
            "count": len(items),
        }

    @router.get("/config/{tenant_id}")
    async def get_config(tenant_id: str, response: Response):
        config = await store.get_config(tenant_id, catalog=service.catalog())
        _set_version_headers(response, int(config["version"]))
        return config

    @router.post("/config/{tenant_id}")
    async def set_config(
        tenant_id: str,
        body: CommandConfigUpdate,
        request: Request,
        response: Response,
        if_match: str | None = Header(default=None, alias="If-Match"),
    ):
        updates = {k: v for k, v in body.model_dump().items() if v is not None}
        if not updates:
            raise HTTPException(status_code=400, detail="no_mutable_fields")
        expected_version = _required_if_match(if_match)
        try:
            mutation = await store.set_config(
                tenant_id,
                expected_version=expected_version,
                catalog=service.catalog(),
                **updates,
            )
        except CommandConfigVersionConflictError as exc:
            raise _version_conflict(exc.expected, exc.current) from exc
        after = mutation.after
        _set_version_headers(response, int(after["version"]))
        set_admin_audit_context(
            request,
            target_type="plugin_command_center_config",
            tenant_id=tenant_id,
            before_state=_audit_summary(mutation.before),
            after_state=_audit_summary(after),
            policy_version=int(after["version"]),
            trace_id=_trace_id(request),
            reason="conditional_config_update",
        )
        return after

    return router


def _required_if_match(value: str | None) -> int:
    if value is None or not value.strip():
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="if_match_required",
        )
    normalized = value.strip()
    if normalized.startswith("W/"):
        normalized = normalized[2:].strip()
    if len(normalized) >= 2 and normalized[0] == '"' and normalized[-1] == '"':
        normalized = normalized[1:-1]
    if not normalized.isdigit():
        raise HTTPException(status_code=400, detail="invalid_if_match")
    return int(normalized)


def _etag(version: int) -> str:
    return f'"{max(0, int(version))}"'


def _set_version_headers(response: Response, version: int) -> None:
    response.headers["ETag"] = _etag(version)
    response.headers["Cache-Control"] = "no-store"


def _version_conflict(expected: int, current: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "version_conflict",
            "expected_version": expected,
            "current_version": current,
        },
        headers={"ETag": _etag(current), "Cache-Control": "no-store"},
    )


def _audit_summary(config: dict[str, Any]) -> dict[str, object]:
    return {
        "version": int(config.get("version") or 0),
        "admin_user_count": len(config.get("admin_user_ids") or []),
        "user_command_count": len(config.get("user_commands") or []),
        "admin_command_count": len(config.get("admin_commands") or []),
    }


def _trace_id(request: Request) -> str:
    return str(
        request.headers.get("X-Trace-ID")
        or request.headers.get("X-Request-ID")
        or getattr(request.state, "admin_request_id", "")
    ).strip()[:128]
