from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace

import pytest

import plugins.persona_extract.store as persona_store_module
from app.plugin.base import PluginContext
from plugins.draw.plugin import DrawPlugin
from plugins.group_activity.plugin import GroupActivityPlugin
from plugins.persona_extract.plugin import PersonaExtractPlugin
from plugins.persona_extract.router import _schedule_job
from plugins.persona_extract.store import PersonaExtractStore
from plugins.tibo_reset.plugin import TiboResetPlugin
from plugins.wxbot.plugin import WxbotPlugin


def _context(
    *,
    role: str = "scheduler",
    db_ok: bool = True,
    plugin_registry: object | None = None,
) -> PluginContext:
    return PluginContext(
        container=SimpleNamespace(
            capabilities={},
            channel_registry=None,
            plugin_registry=plugin_registry,
            llm_service=object(),
        ),
        settings=SimpleNamespace(
            app_process_role=role,
            persona_extract_worker_roles="api,scheduler",
            persona_extract_job_poll_interval_seconds=0.01,
            persona_extract_job_lease_seconds=180.0,
            persona_extract_job_heartbeat_seconds=30.0,
            tibo_reset_poll_interval_seconds=30,
        ),
        db_ok=db_ok,
        redis_ok=False,
    )


async def _wait_until(
    predicate: Callable[[], bool],
    *,
    attempts: int = 20,
    delay: float = 0.0,
) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(delay)
    raise AssertionError("condition was not reached")


class _ExecutionGate:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.calls: list[str] = []
        self.scope_calls: list[tuple[str, str, str]] = []

    async def global_execution_allowed(self, owner: str) -> bool:
        self.calls.append(owner)
        return self.enabled

    async def scope_execution_allowed(
        self,
        owner: str,
        *,
        tenant_id: str,
        session_id: str,
    ) -> bool:
        self.calls.append(owner)
        self.scope_calls.append((owner, tenant_id, session_id))
        return self.enabled


class _ActivatingExecutionGate(_ExecutionGate):
    def __init__(self) -> None:
        super().__init__()
        self.active = False

    def is_active(self, owner: str) -> bool:
        _ = owner
        return self.active


class _BlockingSchedulerService:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        self.calls = 0

    async def process_due_sessions(self) -> None:
        await self._run()

    async def poll_once(self) -> None:
        await self._run()

    async def _run(self) -> None:
        self.calls += 1
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class _CloseTrackingClient:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_group_activity_disable_finishes_current_persistence_then_restarts() -> None:
    plugin = GroupActivityPlugin()
    service = _BlockingSchedulerService()
    plugin._ctx = _context()
    plugin._service = service  # type: ignore[assignment]

    await plugin.on_enable()
    await asyncio.wait_for(service.started.wait(), timeout=1)
    disable = asyncio.create_task(plugin.on_disable())
    await asyncio.sleep(0)

    assert disable.done() is False
    assert service.cancelled is False

    service.release.set()
    await asyncio.wait_for(disable, timeout=1)
    assert (await plugin.get_runtime_status())["running"] is False

    await plugin.on_enable()
    await asyncio.sleep(0)
    assert (await plugin.get_runtime_status())["running"] is True
    await plugin.on_disable()
    await plugin.shutdown()
    await plugin.shutdown()


@pytest.mark.asyncio
async def test_group_activity_concurrent_disable_then_enable_keeps_new_generation() -> None:
    plugin = GroupActivityPlugin()
    service = _BlockingSchedulerService()
    plugin._ctx = _context()
    plugin._service = service  # type: ignore[assignment]

    await plugin.on_enable()
    await asyncio.wait_for(service.started.wait(), timeout=1)
    disable = asyncio.create_task(plugin.on_disable())
    await asyncio.sleep(0)
    enable = asyncio.create_task(plugin.on_enable())
    await asyncio.sleep(0)

    assert disable.done() is False
    assert enable.done() is False

    service.release.set()
    await asyncio.wait_for(asyncio.gather(disable, enable), timeout=1)
    assert plugin._scheduler_enabled is True
    assert (await plugin.get_runtime_status())["running"] is True

    await plugin.on_disable()


