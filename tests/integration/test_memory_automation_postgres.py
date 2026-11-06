"""Exercise historical paging and durable replay against isolated PostgreSQL tables."""
from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.jev import store as runtime
from app.jev.store import JevStore

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.getenv('MEMORY_TEST_DSN'), reason='requires isolated PostgreSQL temporary tables')]


async def test_replay_is_bounded_isolated_and_only_revives_disabled_jobs(monkeypatch):
    engine = create_async_engine(os.environ['MEMORY_TEST_DSN'])
    async with engine.connect() as conn:
        transaction = await conn.begin()
        try:
            await conn.execute(text('''CREATE TEMP TABLE plugin_memory_item (
                id bigint PRIMARY KEY, deleted_at timestamp, pinned boolean DEFAULT false,
                source_type text DEFAULT 'llm_group_window', status text DEFAULT 'active', expires_at timestamp
            ) ON COMMIT DROP'''))
            await conn.execute(text('INSERT INTO plugin_memory_item (id) SELECT generate_series(1,105)'))
            await conn.execute(text("UPDATE plugin_memory_item SET status='archived' WHERE id=1"))
            await conn.execute(text('UPDATE plugin_memory_item SET pinned=true WHERE id=2'))
            await conn.execute(text("UPDATE plugin_memory_item SET expires_at='2020-01-01' WHERE id=3"))
            await conn.execute(text('''CREATE TEMP TABLE jev_evaluation (
                id text PRIMARY KEY, tenant_id text, session_id text, domain text, fingerprint text,
                input_hash text, target_id bigint, state json, status text DEFAULT 'pending',
                attempts integer DEFAULT 0, error_type text DEFAULT '', next_run_at timestamp DEFAULT NOW(),
                updated_at timestamp DEFAULT NOW(), UNIQUE(tenant_id,domain,fingerprint)
            ) ON COMMIT DROP'''))
            async def execute(sql, params=None):
                result = await conn.execute(text(sql), params or {})
                return [dict(row) for row in result.mappings()] if result.returns_rows else []
            monkeypatch.setattr(runtime, 'execute', execute)
            store = JevStore()
            page = await store.memory_review_page(0)
            assert [row['id'] for row in page] == list(range(4,104))
            assert [row['id'] for row in await store.memory_review_page(103)] == [104,105]
            kwargs = dict(tenant_id='a', session_id='room', domain='relationship',
                          fingerprint='same', input_hash='hash', target_id=4, run=execute)
            job_id = await store.enqueue(**kwargs)
            assert job_id
            assert await store.enqueue(**kwargs) is None
            assert await store.enqueue(**{**kwargs, 'tenant_id': 'b'}) != job_id
            await conn.execute(text("UPDATE jev_evaluation SET status='skipped',error_type='disabled',attempts=2 WHERE id=:id"), {'id':job_id})
            assert await store.enqueue(**kwargs) == job_id
            assert (await conn.execute(text('SELECT status,attempts FROM jev_evaluation WHERE id=:id'), {'id':job_id})).one() == ('pending',0)
            await conn.execute(text("UPDATE jev_evaluation SET status='completed' WHERE id=:id"), {'id':job_id})
            assert await store.enqueue(**kwargs) is None
        finally:
            await transaction.rollback()
    await engine.dispose()
