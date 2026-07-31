from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

import plugins.memory.store as memory_store_module
from plugins.memory.store import GROUP_HISTORY_USER_ID_SCOPE, MemoryStore


def _settings(**overrides: Any) -> SimpleNamespace:
    values = {
        "memory_llm_extraction_enabled": True,
        "memory_llm_extraction_job_enabled": True,
        "memory_llm_extraction_job_drain_batch_size": 5,
        "memory_llm_extraction_job_lock_ttl_seconds": 30.0,
        "memory_llm_extraction_job_timeout_seconds": 1.0,
        "memory_llm_extraction_job_backoff_seconds": 1.0,
        "memory_graph_llm_extraction_enabled": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def _allow_scope(_tenant_id: str, _session_id: str) -> bool:
    return True


class _JobQueueFake:
    def __init__(self) -> None:
        self.row: dict[str, Any] = {
            "id": 9,
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "member-a",
            "session_id": "session-a",
            "source_event_id": 7,
            "source_trace_id": "trace-7",
            "status": "pending",
            "attempts": 0,
            "max_attempts": 3,
            "next_run_at": None,
            "locked_until": None,
            "locked_by": "",
            "last_error": "",
            "result_json": "{}",
            "idempotency_key": "job-9",
            "created_at": None,
            "updated_at": None,
        }
        self.lease_expired = False
        self.renewals = 0
        self.terminal_mutations = 0

    def expire_lease(self) -> None:
        self.lease_expired = True

    async def exec(self, sql: str, params: dict | None = None) -> list[dict]:
        values = params or {}
        if sql.startswith("WITH candidate AS"):
            eligible = self.row["status"] in {"pending", "failed"} or (
                self.row["status"] == "running" and self.lease_expired
            )
            if not eligible:
                return []
            self.row["status"] = "running"
            self.row["locked_by"] = str(values["locked_by"])
            self.row["locked_until"] = "future"
            self.lease_expired = False
            return [dict(self.row)]

        if "FROM plugin_memory_event WHERE id = :id" in sql:
            return [
                {
                    "id": 7,
                    "user_text": "用户输入",
                    "assistant_text": "助手回复",
                    "trace_id": "trace-7",
                }
            ]

        if sql.startswith(
            "SELECT status, locked_by, result_json FROM plugin_memory_extraction_job"
        ):
            return [
                {
                    "status": self.row["status"],
                    "locked_by": self.row["locked_by"],
                    "result_json": self.row["result_json"],
                }
            ]

        if sql.startswith("UPDATE plugin_memory_extraction_job SET locked_until = NOW()"):
            assert "status = 'running'" in sql
            assert "locked_by = :locked_by" in sql
            assert "locked_until > NOW()" in sql
            if (
                self.row["status"] == "running"
                and self.row["locked_by"] == values["locked_by"]
                and not self.lease_expired
            ):
                self.renewals += 1
                self.row["locked_until"] = f"renewed-{self.renewals}"
                return [{"id": self.row["id"]}]
            return []

        if sql.startswith("UPDATE plugin_memory_extraction_job SET status = 'pending'"):
            assert "status = 'running'" in sql
            assert "locked_by = :locked_by" in sql
            if self.row["status"] == "running" and self.row["locked_by"] == values["locked_by"]:
                self.row["status"] = "pending"
                self.row["locked_by"] = ""
                self.row["locked_until"] = None
                return [{"id": self.row["id"]}]
            return []

        if "status = CAST(:status AS VARCHAR)" in sql:
            assert "status = 'running'" in sql
            assert "locked_by = :locked_by" in sql
            if self.row["status"] == "running" and self.row["locked_by"] == values["locked_by"]:
                self.row.update(
                    status=str(values["status"]),
                    attempts=int(values["attempts"]),
                    locked_by="",
                    locked_until=None,
                    last_error=str(values["last_error"]),
                    result_json=str(values["result_json"]),
                )
                self.terminal_mutations += 1
            return []

        if "status = 'succeeded'" in sql:
            assert "status = 'running'" in sql
            assert "locked_by = :locked_by" in sql
            if self.row["status"] == "running" and self.row["locked_by"] == values["locked_by"]:
                self.row.update(
                    status="succeeded",
                    attempts=int(values["attempts"]),
                    locked_by="",
                    locked_until=None,
                    last_error="",
                    result_json=str(values["result_json"]),
                )
                self.terminal_mutations += 1
            return []

        return []


def _store() -> MemoryStore:
    return MemoryStore(_settings(), llm_service=object())


@pytest.fixture(autouse=True)
def _stub_job_mutation_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep queue fencing tests on their in-memory transaction fake."""

    @asynccontextmanager
    async def fake_mutation_transaction(_self: MemoryStore):
        connection = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
        token = memory_store_module._ACTIVE_MUTATION_CONNECTION.set(connection)
        try:
            yield connection
        finally:
            memory_store_module._ACTIVE_MUTATION_CONNECTION.reset(token)

    monkeypatch.setattr(
        MemoryStore,
        "_mutation_transaction",
        fake_mutation_transaction,
    )


@pytest.mark.asyncio
async def test_each_claim_uses_a_unique_bounded_fencing_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _JobQueueFake()
    monkeypatch.setattr(memory_store_module, "_exec", queue.exec)
    store = _store()

    first = (await store.claim_llm_extraction_jobs(worker_id="worker-a"))[0]
    queue.expire_lease()
    second = (await store.claim_llm_extraction_jobs(worker_id="worker-a"))[0]

    assert first["locked_by"].startswith("worker-a:")
    assert second["locked_by"].startswith("worker-a:")
    assert first["locked_by"] != second["locked_by"]
    assert first["claim_token"] == first["locked_by"]
    assert first["worker_id"] == "worker-a"
    assert len(first["locked_by"]) <= 128
    assert len(second["locked_by"]) <= 128


@pytest.mark.asyncio
async def test_renew_and_defer_require_the_current_claim_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _JobQueueFake()
    monkeypatch.setattr(memory_store_module, "_exec", queue.exec)
    store = _store()

    old_claim = (await store.claim_llm_extraction_jobs(worker_id="worker-a"))[0]
    queue.expire_lease()
    new_claim = (await store.claim_llm_extraction_jobs(worker_id="worker-a"))[0]

    assert await store.renew_llm_extraction_job_lease(old_claim) is False
    assert await store.renew_llm_extraction_job_lease(new_claim) is True
    assert queue.renewals == 1
    assert await store.defer_llm_extraction_job(old_claim) is False
    assert queue.row["status"] == "running"
    assert await store.defer_llm_extraction_job(new_claim) is True
    assert queue.row["status"] == "pending"


@pytest.mark.asyncio
async def test_stale_and_repeated_completion_are_noops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _JobQueueFake()
    monkeypatch.setattr(memory_store_module, "_exec", queue.exec)
    store = _store()
    enhance_calls = 0

    async def enhance(**_kwargs: Any) -> int:
        nonlocal enhance_calls
        enhance_calls += 1
        return 0

    monkeypatch.setattr(store, "_enhance_memory_with_llm", enhance)
    old_claim = (await store.claim_llm_extraction_jobs(worker_id="worker-a"))[0]
    queue.expire_lease()
    new_claim = (await store.claim_llm_extraction_jobs(worker_id="worker-b"))[0]

    assert (
        await store.process_llm_extraction_job(
            old_claim,
            scope_execution_allowed=_allow_scope,
        )
        == "stale"
    )
    assert queue.row["status"] == "running"
    assert queue.row["locked_by"] == new_claim["locked_by"]
    assert queue.terminal_mutations == 0
    assert enhance_calls == 0

    assert (
        await store.process_llm_extraction_job(
            new_claim,
            scope_execution_allowed=_allow_scope,
        )
        == "succeeded"
    )
    first_result = dict(queue.row)
    assert queue.terminal_mutations == 1
    assert enhance_calls == 1

    assert (
        await store.process_llm_extraction_job(
            new_claim,
            scope_execution_allowed=_allow_scope,
        )
        == "succeeded"
    )
    assert queue.row == first_result
    assert queue.terminal_mutations == 1
    assert enhance_calls == 1

    assert (
        await store.process_llm_extraction_job(
            old_claim,
            scope_execution_allowed=_allow_scope,
        )
        == "stale"
    )
    assert queue.row == first_result
    assert queue.terminal_mutations == 1
    assert enhance_calls == 1


@pytest.mark.asyncio
async def test_failure_is_idempotent_and_job_can_be_reclaimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _JobQueueFake()
    monkeypatch.setattr(memory_store_module, "_exec", queue.exec)
    store = _store()
    failure_calls = 0

    async def fail(**_kwargs: Any) -> int:
        nonlocal failure_calls
        failure_calls += 1
        raise RuntimeError("injected extraction failure")

    monkeypatch.setattr(store, "_enhance_memory_with_llm", fail)
    claim = (await store.claim_llm_extraction_jobs(worker_id="worker-a"))[0]

    assert (
        await store.process_llm_extraction_job(
            claim,
            scope_execution_allowed=_allow_scope,
        )
        == "failed"
    )
    first_failure = dict(queue.row)
    assert first_failure["attempts"] == 1
    assert queue.terminal_mutations == 1
    assert failure_calls == 1

    assert (
        await store.process_llm_extraction_job(
            claim,
            scope_execution_allowed=_allow_scope,
        )
        == "failed"
    )
    assert queue.row == first_failure
    assert queue.terminal_mutations == 1
    assert failure_calls == 1

    retry = (await store.claim_llm_extraction_jobs(worker_id="worker-a"))[0]
    assert retry["status"] == "running"
    assert retry["locked_by"] != claim["locked_by"]


@pytest.mark.asyncio
async def test_stale_failure_cannot_overwrite_a_new_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _JobQueueFake()
    monkeypatch.setattr(memory_store_module, "_exec", queue.exec)
    store = _store()
    failure_calls = 0

    async def fail(**_kwargs: Any) -> int:
        nonlocal failure_calls
        failure_calls += 1
        raise RuntimeError("old worker failed late")

    monkeypatch.setattr(store, "_enhance_memory_with_llm", fail)
    old_claim = (await store.claim_llm_extraction_jobs(worker_id="worker-a"))[0]
    queue.expire_lease()
    new_claim = (await store.claim_llm_extraction_jobs(worker_id="worker-b"))[0]

    assert (
        await store.process_llm_extraction_job(
            old_claim,
            scope_execution_allowed=_allow_scope,
        )
        == "stale"
    )
    assert queue.row["status"] == "running"
    assert queue.row["locked_by"] == new_claim["locked_by"]
    assert queue.row["attempts"] == 0
    assert queue.terminal_mutations == 0
    assert failure_calls == 0

    assert (
        await store.process_llm_extraction_job(
            new_claim,
            scope_execution_allowed=_allow_scope,
        )
        == "failed"
    )
    assert queue.terminal_mutations == 1
    assert failure_calls == 1
    terminal_failure = dict(queue.row)

    assert (
        await store.process_llm_extraction_job(
            old_claim,
            scope_execution_allowed=_allow_scope,
        )
        == "stale"
    )
    assert queue.row == terminal_failure
    assert queue.terminal_mutations == 1
    assert failure_calls == 1


@pytest.mark.asyncio
async def test_claim_lost_during_llm_blocks_projection_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _JobQueueFake()
    monkeypatch.setattr(memory_store_module, "_exec", queue.exec)
    store = _store()
    projection_calls = 0

    async def list_items(**_kwargs: Any) -> list[dict[str, Any]]:
        return []

    async def extract_actions(**_kwargs: Any) -> list[dict[str, Any]]:
        queue.expire_lease()
        reclaimed = await store.claim_llm_extraction_jobs(worker_id="worker-b")
        assert reclaimed
        return [{"op": "add", "content": "不得由旧 worker 写入"}]

    async def apply_projection(**_kwargs: Any) -> dict[str, Any]:
        nonlocal projection_calls
        projection_calls += 1
        return {"id": 88}

    monkeypatch.setattr(store, "list_memory_items", list_items)
    monkeypatch.setattr(store.structured_extractor, "extract_actions", extract_actions)
    monkeypatch.setattr(store, "_apply_structured_memory_action", apply_projection)

    old_claim = (await store.claim_llm_extraction_jobs(worker_id="worker-a"))[0]
    assert (
        await store.process_llm_extraction_job(
            old_claim,
            scope_execution_allowed=_allow_scope,
        )
        == "stale"
    )
    assert queue.row["status"] == "running"
    assert queue.row["locked_by"] != old_claim["locked_by"]
    assert projection_calls == 0


@pytest.mark.asyncio
async def test_member_opt_out_wins_lock_before_group_job_and_blocks_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _JobQueueFake()
    queue.row.update(
        user_id=GROUP_HISTORY_USER_ID_SCOPE,
        session_id="group:alpha",
    )
    member_lock = asyncio.Lock()
    job_waiting_for_lock = asyncio.Event()
    job_acquired_lock = False
    opt_out = False
    llm_calls = 0
    lock_members: list[str] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "FROM plugin_memory_event WHERE id = :id" in sql:
            return [
                {
                    "id": 7,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": GROUP_HISTORY_USER_ID_SCOPE,
                    "session_id": "group:alpha",
                    "user_text": "群成员消息",
                    "assistant_text": "",
                    "trace_id": "trace-7",
                    "source_member_id": "member-a",
                }
            ]
        if "FROM social_tenant_member_control" in sql:
            return [
                {
                    "memory_opt_out": opt_out,
                    "deletion_state": "requested" if opt_out else "none",
                }
            ]
        return await queue.exec(sql, params)

    @asynccontextmanager
    async def fake_mutation_transaction():
        nonlocal job_acquired_lock
        connection = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        token = memory_store_module._ACTIVE_MUTATION_CONNECTION.set(connection)
        try:
            yield connection
        finally:
            if job_acquired_lock and member_lock.locked():
                member_lock.release()
                job_acquired_lock = False
            memory_store_module._ACTIVE_MUTATION_CONNECTION.reset(token)

    async def lock_member(*, tenant_id: str, user_id: str) -> None:
        nonlocal job_acquired_lock
        assert tenant_id == "demo"
        lock_members.append(user_id)
        job_waiting_for_lock.set()
        await member_lock.acquire()
        job_acquired_lock = True

    async def enhance(**_kwargs: Any) -> int:
        nonlocal llm_calls
        llm_calls += 1
        return 1

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    store = _store()
    monkeypatch.setattr(store, "_mutation_transaction", fake_mutation_transaction)
    monkeypatch.setattr(store, "_lock_member_memory_mutation", lock_member)
    monkeypatch.setattr(store, "_enhance_memory_with_llm", enhance)
    claim = (await store.claim_llm_extraction_jobs(worker_id="worker-a"))[0]

    await member_lock.acquire()
    task = asyncio.create_task(
        store.process_llm_extraction_job(
            claim,
            scope_execution_allowed=_allow_scope,
        )
    )
    await asyncio.wait_for(job_waiting_for_lock.wait(), timeout=1)
    opt_out = True
    member_lock.release()

    assert await asyncio.wait_for(task, timeout=1) == "stale"
    assert lock_members == ["member-a"]
    assert llm_calls == 0
    assert queue.terminal_mutations == 0


@pytest.mark.asyncio
async def test_job_wins_member_lock_then_forget_cleans_all_job_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _JobQueueFake()
    member_lock = asyncio.Lock()
    job_has_lock = asyncio.Event()
    allow_job_to_finish = asyncio.Event()
    forget_completed = asyncio.Event()
    job_acquired_lock = False
    durable_rows: set[str] = set()
    sequence: list[str] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "FROM plugin_memory_event WHERE id = :id" in sql:
            return [
                {
                    "id": 7,
                    "tenant_id": "demo",
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": "member-a",
                    "session_id": "session-a",
                    "user_text": "成员消息",
                    "assistant_text": "",
                    "trace_id": "trace-7",
                    "source_member_id": "member-a",
                }
            ]
        if "FROM social_tenant_member_control" in sql:
            return [{"memory_opt_out": False, "deletion_state": "none"}]
        if "status = 'succeeded'" in sql:
            assert member_lock.locked()
            sequence.append("terminal")
        return await queue.exec(sql, params)

    @asynccontextmanager
    async def fake_mutation_transaction():
        nonlocal job_acquired_lock
        connection = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        token = memory_store_module._ACTIVE_MUTATION_CONNECTION.set(connection)
        try:
            yield connection
            sequence.append("job_commit")
        finally:
            if job_acquired_lock and member_lock.locked():
                member_lock.release()
                job_acquired_lock = False
            memory_store_module._ACTIVE_MUTATION_CONNECTION.reset(token)

    async def lock_member(*, tenant_id: str, user_id: str) -> None:
        nonlocal job_acquired_lock
        assert (tenant_id, user_id) == ("demo", "member-a")
        await member_lock.acquire()
        job_acquired_lock = True
        sequence.append("job_lock")
        job_has_lock.set()

    async def enhance(**_kwargs: Any) -> int:
        assert member_lock.locked()
        sequence.append("llm")
        await allow_job_to_finish.wait()
        durable_rows.update({"entity", "fact", "episode", "profile"})
        sequence.append("job_side_effects")
        return len(durable_rows)

    async def forget_member() -> None:
        await member_lock.acquire()
        try:
            sequence.append("forget_delete")
            durable_rows.clear()
            queue.row["status"] = "deleted"
            queue.row["locked_by"] = ""
            forget_completed.set()
        finally:
            member_lock.release()

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    store = _store()
    monkeypatch.setattr(store, "_mutation_transaction", fake_mutation_transaction)
    monkeypatch.setattr(store, "_lock_member_memory_mutation", lock_member)
    monkeypatch.setattr(store, "_enhance_memory_with_llm", enhance)
    claim = (await store.claim_llm_extraction_jobs(worker_id="worker-a"))[0]

    job_task = asyncio.create_task(
        store.process_llm_extraction_job(
            claim,
            scope_execution_allowed=_allow_scope,
        )
    )
    await asyncio.wait_for(job_has_lock.wait(), timeout=1)
    forget_task = asyncio.create_task(forget_member())
    await asyncio.sleep(0)
    assert not forget_completed.is_set()

    allow_job_to_finish.set()
    assert await asyncio.wait_for(job_task, timeout=1) == "succeeded"
    await asyncio.wait_for(forget_task, timeout=1)

    assert durable_rows == set()
    assert sequence.index("job_side_effects") < sequence.index("terminal")
    assert sequence.index("terminal") < sequence.index("job_commit")
    assert sequence.index("job_commit") < sequence.index("forget_delete")
    assert sequence[-1] == "forget_delete"


@pytest.mark.asyncio
async def test_process_rejects_an_unclaimed_job_without_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = _JobQueueFake()
    monkeypatch.setattr(memory_store_module, "_exec", queue.exec)
    store = _store()
    enhance_calls = 0

    async def enhance(**_kwargs: Any) -> int:
        nonlocal enhance_calls
        enhance_calls += 1
        return 0

    monkeypatch.setattr(store, "_enhance_memory_with_llm", enhance)
    unclaimed = {
        key: value for key, value in queue.row.items() if key not in {"locked_by", "locked_until"}
    }
    unclaimed["status"] = "running"

    assert (
        await store.process_llm_extraction_job(
            unclaimed,
            worker_id="worker-a",
            scope_execution_allowed=_allow_scope,
        )
        == "stale"
    )
    assert enhance_calls == 0
    assert queue.row["status"] == "pending"


@pytest.mark.asyncio
async def test_admin_retry_locks_candidate_and_rechecks_its_claim_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, dict(params or {})))
        return [{"id": 9}]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    result = await _store().maintain_llm_extraction_jobs(
        actions=["retry"],
        dry_run=False,
        tenant_id="demo",
        status="failed",
    )

    assert result["affected"] == 1
    sql = calls[0][0]
    assert "SELECT id, locked_by FROM plugin_memory_extraction_job" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "COALESCE(job.locked_by, '') = COALESCE(candidate.locked_by, '')" in sql
