from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from app.common.config import Settings
from app.common.logging import configure_logging, get_logger
from app.container import SchedulerContainer, set_container
from app.infra.redis_client import get_redis
from app.plugin.registry import PluginRegistry
from app.workers.heartbeat import WorkerHeartbeat
from app.workers.readiness import probe_role_dependencies
from app.workers.runtime import run_worker_process, worker_process_settings
from app.workers.scheduler_lease import (
    SchedulerLeaderLease,
    SchedulerLeaseLostError,
)

log = get_logger(__name__)


class SchedulerWorker:
    """Runs every periodic application job behind one renewable leader lease."""

    def __init__(
        self,
        lease: SchedulerLeaderLease,
        plugin_registry: PluginRegistry,
        settings: Settings,
        *,
        worker_id: str | None = None,
    ) -> None:
        self._lease = lease
        self._plugin_registry = plugin_registry
        self._settings = settings
        self._worker_id = worker_id or (
            f"scheduler-{settings.resolved_worker_instance_id}:{lease.token}"
        )
        self._stop = asyncio.Event()
        self._memory_job_drain_claimed = 0
        self._memory_job_drain_lock = asyncio.Lock()
        self._wxbot_group_summary_drain_lock = asyncio.Lock()
        self._draw_recovery_lock = asyncio.Lock()
        self._draw_queue_lock = asyncio.Lock()
        self._initialized = False

    def _loaded_plugin(self, name: str) -> object | None:
        plugins = self._plugin_registry.loaded_plugins
        if not isinstance(plugins, dict):
            return None
        initialization_order = getattr(
            self._plugin_registry,
            "initialization_order",
            None,
        )
        if isinstance(initialization_order, tuple | list) and name not in initialization_order:
            return None
        return plugins.get(name)

    async def _global_owner_allowed(self, owner: str) -> bool:
        """Fail closed on every direct scheduler-to-plugin invocation."""

        gate = getattr(self._plugin_registry, "global_execution_allowed", None)
        if not callable(gate):
            log.error(
                "scheduler.plugin_owner_gate_missing",
                plugin=owner,
            )
            return False
        try:
            allowed = await gate(owner)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "scheduler.plugin_owner_gate_failed",
                plugin=owner,
                error_type=exc.__class__.__name__,
            )
            return False
        if not isinstance(allowed, bool):
            log.error(
                "scheduler.plugin_owner_gate_invalid",
                plugin=owner,
                result_type=type(allowed).__name__,
            )
            return False
        if not allowed:
            log.info(
                "scheduler.plugin_owner_skipped",
                plugin=owner,
            )
        return allowed

    async def _scope_owner_allowed(
        self,
        owner: str,
        tenant_id: str,
        session_id: str = "",
    ) -> bool:
        """Fail closed when a durable tenant/session owner gate is unavailable."""

        gate = getattr(self._plugin_registry, "scope_execution_allowed", None)
        if not callable(gate):
            log.error(
                "scheduler.plugin_scope_gate_missing",
                plugin=owner,
                tenant_id=tenant_id,
                session_id=session_id,
            )
            return False
        try:
            allowed = await gate(
                owner,
                tenant_id=str(tenant_id or ""),
                session_id=str(session_id or ""),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "scheduler.plugin_scope_gate_failed",
                plugin=owner,
                tenant_id=tenant_id,
                session_id=session_id,
                error_type=exc.__class__.__name__,
            )
            return False
        if not isinstance(allowed, bool):
            log.error(
                "scheduler.plugin_scope_gate_invalid",
                plugin=owner,
                tenant_id=tenant_id,
                session_id=session_id,
                result_type=type(allowed).__name__,
            )
            return False
        return allowed

    def _scope_gate(self, owner: str) -> Callable[[str, str], Awaitable[bool]]:
        async def allowed(tenant_id: str, session_id: str = "") -> bool:
            return await self._scope_owner_allowed(
                owner,
                tenant_id,
                session_id,
            )

        return allowed

    def _memory_job_drain_enabled(self) -> bool:
        return bool(
            self._settings.memory_llm_extraction_enabled
            and self._settings.memory_llm_extraction_job_enabled
            and self._settings.memory_llm_extraction_job_drain_enabled
        )

    def _wxbot_group_summary_enabled(self) -> bool:
        return bool(self._settings.wxbot_group_summary_enabled)

    def _draw_task_recovery_enabled(self) -> bool:
        return bool(self._settings.draw_task_recovery_enabled)

    def _draw_task_queue_worker_enabled(self) -> bool:
        return bool(self._settings.draw_task_queue_worker_enabled)

    def _memory_job_drain_remaining_claims(self) -> int | None:
        max_claims = int(
            self._settings.memory_llm_extraction_job_drain_max_claims
        )
        if max_claims <= 0:
            return None
        return max(0, max_claims - self._memory_job_drain_claimed)

    async def _require_job_method(
        self,
        plugin_name: str,
        method_name: str,
    ) -> None:
        if not await self._global_owner_allowed(plugin_name):
            return
        method = getattr(self._loaded_plugin(plugin_name), method_name, None)
        if not callable(method):
            raise RuntimeError(
                f"scheduler job requires active plugin method "
                f"{plugin_name}.{method_name}"
            )

    async def _validate_jobs(self) -> None:
        if self._memory_job_drain_enabled():
            await self._require_job_method("memory", "drain_extraction_jobs")
        if self._wxbot_group_summary_enabled():
            await self._require_job_method("wxbot", "drain_group_summary_jobs")
        if self._draw_task_recovery_enabled():
            await self._require_job_method("draw", "recover_stale_tasks")
        if self._draw_task_queue_worker_enabled():
            await self._require_job_method("draw", "drain_queued_tasks")

    async def _drain_memory_jobs(self) -> None:
        if not self._memory_job_drain_enabled():
            return
        async with self._memory_job_drain_lock:
            remaining_claims = self._memory_job_drain_remaining_claims()
            if remaining_claims == 0:
                return
            if not await self._global_owner_allowed("memory"):
                return
            drain = getattr(
                self._loaded_plugin("memory"),
                "drain_extraction_jobs",
                None,
            )
            if not callable(drain):
                raise RuntimeError("memory extraction scheduler disappeared")
            batch_size = self._settings.memory_llm_extraction_job_drain_batch_size
            if remaining_claims is not None:
                batch_size = min(batch_size, remaining_claims)
            try:
                result = await drain(
                    limit=batch_size,
                    worker_id=self._worker_id,
                    scope_allowlist=(
                        self._settings.memory_llm_extraction_job_scope_allowlist
                    ),
                    scope_execution_allowed=self._scope_gate("memory"),
                )
                if remaining_claims is not None:
                    claimed = (
                        int(result.get("claimed", 0) or 0)
                        if isinstance(result, dict)
                        else 0
                    )
                    self._memory_job_drain_claimed += max(0, claimed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "scheduler.memory_job_drain_failed",
                    error_type=exc.__class__.__name__,
                )

    async def _drain_wxbot_group_summary_jobs(self) -> None:
        if not self._wxbot_group_summary_enabled():
            return
        async with self._wxbot_group_summary_drain_lock:
            if not await self._global_owner_allowed("wxbot"):
                return
            drain = getattr(
                self._loaded_plugin("wxbot"),
                "drain_group_summary_jobs",
                None,
            )
            if not callable(drain):
                raise RuntimeError("wxbot group summary scheduler disappeared")
            try:
                await drain(
                    limit=1,
                    worker_id=self._worker_id,
                    scope_execution_allowed=self._scope_gate("wxbot"),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "scheduler.wxbot_group_summary_drain_failed",
                    error_type=exc.__class__.__name__,
                )

    async def _recover_draw_tasks(self) -> None:
        if not self._draw_task_recovery_enabled():
            return
        async with self._draw_recovery_lock:
            if not await self._global_owner_allowed("draw"):
                return
            recover = getattr(
                self._loaded_plugin("draw"),
                "recover_stale_tasks",
                None,
            )
            if not callable(recover):
                raise RuntimeError("draw recovery scheduler disappeared")
            try:
                await recover(
                    stale_seconds=self._settings.draw_task_stale_seconds,
                    worker_id=self._worker_id,
                    scope_execution_allowed=self._scope_gate("draw"),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "scheduler.draw_task_recovery_failed",
                    error_type=exc.__class__.__name__,
                )

    async def _drain_draw_task_queue(self) -> None:
        if not self._draw_task_queue_worker_enabled():
            return
        async with self._draw_queue_lock:
            if not await self._global_owner_allowed("draw"):
                return
            drain = getattr(
                self._loaded_plugin("draw"),
                "drain_queued_tasks",
                None,
            )
            if not callable(drain):
                raise RuntimeError("draw queue scheduler disappeared")
            try:
                await drain(
                    worker_id=self._worker_id,
                    batch_size=self._settings.draw_task_queue_batch_size,
                    lock_ttl_seconds=self._settings.draw_task_lock_ttl_seconds,
                    auto_retry_enabled=self._settings.draw_task_auto_retry_enabled,
                    max_retries=self._settings.draw_task_max_retries,
                    retry_backoff_seconds=(
                        self._settings.draw_task_retry_backoff_seconds
                    ),
                    scope_execution_allowed=self._scope_gate("draw"),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "scheduler.draw_task_queue_failed",
                    error_type=exc.__class__.__name__,
                )

    async def _wait_interval(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.1, seconds))
        except TimeoutError:
            return False
        return True

    async def _run_periodic(
        self,
        operation: Callable[[], Awaitable[None]],
        *,
        interval_seconds: float,
        run_immediately: bool,
    ) -> None:
        if not run_immediately and await self._wait_interval(interval_seconds):
            return
        while not self._stop.is_set():
            await operation()
            if await self._wait_interval(interval_seconds):
                return

    async def _await_while_leader(self, operation: Awaitable[None]) -> None:
        operation_task: asyncio.Future[None] = asyncio.ensure_future(operation)
        lease_task = asyncio.create_task(self._lease.lost.wait())
        try:
            done, _ = await asyncio.wait(
                {operation_task, lease_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if lease_task in done and self._lease.lost.is_set():
                operation_task.cancel()
                await asyncio.gather(operation_task, return_exceptions=True)
                raise SchedulerLeaseLostError(
                    "scheduler leader lease was lost during initialization"
                )
            await operation_task
        finally:
            if not lease_task.done():
                lease_task.cancel()
            await asyncio.gather(lease_task, return_exceptions=True)

    async def initialize(self) -> None:
        if self._initialized:
            return
        # The lease is acquired before the container is built. It may be lost
        # during a slow startup, in which case this process must never become
        # ready or start scheduled work.
        if self._lease.lost.is_set():
            raise SchedulerLeaseLostError(
                "scheduler leader lease was lost during initialization"
            )
        await self._validate_jobs()
        # Recover durable work before readiness is advertised. Each call is
        # cancelled immediately if leadership changes during startup.
        if self._draw_task_recovery_enabled():
            await self._await_while_leader(self._recover_draw_tasks())
        if self._draw_task_queue_worker_enabled():
            await self._await_while_leader(self._drain_draw_task_queue())
        if self._wxbot_group_summary_enabled():
            await self._await_while_leader(
                self._drain_wxbot_group_summary_jobs()
            )
        self._initialized = True

    async def run(self) -> None:
        await self.initialize()
        job_tasks: list[asyncio.Task[None]] = []
        if self._memory_job_drain_enabled():
            job_tasks.append(
                asyncio.create_task(
                    self._run_periodic(
                        self._drain_memory_jobs,
                        interval_seconds=(
                            self._settings.memory_llm_extraction_job_drain_interval_seconds
                        ),
                        run_immediately=True,
                    ),
                    name="scheduler-memory-job-drain",
                )
            )
        if self._wxbot_group_summary_enabled():
            job_tasks.append(
                asyncio.create_task(
                    self._run_periodic(
                        self._drain_wxbot_group_summary_jobs,
                        interval_seconds=(
                            self._settings.wxbot_group_summary_drain_interval_seconds
                        ),
                        run_immediately=True,
                    ),
                    name="scheduler-wxbot-group-summary-drain",
                )
            )
        if self._draw_task_recovery_enabled():
            job_tasks.append(
                asyncio.create_task(
                    self._run_periodic(
                        self._recover_draw_tasks,
                        interval_seconds=(
                            self._settings.draw_task_recovery_interval_seconds
                        ),
                        run_immediately=False,
                    ),
                    name="scheduler-draw-task-recovery",
                )
            )
        if self._draw_task_queue_worker_enabled():
            job_tasks.append(
                asyncio.create_task(
                    self._run_periodic(
                        self._drain_draw_task_queue,
                        interval_seconds=(
                            self._settings.draw_task_queue_interval_seconds
                        ),
                        run_immediately=True,
                    ),
                    name="scheduler-draw-task-queue",
                )
            )
        stop_wait = asyncio.create_task(
            self._stop.wait(),
            name="scheduler-stop-wait",
        )
        lease_wait = asyncio.create_task(
            self._lease.lost.wait(),
            name="scheduler-lease-wait",
        )
        try:
            wait_for = {stop_wait, lease_wait, *job_tasks}
            done, _ = await asyncio.wait(
                wait_for,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if lease_wait in done and self._lease.lost.is_set():
                raise SchedulerLeaseLostError(
                    "scheduler stopped because its leader lease was lost"
                )
            completed_jobs = [task for task in job_tasks if task in done]
            if completed_jobs and not self._stop.is_set():
                task = completed_jobs[0]
                error = task.exception()
                if error is not None:
                    raise RuntimeError(
                        f"scheduled job loop {task.get_name()} failed"
                    ) from error
                raise RuntimeError(
                    f"scheduled job loop {task.get_name()} exited unexpectedly"
                )
        finally:
            self._stop.set()
            for task in (stop_wait, lease_wait, *job_tasks):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                stop_wait,
                lease_wait,
                *job_tasks,
                return_exceptions=True,
            )

    async def stop(self) -> None:
        self._stop.set()


async def run_scheduler_worker() -> None:
    with worker_process_settings("scheduler") as settings:
        from app.main import build_container

        configure_logging()
        redis = get_redis()
        lease = SchedulerLeaderLease(
            redis=redis,
            key=settings.scheduler_lease_key,
            ttl_seconds=settings.scheduler_lease_ttl_seconds,
            acquire_timeout_seconds=settings.scheduler_lease_acquire_timeout_seconds,
            poll_interval_seconds=settings.scheduler_lease_poll_interval_seconds,
        )
        await lease.acquire()

        container: SchedulerContainer | None = None
        try:
            built_container = await build_container(settings)
            if not isinstance(built_container, SchedulerContainer):
                raise RuntimeError("scheduler worker requires a SchedulerContainer")
            container = built_container
            set_container(container)
            worker = SchedulerWorker(
                lease,
                container.plugin_registry,
                settings,
            )
            log.info("scheduler.starting")
            await run_worker_process(
                "scheduler",
                initialize=worker.initialize,
                run=worker.run,
                stop=worker.stop,
                container=container,
                shutdown_timeout_seconds=settings.worker_shutdown_timeout_seconds,
                before_cleanup=lease.release,
                heartbeat=WorkerHeartbeat.from_settings(redis, settings),
                readiness_check=lambda: probe_role_dependencies(
                    "scheduler",
                    settings,
                    redis=redis,
                ),
                readiness_interval_seconds=settings.worker_heartbeat_interval_seconds,
                metrics_host=settings.worker_metrics_host,
                metrics_port=settings.worker_metrics_port,
            )
        finally:
            if container is None:
                await lease.release()


def main() -> None:
    asyncio.run(run_scheduler_worker())


if __name__ == "__main__":
    main()