@pytest.mark.asyncio
async def test_group_activity_cancelled_disable_settles_busy_iteration() -> None:
    plugin = GroupActivityPlugin()
    service = _BlockingSchedulerService()
    plugin._ctx = _context()
    plugin._service = service  # type: ignore[assignment]

    await plugin.on_enable()
    await asyncio.wait_for(service.started.wait(), timeout=1)
    disable = asyncio.create_task(plugin.on_disable())
    await _wait_until(lambda: not plugin._scheduler_enabled)

    disable.cancel()
    await asyncio.sleep(0)
    disable.cancel()
    await asyncio.sleep(0)

    assert disable.done() is False
    assert service.cancelled is False
    assert plugin._background_tasks

    service.release.set()
    with pytest.raises(asyncio.CancelledError):
        await disable

    assert plugin._background_tasks == {}
    assert plugin._scheduler_busy is False
    await plugin.shutdown()


@pytest.mark.asyncio
async def test_tibo_disable_finishes_current_poll_and_shutdown_is_idempotent() -> None:
    plugin = TiboResetPlugin()
    service = _BlockingSchedulerService()
    plugin._ctx = _context()
    plugin._service = service  # type: ignore[assignment]

    await plugin.on_enable()
    await asyncio.wait_for(service.started.wait(), timeout=1)
    disable = asyncio.create_task(plugin.on_disable())
    await asyncio.sleep(0)

    assert disable.done() is False
    assert service.cancelled is False

    service.release.set()
    await asyncio.wait_for(disable, timeout=1)
    assert plugin._scheduler_task is None
    assert plugin._scheduler_enabled is False

    await plugin.on_enable()
    await asyncio.sleep(0)
    assert plugin._scheduler_task is not None
    await plugin.on_disable()
    await plugin.shutdown()
    await plugin.shutdown()


@pytest.mark.asyncio
async def test_tibo_cancelled_shutdown_settles_poll_before_closing_client() -> None:
    plugin = TiboResetPlugin()
    service = _BlockingSchedulerService()
    client = _CloseTrackingClient()
    plugin._ctx = _context()
    plugin._service = service  # type: ignore[assignment]
    plugin._client = client  # type: ignore[assignment]

    await plugin.on_enable()
    await asyncio.wait_for(service.started.wait(), timeout=1)
    shutdown = asyncio.create_task(plugin.shutdown())
    await _wait_until(lambda: not plugin._scheduler_enabled)

    shutdown.cancel()
    await asyncio.sleep(0)
    shutdown.cancel()
    await asyncio.sleep(0)

    assert shutdown.done() is False
    assert service.cancelled is False
    assert client.closed is False

    service.release.set()
    with pytest.raises(asyncio.CancelledError):
        await shutdown

    assert plugin._scheduler_task is None
    assert plugin._scheduler_busy is False
    assert plugin._service is service
    assert client.closed is False

    await plugin.shutdown()
    assert client.closed is True


@pytest.mark.asyncio
async def test_scheduler_plugins_do_not_restart_without_database() -> None:
    group = GroupActivityPlugin()
    group._ctx = _context(db_ok=False)
    group._service = _BlockingSchedulerService()  # type: ignore[assignment]
    await group.on_enable()

    tibo = TiboResetPlugin()
    tibo._ctx = _context(db_ok=False)
    tibo._service = _BlockingSchedulerService()  # type: ignore[assignment]
    await tibo.on_enable()

    wxbot = WxbotPlugin()
    wxbot._ctx = _context(db_ok=False)
    await wxbot.on_enable()

    assert group._background_tasks == {}
    assert tibo._scheduler_task is None
    assert wxbot._background_tasks == {}

    await group.shutdown()
    await tibo.shutdown()
    await wxbot.shutdown()


@pytest.mark.asyncio
async def test_group_and_tibo_stop_when_durable_execution_gate_is_disabled() -> None:
    gate = _ExecutionGate(enabled=False)

    group = GroupActivityPlugin()
    group_service = _BlockingSchedulerService()
    group._ctx = _context(plugin_registry=gate)
    group._service = group_service  # type: ignore[assignment]
    await group.on_enable()

    tibo = TiboResetPlugin()
    tibo_service = _BlockingSchedulerService()
    tibo._ctx = _context(plugin_registry=gate)
    tibo._service = tibo_service  # type: ignore[assignment]
    await tibo.on_enable()

    await _wait_until(
        lambda: not group._background_tasks
        and tibo._scheduler_task is not None
        and tibo._scheduler_task.done()
    )

    assert group_service.calls == 0
    assert tibo_service.calls == 0
    assert gate.calls.count("group_activity") == 1
    assert gate.calls.count("tibo_reset") == 1
    assert group._scheduler_enabled is False
    assert tibo._scheduler_enabled is False

    await group.shutdown()
    await tibo.shutdown()


