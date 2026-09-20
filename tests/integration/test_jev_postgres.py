"""Run against an explicitly supplied, migrated disposable PostgreSQL database."""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from app.common.config import Settings
from app.jev.models import JevPolicy
from app.jev.service import JevService
from app.jev.store import JevStore, execute
from plugins.memory.store import MemoryStore

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.getenv('JEV_TEST_DSN'), reason='requires disposable JEV_TEST_DSN')]


@pytest_asyncio.fixture
async def env(monkeypatch):
    import app.admin.jev_router as router
    import app.jev.store as jobs
    import plugins.memory.store as memories
    engine = create_async_engine(os.environ['JEV_TEST_DSN'])
    for module in (jobs, memories, router):
        monkeypatch.setattr(module, 'get_engine', lambda: engine)
    tid = 'jev-test-' + uuid4().hex[:16]
    store = JevStore()
    settings = Settings(typesafe_enabled=True)
    response = {'model':'jev-test', 'answers': {'decision':{'choice':'accepted','confidence':.95},
        'supported':{'noul':.95}, 'sensitive':{'noul':.01}, 'quality':{'score':.9}},
        'usage':{'input_tokens':10,'output_tokens':2}}
    svc = JevService(settings, store=store, client=SimpleNamespace(evaluate=AsyncMock(return_value=response)))
    svc.registry = SimpleNamespace(scope_execution_allowed=AsyncMock(return_value=True))
    memory = MemoryStore(settings)
    memory.jev_service = svc
    svc.memory_store = memory
    # External cache/vector sinks are irrelevant to relational transaction checks.
    for name in ('_refresh_legacy_cache_for_item_scope','_sync_memory_graph_for_item_safe','_sync_memory_vector_for_item_safe'):
        setattr(memory, name, AsyncMock())
    yield tid, store, svc, memory, response
    for table in ('jev_evaluation', 'jev_policy', 'plugin_memory_acceptance_audit', 'plugin_memory_item'):
        try:
            await execute(f'DELETE FROM {table} WHERE tenant_id=:tid', {'tid':tid})
        except Exception:
            pass
    await engine.dispose()


async def create(memory, tid):
    return await memory._insert_or_touch_memory_item(tenant_id=tid, channel='web', source_key='test',
        user_id='speaker', content='Prefers jasmine tea', original_text='I prefer jasmine tea', confidence=.95)


async def test_memory_saved_before_upstream_and_deduplicated(env):
    tid, store, svc, memory, _ = env
    item = await create(memory, tid)
    assert item['_jev_queued']
    svc.client.evaluate.assert_not_awaited()
    assert (await store.dashboard(tid))['summary'][0]['count'] == 1
    await create(memory, tid)
    assert (await store.dashboard(tid))['summary'][0]['count'] == 1
    await svc.run_batch()
    current = await memory.get_memory_item(item['id'])
    assert current['status'] == item['status']
    assert current['value']['jev']['decision']['choice'] == 'accepted'
    assert (await store.dashboard(tid))['items'][0]['status'] == 'completed'
    assert not (await store.dashboard(tid))['items'][0]['applied']
    assert (await store.dashboard('different-tenant'))['items'] == []


@pytest.mark.parametrize('mode', ['active', 'human', 'stale', 'pinned', 'sensitive', 'nan'])
async def test_freshness_and_active_review(env, mode):
    tid, store, svc, memory, response = env
    await execute('INSERT INTO jev_policy VALUES (:tid,1,CAST(:policy AS JSON))', {'tid':tid,'policy':JevPolicy(shadow_only=False).model_dump_json()})
    item = await create(memory, tid)
    jobs = await store.claim(limit=1, lease_seconds=60, max_attempts=3)
    assert len(jobs) == 1
    if mode == 'human':
        await memory.review_memory_item_acceptance(item['id'], action='reject', reviewed_by='admin/human')
    elif mode == 'stale':
        await memory.update_memory_item(item['id'], content='Different fact')
    elif mode == 'pinned':
        await memory.update_memory_item(item['id'], pinned=True)
    elif mode == 'sensitive':
        response['answers']['sensitive']['noul'] = .99
    elif mode == 'nan':
        response['answers']['supported']['noul'] = 'NaN'
    await svc._process(jobs[0])
    current = await memory.get_memory_item(item['id'])
    job = (await store.dashboard(tid))['items'][0]
    if mode in ('human','stale','pinned'):
        assert job['status'] == 'skipped'
        svc.client.evaluate.assert_not_awaited()
    else:
        assert job['status'] == 'completed', job
        assert job['applied']
        assert current['acceptance_status'] == ('accepted' if mode == 'active' else 'needs_review')
        assert current['value']['acceptance']['reviewed_by'] == 'system/jev'
        audit = job['result']['_audit']
        assert audit['effective_decision'] == ('accepted' if mode == 'active' else 'needs_review')
        assert audit['reason'] == ('applied' if mode == 'active' else 'evidence_review_required')


