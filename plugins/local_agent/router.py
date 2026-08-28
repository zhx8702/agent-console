"""Admin routes for local-agent probe and jobs."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from plugins.local_agent.probe import LocalAgentProbe
from plugins.local_agent.store import LocalAgentStore


def build_local_agent_router(
    store: LocalAgentStore,
    probe: LocalAgentProbe,
) -> APIRouter:
    router = APIRouter()

    @router.get("/backends")
    async def get_backends(force: bool = False):
        snapshot = await probe.snapshot(force=force)
        return snapshot.as_dict()

    @router.post("/probe")
    async def run_probe():
        snapshot = await probe.snapshot(force=True)
        return snapshot.as_dict()

    @router.get("/tasks")
    async def list_tasks(
        tenant_id: str = "",
        status: str = "",
        limit: int = Query(default=50, ge=1, le=200),
    ):
        jobs = await store.list_jobs(tenant_id=tenant_id, status=status, limit=limit)
        return {"items": [job.as_dict() for job in jobs], "count": len(jobs)}

    @router.get("/tasks/{job_id}")
    async def get_task(job_id: str):
        job = await store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job_not_found")
        return job.as_dict()

    @router.post("/tasks")
    async def create_task(body: dict):
        backend = str((body or {}).get("backend") or "").strip().lower()
        prompt = str((body or {}).get("prompt") or "").strip()
        if backend not in {"grok", "codex"}:
            raise HTTPException(status_code=400, detail="unknown_backend")
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt_required")
        snapshot = await probe.snapshot()
        if not snapshot.backend(backend).ok:
            raise HTTPException(status_code=503, detail="backend_unavailable")
        job = await store.create_job(
            backend=backend,
            prompt=prompt,
            tenant_id=str((body or {}).get("tenant_id") or "admin"),
            channel=str((body or {}).get("channel") or "web"),
            session_id=str((body or {}).get("session_id") or "admin"),
            user_id=str((body or {}).get("user_id") or "admin"),
            request_id=str((body or {}).get("request_id") or ""),
            trace_id=str((body or {}).get("trace_id") or ""),
        )
        return job.as_dict()

    return router