class _WxbotSubscriptionStore:
    settings = SimpleNamespace(wxbot_default_tenant_id="default")

    def __init__(self) -> None:
        self.report_reads = 0
        self.self_review_reads = 0

    async def list_enabled_report_subscriptions(self, tenant_id: str) -> list[dict]:
        assert tenant_id == "default"
        self.report_reads += 1
        return []

    async def list_report_deliveries_to_reconcile(
        self,
        tenant_id: str,
        *,
        limit: int = 100,
    ) -> list[dict]:
        assert tenant_id == "default"
        assert limit in {1, 100}
        return []

    async def list_enabled_self_review_subscriptions(self, tenant_id: str) -> list[dict]:
        assert tenant_id == "default"
        self.self_review_reads += 1
        return []


@pytest.mark.asyncio
async def test_wxbot_peer_scheduler_stops_after_durable_disable_gate_closes() -> None:
    gate = _ExecutionGate()
    store = _WxbotSubscriptionStore()
    plugin = WxbotPlugin()
    plugin._ctx = _context(plugin_registry=gate)
    plugin._store = store  # type: ignore[assignment]

    await plugin.on_enable()
    await _wait_until(lambda: store.report_reads == 1 and store.self_review_reads == 1)
    assert sorted(plugin._background_tasks) == [
        "report-subscription-scheduler",
        "self-review-subscription-scheduler",
    ]

    gate.enabled = False
    plugin.notify_report_scheduler()
    plugin.notify_self_review_scheduler()
    await _wait_until(lambda: not plugin._background_tasks)

    assert plugin._background_enabled is False
    assert gate.calls.count("wxbot") >= 3
    assert store.report_reads == 1
    assert store.self_review_reads == 1

    await plugin.shutdown()
    await plugin.shutdown()


@pytest.mark.asyncio
async def test_internal_schedulers_wait_for_registry_activation_before_work() -> None:
    gate = _ActivatingExecutionGate()

    group = GroupActivityPlugin()
    group_service = _BlockingSchedulerService()
    group._ctx = _context(plugin_registry=gate)
    group._service = group_service  # type: ignore[assignment]

    tibo = TiboResetPlugin()
    tibo_service = _BlockingSchedulerService()
    tibo._ctx = _context(plugin_registry=gate)
    tibo._service = tibo_service  # type: ignore[assignment]

    store = _WxbotSubscriptionStore()
    wxbot = WxbotPlugin()
    wxbot._ctx = _context(plugin_registry=gate)
    wxbot._store = store  # type: ignore[assignment]

    await group.on_enable()
    await tibo.on_enable()
    await wxbot.on_enable()
    await asyncio.sleep(0.1)

    assert group_service.calls == 0
    assert tibo_service.calls == 0
    assert store.report_reads == 0
    assert store.self_review_reads == 0
    assert gate.calls == []

    gate.active = True
    await asyncio.wait_for(group_service.started.wait(), timeout=1)
    await asyncio.wait_for(tibo_service.started.wait(), timeout=1)
    await _wait_until(
        lambda: store.report_reads == 1 and store.self_review_reads == 1,
        attempts=100,
        delay=0.01,
    )

    group_service.release.set()
    tibo_service.release.set()
    await group.on_disable()
    await tibo.on_disable()
    await wxbot.on_disable()


@pytest.mark.asyncio
async def test_wxbot_disable_cancels_loops_and_enable_restarts_them() -> None:
    plugin = WxbotPlugin()
    plugin._ctx = _context()
    started: list[str] = []

    async def report_loop(_stop_event: asyncio.Event | None = None) -> None:
        started.append("report")
        await asyncio.Event().wait()

    async def review_loop(_stop_event: asyncio.Event | None = None) -> None:
        started.append("review")
        await asyncio.Event().wait()

    plugin._report_subscription_scheduler_loop = report_loop  # type: ignore[method-assign]
    plugin._self_review_subscription_scheduler_loop = review_loop  # type: ignore[method-assign]

    await plugin.on_enable()
    await asyncio.sleep(0)
    assert sorted(plugin._background_tasks) == [
        "report-subscription-scheduler",
        "self-review-subscription-scheduler",
    ]

    await plugin.on_disable()
    assert plugin._background_tasks == {}
    assert await plugin.schedule_background("disabled", report_loop) is False

    await plugin.on_enable()
    await asyncio.sleep(0)
    assert started.count("report") == 2
    assert started.count("review") == 2
    await plugin.shutdown()
    await plugin.shutdown()


