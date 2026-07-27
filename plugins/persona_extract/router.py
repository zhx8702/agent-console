"""
REST API for the reply-style extraction plugin.

Mounted at ``/plugins/persona_extract/`` by the plugin framework.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request, Response
from pydantic import Field

from app.admin.mutation_ledger import MutationIdempotencyConflictError
from app.common.request_models import StrictRequestModel
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
