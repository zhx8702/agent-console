"""Exercise the bulk acceptance boundary using transaction-local tables only."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from plugins.memory import store as runtime
from plugins.memory.store import MemoryStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.getenv("MEMORY_TEST_DSN"),
        reason="requires PostgreSQL for isolated temporary-table tests",
    ),
]


async def test_bulk_accept_only_promotes_unreviewed_direct_observations(monkeypatch):
    engine = create_async_engine(os.environ["MEMORY_TEST_DSN"])
    async with engine.connect() as conn:
        transaction = await conn.begin()
        try:
            await conn.execute(
                text("""
                CREATE TEMP TABLE plugin_memory_item (
                    id bigint PRIMARY KEY, tenant_id text DEFAULT 'demo', channel text DEFAULT 'wechat',
                    source_key text DEFAULT 'wxbot', session_id text DEFAULT 'room@chatroom',
                    source_type text DEFAULT 'deterministic_group_window', status text DEFAULT 'pending',
                    sensitivity text DEFAULT 'normal', pinned boolean DEFAULT false,
                    expires_at timestamp, deleted_at timestamp, updated_at timestamp, value_json text
                ) ON COMMIT DROP
            """)
            )
            await conn.execute(
                text("""
                CREATE TEMP TABLE plugin_memory_fact (
                    id bigint PRIMARY KEY, memory_item_id bigint, status text DEFAULT 'pending',
                    updated_at timestamp, subject_entity_id bigint, object_entity_id bigint
                ) ON COMMIT DROP
            """)
            )
            await conn.execute(
                text("""
                CREATE TEMP TABLE plugin_memory_entity (
                    id bigint PRIMARY KEY, status text DEFAULT 'pending', updated_at timestamp
                ) ON COMMIT DROP
            """)
            )
            values = []
            for number in range(1, 10):
                value = {
                    "kind": "group_window_relation",
                    "relation": {"predicate": "replied_to", "signals": {"quote": 1}},
                    "acceptance": {"status": "needs_review"},
                }
                if number == 3:
                    value["relation"]["signals"] = {"llm": 20}
                if number == 4:
                    value["acceptance"]["reviewed_by"] = "operator@example.test"
                if number == 5:
                    value["acceptance"]["reviewed_by"] = "system/jev"
                if number == 6:
                    value["relation"]["predicate"] = "interested_in"
                values.append({"id": number, "value": json.dumps(value)})
            await conn.execute(
                text("INSERT INTO plugin_memory_item (id,value_json) VALUES (:id,:value)"), values
            )
            await conn.execute(
                text("UPDATE plugin_memory_item SET source_type='llm_group_window' WHERE id=2")
            )
            await conn.execute(
                text("UPDATE plugin_memory_item SET tenant_id='another-tenant' WHERE id=7")
            )
            await conn.execute(text("UPDATE plugin_memory_item SET pinned=true WHERE id=8"))
            await conn.execute(
                text("UPDATE plugin_memory_item SET expires_at='2020-01-01' WHERE id=9")
            )
            await conn.execute(
                text(
                    "INSERT INTO plugin_memory_fact (id,memory_item_id) SELECT id,id FROM plugin_memory_item"
                )
            )

            async def execute(sql, params=None):
                return [
                    dict(row) for row in (await conn.execute(text(sql), params or {})).mappings()
                ]

            monkeypatch.setattr(runtime, "_exec", execute)
            store = MemoryStore(SimpleNamespace())
            result = await store.auto_accept_pending_group_window_relations(tenant_id="demo")
            assert result["accepted"] == 1
            assert result["facts_activated"] == 1
            rows = (
                await conn.execute(text("SELECT id,status FROM plugin_memory_item ORDER BY id"))
            ).all()
            assert rows == [(1, "active"), *[(i, "pending") for i in range(2, 10)]]
            assert (
                await conn.execute(text("SELECT id FROM plugin_memory_fact WHERE status='active'"))
            ).scalars().all() == [1]
        finally:
            await transaction.rollback()
    await engine.dispose()
