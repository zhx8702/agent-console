"""
REST API for the reply-style extraction plugin.

Mounted at ``/plugins/persona_extract/`` by the plugin framework.
"""

from __future__ import annotations

import math
import os
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import Field

from app.admin.mutation_ledger import MutationIdempotencyConflictError
from app.common.request_models import StrictRequestModel
from plugins.persona_extract.offline import (
    OFFLINE_IMPORT_MAX_BYTES,
    file_sha256,
    offline_export_path,
)
from plugins.persona_extract.store import (
    PersonaApplyJobError,
    PersonaExtractStore,
    PersonaJobRequestConflict,
)

PersonaScopeGate = Callable[[str, str], Awaitable[bool]]


class PersonaSourceMessage(StrictRequestModel):
    timestamp: str = Field(default="", max_length=64)
    sender_name: str = Field(default="", max_length=256)
    text: str = Field(min_length=1, max_length=8_000)


class ExtractionRequest(StrictRequestModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=256)
    session_name: str = Field(default="", max_length=256)
    connection_id: str = Field(default="", max_length=64)
    adapter_id: str = Field(default="", max_length=64)
    external_session_id: str = Field(default="", max_length=256)
    target_user_id: str = Field(min_length=1, max_length=256)
    target_name: str = Field(default="", max_length=256)
    days_limit: int = Field(default=90, ge=0, le=3_650)
    max_messages: int = Field(default=2_000, ge=0, le=100_000)
    client_request_id: str = Field(default="", max_length=128)
    messages: list[PersonaSourceMessage] = Field(default_factory=list, max_length=5_000)


class OfflineExportRequest(StrictRequestModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=256)
    session_name: str = Field(default="", max_length=256)
    connection_id: str = Field(default="", max_length=64)
    adapter_id: str = Field(default="", max_length=64)
    external_session_id: str = Field(default="", max_length=256)
    target_user_id: str = Field(min_length=1, max_length=256)
    target_name: str = Field(default="", max_length=256)
    mode: Literal["full", "incremental"] = "full"
    client_request_id: str = Field(default="", max_length=128)


class PersonaProfileUpsertRequest(StrictRequestModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=256)
    session_name: str = Field(default="", max_length=256)
    channel: str = Field(default="all", max_length=32)
    source_key: str = Field(default="*", max_length=128)
    source_label: str = Field(default="", max_length=256)
    profile_name: str = Field(default="default", max_length=128)
    target_user_id: str = Field(default="", max_length=256)
    target_name: str = Field(default="", max_length=256)
    skill_slug: str = Field(default="", max_length=128)
    prompt_text: str = Field(default="", max_length=200_000)
    artifact: dict[str, Any] | None = None
    enabled: bool = True
    job_id: int | None = Field(default=None, ge=1)


class PersonaProfileApplyJobRequest(StrictRequestModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=256)
    session_name: str = Field(default="", max_length=256)
    job_id: int = Field(ge=1)
    channel: str = Field(default="all", max_length=32)
    source_key: str = Field(default="*", max_length=128)
    source_label: str = Field(default="", max_length=256)
    profile_name: str = Field(default="default", max_length=128)
    enabled: bool = True


def _required_idempotency_key(value: str | None) -> str:
    if value is None:
        raise HTTPException(
            status_code=428,
            detail={"code": "idempotency_key_required"},
        )
    normalized = str(value).strip()
    if not normalized or len(normalized) > 128:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_idempotency_key"},
        )
    return normalized


def _mutation_actor(request: Request) -> tuple[str, str, tuple[str, ...], str]:
    principal = getattr(request.state, "admin_principal", None)
    actor = str(getattr(principal, "subject", "") or "unknown")[:128]
    actor_kind = str(getattr(principal, "auth_kind", "") or "unknown")[:32]
    roles = tuple(str(role)[:64] for role in (getattr(principal, "roles", ()) or ()))
    trace_id = str(
        getattr(request.state, "admin_request_id", "") or request.headers.get("X-Request-ID", "")
    )[:128]
    return actor, actor_kind, roles, trace_id


