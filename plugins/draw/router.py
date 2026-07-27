from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from fastapi.responses import FileResponse

from app.admin.auth_router import authenticate_admin_request
from app.admin.mutation_ledger import (
    MutationAudit,
    MutationChange,
    MutationIdempotencyConflictError,
    MutationIdentity,
    MutationOutcome,
    hash_identifier,
)
from app.billing import BillingCoordinator
from app.channel import ChannelRegistry
from plugins.draw.hooks import (
    recover_stale_draw_tasks,
    resend_draw_task_callback,
    retry_draw_task_once,
)
from plugins.draw.store import DrawStore


def build_draw_router(
    store: DrawStore,
    *,
    channel_registry: ChannelRegistry | None = None,
    billing: BillingCoordinator | None = None,
    register_background_task: (
        Callable[[asyncio.Task[None]], Awaitable[None] | None] | None
    ) = None,
    recover_stale_tasks: Callable[..., Awaitable[dict[str, object]]] | None = None,
    scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.get("/images")
    async def list_generated_images(limit: int = 50):
        return {
            "images": [record.as_dict() for record in store.list_images(limit=limit)],
        }

    @router.get("/images/{image_id}")
    async def get_generated_image(image_id: str):
        record = store.resolve_image_id(image_id)
        if record is None:
            raise HTTPException(status_code=404, detail="image not found")
        return record.as_dict()

    @router.get("/tasks")
    async def list_draw_tasks(limit: int = 50, tenant_id: str = "", status: str = ""):
        records = await store.list_draw_tasks(
            limit=limit,
            tenant_id=tenant_id,
            status=status,
        )
        return {
            "tasks": [record.as_dict(include_prompt=False) for record in records],
        }

    @router.post("/tasks/recover-stale")
    async def recover_stale_draw_tasks_endpoint(
        request: Request,
        response: Response,
        stale_seconds: float | None = None,
        limit: int = 50,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        _require_admin(request, store)
        operation_key = _required_idempotency_key(idempotency_key)

        async def mutate() -> MutationChange:
            effective_stale_seconds = stale_seconds
            result: dict[str, object] = {}
            if recover_stale_tasks is not None:
                result.update(
                    await recover_stale_tasks(
                        stale_seconds=effective_stale_seconds,
                        limit=limit,
                    )
                )
            else:
                if channel_registry is None:
                    raise HTTPException(
                        status_code=409,
                        detail="channel registry unavailable",
                    )
                if effective_stale_seconds is None:
                    effective_stale_seconds = float(
                        getattr(
                            store.settings,
                            "draw_task_stale_seconds",
                            3600.0,
                        )
                        or 3600.0
                    )
                result.update(
                    await recover_stale_draw_tasks(
                        store=store,
                        channel_registry=channel_registry,
                        stale_seconds=effective_stale_seconds,
                        limit=limit,
                    )
                )
            return MutationChange(
                response=result,
                before_state={"recovery_requested": True},
                after_state=_recover_audit_state(result),
                resource_version=str(
                    _audit_int(result.get("recovered"))
                    + _audit_int(result.get("callbacks_sent"))
                ),
            )

        try:
            outcome = await store.run_admin_mutation(
                identity=MutationIdentity(
                    tenant_id="__platform__",
                    plugin_name="draw",
                    operation="draw.tasks.recover_stale",
                    resource_key="stale_tasks",
                    idempotency_key=operation_key,
                    request_payload={
                        "stale_seconds": stale_seconds,
                        "limit": limit,
                    },
                ),
                audit=_mutation_audit(
                    request,
                    scope={
                        "limit": limit,
                        "custom_threshold": stale_seconds is not None,
                    },
                    reason_code="manual_draw_stale_recovery",
                ),
                mutate=mutate,
            )
        except MutationIdempotencyConflictError as exc:
            raise _idempotency_conflict() from exc
        _apply_mutation_outcome(response, outcome)
        return outcome.response

    @router.get("/tasks/{task_id}")
    async def get_draw_task(task_id: str):
        record = await store.get_draw_task(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail="task not found")
        return record.as_dict()

    @router.post("/tasks/{task_id}/retry")
    async def retry_draw_task(
        task_id: str,
        request: Request,
        response: Response,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        _require_admin(request, store)
        operation_key = _required_idempotency_key(idempotency_key)
        record = await store.get_draw_task(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail="task not found")
        await _require_task_scope_execution(
            record,
            scope_execution_allowed=scope_execution_allowed,
        )

        async def mutate() -> MutationChange:
            if channel_registry is None:
                raise HTTPException(status_code=409, detail="channel registry unavailable")
            max_retries = _max_retries(store)
            result = await retry_draw_task_once(
                store=store,
                channel_registry=channel_registry,
                billing=billing,
                task=record,
                max_retries=max_retries,
                retry_backoff_seconds=_retry_backoff_seconds(store),
                register_background_task=register_background_task,
            )
            status_code = 200
            response_payload: dict[str, object] = result
            if not result.get("retry_queued"):
                detail = str(
                    result.get("error")
                    or result.get("message")
                    or "retry rejected"
                )
                status_code = 429 if detail == "retry budget exhausted" else 409
                response_payload = {"detail": result}
            return MutationChange(
                response=response_payload,
                before_state=_task_audit_state(record),
                after_state=_retry_audit_state(result),
                resource_version=str(result.get("retry_task_id") or record.updated_at or ""),
                status_code=status_code,
            )

        try:
            outcome = await store.run_admin_mutation(
                identity=MutationIdentity(
                    tenant_id=record.tenant_id,
                    plugin_name="draw",
                    operation="draw.task.retry",
                    resource_key=task_id,
                    idempotency_key=operation_key,
                    request_payload={"task_id": task_id},
                ),
                audit=_mutation_audit(
                    request,
                    scope={"task_hash": hash_identifier(task_id)},
                    reason_code="manual_draw_task_retry",
                ),
                mutate=mutate,
            )
        except MutationIdempotencyConflictError as exc:
            raise _idempotency_conflict() from exc
        _apply_mutation_outcome(response, outcome)
        return outcome.response

    @router.post("/tasks/{task_id}/resend-callback")
    async def resend_draw_callback(
        task_id: str,
        request: Request,
        response: Response,
        force: bool = False,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ):
        _require_admin(request, store)
        operation_key = _required_idempotency_key(idempotency_key)
        record = await store.get_draw_task(task_id)
        if record is None:
            raise HTTPException(status_code=404, detail="task not found")
        await _require_task_scope_execution(
            record,
            scope_execution_allowed=scope_execution_allowed,
        )

        async def mutate() -> MutationChange:
            if channel_registry is None:
                raise HTTPException(status_code=409, detail="channel registry unavailable")
            result = await resend_draw_task_callback(
                store=store,
                channel_registry=channel_registry,
                task=record,
                force=force,
                idempotency_suffix=(
                    f"admin-{hash_identifier(operation_key)[:32]}"
                ),
                scope_execution_allowed=scope_execution_allowed,
            )
            status_code = 409 if result.get("error") else 200
            response_payload: dict[str, object] = (
                {"detail": result} if status_code >= 400 else result
            )
            return MutationChange(
                response=response_payload,
                before_state=_task_audit_state(record),
                after_state=_callback_audit_state(result),
                resource_version=str(record.updated_at or record.task_id),
                status_code=status_code,
            )

        try:
            outcome = await store.run_admin_mutation(
                identity=MutationIdentity(
                    tenant_id=record.tenant_id,
                    plugin_name="draw",
                    operation="draw.task.resend_callback",
                    resource_key=task_id,
                    idempotency_key=operation_key,
                    request_payload={"task_id": task_id, "force": force},
                ),
                audit=_mutation_audit(
                    request,
                    scope={
                        "task_hash": hash_identifier(task_id),
                        "forced": force,
                    },
                    reason_code="manual_draw_callback_resend",
                ),
                mutate=mutate,
            )
        except MutationIdempotencyConflictError as exc:
            raise _idempotency_conflict() from exc
        _apply_mutation_outcome(response, outcome)
        return outcome.response

    @router.get("/files/{file_name:path}")
    async def get_generated_file(file_name: str):
        path = store.resolve_file(file_name)
        if path is None:
            raise HTTPException(status_code=404, detail="file not found")
        return FileResponse(path)

    return router


def _require_admin(request: Request, store: DrawStore) -> None:
    authenticate_admin_request(request, store.settings)


async def _require_task_scope_execution(
    record: object,
    *,
    scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None,
) -> None:
    if scope_execution_allowed is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="draw task scope gate unavailable",
        )
    tenant_id = str(getattr(record, "tenant_id", "") or "")
    session_id = str(
        getattr(record, "session_id", "")
        or getattr(record, "chat_id", "")
        or ""
    )
    try:
        allowed = await scope_execution_allowed(tenant_id, session_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="draw task scope gate unavailable",
        ) from exc
    if allowed is not True:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="draw plugin disabled for task scope",
        )


def _max_retries(store: DrawStore) -> int:
    try:
        return max(0, int(getattr(store.settings, "draw_task_max_retries", 3) or 0))
    except (TypeError, ValueError):
        return 3


def _retry_backoff_seconds(store: DrawStore) -> float:
    try:
        return max(
            0.0,
            float(
                getattr(
                    store.settings,
                    "draw_task_retry_backoff_seconds",
                    0.0,
                )
                or 0.0
            ),
        )
    except (TypeError, ValueError):
        return 0.0


def _required_idempotency_key(value: str | None) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail={"code": "idempotency_key_required"},
        )
    if len(normalized) > 128:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_idempotency_key"},
        )
    return normalized


