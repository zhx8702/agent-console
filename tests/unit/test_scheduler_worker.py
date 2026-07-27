from __future__ import annotations

import asyncio

import pytest

from app.common.config import Settings
from app.workers.scheduler_lease import (
    SchedulerLeaderLease,
    SchedulerLeaseLostError,
)
from app.workers.scheduler_worker import SchedulerWorker


class _LeaseRedis:
    def __init__(self) -> None:
        self.value: str | None = None

    async def set(self, _key, value, **_kwargs):
        if self.value is not None:
            return False
        self.value = value
        return True

    async def get(self, _key):
        return self.value

    async def expire(self, _key, _ttl):
        return int(self.value is not None)

    async def delete(self, _key):
        self.value = None
        return 1

    async def eval(self, script, _keys, _key, token, *args):
        _ = args
        if "pexpire" in script:
            return int(self.value == token)
        if self.value == token:
            self.value = None
            return 1
        return 0


class _EvalFailRedis(_LeaseRedis):
    async def eval(self, script, _keys, _key, token, *args):
        _ = script, _keys, _key, token, args
        raise ConnectionError("eval unavailable")


def _lease(redis: _LeaseRedis, *, timeout: float = 0.1) -> SchedulerLeaderLease:
    return SchedulerLeaderLease(
        redis=redis,
        key="scheduler:test",
        ttl_seconds=1,
        acquire_timeout_seconds=timeout,
        poll_interval_seconds=0.01,
    )


class _Registry:
    def __init__(
        self,
        plugins: dict[str, object] | None = None,
        *,
        owner_allowed: dict[str, bool] | None = None,
        owner_errors: dict[str, Exception] | None = None,
        scope_allowed: dict[tuple[str, str, str], bool] | None = None,
        scope_errors: dict[tuple[str, str, str], Exception] | None = None,
        initialization_order: tuple[str, ...] | None = None,
    ) -> None:
        self.loaded_plugins = plugins or {}
        self.owner_allowed = dict(owner_allowed or {})
        self.owner_errors = dict(owner_errors or {})
        self.scope_allowed = dict(scope_allowed or {})
        self.scope_errors = dict(scope_errors or {})
        self.gate_calls: list[str] = []
        self.scope_gate_calls: list[tuple[str, str, str]] = []
        self.initialization_order = (
            tuple(self.loaded_plugins)
            if initialization_order is None
            else initialization_order
        )

    async def global_execution_allowed(self, owner: str) -> bool:
        self.gate_calls.append(owner)
        error = self.owner_errors.get(owner)
        if error is not None:
            raise error
        return self.owner_allowed.get(owner, True)

    async def scope_execution_allowed(
        self,
        owner: str,
        *,
        tenant_id: str,
        session_id: str = "",
    ) -> bool:
        key = (owner, tenant_id, session_id)
        self.scope_gate_calls.append(key)
        error = self.scope_errors.get(key)
        if error is not None:
            raise error
        return self.scope_allowed.get(key, True)


def _settings(**updates: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "test",
        "app_process_role": "scheduler",
        "knowledge_features_enabled": False,
        "outbound_hmac_secret": "test_secret",
        "tenant_demo_secret": "test_tenant_secret",
        "draw_task_recovery_enabled": False,
        "draw_task_queue_worker_enabled": False,
        "wxbot_group_summary_enabled": False,
        "memory_llm_extraction_enabled": False,
    }
    values.update(updates)
    return Settings(**values)


def _worker(
    lease: SchedulerLeaderLease,
    *,
    plugins: dict[str, object] | None = None,
    registry: _Registry | None = None,
    settings: Settings | None = None,
) -> SchedulerWorker:
    return SchedulerWorker(
        lease,
        registry or _Registry(plugins),  # type: ignore[arg-type]
        settings or _settings(),
        worker_id="scheduler-test",
    )


@pytest.mark.asyncio
async def test_scheduler_lease_allows_exactly_one_owner_and_cas_release() -> None:
    redis = _LeaseRedis()
    first = _lease(redis)
    second = _lease(redis, timeout=0.03)

    await first.acquire()
    with pytest.raises(TimeoutError):
        await second.acquire()

    replacement = "replacement-owner"
    redis.value = replacement
    await first.release()
    assert redis.value == replacement

    redis.value = None
    await second.acquire()
    assert redis.value == second.token
    await second.release()
    assert redis.value is None