@pytest.mark.asyncio
async def test_wxbot_background_task_failure_is_observed_and_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = WxbotPlugin()
    plugin._background_enabled = True
    logged: list[tuple[str, dict[str, object]]] = []

    class _Logger:
        def error(self, event: str, **kwargs: object) -> None:
            logged.append((event, kwargs))

        def info(self, event: str, **kwargs: object) -> None:
            _ = event, kwargs

    async def fail() -> None:
        raise RuntimeError("scheduled failure")

    monkeypatch.setattr("plugins.wxbot.plugin.logger", _Logger())

    assert await plugin.schedule_background("failing-job", fail)
    await _wait_until(lambda: not plugin._background_tasks)

    assert logged == [
        (
            "wxbot.background_task_failed",
            {"key": "failing-job", "error_type": "RuntimeError"},
        )
    ]


@pytest.mark.asyncio
async def test_wxbot_report_send_wakes_only_for_sdk_ack_reconciliation() -> None:
    gate = _ExecutionGate()

    class _Store:
        def __init__(self) -> None:
            self.delivery_status = "pending"

        async def get_report_job(self, job_id: int) -> dict[str, object]:
            assert job_id == 1
            return {
                "id": 1,
                "tenant_id": "default",
                "session_id": "room@chatroom",
                "delivery_status": self.delivery_status,
            }

    class _ReportService:
        def __init__(self, store: _Store) -> None:
            self.store = store

        async def send_report_job(self, job_id: int) -> bool:
            assert job_id == 1
            return False

    store = _Store()
    plugin = WxbotPlugin()
    plugin._ctx = _context(plugin_registry=gate)
    plugin._store = store  # type: ignore[assignment]
    plugin._report_service = _ReportService(store)  # type: ignore[assignment]

    store.delivery_status = "failed"
    await plugin._send_report_job(1)
    assert plugin._report_scheduler_wakeup.is_set() is False

    store.delivery_status = "queued"
    await plugin._send_report_job(1)
    assert plugin._report_scheduler_wakeup.is_set() is True


@pytest.mark.asyncio
async def test_wxbot_disable_does_not_cancel_active_delivery_critical_section() -> None:
    plugin = WxbotPlugin()
    plugin._ctx = _context(role="api")
    await plugin.on_enable()
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = False

    async def persistent_operation() -> None:
        nonlocal cancelled
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled = True
            raise

    async def job() -> None:
        await plugin._await_persistent_section(persistent_operation())

    assert await plugin.schedule_background("report-send-1", job) is True
    await asyncio.wait_for(started.wait(), timeout=1)
    disable = asyncio.create_task(plugin.on_disable())
    await asyncio.sleep(0)

    assert disable.done() is False
    assert cancelled is False

    release.set()
    await asyncio.wait_for(disable, timeout=1)
    assert plugin._background_tasks == {}


@pytest.mark.asyncio
async def test_wxbot_cancelled_disable_settles_critical_jobs_before_escaping() -> None:
    plugin = WxbotPlugin()
    plugin._ctx = _context(role="api")
    await plugin.on_enable()
    started = asyncio.Event()
    release = asyncio.Event()
    operation_cancelled = False

    async def persistent_operation() -> None:
        nonlocal operation_cancelled
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            operation_cancelled = True
            raise

    async def job() -> None:
        await plugin._await_persistent_section(persistent_operation())

    assert await plugin.schedule_background("report-send-cancel-disable", job)
    await asyncio.wait_for(started.wait(), timeout=1)
    disable = asyncio.create_task(plugin.on_disable())
    await asyncio.sleep(0)
    disable.cancel()
    await asyncio.sleep(0)
    disable.cancel()
    await asyncio.sleep(0)

    assert disable.done() is False
    assert operation_cancelled is False

    release.set()
    result = await asyncio.wait_for(
        asyncio.gather(disable, return_exceptions=True),
        timeout=1,
    )
    assert isinstance(result[0], asyncio.CancelledError)
    assert plugin._background_tasks == {}


