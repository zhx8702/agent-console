"""Speaker portrait plugin: understand a person from their chat history."""

from __future__ import annotations

import asyncio
import uuid

from app.common.logging import get_logger
from app.orchestrator.flow import FlowStepDefinition
from app.plugin.base import Plugin, PluginContext, PluginMeta
from plugins.speaker_portrait.hooks import SpeakerPortraitEnrichStep, SpeakerPortraitNoteStep
from plugins.speaker_portrait.jobs import run_portrait_job
from plugins.speaker_portrait.router import build_speaker_portrait_router
from plugins.speaker_portrait.store import SpeakerPortraitStore

logger = get_logger(__name__)


class SpeakerPortraitPlugin(Plugin):
    meta = PluginMeta(
        name="speaker_portrait",
        version="0.1.0",
        description="Build speaker portraits from chat history via local CLI",
    )

    def __init__(self) -> None:
        self._store: SpeakerPortraitStore | None = None
        self._ctx: PluginContext | None = None
        self._accept_jobs = False
        self._worker_task: asyncio.Task[None] | None = None
        self._worker_wakeup = asyncio.Event()
        self._worker_owner = f"speaker-portrait-{uuid.uuid4().hex[:12]}"

    async def initialize(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._store = SpeakerPortraitStore(ctx.settings)
        if ctx.db_ok:
            await self._store.ensure_tables()
        self._accept_jobs = True
        self._ensure_worker()

    async def shutdown(self) -> None:
        self._accept_jobs = False
        task = self._worker_task
        self._worker_wakeup.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._worker_task = None
        self._store = None
        self._ctx = None

    async def on_enable(self, scope=None) -> None:
        _ = scope
        self._accept_jobs = True
        self._ensure_worker()

    async def on_disable(self, scope=None) -> None:
        _ = scope
        self._accept_jobs = False

    def _should_run_worker(self) -> bool:
        if self._ctx is None or self._store is None or not self._accept_jobs:
            return False
        if not bool(getattr(self._ctx, "db_ok", True)):
            return False
        role = str(getattr(self._ctx.settings, "app_process_role", "api") or "api").strip().lower()
        raw = str(getattr(self._ctx.settings, "speaker_portrait_worker_roles", "scheduler") or "scheduler")
        roles = {item.strip().lower() for item in raw.split(",") if item.strip()}
        return role in roles

    def _ensure_worker(self) -> None:
        if not self._should_run_worker():
            return
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._worker_task = asyncio.create_task(self._worker_loop(), name="speaker-portrait-worker")

    async def _worker_loop(self) -> None:
        assert self._store is not None
        while self._should_run_worker():
            try:
                job = await self._store.claim_next_job(
                    claim_owner=self._worker_owner,
                    lease_seconds=float(
                        getattr(self._ctx.settings, "speaker_portrait_timeout_seconds", 600.0) or 600.0
                    )
                    + 60.0,
                )
            except Exception:
                logger.warning("speaker_portrait.claim_failed", exc_info=True)
                job = None
            if job is None:
                try:
                    await self._enqueue_hot_updates()
                except Exception:
                    logger.warning("speaker_portrait.hot_update_enqueue_failed", exc_info=True)
                try:
                    await asyncio.wait_for(self._worker_wakeup.wait(), timeout=3.0)
                except TimeoutError:
                    pass
                self._worker_wakeup.clear()
                continue
            try:
                await run_portrait_job(self._store, job)
            except Exception as exc:
                logger.warning(
                    "speaker_portrait.job_failed",
                    job_id=job.get("id"),
                    error_type=exc.__class__.__name__,
                )
                await self._store.fail_job(int(job["id"]), str(exc))

    async def _enqueue_hot_updates(self) -> None:
        assert self._store is not None
        assert self._ctx is not None
        if not bool(getattr(self._ctx.settings, "speaker_portrait_hot_update_enabled", True)):
            return
        due = await self._store.due_hot_updates(
            min_messages=int(
                getattr(self._ctx.settings, "speaker_portrait_hot_update_min_messages", 40) or 40
            ),
            min_seconds=float(
                getattr(self._ctx.settings, "speaker_portrait_hot_update_min_seconds", 3600.0)
                or 3600.0
            ),
        )
        for portrait in due:
            await self._store.create_job(
                tenant_id=str(portrait.get("tenant_id") or ""),
                session_id=str(portrait.get("session_id") or ""),
                session_name=str(portrait.get("session_id") or ""),
                speaker_id=str(portrait.get("speaker_id") or ""),
                speaker_name=str(portrait.get("display_name") or ""),
                external_session_id=str(portrait.get("session_id") or ""),
                days_limit=14,
                max_messages=800,
                mode="incremental",
                since_timestamp=str(portrait.get("last_distilled_message_at") or ""),
                portrait_id=int(portrait["id"]),
                claimed_pending_messages=int(portrait.get("pending_messages") or 0),
            )

    def get_api_router(self):
        if self._store is None:
            return None
        return build_speaker_portrait_router(self._store, wakeup=self._worker_wakeup)

    def get_flow_steps(self) -> list[FlowStepDefinition]:
        return [
            FlowStepDefinition(
                kind="plugin.speaker_portrait.note",
                owner=self.meta.name,
                name="Note speaker message for hot update",
                permissions=["storage:shared"],
                inputs={"event", "session"},
                outputs={"signals.speaker_portrait_note"},
                timeout_seconds=0.8,
                error_policy="fail_open",
            ),
            FlowStepDefinition(
                kind="plugin.speaker_portrait.enrich",
                owner=self.meta.name,
                name="Load speaker portrait",
                permissions=["storage:shared"],
                inputs={"event", "session"},
                outputs={"signals.speaker_portrait"},
                timeout_seconds=1.5,
                error_policy="fail_open",
            ),
        ]

    def get_flow_executors(self):
        if self._store is None:
            return {}
        return {
            "plugin.speaker_portrait.note": SpeakerPortraitNoteStep(self._store),
            "plugin.speaker_portrait.enrich": SpeakerPortraitEnrichStep(self._store),
        }

    def get_permissions(self) -> list[str]:
        return [
            "network:wxbot",
            "storage:shared",
            "hooks:pipeline",
            "admin_api",
        ]


plugin = SpeakerPortraitPlugin()