@pytest.mark.asyncio
async def test_scheduler_worker_fails_closed_when_leader_lease_is_lost() -> None:
    redis = _LeaseRedis()
    lease = _lease(redis)
    await lease.acquire()
    worker = _worker(lease)
    task = asyncio.create_task(worker.run())

    redis.value = "new-owner"
    await asyncio.wait_for(lease.lost.wait(), timeout=1.5)
    with pytest.raises(SchedulerLeaseLostError):
        await task

    await lease.release()
    assert redis.value == "new-owner"


@pytest.mark.asyncio
async def test_scheduler_worker_stops_without_reporting_lease_loss() -> None:
    redis = _LeaseRedis()
    lease = _lease(redis)
    await lease.acquire()
    worker = _worker(lease)
    task = asyncio.create_task(worker.run())

    await worker.stop()
    await asyncio.wait_for(task, timeout=0.5)
    await lease.release()


@pytest.mark.asyncio
async def test_scheduler_worker_rejects_lease_lost_during_initialization() -> None:
    redis = _LeaseRedis()
    lease = _lease(redis)
    await lease.acquire()
    worker = _worker(lease)
    lease.lost.set()

    with pytest.raises(SchedulerLeaseLostError, match="during initialization"):
        await worker.initialize()

    await lease.release()


@pytest.mark.asyncio
async def test_scheduler_lease_fails_closed_when_atomic_renew_is_unavailable() -> None:
    redis = _EvalFailRedis()
    lease = _lease(redis)
    await lease.acquire()

    await asyncio.wait_for(lease.lost.wait(), timeout=1.0)

    # Release must not use a racy GET/DELETE fallback either. The Redis TTL
    # remains the only safe cleanup path when Lua is unavailable.
    replacement = "replacement-owner"
    redis.value = replacement
    await lease.release()
    assert redis.value == replacement


class _MemoryPlugin:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.claimed_per_call: list[int] = []

    async def drain_extraction_jobs(self, **kwargs: object) -> dict[str, int]:
        self.calls.append(dict(kwargs))
        claimed = self.claimed_per_call.pop(0) if self.claimed_per_call else 0
        return {"claimed": claimed, "succeeded": claimed, "failed": 0, "dead": 0}


class _WxbotPlugin:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def drain_group_summary_jobs(self, **kwargs: object) -> dict[str, int]:
        self.calls.append(dict(kwargs))
        return {"claimed": 0, "succeeded": 0, "failed": 0}


class _DrawPlugin:
    def __init__(self) -> None:
        self.recovery_calls: list[dict[str, object]] = []
        self.queue_calls: list[dict[str, object]] = []

    async def recover_stale_tasks(self, **kwargs: object) -> dict[str, int]:
        self.recovery_calls.append(dict(kwargs))
        return {"recovered": 0, "callbacks_sent": 0, "callback_failed": 0}

    async def drain_queued_tasks(self, **kwargs: object) -> dict[str, int]:
        self.queue_calls.append(dict(kwargs))
        return {"claimed": 0, "completed": 0, "failed": 0, "auto_retried": 0}


def _all_job_settings() -> Settings:
    return _settings(
        memory_llm_extraction_enabled=True,
        memory_llm_extraction_job_enabled=True,
        memory_llm_extraction_job_drain_enabled=True,
        wxbot_group_summary_enabled=True,
        draw_task_recovery_enabled=True,
        draw_task_queue_worker_enabled=True,
    )


@pytest.mark.asyncio
async def test_scheduler_gates_every_direct_plugin_callback_and_skips_disabled() -> None:
    redis = _LeaseRedis()
    lease = _lease(redis)
    await lease.acquire()
    memory = _MemoryPlugin()
    wxbot = _WxbotPlugin()
    draw = _DrawPlugin()
    registry = _Registry(
        {"memory": memory, "wxbot": wxbot, "draw": draw},
        owner_allowed={"memory": False, "wxbot": False, "draw": False},
    )
    worker = _worker(
        lease,
        registry=registry,
        settings=_all_job_settings(),
    )

    await worker._drain_memory_jobs()
    await worker._drain_wxbot_group_summary_jobs()
    await worker._recover_draw_tasks()
    await worker._drain_draw_task_queue()

    assert registry.gate_calls == ["memory", "wxbot", "draw", "draw"]
    assert memory.calls == []
    assert wxbot.calls == []
    assert draw.recovery_calls == []
    assert draw.queue_calls == []
    await lease.release()


