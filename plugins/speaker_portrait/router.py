"""Admin API for speaker portraits."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import Field

from app.admin.auth_router import authenticate_admin_request
from app.common.request_models import StrictRequestModel
from plugins.persona_extract.store import PersonaExtractStore
from plugins.speaker_portrait.pipeline import compile_reply_style, portrait_style_slug
from plugins.speaker_portrait.store import SpeakerPortraitStore


class PortraitJobCreateRequest(StrictRequestModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=256)
    session_name: str = Field(default="", max_length=256)
    speaker_id: str = Field(min_length=1, max_length=256)
    speaker_name: str = Field(default="", max_length=256)
    connection_id: str = Field(default="", max_length=64)
    external_session_id: str = Field(default="", max_length=256)
    days_limit: int = Field(default=90, ge=0, le=3650)
    max_messages: int = Field(default=4000, ge=0, le=100_000)
    mode: str = Field(default="full", max_length=16)


class PortraitApplyStyleRequest(StrictRequestModel):
    tenant_id: str = Field(min_length=1, max_length=64)
    session_id: str = Field(min_length=1, max_length=256)
    session_name: str = Field(default="", max_length=256)
    channel: str = Field(default="wechat", max_length=32)
    source_key: str = Field(default="wxbot", max_length=128)
    enabled: bool = True


def build_speaker_portrait_router(
    store: SpeakerPortraitStore,
    wakeup: Any | None = None,
) -> APIRouter:
    router = APIRouter()

    def _require_admin(request: Request) -> None:
        authenticate_admin_request(request, store.settings)

    def _wake() -> None:
        if wakeup is not None:
            wakeup.set()

    @router.post("/jobs")
    async def create_job(
        body: PortraitJobCreateRequest,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict:
        _ = idempotency_key
        _require_admin(request)
        job = await store.create_job(
            tenant_id=body.tenant_id,
            session_id=body.session_id,
            session_name=body.session_name,
            speaker_id=body.speaker_id,
            speaker_name=body.speaker_name or body.speaker_id,
            connection_id=body.connection_id,
            external_session_id=body.external_session_id or body.session_id,
            days_limit=body.days_limit,
            max_messages=body.max_messages,
            mode=body.mode,
        )
        _wake()
        return {"status": "queued", "job": job}

    @router.get("/jobs")
    async def list_jobs(
        request: Request,
        tenant_id: str = Query(min_length=1, max_length=64),
        session_id: str = Query(default="", max_length=256),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict:
        _require_admin(request)
        items = await store.list_jobs(tenant_id=tenant_id, session_id=session_id, limit=limit)
        return {"items": items, "count": len(items)}

    @router.get("/jobs/{job_id}")
    async def get_job(job_id: int, request: Request) -> dict:
        _require_admin(request)
        job = await store.get_job(job_id)
        if not job:
            raise HTTPException(404, "job_not_found")
        return job

    @router.get("/portraits")
    async def list_portraits(
        request: Request,
        tenant_id: str = Query(min_length=1, max_length=64),
        session_id: str = Query(default="", max_length=256),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict:
        _require_admin(request)
        items = await store.list_portraits(
            tenant_id=tenant_id,
            session_id=session_id,
            limit=limit,
        )
        return {"items": items, "count": len(items)}

    def _style_payload(record: dict) -> dict:
        name = str(record.get("display_name") or record.get("speaker_id") or "这个人")
        portrait = record.get("portrait") if isinstance(record.get("portrait"), dict) else {}
        prompt = compile_reply_style(portrait, name=name)
        return {"name": name, "prompt": prompt, "prompt_chars": len(prompt)}

    @router.get("/portraits/{speaker_id}/style")
    async def preview_style(
        speaker_id: str,
        request: Request,
        tenant_id: str = Query(min_length=1, max_length=64),
        channel: str = Query(default="wechat", max_length=32),
        source_key: str = Query(default="wxbot", max_length=128),
    ) -> dict:
        _require_admin(request)
        record = await store.get_portrait(
            tenant_id=tenant_id,
            speaker_id=speaker_id,
            channel=channel,
            source_key=source_key,
        )
        if not record:
            raise HTTPException(404, "portrait_not_found")
        return {"status": "ok", **_style_payload(record)}

    @router.post("/portraits/{speaker_id}/apply-style")
    async def apply_style(
        speaker_id: str,
        body: PortraitApplyStyleRequest,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict:
        _ = idempotency_key
        _require_admin(request)
        record = await store.get_portrait(
            tenant_id=body.tenant_id,
            speaker_id=speaker_id,
            channel=body.channel,
            source_key=body.source_key,
        )
        if not record:
            raise HTTPException(404, "portrait_not_found")
        payload = _style_payload(record)
        persona_store = PersonaExtractStore(store.settings)
        slug = portrait_style_slug(speaker_id)
        profile = await persona_store.upsert_profile(
            tenant_id=body.tenant_id,
            session_id=body.session_id,
            channel=body.channel,
            source_key=body.source_key,
            source_label=str(record.get("display_name") or speaker_id),
            profile_name=payload["name"],
            prompt_text=payload["prompt"],
            enabled=body.enabled,
            session_name=body.session_name or body.session_id,
            target_user_id=speaker_id,
            target_name=payload["name"],
            skill_slug=slug,
        )
        return {"status": "applied", "profile_id": profile.get("id"), **payload}

    @router.get("/portraits/{speaker_id:path}")
    async def get_portrait(
        speaker_id: str,
        request: Request,
        tenant_id: str = Query(min_length=1, max_length=64),
        channel: str = Query(default="wechat", max_length=32),
        source_key: str = Query(default="wxbot", max_length=128),
    ) -> dict:
        _require_admin(request)
        record = await store.get_portrait(
            tenant_id=tenant_id,
            speaker_id=speaker_id,
            channel=channel,
            source_key=source_key,
        )
        if not record:
            raise HTTPException(404, "portrait_not_found")
        return record

    return router
