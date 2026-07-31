from __future__ import annotations

from types import SimpleNamespace

import pytest

import plugins.memory.store as memory_store_module
from plugins.memory.store import MemoryStore


@pytest.fixture(autouse=True)
def _bind_unit_memory_transaction():
    token = memory_store_module._ACTIVE_MUTATION_CONNECTION.set(
        SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    )
    try:
        yield
    finally:
        memory_store_module._ACTIVE_MUTATION_CONNECTION.reset(token)


@pytest.mark.asyncio
async def test_memory_governance_dry_run_and_bounded_cleanup(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params or {}))
        if sql.startswith("SELECT id"):
            if "candidate" in sql:
                return [{"id": 1}]
            if "= 'rejected'" in sql:
                return [{"id": 2}]
            if "status = 'active'" in sql:
                return [{"id": 3}]
        return []

    class _VectorIndex:
        def __init__(self) -> None:
            self.deleted: list[int] = []

        async def delete_item(self, item_id: int) -> None:
            self.deleted.append(item_id)

    monkeypatch.setattr("plugins.memory.store._exec", fake_exec)
    store = MemoryStore(
        SimpleNamespace(
            memory_needs_review_retention_days=30,
            memory_rejected_retention_days=7,
            memory_auto_expire_days=180,
            memory_governance_batch_size=100,
        )
    )
    vector_index = _VectorIndex()
    store.vector_index = vector_index  # type: ignore[assignment]

    preview = await store.run_governance_cleanup(dry_run=True)
    assert preview["selected"] == 3
    assert vector_index.deleted == []
    assert not any(sql.startswith("UPDATE") for sql, _params in calls)

    calls.clear()
    applied = await store.run_governance_cleanup(dry_run=False)
    assert applied["needs_review_expired"] == 1
    assert applied["rejected_purged"] == 1
    assert applied["stale_auto_expired"] == 1
    assert vector_index.deleted == [1, 2, 3]
    assert any("plugin_memory_fact" in sql for sql, _params in calls)
    item_expiry_sql = next(
        sql
        for sql, _params in calls
        if sql.startswith("UPDATE plugin_memory_item") and "acceptance,status" in sql
    )
    assert "status = 'archived'" in item_expiry_sql
    assert "status = 'expired'" not in item_expiry_sql
    select_sql = next(sql for sql, _params in calls if sql.startswith("SELECT id"))
    assert "explicit_user" in select_sql