@pytest.mark.asyncio
async def test_wxbot_concurrent_disable_then_enable_is_serialized() -> None:
    plugin = WxbotPlugin()
    plugin._ctx = _context(role="api")
    await plugin.on_enable()
    started = asyncio.Event()
    release = asyncio.Event()

    async def persistent_operation() -> None:
        started.set()
        await release.wait()

    async def job() -> None:
        await plugin._await_persistent_section(persistent_operation())

    assert await plugin.schedule_background("report-send-race", job) is True
    await asyncio.wait_for(started.wait(), timeout=1)
    disable = asyncio.create_task(plugin.on_disable())
    await asyncio.sleep(0)
    enable = asyncio.create_task(plugin.on_enable())
    await asyncio.sleep(0)

    assert disable.done() is False
    assert enable.done() is False

    release.set()
    await asyncio.wait_for(asyncio.gather(disable, enable), timeout=1)
    assert plugin._background_enabled is True

    await plugin.on_disable()


@pytest.mark.asyncio
async def test_wxbot_direct_task_cancel_waits_for_delivery_ack_boundary() -> None:
    plugin = WxbotPlugin()
    plugin._ctx = _context(role="api")
    await plugin.on_enable()
    started = asyncio.Event()
    release = asyncio.Event()
    operation_cancelled = False

    async def persistent_operation() -> None:
        nonlocal operation_cancelled
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            operation_cancelled = True
            raise

    async def job() -> None:
        await plugin._await_persistent_section(persistent_operation())

    assert await plugin.schedule_background("report-send-2", job) is True
    task = plugin._background_tasks["report-send-2"]
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)

    assert task.done() is False
    assert operation_cancelled is False

    release.set()
    result = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    await plugin.on_disable()


@pytest.mark.asyncio
async def test_draw_disable_drains_accepted_jobs_without_cancelling_persistence() -> None:
    plugin = DrawPlugin()
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = False

    async def job() -> None:
        nonlocal cancelled
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled = True
            raise

    plugin._track_background_task(asyncio.create_task(job()))
    await asyncio.wait_for(started.wait(), timeout=1)
    disable = asyncio.create_task(plugin.on_disable())
    await asyncio.sleep(0)

    assert disable.done() is False
    assert cancelled is False

    release.set()
    await asyncio.wait_for(disable, timeout=1)
    assert plugin._background_tasks == set()
    await plugin.shutdown()
    await plugin.shutdown()


@pytest.mark.asyncio
async def test_draw_cancelled_disable_settles_jobs_before_cancellation_escapes() -> None:
    plugin = DrawPlugin()
    started = asyncio.Event()
    cancellation_started = asyncio.Event()
    release_settlement = asyncio.Event()

    async def job() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_started.set()
            await release_settlement.wait()
            raise

    plugin._track_background_task(asyncio.create_task(job()))
    await asyncio.wait_for(started.wait(), timeout=1)
    disable = asyncio.create_task(plugin.on_disable())
    await asyncio.sleep(0)
    disable.cancel()
    await asyncio.wait_for(cancellation_started.wait(), timeout=1)
    disable.cancel()
    await asyncio.sleep(0)

    assert disable.done() is False
    assert plugin._background_tasks

    release_settlement.set()
    with pytest.raises(asyncio.CancelledError):
        await disable

    assert plugin._background_tasks == set()


