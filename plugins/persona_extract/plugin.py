"""Reply-style extraction plugin with a durable database-backed worker."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable
from typing import Any

from app.common.logging import get_logger
from app.orchestrator.flow import FlowStepDefinition
from app.plugin.base import Plugin, PluginContext, PluginMeta
from plugins.persona_extract.hooks import PersonaSkillEnrichStep, PersonaSkillHook
from plugins.persona_extract.offline import cleanup_expired_offline_exports
from plugins.persona_extract.router import build_persona_extract_router
from plugins.persona_extract.store import (
    PersonaExtractStore,
    PersonaJobCancelled,
    PersonaJobLeaseLost,
    _is_transient_llm_error,
)

logger = get_logger(__name__)


class PersonaExtractPlugin(Plugin):
    meta = PluginMeta(
        name="persona_extract",
        version="0.2.0",
        description="Extract and synthesize reply-style profiles from user message history",
    )

    def __init__(self) -> None:
        self._store: PersonaExtractStore | None = None
        self._ctx: PluginContext | None = None
        self._accept_jobs = False
        self._worker_task: asyncio.Task[None] | None = None
        self._worker_wakeup = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._active_claim: tuple[int, int, str] | None = None
        self._worker_owner = f"persona-{uuid.uuid4().hex}"
        self._last_offline_cleanup_at = 0.0

    @staticmethod
    async def _resolve_persistent_operation(
        operation: Awaitable[Any],
    ) -> tuple[Any, bool]:
        operation_task = asyncio.ensure_future(operation)
        cancellation_requested = False
        while not operation_task.done():
            try:
                await asyncio.shield(operation_task)
            except asyncio.CancelledError:
                cancellation_requested = True
        return operation_task.result(), cancellation_requested

    @classmethod
    async def _finish_persistent_operation(cls, operation: Awaitable[Any]) -> Any:
        result, _cancelled = await cls._resolve_persistent_operation(operation)
        return result

    def _worker_roles(self) -> set[str]:
        if self._ctx is None:
            return set()
        raw = str(
            getattr(
                self._ctx.settings,
                "persona_extract_worker_roles",
                "scheduler",
            )
            or "scheduler"
        )
        return {item.strip().lower() for item in raw.split(",") if item.strip()}

    def _should_run_worker(self) -> bool:
        if self._ctx is None or self._store is None or not self._accept_jobs:
            return False
        role = str(
            getattr(self._ctx.settings, "app_process_role", "api") or "api"
        ).strip().lower()
        return bool(
            role in self._worker_roles()
            and bool(getattr(self._ctx, "db_ok", True))
        )

    def _ensure_worker(self) -> None:
        if not self._should_run_worker():
            return
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._worker_task = asyncio.create_task(
            self._worker_loop(),
            name="persona-extract-worker",
        )

    async def _stop_worker(self) -> None:
        task = self._worker_task
        self._worker_wakeup.set()
        if task is None:
            return
        if not task.done():
            task.cancel()
        await self._finish_persistent_operation(
            asyncio.gather(task, return_exceptions=True)
        )
        if self._worker_task is task:
            self._worker_task = None
        self._active_claim = None

    async def _wait_for_work(self) -> None:
        assert self._ctx is not None
        timeout = max(
            0.1,
            float(
                getattr(
                    self._ctx.settings,
                    "persona_extract_job_poll_interval_seconds",
                    2.0,
                )
            ),
        )
        try:
            await asyncio.wait_for(self._worker_wakeup.wait(), timeout=timeout)
        except TimeoutError:
            pass
        finally:
            self._worker_wakeup.clear()

    async def _worker_loop(self) -> None:
        assert self._store is not None
        assert self._ctx is not None
        lease_seconds = float(
            getattr(self._ctx.settings, "persona_extract_job_lease_seconds", 180.0)
        )
        while self._should_run_worker():
            now = time.monotonic()
            if now - self._last_offline_cleanup_at >= 60:
                self._last_offline_cleanup_at = now
                try:
                    await asyncio.to_thread(
                        cleanup_expired_offline_exports,
                        self._ctx.settings,
                    )
                except Exception as exc:
                    logger.warning(
                        "persona_extract.offline_cleanup_failed",
                        error_type=exc.__class__.__name__,
                    )
            try:
                job = await self._store.claim_next_job(
                    claim_owner=self._worker_owner,
                    lease_seconds=lease_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "persona_extract.queue_claim_failed",
                    error_type=exc.__class__.__name__,
                )
                await self._wait_for_work()
                continue
            if job is None:
                await self._wait_for_work()
                continue
            await self._run_claimed_job(job)

    async def _lease_heartbeat(
        self,
        *,
        job_id: int,
        run_attempt: int,
        claim_owner: str,
        execution_task: asyncio.Task[Any],
        lease_lost: asyncio.Event,
    ) -> None:
        assert self._store is not None
        assert self._ctx is not None
        lease_seconds = float(
            getattr(self._ctx.settings, "persona_extract_job_lease_seconds", 180.0)
        )
        configured_interval = float(
            getattr(
                self._ctx.settings,
                "persona_extract_job_heartbeat_seconds",
                30.0,
            )
        )
        interval = max(1.0, min(configured_interval, lease_seconds / 3.0))
        while True:
            await asyncio.sleep(interval)
            renewed = await self._store.renew_job_lease(
                job_id,
                run_attempt=run_attempt,
                claim_owner=claim_owner,
                lease_seconds=lease_seconds,
            )
            if renewed:
                continue
            logger.warning(
                "persona_extract.lease_lost",
                job_id=job_id,
                run_attempt=run_attempt,
            )
            lease_lost.set()
            execution_task.cancel()
            return

    async def _run_claimed_job(self, job: dict[str, Any]) -> None:
        assert self._store is not None
        assert self._ctx is not None
        job_id = int(job["id"])
        run_attempt = int(job.get("run_attempt") or 0)
        claim_owner = str(job.get("claim_owner") or self._worker_owner)
        self._active_claim = (job_id, run_attempt, claim_owner)
        execution_task = asyncio.current_task()
        assert execution_task is not None
        lease_lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._lease_heartbeat(
                job_id=job_id,
                run_attempt=run_attempt,
                claim_owner=claim_owner,
                execution_task=execution_task,
                lease_lost=lease_lost,
            ),
            name=f"persona-heartbeat-{job_id}-{run_attempt}",
        )
        try:
            if not await self._execution_allowed(job):
                await self._store.fail_claimed_job(
                    job_id,
                    run_attempt=run_attempt,
                    claim_owner=claim_owner,
                    error="job stopped because persona_extract is disabled",
                    transient=False,
                )
                return
            checkpoint = (
                job.get("checkpoint")
                if isinstance(job.get("checkpoint"), dict)
                else {}
            )
            if str(checkpoint.get("workflow") or "") == "offline_export":
                await self._store.prepare_offline_export(
                    job_id,
                    run_attempt=run_attempt,
                    claim_owner=claim_owner,
                    execution_allowed=lambda: self._execution_allowed(job),
                )
                return
            messages = await self._store.get_job_input_messages(job_id)
            if not messages:
                messages = await self._store.collect_messages_for_job(job_id)
                if messages and not await self._store.persist_job_input_messages(
                    job_id,
                    run_attempt=run_attempt,
                    claim_owner=claim_owner,
                    messages=messages,
                ):
                    raise PersonaJobLeaseLost("persona job lease was lost")
            if not messages:
                raise ValueError("no messages found")
            if self._ctx.container.llm_service is None:
                raise RuntimeError("persona_extract_llm_unavailable")
            await self._store.run_extraction(
                job_id,
                messages,
                self._ctx.container.llm_service,
                run_attempt=run_attempt,
                claim_owner=claim_owner,
                execution_allowed=lambda: self._execution_allowed(job),
            )
        except PersonaJobCancelled:
            logger.info("persona_extract.job_cancelled", job_id=job_id)
        except PersonaJobLeaseLost:
            logger.warning(
                "persona_extract.stale_worker_discarded",
                job_id=job_id,
                run_attempt=run_attempt,
            )
        except asyncio.CancelledError:
            if lease_lost.is_set():
                logger.warning(
                    "persona_extract.lease_lost_execution_cancelled",
                    job_id=job_id,
                    run_attempt=run_attempt,
                )
                return
            try:
                await self._finish_persistent_operation(
                    self._store.release_claimed_job(
                        job_id,
                        run_attempt=run_attempt,
                        claim_owner=claim_owner,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "persona_extract.release_failed",
                    job_id=job_id,
                    error_type=exc.__class__.__name__,
                )
            raise
        except Exception as exc:
            status = await self._store.fail_claimed_job(
                job_id,
                run_attempt=run_attempt,
                claim_owner=claim_owner,
                error=str(exc).strip() or "persona extraction failed",
                transient=_is_transient_llm_error(exc),
            )
            logger.warning(
                "persona_extract.job_failed",
                job_id=job_id,
                run_attempt=run_attempt,
                status=status or "lease_lost",
                error_type=exc.__class__.__name__,
            )
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            if self._active_claim == (job_id, run_attempt, claim_owner):
                self._active_claim = None

    async def scope_execution_allowed(
        self,
        tenant_id: str,
        session_id: str,
    ) -> bool:
        registry = (
            getattr(self._ctx.container, "plugin_registry", None)
            if self._ctx is not None
            else None
        )
        tenant = str(tenant_id or "").strip()
        session = str(session_id or "").strip()
        if not tenant:
            return False
        gate = getattr(registry, "scope_execution_allowed", None)
        if callable(gate):
            try:
                return (
                    await gate(
                        self.meta.name,
                        tenant_id=tenant,
                        session_id=session,
                    )
                    is True
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "persona_extract.execution_gate_error",
                    tenant_id=tenant,
                    session_id=session,
                    error_type=exc.__class__.__name__,
                )
                return False
        logger.warning(
            "persona_extract.scope_execution_gate_missing",
            tenant_id=tenant,
            session_id=session,
        )
        return False

    async def _execution_allowed(self, job: dict[str, Any]) -> bool:
        return await self.scope_execution_allowed(
            str(job.get("tenant_id") or ""),
            str(job.get("session_id") or ""),
        )

    async def _history_execution_allowed(
        self,
        tenant_id: str,
        session_id: str,
    ) -> bool:
        registry = (
            getattr(self._ctx.container, "plugin_registry", None)
            if self._ctx is not None
            else None
        )
        gate = getattr(registry, "owners_scope_execution_allowed", None)
        if not callable(gate):
            logger.warning(
                "persona_extract.history_execution_gate_missing",
                tenant_id=str(tenant_id or "").strip(),
                session_id=str(session_id or "").strip(),
            )
            return False
        try:
            return (
                await gate(
                    (self.meta.name, "wxbot"),
                    tenant_id=str(tenant_id or "").strip(),
                    session_id=str(session_id or "").strip(),
                )
                is True
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "persona_extract.history_execution_gate_error",
                tenant_id=str(tenant_id or "").strip(),
                session_id=str(session_id or "").strip(),
                error_type=exc.__class__.__name__,
            )
            return False

    async def initialize(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._store = PersonaExtractStore(
            ctx.settings,
            history_scope_gate=self._history_execution_allowed,
        )
        await self._store.ensure_tables()
        await self._store.fail_stale_running_jobs()
        self._accept_jobs = True
        self._ensure_worker()

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            self._accept_jobs = False
            await self._stop_worker()
            self._store = None
            self._ctx = None

    async def on_enable(self, scope=None) -> None:
        _ = scope
        async with self._lifecycle_lock:
            self._accept_jobs = self._store is not None and self._ctx is not None
            self._ensure_worker()

    async def on_disable(self, scope=None) -> None:
        _ = scope
        async with self._lifecycle_lock:
            self._accept_jobs = False
            await self._stop_worker()

    async def schedule_job(
        self,
        job_id: int,
        messages: list[dict] | None = None,
    ) -> dict | None:
        _ = messages  # Inputs are persisted before enqueue; never rely on RAM.
        if self._store is None or self._ctx is None or not self._accept_jobs:
            return None
        self._worker_wakeup.set()
        self._ensure_worker()
        return await self._store.get_job(job_id)

    def is_accepting_jobs(self) -> bool:
        return bool(
            self._accept_jobs
            and self._store is not None
            and self._ctx is not None
        )

    def get_api_router(self):
        if self._store is None:
            return None
        return build_persona_extract_router(
            self._store,
            self,
            scope_gate=self.scope_execution_allowed,
        )

    def get_pipeline_hooks(self):
        if self._store is None:
            return []
        return [PersonaSkillHook(self._store)]

    def get_flow_steps(self) -> list[FlowStepDefinition]:
        return [
            FlowStepDefinition(
                kind="plugin.persona_extract.skill_enrich",
                owner=self.meta.name,
                name="Persona skill enrich",
                permissions=["storage:shared"],
                inputs={"event", "session", "pre", "route"},
                outputs={"signals.persona.skill"},
                timeout_seconds=1.5,
                error_policy="fail_open",
            )
        ]

    def get_flow_executors(self):
        if self._store is None:
            return {}
        return {
            "plugin.persona_extract.skill_enrich": PersonaSkillEnrichStep(self._store)
        }

    def get_permissions(self) -> list[str]:
        return [
            "network:wxbot",
            "storage:shared",
            "hooks:pipeline",
            "admin_api",
        ]


plugin = PersonaExtractPlugin()