def _handle_mutation_error(exc: Exception) -> None:
    if isinstance(exc, MutationIdempotencyConflictError):
        raise HTTPException(
            status_code=409,
            detail={"code": "idempotency_key_conflict"},
        ) from exc
    if isinstance(exc, PersonaApplyJobError):
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    if isinstance(exc, PersonaJobRequestConflict):
        raise HTTPException(
            status_code=409,
            detail={"code": "idempotency_key_conflict"},
        ) from exc
    raise exc


def _incremental_days_limit(cursor: dict[str, Any]) -> int:
    raw = str(
        cursor.get("overlap_start_timestamp")
        or cursor.get("last_timestamp")
        or ""
    ).strip()
    if not raw:
        return 0
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    elapsed = max(
        0.0,
        (datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds(),
    )
    days = max(3, math.ceil(elapsed / 86_400) + 2)
    return days if days <= 3_650 else 0


async def _schedule_job(
    store: PersonaExtractStore,
    scheduler: Any | None,
    *,
    job_id: int,
    messages: list[dict[str, Any]] | None = None,
    scope_gate: PersonaScopeGate | None = None,
) -> dict:
    job = await store.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    await _require_resource_scope(job, scope_gate)

    if scheduler is not None and hasattr(scheduler, "schedule_job"):
        accepting_jobs = getattr(scheduler, "is_accepting_jobs", None)
        if callable(accepting_jobs) and not bool(accepting_jobs()):
            return {
                "job_id": job_id,
                "status": str(job.get("status") or "pending"),
                "accepted": False,
                "job": job,
                "message": "plugin disabled",
                "status_url": f"/plugins/persona_extract/jobs/{job_id}",
            }
        scheduled = await scheduler.schedule_job(job_id, messages)
        current = scheduled or await store.get_job(job_id)
        if scheduled is None:
            return {
                "job_id": job_id,
                "status": str((current or {}).get("status") or "pending"),
                "accepted": False,
                "job": current,
                "message": "plugin disabled",
                "status_url": f"/plugins/persona_extract/jobs/{job_id}",
            }
        return {
            "job_id": job_id,
            "status": str((current or {}).get("status") or "pending"),
            "accepted": True,
            "job": current,
            "status_url": f"/plugins/persona_extract/jobs/{job_id}",
        }

    return {
        "job_id": job_id,
        "status": str(job.get("status") or "pending"),
        "accepted": False,
        "job": job,
        "message": "scheduler unavailable",
        "status_url": f"/plugins/persona_extract/jobs/{job_id}",
    }


async def _require_resource_scope(
    resource: dict[str, Any],
    scope_gate: PersonaScopeGate | None,
) -> None:
    if scope_gate is None:
        raise HTTPException(status_code=503, detail="plugin_scope_unavailable")
    tenant_id = str(resource.get("tenant_id") or "").strip()
    session_id = str(resource.get("session_id") or "").strip()
    if not tenant_id:
        raise HTTPException(status_code=503, detail="plugin_scope_unavailable")
    try:
        allowed = await scope_gate(tenant_id, session_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="plugin_scope_unavailable",
        ) from exc
    if allowed is not True:
        raise HTTPException(status_code=503, detail="plugin_runtime_disabled")


def build_persona_extract_router(
    store: PersonaExtractStore,
    scheduler: Any | None = None,
    *,
    scope_gate: PersonaScopeGate | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.post("/jobs", status_code=202)
    async def create_job(
        req: ExtractionRequest,
        response: Response,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        key = _required_idempotency_key(idempotency_key)
        if req.client_request_id.strip() and req.client_request_id.strip() != key:
            raise HTTPException(
                status_code=409,
                detail={"code": "client_request_id_mismatch"},
            )
        await _require_resource_scope(
            {"tenant_id": req.tenant_id, "session_id": req.session_id},
            scope_gate,
        )
        if req.days_limit == 0 or req.max_messages == 0:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "persona_full_export_required",
                    "message": "全量数据请使用离线导出流程",
                },
            )
        online_max = int(
            getattr(
                getattr(store, "settings", None),
                "persona_extract_online_max_messages",
                10_000,
            )
        )
        if req.max_messages > online_max:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "persona_online_limit_exceeded",
                    "max_messages": online_max,
                    "message": "在线生成范围过大，请使用离线导出流程",
                },
            )
        messages = [message.model_dump(exclude_defaults=True) for message in req.messages]
        create_kwargs = {
            "tenant_id": req.tenant_id,
            "session_id": req.session_id,
            "target_user_id": req.target_user_id,
            "target_name": req.target_name,
            "days_limit": req.days_limit,
            "max_messages": req.max_messages,
            "connection_id": req.connection_id,
            "adapter_id": req.adapter_id,
            "external_session_id": req.external_session_id,
            "messages": messages or None,
            "request_id": key,
        }
        if req.session_name.strip():
            create_kwargs["session_name"] = req.session_name
        try:
            job, replayed = await store.create_job_idempotent(**create_kwargs)
        except Exception as exc:
            _handle_mutation_error(exc)
        result = await _schedule_job(
            store,
            scheduler,
            job_id=int(job["id"]),
            scope_gate=scope_gate,
        )
        result["replayed"] = replayed
        response.status_code = 202
        if replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return result

    @router.post("/offline-exports", status_code=202)
    async def create_offline_export(
        req: OfflineExportRequest,
        response: Response,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key"),
        ] = None,
    ):
        key = _required_idempotency_key(idempotency_key)
        if req.client_request_id.strip() and req.client_request_id.strip() != key:
            raise HTTPException(
                status_code=409,
                detail={"code": "client_request_id_mismatch"},
            )
        await _require_resource_scope(
            {"tenant_id": req.tenant_id, "session_id": req.session_id},
            scope_gate,
        )
        checkpoint_seed: dict[str, Any] = {"export_mode": req.mode}
        days_limit = 0
        if req.mode == "incremental":
            baseline = await store.get_latest_artifact_for_target(
                tenant_id=req.tenant_id,
                session_id=req.session_id,
                target_user_id=req.target_user_id,
            )
            baseline_meta = (
                baseline.get("meta")
                if isinstance(baseline, dict)
                and isinstance(baseline.get("meta"), dict)
                else {}
            )
            baseline_cursor = (
                baseline_meta.get("offline_cursor")
                if isinstance(baseline_meta.get("offline_cursor"), dict)
                else None
            )
            if baseline_cursor is None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "persona_incremental_baseline_required",
                        "message": "请先完成一次离线全量生成并应用",
                    },
                )
            checkpoint_seed["baseline_cursor"] = baseline_cursor
            checkpoint_seed["slug"] = str(baseline.get("slug") or "")
            days_limit = _incremental_days_limit(baseline_cursor)
        try:
            job, replayed = await store.create_job_idempotent(
                tenant_id=req.tenant_id,
                session_id=req.session_id,
                session_name=req.session_name,
                target_user_id=req.target_user_id,
                target_name=req.target_name,
                days_limit=days_limit,
                max_messages=0,
                connection_id=req.connection_id,
                adapter_id=req.adapter_id,
                external_session_id=req.external_session_id,
                messages=None,
                request_id=key,
                workflow="offline_export",
                checkpoint_seed=checkpoint_seed,
            )
        except Exception as exc:
            _handle_mutation_error(exc)
        result = await _schedule_job(
            store,
            scheduler,
            job_id=int(job["id"]),
            scope_gate=scope_gate,
        )
        result["replayed"] = replayed
        result["offline_mode"] = req.mode
        result["download_url"] = (
            f"/plugins/persona_extract/offline-exports/{int(job['id'])}/download"
        )
        response.status_code = 202
        if replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return result

    @router.get("/offline-exports/{job_id}/download")
    async def download_offline_export(job_id: int):
        job = await store.get_job(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        await _require_resource_scope(job, scope_gate)
        checkpoint = (
            job.get("checkpoint")
            if isinstance(job.get("checkpoint"), dict)
            else {}
        )
        export_meta = (
            checkpoint.get("offline_export")
            if isinstance(checkpoint.get("offline_export"), dict)
            else {}
        )
        if (
            str(checkpoint.get("workflow") or "") != "offline_export"
            or str(job.get("status") or "") != "awaiting_import"
            or export_meta.get("download_ready") is not True
        ):
            raise HTTPException(status_code=409, detail="offline export is not ready")
        path = offline_export_path(store.settings, job_id)
        expected_sha = str(export_meta.get("archive_sha256") or "")
        if not path.is_file() or not expected_sha or file_sha256(path) != expected_sha:
            raise HTTPException(status_code=410, detail="offline export is unavailable")
        return FileResponse(
            path,
            media_type="application/zip",
            filename=str(export_meta.get("filename") or path.name),
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.put("/offline-exports/{job_id}/artifact")
    async def upload_offline_artifact(
        job_id: int,
        request: Request,
        response: Response,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key"),
        ] = None,
    ):
        job = await store.get_job(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        await _require_resource_scope(job, scope_gate)
        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type not in {
            "application/zip",
            "application/x-zip-compressed",
            "application/octet-stream",
        }:
            raise HTTPException(status_code=415, detail="a ZIP artifact is required")
        raw_length = request.headers.get("content-length", "")
        if raw_length:
            try:
                content_length = int(raw_length)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="invalid content length") from exc
            if content_length > OFFLINE_IMPORT_MAX_BYTES:
                raise HTTPException(status_code=413, detail="offline artifact is too large")
        temp_file = tempfile.NamedTemporaryFile(
            prefix=f"persona-import-{job_id}-",
            suffix=".zip",
            delete=False,
        )
        temp_path = Path(temp_file.name)
        total = 0
        try:
            try:
                os.chmod(temp_path, 0o600)
            except OSError:
                pass
            with temp_file:
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > OFFLINE_IMPORT_MAX_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail="offline artifact is too large",
                        )
                    temp_file.write(chunk)
            if total == 0:
                raise HTTPException(status_code=400, detail="offline artifact is empty")
            actor, actor_kind, roles, trace_id = _mutation_actor(request)
            try:
                outcome = await store.import_offline_artifact_idempotent(
                    job_id=job_id,
                    tenant_id=str(job.get("tenant_id") or ""),
                    archive_path=temp_path,
                    idempotency_key=_required_idempotency_key(idempotency_key),
                    actor=actor,
                    actor_kind=actor_kind,
                    roles=roles,
                    trace_id=trace_id,
                )
            except Exception as exc:
                _handle_mutation_error(exc)
            response.status_code = outcome.status_code
            if outcome.replayed:
                response.headers["Idempotent-Replayed"] = "true"
            return outcome.response
        finally:
            temp_path.unlink(missing_ok=True)

    @router.get("/jobs")
    async def list_jobs(
        tenant_id: str = Query(...),
        session_id: str | None = Query(default=None),
    ):
        rows = await store.list_jobs(tenant_id, session_id)
        return {"items": rows}

    @router.get("/jobs/{job_id}")
    async def get_job(job_id: int):
        job = await store.get_job(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        await _require_resource_scope(job, scope_gate)
        return job

    @router.post("/jobs/{job_id}/run", status_code=202)
    async def run_job(
        job_id: int,
        request: Request,
        response: Response,
        messages: Annotated[
            list[PersonaSourceMessage],
            Body(max_length=5_000),
        ],
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        job = await store.get_job(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        await _require_resource_scope(job, scope_gate)
        if messages:
            raise HTTPException(
                status_code=409,
                detail="re-run uses the frozen message snapshot; create a new job to replace it",
            )
        actor, actor_kind, roles, trace_id = _mutation_actor(request)
        try:
            outcome = await store.requeue_job_idempotent(
                job_id=job_id,
                tenant_id=str(job.get("tenant_id") or ""),
                idempotency_key=_required_idempotency_key(idempotency_key),
                actor=actor,
                actor_kind=actor_kind,
                roles=roles,
                trace_id=trace_id,
            )
        except Exception as exc:
            _handle_mutation_error(exc)
        result = await _schedule_job(
            store,
            scheduler,
            job_id=job_id,
            scope_gate=scope_gate,
        )
        result["replayed"] = outcome.replayed
        response.status_code = 202
        if outcome.replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return result

    @router.post("/jobs/{job_id}/cancel")
    async def cancel_job(
        job_id: int,
        request: Request,
        response: Response,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        job = await store.get_job(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        await _require_resource_scope(job, scope_gate)
        actor, actor_kind, roles, trace_id = _mutation_actor(request)
        try:
            outcome = await store.cancel_job_idempotent(
                job_id=job_id,
                tenant_id=str(job.get("tenant_id") or ""),
                idempotency_key=_required_idempotency_key(idempotency_key),
                actor=actor,
                actor_kind=actor_kind,
                roles=roles,
                trace_id=trace_id,
            )
        except Exception as exc:
            _handle_mutation_error(exc)
        response.status_code = outcome.status_code
        if outcome.replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return outcome.response

    @router.get("/profiles")
    async def list_profiles(
        tenant_id: str = Query(...),
        session_id: str = Query(...),
    ):
        rows = await store.list_profiles(tenant_id, session_id)
        return {"items": rows}

    @router.get("/profiles/{profile_id}")
    async def get_profile(profile_id: int):
        profile = await store.get_profile(profile_id)
        if not profile:
            raise HTTPException(404, "profile not found")
        await _require_resource_scope(profile, scope_gate)
        return profile

    @router.post("/profiles")
    async def upsert_profile(
        body: PersonaProfileUpsertRequest,
        request: Request,
        response: Response,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        actor, actor_kind, roles, trace_id = _mutation_actor(request)
        try:
            outcome = await store.upsert_profile_idempotent(
                tenant_id=body.tenant_id,
                session_id=body.session_id,
                session_name=body.session_name,
                channel=body.channel,
                source_key=body.source_key,
                source_label=body.source_label,
                profile_name=body.profile_name,
                target_user_id=body.target_user_id,
                target_name=body.target_name,
                skill_slug=body.skill_slug,
                prompt_text=body.prompt_text,
                artifact=body.artifact,
                enabled=body.enabled,
                job_id=body.job_id,
                idempotency_key=_required_idempotency_key(idempotency_key),
                actor=actor,
                actor_kind=actor_kind,
                roles=roles,
                trace_id=trace_id,
                reason="profile workspace save",
            )
        except Exception as exc:
            _handle_mutation_error(exc)
        response.status_code = outcome.status_code
        if outcome.replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return outcome.response

    @router.delete("/profiles/{profile_id}")
    async def delete_profile(
        profile_id: int,
        request: Request,
        response: Response,
        tenant_id: str = Query(...),
        session_id: str = Query(...),
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        actor, actor_kind, roles, trace_id = _mutation_actor(request)
        try:
            outcome = await store.delete_profile_idempotent(
                profile_id=profile_id,
                tenant_id=tenant_id,
                session_id=session_id,
                idempotency_key=_required_idempotency_key(idempotency_key),
                actor=actor,
                actor_kind=actor_kind,
                roles=roles,
                trace_id=trace_id,
                reason="profile workspace delete",
            )
        except Exception as exc:
            _handle_mutation_error(exc)
        response.status_code = outcome.status_code
        if outcome.replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return outcome.response

    @router.post("/profiles/apply-job")
    async def apply_job(
        body: PersonaProfileApplyJobRequest,
        request: Request,
        response: Response,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ):
        actor, actor_kind, roles, trace_id = _mutation_actor(request)
        try:
            outcome = await store.apply_job_idempotent(
                tenant_id=body.tenant_id,
                session_id=body.session_id,
                session_name=body.session_name,
                job_id=body.job_id,
                channel=body.channel,
                source_key=body.source_key,
                source_label=body.source_label,
                profile_name=body.profile_name,
                enabled=body.enabled,
                idempotency_key=_required_idempotency_key(idempotency_key),
                actor=actor,
                actor_kind=actor_kind,
                roles=roles,
                trace_id=trace_id,
                reason="apply completed extraction job",
            )
        except Exception as exc:
            _handle_mutation_error(exc)
        response.status_code = outcome.status_code
        if outcome.replayed:
            response.headers["Idempotent-Replayed"] = "true"
        return outcome.response

    return router
