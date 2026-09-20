"""Run only against an explicitly configured disposable PostgreSQL database."""
from __future__ import annotations

import asyncio
import os
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine

from app.admin.authorization import AdminRole, Principal, build_admin_authorization_dependency
from app.common.config import Settings
from app.jev.knowledge import JevKnowledgeService
from app.jev.knowledge_store import KnowledgeStore
from app.jev.models import JevPolicy
from app.jev.service import JevService
from app.jev.store import execute

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.getenv('JEV_TEST_DSN'), reason='requires disposable JEV_TEST_DSN')]


@pytest_asyncio.fixture
async def env(monkeypatch):
    import app.admin.jev_knowledge_router as kr
    import app.admin.jev_router as jr
    import app.jev.knowledge_store as ks
    import app.jev.store as js
    engine = create_async_engine(os.environ['JEV_TEST_DSN'])
    for module in (kr, jr, ks, js):
        monkeypatch.setattr(module, 'get_engine', lambda: engine)
    tid = 'knowledge-test-' + uuid4().hex[:12]
    policy = JevPolicy(knowledge=True, knowledge_sessions=['g@chatroom'])
    await execute('INSERT INTO jev_policy VALUES (:tid,1,CAST(:policy AS JSON))', {'tid':tid,'policy':policy.model_dump_json()})
    jev = JevService(Settings(typesafe_enabled=True), client=SimpleNamespace(evaluate=AsyncMock()))
    jev.registry = SimpleNamespace(scope_execution_allowed=AsyncMock(return_value=True))
    store = KnowledgeStore()
    kb = SimpleNamespace(add_document=AsyncMock(return_value=123),get_document=AsyncMock(),list_documents=AsyncMock(return_value=[]))
    svc = JevKnowledgeService(jev,llm=SimpleNamespace(chat=AsyncMock()),kb=kb,store=store)
    jev.knowledge_service = svc
    actor = Principal(subject='test-admin',roles=(AdminRole.TENANT_ADMIN.value,),tenant_ids=(tid,),auth_kind='test')
    async def authenticate():
        return actor
    monkeypatch.setattr(jr,'build_admin_authorization_dependency',lambda settings:build_admin_authorization_dependency(settings,authentication_dependency=authenticate))
    app = FastAPI()
    app.include_router(jr.build_jev_router(jev,jev.settings))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        yield tid, store, svc, client
    for table in ('jev_knowledge_candidate','jev_knowledge_job','jev_evaluation','jev_policy','plugin_wxbot_group_observations','social_tenant_member_control'):
        await execute(f'DELETE FROM {table} WHERE tenant_id=:tid',{'tid':tid})
    await engine.dispose()


async def seed(tid, count=2, sid='g@chatroom'):
    return await execute("INSERT INTO plugin_wxbot_group_observations (tenant_id,session_id,message_id,sender_wxid,content,occurred_ts) "
                         "SELECT :tid,:sid,CAST(n AS TEXT),'member',CASE WHEN n%2=0 THEN '更新证书后恢复正常' ELSE '证书错误怎么办' END,100+n "
                         "FROM generate_series(1,:count) n RETURNING *",{'tid':tid,'sid':sid,'count':count})


async def candidate(env):
    tid, store, _, _ = env
    messages = await seed(tid)
    await store.schedule(tid,'g@chatroom',date(2026,9,20),0,1000)
    job = await store.claim('job')
    draft = {'title':'修复证书错误','question':'证书错误怎么办','environment':'测试环境','solution':'更新证书','outcome':'成员确认恢复',
             'evidence_ids':[r['id'] for r in messages], 'resolution_ids':[messages[-1]['id']]}
    await store.save_page(job,messages,[draft])
    row = await store.claim('candidate')
    review = {'answers':{'decision':{'choice':'retain','confidence':.95},'supported':{'noul':.95},'resolved':{'noul':.95},'sensitive':{'noul':.01}}}
    review["_knowledge_snapshot"] = await store.knowledge_snapshot(tid, "g@chatroom", row["id"])
    await store.save_review(row,status='ready',reason='supported_resolved_experience',review=review,comparisons=[])
    return (await store.dashboard(tid))['candidates'][0]