@pytest.mark.asyncio
async def test_scheduler_rechecks_global_owner_gate_before_each_callback() -> None:
    redis = _LeaseRedis()
    lease = _lease(redis)
    await lease.acquire()
    memory = _MemoryPlugin()
    registry = _Registry(
        {"memory": memory},
        owner_allowed={"memory": False},
    )
    worker = _worker(
        lease,
        registry=registry,
        settings=_settings(
            memory_llm_extraction_enabled=True,
            memory_llm_extraction_job_enabled=True,
            memory_llm_extraction_job_drain_enabled=True,
        ),
    )

    await worker._drain_memory_jobs()
    registry.owner_allowed["memory"] = True
    await worker._drain_memory_jobs()
    registry.owner_allowed["memory"] = False
    await worker._drain_memory_jobs()

    assert registry.gate_calls == ["memory", "memory", "memory"]
    assert len(memory.calls) == 1
    await lease.release()


@pytest.mark.asyncio
async def test_scheduler_store_failure_fails_closed_before_plugin_callback() -> None:
    redis = _LeaseRedis()
    lease = _lease(redis)
    await lease.acquire()
    memory = _MemoryPlugin()
    registry = _Registry(
        {"memory": memory},
        owner_errors={"memory": ConnectionError("plugin state unavailable")},
    )
    worker = _worker(
        lease,
        registry=registry,
        settings=_settings(
            memory_llm_extraction_enabled=True,
            memory_llm_extraction_job_enabled=True,
            memory_llm_extraction_job_drain_enabled=True,
        ),
    )

    await worker._drain_memory_jobs()

    assert registry.gate_calls == ["memory"]
    assert memory.calls == []
    await lease.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("gate_mode", ["disabled", "store_error"])
async def test_scheduler_initialization_skips_unavailable_plugin_job(
    gate_mode: str,
) -> None:
    redis = _LeaseRedis()
    lease = _lease(redis)
    await lease.acquire()
    draw = _DrawPlugin()
    registry = _Registry(
        {"draw": draw},
        owner_allowed={"draw": gate_mode != "disabled"},
        owner_errors=(
            {"draw": ConnectionError("plugin state unavailable")}
            if gate_mode == "store_error"
            else None
        ),
        initialization_order=(),
    )
    worker = _worker(
        lease,
        registry=registry,
        settings=_settings(draw_task_recovery_enabled=True),
    )

    await worker.initialize()

    assert registry.gate_calls == ["draw", "draw"]
    assert draw.recovery_calls == []
    await lease.release()


@pytest.mark.asyncio
async def test_scheduler_runs_memory_summary_and_draw_jobs_outside_inbound() -> None:
    redis = _LeaseRedis()
    lease = _lease(redis)
    await lease.acquire()
    memory = _MemoryPlugin()
    wxbot = _WxbotPlugin()
    draw = _DrawPlugin()
    settings = _settings(
        memory_llm_extraction_enabled=True,
        memory_llm_extraction_job_enabled=True,
        memory_llm_extraction_job_drain_enabled=True,
        memory_llm_extraction_job_drain_batch_size=2,
        memory_llm_extraction_job_scope_allowlist="demo:wechat:wxbot:room",
        memory_llm_extraction_job_drain_interval_seconds=0.01,
        wxbot_group_summary_enabled=True,
        wxbot_group_summary_drain_interval_seconds=0.01,
        draw_task_recovery_enabled=True,
        draw_task_recovery_interval_seconds=0.01,
        draw_task_queue_worker_enabled=True,
        draw_task_queue_interval_seconds=0.01,
        draw_task_queue_batch_size=3,
        draw_task_lock_ttl_seconds=12,
        draw_task_auto_retry_enabled=True,
        draw_task_max_retries=1,
        draw_task_retry_backoff_seconds=7,
        draw_task_stale_seconds=42,
    )
    worker = _worker(
        lease,
        plugins={"memory": memory, "wxbot": wxbot, "draw": draw},
        settings=settings,
    )
    run_task = asyncio.create_task(worker.run())

    try:
        for _ in range(50):
            if (
                memory.calls
                and len(wxbot.calls) >= 2
                and len(draw.recovery_calls) >= 2
                and len(draw.queue_calls) >= 2
            ):
                break
            await asyncio.sleep(0.01)
    finally:
        await worker.stop()
        await asyncio.wait_for(run_task, timeout=1.0)
        await lease.release()

    memory_call = dict(memory.calls[0])
    assert callable(memory_call.pop("scope_execution_allowed"))
    assert memory_call == {
        "limit": 2,
        "worker_id": "scheduler-test",
        "scope_allowlist": "demo:wechat:wxbot:room",
    }
    wxbot_call = dict(wxbot.calls[0])
    assert callable(wxbot_call.pop("scope_execution_allowed"))
    assert wxbot_call == {"limit": 1, "worker_id": "scheduler-test"}
    recovery_call = dict(draw.recovery_calls[0])
    assert callable(recovery_call.pop("scope_execution_allowed"))
    assert recovery_call == {
        "stale_seconds": 42.0,
        "worker_id": "scheduler-test",
    }
    queue_call = dict(draw.queue_calls[0])
    assert callable(queue_call.pop("scope_execution_allowed"))
    assert queue_call == {
        "worker_id": "scheduler-test",
        "batch_size": 3,
        "lock_ttl_seconds": 12.0,
        "auto_retry_enabled": True,
        "max_retries": 1,
        "retry_backoff_seconds": 7.0,
    }


