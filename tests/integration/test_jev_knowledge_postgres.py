"""Run only against an explicitly configured disposable PostgreSQL database."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
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
    import app.admin.jev_quality_router as qr
    import app.admin.jev_revision_router as rr
    import app.admin.jev_router as jr
    import app.jev.knowledge_store as ks
    import app.jev.store as js
    engine = create_async_engine(os.environ['JEV_TEST_DSN'])
    for module in (kr, jr, rr, qr, ks, js):
        monkeypatch.setattr(module, 'get_engine', lambda: engine)
    tid = 'knowledge-test-' + uuid4().hex[:12]
    policy = JevPolicy(knowledge=True, knowledge_sessions=['g@chatroom'])
    await execute('INSERT INTO jev_policy VALUES (:tid,1,CAST(:policy AS JSON))', {'tid':tid,'policy':policy.model_dump_json()})
    jev = JevService(Settings(typesafe_enabled=True), client=SimpleNamespace(evaluate=AsyncMock()))
    jev.registry = SimpleNamespace(scope_execution_allowed=AsyncMock(return_value=True))
    store = KnowledgeStore()
    @asynccontextmanager
    async def mutation_scope(*args):
        yield
    kb = SimpleNamespace(mutation_scope=mutation_scope,add_document=AsyncMock(return_value=123),get_document=AsyncMock(),list_documents=AsyncMock(return_value=[]))
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
    for table in ('jev_quality_finding','jev_knowledge_candidate','jev_knowledge_job','jev_evaluation','jev_policy','plugin_wxbot_group_observations','social_tenant_member_control'):
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
    from app.jev.models import fingerprint
    review["_evidence_hash"] = fingerprint(JevKnowledgeService.message_payload(messages))
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


async def test_revision_edit_requires_reapproval_and_tracks_published_baseline(env):
    import json

    from app.jev.models import fingerprint
    tid, store, svc, client=env
    c=await candidate(env)
    doc=SimpleNamespace(id=7,content_hash='a'*64,title='原始标题',content='仅测试环境',source='manual',url=None,meta={},session_id='g@chatroom')
    svc.kb.get_document.return_value=doc
    svc.kb.update_document=AsyncMock(return_value=7)
    await execute("UPDATE jev_knowledge_candidate SET comparisons=CAST(:c AS JSON) WHERE id=:id",
        {'id':c['id'],'c':json.dumps([{'doc_id':7,'session_id':'g@chatroom','content_hash':'a'*64}])})
    path=f"/v1/admin/jev/knowledge/candidates/{c['id']}"
    body={'version':c['version'],'target_doc_id':7,'base_hash':'a'*64,'title':'修正标题','content':'测试环境更新证书后恢复','reason':'核对群友结果'}
    response=await client.post(path+'/revision',params={'tenant_id':tid},json=body,headers={'Idempotency-Key':'edit'})
    assert response.status_code==200,response.text
    assert response.json()['status']=='pending'
    replay=await client.post(path+'/revision',params={'tenant_id':tid},json=body,headers={'Idempotency-Key':'edit'})
    assert replay.json()==response.json()
    premature=await client.post(path,params={'tenant_id':tid},json={'version':body['version']+1,'action':'apply_revision','reason':'审核'},headers={'Idempotency-Key':'premature'})
    assert premature.status_code==409
    running=await store.claim('candidate')
    revision=running['revision']
    revision.update(status='ready',evaluation={'answers':{'decision':{'choice':'accept','confidence':.95},'supported':{'noul':.95},'sensitive':{'noul':0}}},
        evidence_hash=fingerprint(svc.message_payload(await store.evidence(tid,'g@chatroom',c['draft']['evidence_ids']))))
    await store.save_review(running,status='revision_ready',reason='revision_supported',review=c['review'],comparisons=running['comparisons'],revision=revision)
    ready=await store.candidate(tid,'g@chatroom',c['id'])
    published=await client.post(path,params={'tenant_id':tid},json={'version':ready['version'],'action':'apply_revision','reason':'通过独立复核'},headers={'Idempotency-Key':'approve'})
    assert published.status_code==200,published.text
    svc.kb.update_document.assert_awaited_once()
    history=await client.get('/v1/admin/jev/knowledge/documents/7/history',params={'tenant_id':tid,'session_id':'g@chatroom'})
    assert history.status_code==200,history.text
    assert history.json()['items'][0]['revision']['before']['content']=='仅测试环境'
    other=await client.get('/v1/admin/jev/knowledge/documents/7/history',params={'tenant_id':tid,'session_id':'other@chatroom'})
    assert other.json()['items']==[]


async def test_changed_source_is_not_published(env):
    tid,_,svc,client=env
    c=await candidate(env)
    await execute("UPDATE plugin_wxbot_group_observations SET content='仍然失败，先前判断有误' WHERE id=:id",{'id':c['draft']['resolution_ids'][0]})
    response=await client.post(f"/v1/admin/jev/knowledge/candidates/{c['id']}",params={'tenant_id':tid},
        json={'version':c['version'],'action':'publish','reason':'核对'},headers={'Idempotency-Key':'changed-source'})
    assert response.status_code==409,response.text
    svc.kb.add_document.assert_not_awaited()


async def test_quality_findings_checkpoint_evidence_and_deduplication(env):
    tid,store,_,client=env
    rows=await seed(tid)
    await store.schedule(tid,'g@chatroom',date(2026,9,20),0,1000)
    job=await store.claim('job')
    finding={'finding':{'kind':'missed_help','title':'可能漏答','explanation':'缺少运行记录','evidence_ids':[rows[0]['id']],'trace_ids':[]},
        'review':{},'status':'needs_review','reason':'runtime_evidence_missing'}
    assert await store.save_page(job,rows,[],findings=[finding,finding])
    dashboard=await store.dashboard(tid)
    assert dashboard['jobs'][0]['quality_count']==1
    assert len(dashboard['findings'])==1 and not dashboard['candidates']
    f=dashboard['findings'][0]
    evidence=await client.get(f"/v1/admin/jev/knowledge/findings/{f['id']}/evidence",params={'tenant_id':tid})
    assert evidence.status_code==200,evidence.text
    assert evidence.json()['messages'][0]['id']==rows[0]['id']
    runtime=await store.runtime_evidence(tid,'g@chatroom',[rows[0]['id']])
    assert runtime[0]['trace_id'] is None and runtime[0]['deliveries']==[]


async def test_older_relevant_unresolved_candidate_is_recalled(env):
    import json
    tid,store,_,_=env
    c=await candidate(env)
    await execute("UPDATE jev_knowledge_candidate SET status='unresolved',created_at=NOW()-INTERVAL '10 days' WHERE id=:id",{'id':c['id']})
    for i in range(12):
        draft={**c['draft'],'question':f'其他问题 {i}'}
        await execute("INSERT INTO jev_knowledge_candidate (id,job_id,tenant_id,session_id,fingerprint,draft,source_members,status) "
            "VALUES (:id,:job,:tid,'g@chatroom',:hash,CAST(:draft AS JSON),'[]','unresolved')",
            {'id':str(uuid4()),'job':c['job_id'],'tid':tid,'hash':str(i),'draft':json.dumps(draft)})
    recalled=await store.open_candidates(tid,'g@chatroom',messages=[{'content':'证书错误通过更新证书解决了'}])
    assert recalled[0]['id']==c['id'] and len(recalled)==8


async def test_optout_during_review_releases_lease(env):
    tid,store,_,_=env
    c=await candidate(env)
    await execute("UPDATE jev_knowledge_candidate SET status='pending' WHERE id=:id",{'id':c['id']})
    running=await store.claim('candidate')
    await execute("INSERT INTO social_tenant_member_control (tenant_id,user_id,memory_opt_out,version) VALUES (:tid,'member',TRUE,1)",{'tid':tid})
    assert not await store.save_review(running,status='ready',reason='reviewed',review=c['review'],comparisons=[])
    current=await store.candidate(tid,'g@chatroom',c['id'])
    assert current['status']=='skipped' and current['lease_token'] is None


async def test_real_kb_revision_lock_serializes_writers_and_failed_index_preserves_baseline(env):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.kb.ingest import IngestionService
    from app.kb.service import KnowledgeBaseService, SQLAlchemyKBStore
    from app.kb.vector.memory_store import InMemoryVectorStore
    from tests.unit._fake_llm import FakeEmbeddingsProvider

    tid, _, _, _=env
    engine=create_async_engine(os.environ['JEV_TEST_DSN'])
    factory=async_sessionmaker(engine,expire_on_commit=False)
    @asynccontextmanager
    async def sessions():
        async with factory() as session, session.begin():
            yield session
    store=SQLAlchemyKBStore(sessions)
    vector=InMemoryVectorStore()
    ingest=IngestionService(store,vector,FakeEmbeddingsProvider(),settings=Settings())
    kb=KnowledgeBaseService(store,vector,ingest,settings=Settings())
    doc_id=await kb.add_document(tenant_id=tid,session_id='g@chatroom',title='旧知识',content='旧正文有效')
    attempted,finished=asyncio.Event(),asyncio.Event()
    async def writer():
        attempted.set()
        await kb.update_document(tenant_id=tid,session_id='g@chatroom',doc_id=doc_id,title='并发标题',content='并发更新')
        finished.set()
    try:
        async with kb.mutation_scope(tid,['','g@chatroom']):
            async with asyncio.timeout(3):
                await kb.update_document(tenant_id=tid,session_id='g@chatroom',doc_id=doc_id,title='修订标题',content='审核后的正文')
            task=asyncio.create_task(writer())
            await attempted.wait()
            assert not finished.is_set()
        await asyncio.wait_for(task,3)
        baseline=await kb.get_document(tid,doc_id,session_id='g@chatroom')
        vector.upsert=AsyncMock(side_effect=RuntimeError('index unavailable'))
        with pytest.raises(RuntimeError,match='index unavailable'):
            async with kb.mutation_scope(tid,['','g@chatroom']):
                await kb.update_document(tenant_id=tid,session_id='g@chatroom',doc_id=doc_id,title='不应发布',content='失败正文')
        retained=await kb.get_document(tid,doc_id,session_id='g@chatroom')
        assert retained.content==baseline.content and retained.content_hash==baseline.content_hash
    finally:
        await kb.delete_document(tid,doc_id,session_id='g@chatroom')
        await engine.dispose()


async def test_console_pages_survive_new_inserts_and_reject_cross_scope_cursor(env):
    import json
    tid,store,_,client=env
    c=await candidate(env)
    for i in range(5):
        await execute("INSERT INTO jev_knowledge_candidate (id,job_id,tenant_id,session_id,fingerprint,draft,source_members,status) "
            "VALUES (:id,:job,:tid,'g@chatroom',:hash,CAST(:draft AS JSON),'[]','unresolved')",
            {'id':str(uuid4()),'job':c['job_id'],'tid':tid,'hash':str(i),'draft':json.dumps(c['draft'])})
    first=await store.dashboard(tid,'g@chatroom',candidate_status='unresolved',page_size=2)
    cursor=first['pagination']['candidates']
    assert cursor and len(first['candidates'])==2
    await execute("INSERT INTO jev_knowledge_candidate (id,job_id,tenant_id,session_id,fingerprint,draft,source_members,status) "
        "VALUES (:id,:job,:tid,'g@chatroom','new',CAST(:draft AS JSON),'[]','unresolved')",
        {'id':str(uuid4()),'job':c['job_id'],'tid':tid,'draft':json.dumps(c['draft'])})
    second=await store.dashboard(tid,'g@chatroom',candidate_status='unresolved',candidate_cursor=cursor,page_size=2)
    third=await store.dashboard(tid,'g@chatroom',candidate_status='unresolved',candidate_cursor=second['pagination']['candidates'],page_size=2)
    ids=[r['id'] for page in [first,second,third] for r in page['candidates']]
    assert len(ids)==len(set(ids))==5 and not third['pagination']['candidates']
    for query in [{'session_id':'other@chatroom','candidate_status':'unresolved','candidate_cursor':cursor},
                  {'session_id':'g@chatroom','candidate_status':'ready','candidate_cursor':cursor},
                  {'candidate_cursor':'broken'}]:
        response=await client.get('/v1/admin/jev/knowledge',params={'tenant_id':tid,**query})
        assert response.status_code==400,response.text


async def quality_finding(env):
    tid,store,_,_=env
    rows=await seed(tid)
    await store.schedule(tid,'g@chatroom',date(2026,9,20),0,1000)
    job=await store.claim('job')
    finding={'finding':{'kind':'missed_help','title':'复盘候选','explanation':'待核验','evidence_ids':[rows[0]['id']],'trace_ids':[]},
        'review':{'model':'jev-test'},'status':'needs_review','reason':'low_confidence'}
    await store.save_page(job,rows,[],findings=[finding])
    return (await store.dashboard(tid))['findings'][0]


async def test_manual_quality_review_requires_current_evidence_and_is_idempotent(env):
    tid,store,_,client=env
    f=await quality_finding(env)
    path=f"/v1/admin/jev/knowledge/findings/{f['id']}"
    payload={'action':'confirm','expected_status':'needs_review','reason':'核对了原始问题和实际处理记录'}
    missing=await client.post(path,params={'tenant_id':tid},json=payload,headers={'Idempotency-Key':'missing'})
    assert missing.status_code==409,missing.text
    evidence=await client.get(path+'/evidence',params={'tenant_id':tid})
    assert evidence.status_code==200,evidence.text
    payload['evidence_hash']=evidence.json()['evidence_hash']
    confirmed=await client.post(path,params={'tenant_id':tid},json=payload,headers={'Idempotency-Key':'review'})
    assert confirmed.status_code==200,confirmed.text
    replay=await client.post(path,params={'tenant_id':tid},json=payload,headers={'Idempotency-Key':'review'})
    assert replay.json()==confirmed.json()
    stale=await client.post(path,params={'tenant_id':tid},json={**payload,'action':'dismiss'},headers={'Idempotency-Key':'different'})
    assert stale.status_code==409
    row=(await store.dashboard(tid))['findings'][0]
    assert row['status']=='confirmed' and row['review']['model']=='jev-test'
    assert row['review']['_operator']['reason']==payload['reason']
    assert not (await store.dashboard(tid))['candidates']


async def test_quality_confirmation_rejects_changed_evidence_and_dismissal_retains_reason(env):
    tid,store,_,client=env
    f=await quality_finding(env)
    path=f"/v1/admin/jev/knowledge/findings/{f['id']}"
    evidence=await client.get(path+'/evidence',params={'tenant_id':tid})
    await execute("UPDATE plugin_wxbot_group_observations SET content='已经得到回答' WHERE id=:id",{'id':f['finding']['evidence_ids'][0]})
    payload={'action':'confirm','expected_status':'needs_review','reason':'核验','evidence_hash':evidence.json()['evidence_hash']}
    response=await client.post(path,params={'tenant_id':tid},json=payload,headers={'Idempotency-Key':'changed'})
    assert response.status_code==409 and response.json()['detail']=='finding_evidence_changed_reload'
    denied=await client.post(path,params={'tenant_id':'other'},json=payload,headers={'Idempotency-Key':'denied'})
    assert denied.status_code==403
    response=await client.post(path,params={'tenant_id':tid},json={**payload,'action':'dismiss','reason':'群友已经提供了正确答案'},headers={'Idempotency-Key':'dismiss'})
    assert response.status_code==200,response.text
    assert (await store.dashboard(tid,finding_status='dismissed'))['findings'][0]['review']['_operator']['reason']=='群友已经提供了正确答案'