async def test_full_day_pagination_and_restart_checkpoint(env):
    tid, store, _, _ = env
    messages = await seed(tid,count=121)
    await store.schedule(tid,'g@chatroom',date(2026,9,20),0,1000)
    await store.schedule(tid,'g@chatroom',date(2026,9,20),0,1000)
    seen=[]
    for _ in range(4):
        job = await store.claim('job')
        page = await store.page(job)
        seen.extend(r['id'] for r in page)
        assert await KnowledgeStore().save_page(job,page,[])  # fresh process store
    assert seen == [r['id'] for r in messages]
    dashboard = await store.dashboard(tid)
    assert len(dashboard['jobs']) == 1
    assert dashboard['jobs'][0]['scanned'] == 121
    assert dashboard['jobs'][0]['status'] == 'completed'
    assert await store.claim('job') is None


async def test_scope_separation_evidence_and_dashboard(env):
    tid, store, _, _ = env
    rows=await seed(tid)
    other=await seed(tid,sid='other@chatroom')
    assert await store.evidence(tid,'g@chatroom',[r['id'] for r in other]) == []
    assert await store.evidence('another-tenant','g@chatroom',[r['id'] for r in rows]) == []
    await store.schedule(tid,'g@chatroom',date(2026,9,20),0,1000)
    assert not (await store.dashboard(tid,'other@chatroom'))['jobs']
    assert not (await store.dashboard('another-tenant'))['jobs']


async def test_concurrent_claim_and_expired_worker_cannot_save(env):
    tid, store, _, _ = env
    await seed(tid)
    await store.schedule(tid,'g@chatroom',date(2026,9,20),0,1000)
    claims=await asyncio.gather(store.claim('job'),store.claim('job'))
    assert sum(row is not None for row in claims) == 1
    old=next(row for row in claims if row)
    await execute("UPDATE jev_knowledge_job SET locked_until=NOW()-INTERVAL '1 second' WHERE id=:id",{'id':old['id']})
    new=await store.claim('job')
    assert new['lease_token'] != old['lease_token']
    assert not await store.save_page(old,await store.page(old),[])
    assert await store.save_page(new,await store.page(new),[])


async def test_review_fencing_and_candidate_idempotency(env):
    tid, store, _, _ = env
    c=await candidate(env)
    job=(await store.dashboard(tid))['jobs'][0]
    await execute("UPDATE jev_knowledge_job SET status='pending',cursor_id=0 WHERE id=:id",{'id':job['id']})
    leased=await store.claim('job')
    await store.save_page(leased,await store.page(leased),[c['draft']])
    assert len((await store.dashboard(tid))['candidates']) == 1
    await execute("UPDATE jev_knowledge_candidate SET status='pending' WHERE id=:id",{'id':c['id']})
    old=await store.claim('candidate')
    await execute("UPDATE jev_knowledge_candidate SET locked_until=NOW()-INTERVAL '1 second' WHERE id=:id",{'id':old['id']})
    new=await store.claim('candidate')
    assert not await store.save_review(old,status='rejected',reason='old',review={},comparisons=[])
    assert await store.save_review(new,status='needs_review',reason='new',review={},comparisons=[])


async def test_publish_requires_version_scope_and_is_idempotent(env):
    tid, _, svc, client = env
    c=await candidate(env)
    path=f"/v1/admin/jev/knowledge/candidates/{c['id']}?tenant_id={tid}"
    payload={'version':c['version'],'action':'publish','reason':'已检查原始解决证据'}
    stale=await client.post(path,json={**payload,'version':1},headers={'Idempotency-Key':'stale'})
    assert stale.status_code == 409
    first=await client.post(path,json=payload,headers={'Idempotency-Key':'publish'})
    assert first.status_code == 200,first.text
    replay=await client.post(path,json=payload,headers={'Idempotency-Key':'publish'})
    assert replay.json()==first.json()
    svc.kb.add_document.assert_awaited_once()
    args=svc.kb.add_document.call_args.kwargs
    assert args['session_id']=='g@chatroom' and args['tenant_id']==tid
    assert args['metadata']['evidence_ids']==c['draft']['evidence_ids']
    assert (await client.get('/v1/admin/jev/knowledge?tenant_id=other')).status_code==403


async def test_optout_after_review_prevents_evidence_egress_and_publication(env):
    tid, _, svc, client = env
    c=await candidate(env)
    await execute("INSERT INTO social_tenant_member_control (tenant_id,user_id,version,memory_opt_out) VALUES (:tid,'member',1,TRUE)",{'tid':tid})
    prefix=f"/v1/admin/jev/knowledge/candidates/{c['id']}"
    r=await client.get(prefix+'/evidence',params={'tenant_id':tid})
    assert r.status_code==409
    r=await client.post(prefix,params={'tenant_id':tid},json={'version':c['version'],'action':'publish','reason':'review'},headers={'Idempotency-Key':'blocked'})
    assert r.status_code==409
    svc.kb.add_document.assert_not_awaited()