@pytest.mark.asyncio
async def test_scheduler_scope_gate_distinguishes_disabled_and_enabled_records() -> None:
    redis = _LeaseRedis()
    lease = _lease(redis)
    await lease.acquire()

    class _ScopeAwareMemory:
        def __init__(self) -> None:
            self.results: list[bool] = []

        async def drain_extraction_jobs(self, **kwargs: object) -> dict[str, int]:
            gate = kwargs["scope_execution_allowed"]
            assert callable(gate)
            self.results = [
                await gate("tenant-disabled", "session-a"),
                await gate("tenant-enabled", "session-b"),
            ]
            return {"claimed": 1, "succeeded": 1, "failed": 0, "dead": 0}

    memory = _ScopeAwareMemory()
    registry = _Registry(
        {"memory": memory},
        scope_allowed={
            ("memory", "tenant-disabled", "session-a"): False,
            ("memory", "tenant-enabled", "session-b"): True,
        },
    )
    worker = _worker(
        lease,
        registry=registry,
        settings=_settings(
            memory_llm_extraction_enabled=True,
            memory_llm_extraction_job_enabled=True,
            memory_llm_extraction_job_drain_enabled=True,
        ),
    )

    await worker._drain_memory_jobs()

    assert memory.results == [False, True]
    assert registry.scope_gate_calls == [
        ("memory", "tenant-disabled", "session-a"),
        ("memory", "tenant-enabled", "session-b"),
    ]
    await lease.release()


@pytest.mark.asyncio
async def test_scheduler_memory_claim_cap_is_serialized_and_stops_at_limit() -> None:
    redis = _LeaseRedis()
    lease = _lease(redis)
    await lease.acquire()
    memory = _MemoryPlugin()
    memory.claimed_per_call = [2, 2, 1, 1]
    worker = _worker(
        lease,
        plugins={"memory": memory},
        settings=_settings(
            memory_llm_extraction_enabled=True,
            memory_llm_extraction_job_enabled=True,
            memory_llm_extraction_job_drain_enabled=True,
            memory_llm_extraction_job_drain_batch_size=2,
            memory_llm_extraction_job_drain_max_claims=5,
        ),
    )

    await asyncio.gather(worker._drain_memory_jobs(), worker._drain_memory_jobs())
    await worker._drain_memory_jobs()
    await worker._drain_memory_jobs()

    assert [call["limit"] for call in memory.calls] == [2, 2, 1]
    assert worker._memory_job_drain_claimed == 5
    await lease.release()


@pytest.mark.asyncio
async def test_scheduler_lease_loss_cancels_inflight_periodic_job() -> None:
    class _BlockingMemory:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def drain_extraction_jobs(self, **_kwargs: object) -> dict[str, int]:
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    redis = _LeaseRedis()
    lease = _lease(redis)
    await lease.acquire()
    memory = _BlockingMemory()
    worker = _worker(
        lease,
        plugins={"memory": memory},
        settings=_settings(
            memory_llm_extraction_enabled=True,
            memory_llm_extraction_job_enabled=True,
            memory_llm_extraction_job_drain_enabled=True,
            memory_llm_extraction_job_drain_interval_seconds=0.01,
        ),
    )
    run_task = asyncio.create_task(worker.run())
    await asyncio.wait_for(memory.started.wait(), timeout=0.5)

    redis.value = "successor"
    await asyncio.wait_for(lease.lost.wait(), timeout=1.0)
    with pytest.raises(SchedulerLeaseLostError):
        await asyncio.wait_for(run_task, timeout=0.5)

    assert memory.cancelled.is_set()
    await lease.release()
    assert redis.value == "successor"


@pytest.mark.asyncio
async def test_scheduler_refuses_enabled_job_without_active_plugin() -> None:
    redis = _LeaseRedis()
    lease = _lease(redis)
    await lease.acquire()
    worker = _worker(
        lease,
        settings=_settings(draw_task_recovery_enabled=True),
    )

    with pytest.raises(RuntimeError, match=r"draw\.recover_stale_tasks"):
        await worker.initialize()

    await lease.release()