def _idempotency_conflict() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": "idempotency_key_conflict"},
    )


def _mutation_audit(
    request: Request,
    *,
    scope: dict[str, object],
    reason_code: str,
) -> MutationAudit:
    principal = getattr(request.state, "admin_principal", None)
    trace_id = str(
        request.headers.get("X-Trace-ID")
        or request.headers.get("X-Request-ID")
        or getattr(request.state, "admin_request_id", "")
    ).strip()[:128]
    return MutationAudit(
        actor=str(getattr(principal, "subject", "") or "unknown")[:128],
        actor_kind=str(getattr(principal, "auth_kind", "") or "unknown")[:32],
        roles=tuple(str(role)[:64] for role in (getattr(principal, "roles", ()) or ())),
        scope=scope,
        reason_code=reason_code,
        trace_id=trace_id,
    )


def _apply_mutation_outcome(response: Response, outcome: MutationOutcome) -> None:
    response.status_code = outcome.status_code
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Mutation-ID"] = outcome.mutation_id
    if outcome.replayed:
        response.headers["Idempotent-Replayed"] = "true"


def _task_audit_state(record: object) -> dict[str, object]:
    return {
        "exists": record is not None,
        "status": str(getattr(record, "status", "") or "")[:32],
        "retry_count": int(getattr(record, "retry_count", 0) or 0),
        "callback_sent": bool(getattr(record, "callback_sent", False)),
    }


def _retry_audit_state(result: dict[str, object]) -> dict[str, object]:
    return {
        "retry_queued": bool(result.get("retry_queued")),
        "retry_count": _audit_int(result.get("retry_count")),
        "max_retries": _audit_int(result.get("max_retries")),
        "rejected": not bool(result.get("retry_queued")),
    }


def _callback_audit_state(result: dict[str, object]) -> dict[str, object]:
    return {
        "sent": bool(result.get("sent")),
        "skipped": bool(result.get("skipped")),
        "forced": bool(result.get("force")),
        "status": str(result.get("status") or "")[:32],
        "failed": bool(result.get("error")),
    }


def _recover_audit_state(result: dict[str, object]) -> dict[str, object]:
    return {
        "recovered": _audit_int(result.get("recovered")),
        "callbacks_sent": _audit_int(result.get("callbacks_sent")),
        "callback_failed": _audit_int(result.get("callback_failed")),
    }


def _audit_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    return 0