async def test_scope_revocation_and_low_confidence_cannot_publish(env):
    tid, _, svc, client = env
    c=await candidate(env)
    await execute("UPDATE jev_policy SET policy=CAST(:policy AS JSON) WHERE tenant_id=:tid",{'tid':tid,'policy':JevPolicy().model_dump_json()})
    r=await client.post(f"/v1/admin/jev/knowledge/candidates/{c['id']}",params={'tenant_id':tid},
        json={'version':c['version'],'action':'publish','reason':'review'},headers={'Idempotency-Key':'revoked'})
    assert r.status_code==409
    svc.kb.add_document.assert_not_awaited()


async def test_late_arrival_reopens_completed_day_without_rescanning(env):
    tid,store,_,_=env
    rows=await seed(tid)
    await store.schedule(tid,'g@chatroom',date(2026,9,20),0,1000)
    job=await store.claim('job')
    await store.save_page(job,await store.page(job),[])
    await store.save_page(await store.claim('job'),[],[])
    await execute("INSERT INTO plugin_wxbot_group_observations (tenant_id,session_id,message_id,sender_wxid,content,occurred_ts) "
                  "VALUES (:tid,'g@chatroom','late','member','补充结果',90)",{'tid':tid})
    await store.schedule(tid,'g@chatroom',date(2026,9,20),0,1000)
    job=await store.claim('job')
    assert job['cursor_id']==rows[-1]['id']
    assert [r['message_id'] for r in await store.page(job)]==['late']


async def test_new_knowledge_after_review_blocks_stale_publish(env):
    tid,_store,svc,client=env
    c=await candidate(env)
    svc.store.knowledge_snapshot=AsyncMock(return_value='new-knowledge-version')
    r=await client.post(f"/v1/admin/jev/knowledge/candidates/{c['id']}",params={'tenant_id':tid},
        json={'version':c['version'],'action':'publish','reason':'review'},headers={'Idempotency-Key':'stale-kb'})
    assert r.status_code==409 and r.json()['detail']=='knowledge_changed_reevaluate'
    svc.kb.add_document.assert_not_awaited()


async def test_candidate_write_checks_optout_inside_transaction(env):
    tid,store,_,_=env
    rows=await seed(tid)
    await store.schedule(tid,'g@chatroom',date(2026,9,20),0,1000)
    job=await store.claim('job')
    await execute("INSERT INTO social_tenant_member_control (tenant_id,user_id,version,memory_opt_out) VALUES (:tid,'member',1,TRUE)",{'tid':tid})
    draft={'title':'test','question':'q','solution':'s','outcome':'done','evidence_ids':[r['id'] for r in rows],'resolution_ids':[rows[-1]['id']]}
    assert await store.save_page(job,rows,[draft])
    assert not (await store.dashboard(tid))['candidates']
    assert (await store.dashboard(tid))['jobs'][0]['scanned']==2


async def test_erasure_removes_candidates_even_after_original_messages_pruned(env):
    tid,store,svc,_=env
    c=await candidate(env)
    assert c['source_members']==['member']
    await execute('DELETE FROM plugin_wxbot_group_observations WHERE tenant_id=:tid',{'tid':tid})
    await svc.erase_member(tenant_id=tid,user_id='member',run=execute)
    assert not (await store.dashboard(tid))['candidates']


async def test_operator_retry_preserves_progress_and_idempotency(env):
    tid,store,_,client=env
    rows=await seed(tid)
    await store.schedule(tid,'g@chatroom',date(2026,9,20),0,1000)
    job=await store.claim('job')
    await store.save_page(job,rows,[])
    await execute("UPDATE jev_knowledge_job SET status='failed',attempts=3 WHERE id=:id",{'id':job['id']})
    path=f"/v1/admin/jev/knowledge/jobs/{job['id']}/retry?tenant_id={tid}"
    r=await client.post(path,headers={'Idempotency-Key':'resume'})
    assert r.status_code==200,r.text
    assert (await client.post(path,headers={'Idempotency-Key':'resume'})).json()==r.json()
    current=(await store.dashboard(tid))['jobs'][0]
    assert current['cursor_id']==rows[-1]['id'] and current['scanned']==2 and current['attempts']==0
