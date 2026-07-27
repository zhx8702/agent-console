from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.admin.mutation_ledger import (
    MutationIdempotencyConflictError,
    plugin_admin_mutation_audit,
    plugin_admin_mutation_idempotency,
)
from plugins.memory import store as memory_store_module
from plugins.memory.store import MemoryMutationError, MemoryStore, memory_item_version


async def _create_schema(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(plugin_admin_mutation_idempotency.metadata.create_all)
        await conn.execute(
            text(
                "CREATE TABLE command_counter ("
                "id INTEGER PRIMARY KEY, value INTEGER NOT NULL, version TEXT NOT NULL)"
            )
        )
        await conn.execute(
            text("INSERT INTO command_counter (id, value, version) VALUES (1, 0, 'v1')")
        )


class _AdminCommandStore(MemoryStore):
    def __init__(self) -> None:
        self.vector_calls = 0
        self.for_update_reads = 0

    async def create_memory_item(self, **kwargs):
        rows = await memory_store_module._exec(
            "UPDATE command_counter SET value = value + 1 WHERE id = 1 RETURNING value"
        )
        return {
            "id": int(rows[0]["value"]),
            **kwargs,
            "status": kwargs.get("status") or "active",
            "updated_at": f"v{int(rows[0]['value'])}",
        }

    async def get_memory_item(self, item_id: int, *, for_update: bool = False):
        if for_update:
            self.for_update_reads += 1
        if item_id != 1:
            return None
        rows = await memory_store_module._exec(
            "SELECT id, value, version FROM command_counter WHERE id = :id",
            {"id": item_id},
        )
        if not rows:
            return None
        return {
            "id": item_id,
            "tenant_id": "tenant-a",
            "status": "active",
            "content": f"value-{rows[0]['value']}",
            "updated_at": rows[0]["version"],
        }

    async def update_memory_item(self, item_id: int, **updates):
        await memory_store_module._exec(
            "UPDATE command_counter SET value = value + 1, version = 'v2' WHERE id = :id",
            {"id": item_id},
        )
        return {
            "id": item_id,
            "tenant_id": "tenant-a",
            "status": "active",
            "content": updates.get("content") or "unchanged",
            "updated_at": "v2",
        }

    async def rebuild_memory_item_vector_index(self, **kwargs):
        self.vector_calls += 1
        return {
            "dry_run": kwargs.get("dry_run"),
            "scanned": int(kwargs.get("limit") or 0),
            "indexed": 0 if kwargs.get("dry_run") else int(kwargs.get("limit") or 0),
            "deleted": 0,
            "skipped": 0,
            "errors": 0,
        }


def _actor_fields() -> dict:
    return {
        "actor": "operator-a",
        "actor_kind": "session",
        "roles": ("tenant_admin",),
        "trace_id": "trace-a",
    }


def test_memory_item_version_matches_json_datetime_encoding() -> None:
    updated_at = datetime(2026, 7, 18, 9, 30, 45, tzinfo=UTC)
    assert memory_item_version({"updated_at": updated_at}) == updated_at.isoformat()


@pytest.mark.asyncio
async def test_memory_admin_create_has_exact_replay_conflict_and_secret_free_audit(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'memory-admin.db'}")
    await _create_schema(engine)
    monkeypatch.setattr(memory_store_module, "get_engine", lambda: engine)
    store = _AdminCommandStore()
    fields = {
        "tenant_id": "tenant-a",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "member-a",
        "session_id": "room-a",
        "scope_type": "session",
        "source_type": "manual",
        "memory_type": "note",
        "content": "PRIVATE-CONTENT-SENTINEL",
        "status": "active",
    }

    first = await store.create_memory_item_idempotent(
        item_fields=fields,
        idempotency_key="create-memory-1",
        **_actor_fields(),
    )
    replay = await store.create_memory_item_idempotent(
        item_fields=fields,
        idempotency_key="create-memory-1",
        **_actor_fields(),
    )
    assert first.replayed is False
    assert replay.replayed is True
    assert replay.response == first.response
    with pytest.raises(MutationIdempotencyConflictError):
        await store.create_memory_item_idempotent(
            item_fields={**fields, "content": "changed"},
            idempotency_key="create-memory-1",
            **_actor_fields(),
        )

    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT value FROM command_counter WHERE id = 1")) == 1
        audit = (await conn.execute(select(plugin_admin_mutation_audit))).mappings().one()
    semantic_audit = json.dumps(
        {
            "scope": audit["scope_json"],
            "before": audit["before_state_json"],
            "after": audit["after_state_json"],
        },
        sort_keys=True,
    )
    assert "PRIVATE-CONTENT-SENTINEL" not in semantic_audit
    assert "content" not in semantic_audit
    await engine.dispose()


@pytest.mark.asyncio
async def test_memory_item_update_enforces_tenant_and_if_match_cas(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'memory-cas.db'}")
    await _create_schema(engine)
    monkeypatch.setattr(memory_store_module, "get_engine", lambda: engine)
    store = _AdminCommandStore()

    with pytest.raises(MemoryMutationError) as stale:
        await store.update_memory_item_idempotent(
            1,
            tenant_id="tenant-a",
            updates={"content": "new value"},
            expected_version="stale-version",
            idempotency_key="update-stale",
            **_actor_fields(),
        )
    assert stale.value.status_code == 412
    with pytest.raises(MemoryMutationError) as wrong_tenant:
        await store.update_memory_item_idempotent(
            1,
            tenant_id="tenant-b",
            updates={"content": "new value"},
            expected_version="v1",
            idempotency_key="update-wrong-tenant",
            **_actor_fields(),
        )
    assert wrong_tenant.value.status_code == 404

    first = await store.update_memory_item_idempotent(
        1,
        tenant_id="tenant-a",
        updates={"content": "new value"},
        expected_version='W/"v1"',
        idempotency_key="update-item-1",
        **_actor_fields(),
    )
    replay = await store.update_memory_item_idempotent(
        1,
        tenant_id="tenant-a",
        updates={"content": "new value"},
        expected_version='W/"v1"',
        idempotency_key="update-item-1",
        **_actor_fields(),
    )
    assert first.response["updated_at"] == "v2"
    assert replay.response == first.response
    assert replay.replayed is True
    assert store.for_update_reads == 3
    await engine.dispose()


@pytest.mark.asyncio
async def test_memory_vector_rebuild_replays_exact_response_once(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'memory-vector.db'}")
    await _create_schema(engine)
    monkeypatch.setattr(memory_store_module, "get_engine", lambda: engine)
    store = _AdminCommandStore()
    params = {
        "tenant_id": "tenant-a",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "member-a",
        "limit": 9,
        "dry_run": False,
    }

    first = await store.rebuild_memory_item_vector_index_idempotent(
        params=params,
        idempotency_key="rebuild-items",
        **_actor_fields(),
    )
    replay = await store.rebuild_memory_item_vector_index_idempotent(
        params=params,
        idempotency_key="rebuild-items",
        **_actor_fields(),
    )
    assert first.response == replay.response
    assert replay.replayed is True
    assert store.vector_calls == 1
    with pytest.raises(MutationIdempotencyConflictError):
        await store.rebuild_memory_item_vector_index_idempotent(
            params={**params, "limit": 10},
            idempotency_key="rebuild-items",
            **_actor_fields(),
        )
    await engine.dispose()