class _PersonaStore:
    def __init__(self) -> None:
        self.jobs = {
            1: {
                "id": 1,
                "tenant_id": "tenant-a",
                "session_id": "group-a",
                "status": "pending",
                "current_stage": "queued",
            },
            2: {
                "id": 2,
                "tenant_id": "tenant-a",
                "session_id": "group-a",
                "status": "pending",
                "current_stage": "queued",
            },
            3: {
                "id": 3,
                "tenant_id": "tenant-a",
                "session_id": "group-a",
                "status": "pending",
                "current_stage": "queued",
            },
        }
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.update_calls: list[tuple[int, dict[str, object]]] = []
        self.claims = 0
        self.extraction_calls = 0

    async def claim_next_job(self, *, claim_owner: str, lease_seconds: float):
        _ = lease_seconds
        for job in self.jobs.values():
            if job["status"] not in {"pending", "retry_wait"}:
                continue
            self.claims += 1
            job.update(
                status="running",
                current_stage="collecting_messages",
                run_attempt=int(job.get("run_attempt") or 0) + 1,
                claim_owner=claim_owner,
            )
            return dict(job)
        return None

    async def get_job(self, job_id: int) -> dict[str, object] | None:
        return self.jobs.get(job_id)

    async def update_job(self, job_id: int, **values: object) -> None:
        self.update_calls.append((job_id, values))
        self.jobs[job_id].update(values)

    async def renew_job_lease(
        self,
        job_id: int,
        *,
        run_attempt: int,
        claim_owner: str,
        lease_seconds: float,
    ) -> bool:
        _ = lease_seconds
        job = self.jobs[job_id]
        return (
            job.get("status") == "running"
            and job.get("run_attempt") == run_attempt
            and job.get("claim_owner") == claim_owner
        )

    async def get_job_input_messages(self, job_id: int) -> list[dict]:
        _ = job_id
        return [{"text": "persisted"}]

    async def release_claimed_job(
        self,
        job_id: int,
        *,
        run_attempt: int,
        claim_owner: str,
    ) -> bool:
        job = self.jobs[job_id]
        if (
            job.get("status") != "running"
            or job.get("run_attempt") != run_attempt
            or job.get("claim_owner") != claim_owner
        ):
            return False
        job.update(status="pending", current_stage="interrupted", claim_owner="")
        return True

    async def fail_claimed_job(
        self,
        job_id: int,
        *,
        run_attempt: int,
        claim_owner: str,
        error: str,
        transient: bool,
    ) -> str | None:
        _ = error, transient
        job = self.jobs[job_id]
        if (
            job.get("status") != "running"
            or job.get("run_attempt") != run_attempt
            or job.get("claim_owner") != claim_owner
        ):
            return None
        job.update(status="failed", current_stage="disabled", claim_owner="")
        return "failed"

    async def run_extraction(
        self,
        job_id: int,
        messages: list[dict],
        llm_service: object,
        *,
        run_attempt: int,
        claim_owner: str,
        execution_allowed=None,
    ) -> None:
        _ = messages, llm_service, run_attempt, claim_owner
        self.extraction_calls += 1
        self.started.set()
        await self.release.wait()
        if execution_allowed is not None and not await execution_allowed():
            self.jobs[job_id].update(status="failed", current_stage="disabled")
            return
        self.jobs[job_id].update(status="completed", current_stage="completed")


@pytest.mark.asyncio
async def test_persona_store_claim_is_atomic_and_stale_recovery_has_age_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict | None]] = []
    claim_results = [[{"id": 1, "status": "running", "run_attempt": 1}], []]

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params))
        if "RETURNING *" in sql:
            return claim_results.pop(0)
        return []

    monkeypatch.setattr(persona_store_module, "_exec", fake_exec)
    store = PersonaExtractStore(
        SimpleNamespace(persona_extract_stage_timeout_seconds=720.0)
    )

    assert await store.try_start_job(1) is True
    assert await store.try_start_job(1) is False
    await store.fail_stale_running_jobs()

    claim_sql = calls[0][0]
    assert "status IN ('pending', 'retry_wait', 'failed')" in claim_sql
    stale_sql, stale_params = calls[-1]
    assert "lease_expires_at < NOW()" in stale_sql
    assert stale_params is None


@pytest.mark.asyncio
async def test_persona_disabled_router_rejects_without_requeueing_job() -> None:
    store = _PersonaStore()
    store.jobs[1].update(
        status="failed",
        current_stage="skill",
        error="previous failure",
    )
    plugin = PersonaExtractPlugin()
    plugin._store = store  # type: ignore[assignment]
    plugin._ctx = _context(role="api")

    response = await _schedule_job(
        store,  # type: ignore[arg-type]
        plugin,
        job_id=1,
        messages=[{"text": "retry"}],
        scope_gate=lambda _tenant_id, _session_id: asyncio.sleep(0, result=True),
    )

    assert response["accepted"] is False
    assert response["message"] == "plugin disabled"
    assert store.jobs[1]["status"] == "failed"
    assert store.jobs[1]["current_stage"] == "skill"
    assert store.update_calls == []