async def test_claim_fencing_crash_recovery_and_finite_retries(env):
    tid, store, svc, _, _ = env
    await store.enqueue(tenant_id=tid, session_id='s', domain='intent', fingerprint='one', input_hash='one', state={'message':'hi'})
    batches = await asyncio.gather(*(store.claim(limit=1, lease_seconds=60, max_attempts=3) for _ in range(2)))
    assert sorted(map(len,batches)) == [0,1]
    old = next(batch[0] for batch in batches if batch)
    await execute("UPDATE jev_evaluation SET locked_until=NOW()-INTERVAL '1 second' WHERE id=:id", {'id':old['id']})
    new = (await store.claim(limit=1, lease_seconds=60, max_attempts=3))[0]
    assert old['lease_token'] != new['lease_token']
    assert not await store.finish(old, status='completed')
    svc.client.evaluate.side_effect = TimeoutError()
    await svc._process(new)
    await execute('UPDATE jev_evaluation SET next_run_at=NOW() WHERE id=:id', {'id':new['id']})
    await svc.run_batch()
    job = (await store.dashboard(tid))['items'][0]
    assert job['status'] == 'failed'
    assert job['attempts'] == 3
    assert job['retryable']
    assert await store.claim(limit=1, lease_seconds=60, max_attempts=3) == []


async def test_enqueue_sql_failure_does_not_abort_memory_transaction(env):
    tid, _, svc, memory, _ = env
    async def broken(**kwargs):
        await kwargs['run']('SELECT missing_column FROM jev_evaluation')
    svc.store.enqueue = broken
    async with memory._mutation_transaction():
        item = await create(memory, tid)
        assert (await execute('SELECT 1 AS ok'))[0]['ok'] == 1
    assert await memory.get_memory_item(item['id'])


async def test_delayed_human_review_is_not_overwritten(env):
    tid, store, svc, memory, response = env
    item = await create(memory, tid)
    async def evaluate(**kwargs):
        await memory.review_memory_item_acceptance(item['id'], action='needs_review', reviewed_by='admin/reviewer')
        return response
    svc.client.evaluate.side_effect = evaluate
    await svc.run_batch()
    assert (await store.dashboard(tid))['items'][0]['status'] == 'skipped'
    assert (await memory.get_memory_item(item['id']))['value']['acceptance']['reviewed_by'] == 'admin/reviewer'


async def test_admin_policy_idempotency_version_and_tenant_scope(env, monkeypatch):
    import httpx
    from fastapi import FastAPI

    import app.admin.jev_router as router_module
    from app.admin.authorization import AdminRole, Principal, build_admin_authorization_dependency
    tid, store, svc, _, _ = env
    actor = Principal(subject='test-admin', roles=(AdminRole.TENANT_ADMIN.value,), tenant_ids=(tid,), auth_kind='test')
    async def authenticate():
        return actor
    monkeypatch.setattr(router_module, 'build_admin_authorization_dependency',
        lambda settings: build_admin_authorization_dependency(settings, authentication_dependency=authenticate))
    app = FastAPI()
    app.include_router(router_module.build_jev_router(svc, svc.settings))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        path = f'/v1/admin/jev/policy?tenant_id={tid}'
        payload = {'version':0, 'policy':JevPolicy(shadow_only=False).model_dump()}
        headers = {'Idempotency-Key':'one'}
        first = await client.put(path, json=payload, headers=headers)
        assert first.status_code == 200, first.text
        replay = await client.put(path, json=payload, headers=headers)
        assert replay.json() == first.json()
        conflict = await client.put(path, json=payload, headers={'Idempotency-Key':'two'})
        assert conflict.status_code == 409
        assert (await client.put(path, json=payload)).status_code == 422
        assert (await client.get('/v1/admin/jev?tenant_id=other')).status_code == 403
        jid = await store.enqueue(tenant_id=tid, session_id='s', domain='intent', fingerprint='retry', input_hash='retry',state={'message':'hi'})
        await execute("UPDATE jev_evaluation SET status='failed',attempts=3 WHERE id=:id", {'id':jid})
        retry_path = f'/v1/admin/jev/jobs/{jid}/retry?tenant_id={tid}'
        assert (await client.post(retry_path, headers=headers)).status_code == 200
        assert (await client.post(retry_path, headers=headers)).status_code == 200
        assert (await client.post(retry_path, headers={'Idempotency-Key':'two'})).status_code == 409
        actor = Principal(subject='reader', roles=(AdminRole.PLATFORM_READER.value,), tenant_ids=('*',), auth_kind='test')
        assert (await client.put(path, json=payload,headers=headers)).status_code == 403
        actor = Principal(subject='reviewer', roles=(AdminRole.REVIEWER.value,), tenant_ids=(tid,), group_ids=('s',), auth_kind='test')
        assert (await client.get(f'/v1/admin/jev?tenant_id={tid}')).status_code == 403


async def test_dashboard_filters_scope_and_preserves_audit(env):
    tid, store, _, _, response = env
    await store.record_online(tenant_id=tid,session_id='room@chatroom',domain='participation',key='help',
        result={**response,'_audit':{'trace_id':'trace-help','reason':'low_confidence'}},duration_ms=25)
    await store.enqueue(tenant_id=tid,session_id='other@chatroom',domain='intent',fingerprint='other',input_hash='other')
    dashboard = await store.dashboard(tid,session_id='room@chatroom')
    assert len(dashboard['items']) == 1
    assert dashboard['items'][0]['session_id'] == 'room@chatroom'
    assert dashboard['items'][0]['result']['_audit']['trace_id'] == 'trace-help'
    assert sum(x['count'] for x in dashboard['summary']) == 1
    assert not (await store.dashboard(tid,session_id='missing@chatroom'))['items']
    assert not (await store.dashboard('other-tenant',session_id='room@chatroom'))['items']