@pytest.mark.asyncio
async def test_persona_disable_cancels_jobs_durably_and_enable_accepts_again() -> None:
    plugin = PersonaExtractPlugin()
    store = _PersonaStore()
    plugin._store = store  # type: ignore[assignment]
    plugin._ctx = _context(role="api", plugin_registry=_ExecutionGate())
    plugin._accept_jobs = True

    await plugin.schedule_job(1, [{"text": "hello"}])
    await asyncio.wait_for(store.started.wait(), timeout=1)
    await plugin.on_disable()

    assert plugin._worker_task is None
    assert store.jobs[1]["status"] == "pending"
    assert store.jobs[1]["current_stage"] == "interrupted"
    assert await plugin.schedule_job(2, [{"text": "disabled"}]) is None

    store.jobs[2]["status"] = "completed"
    store.jobs[3]["status"] = "completed"
    store.started.clear()
    store.release.set()
    await plugin.on_enable()
    await plugin.schedule_job(1, [{"text": "enabled"}])
    await asyncio.wait_for(store.started.wait(), timeout=1)
    await _wait_until(lambda: store.jobs[1]["status"] == "completed", delay=0.01)
    assert plugin._accept_jobs is True
    await plugin.shutdown()
    await plugin.shutdown()


@pytest.mark.asyncio
async def test_persona_job_claim_allows_only_one_replica_to_execute() -> None:
    store = _PersonaStore()
    store.jobs[2]["status"] = "completed"
    store.jobs[3]["status"] = "completed"
    first = PersonaExtractPlugin()
    first._store = store  # type: ignore[assignment]
    gate = _ExecutionGate()
    first._ctx = _context(role="api", plugin_registry=gate)
    first._accept_jobs = True
    second = PersonaExtractPlugin()
    second._store = store  # type: ignore[assignment]
    second._ctx = _context(role="api", plugin_registry=gate)
    second._accept_jobs = True

    await first.schedule_job(1, [{"text": "first"}])
    await second.schedule_job(1, [{"text": "second"}])
    await asyncio.wait_for(store.started.wait(), timeout=1)
    await asyncio.sleep(0)

    assert store.claims == 1
    assert store.extraction_calls == 1

    store.release.set()
    await _wait_until(lambda: store.jobs[1]["status"] == "completed", delay=0.01)
    assert store.jobs[1]["status"] == "completed"

    await first.shutdown()
    await second.shutdown()


@pytest.mark.asyncio
async def test_persona_unclaimed_replica_disable_does_not_fail_peer_job() -> None:
    store = _PersonaStore()
    store.jobs[2]["status"] = "completed"
    store.jobs[3]["status"] = "completed"
    owner = PersonaExtractPlugin()
    owner._store = store  # type: ignore[assignment]
    gate = _ExecutionGate()
    owner._ctx = _context(role="api", plugin_registry=gate)
    owner._accept_jobs = True
    peer = PersonaExtractPlugin()
    peer._store = store  # type: ignore[assignment]
    peer._ctx = _context(role="api", plugin_registry=gate)
    peer._accept_jobs = True

    await owner.schedule_job(1, [{"text": "owner"}])
    await asyncio.wait_for(store.started.wait(), timeout=1)
    await peer.schedule_job(1, [{"text": "peer"}])
    await peer.on_disable()

    assert store.jobs[1]["status"] == "running"
    assert store.claims == 1

    store.release.set()
    await _wait_until(lambda: store.jobs[1]["status"] == "completed", delay=0.01)
    assert store.jobs[1]["status"] == "completed"

    await owner.shutdown()
    await peer.shutdown()


@pytest.mark.asyncio
async def test_persona_peer_durable_disable_blocks_new_llm_job() -> None:
    gate = _ExecutionGate(enabled=False)
    store = _PersonaStore()
    store.jobs[2]["status"] = "completed"
    store.jobs[3]["status"] = "completed"
    plugin = PersonaExtractPlugin()
    plugin._store = store  # type: ignore[assignment]
    plugin._ctx = _context(role="api", plugin_registry=gate)
    plugin._accept_jobs = True

    await plugin.schedule_job(1, [{"text": "must not run"}])
    await _wait_until(lambda: store.jobs[1]["status"] == "failed", delay=0.01)

    assert store.claims == 1
    assert store.extraction_calls == 0
    assert store.jobs[1]["status"] == "failed"
    assert store.jobs[1]["current_stage"] == "disabled"
    assert gate.calls == ["persona_extract"]
    assert gate.scope_calls == [
        ("persona_extract", "tenant-a", "group-a")
    ]

    await plugin.shutdown()
