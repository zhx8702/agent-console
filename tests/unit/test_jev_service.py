from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.common.config import Settings
from app.common.intent import IntentDecision, IntentDomain
from app.common.intent_classify import StaticIntentClassifier
from app.jev.intent import JevIntentClassifier
from app.jev.models import JevPolicy, confidence, memory_fingerprint, probability, redact
from app.jev.service import JevService


def result(choice='accepted', confidence_value=.95):
    return {'model': 'jev-test', 'answers': {'decision': {'choice': choice, 'confidence': confidence_value},
        'supported': {'noul': .95}, 'sensitive': {'noul': .01}}, 'usage': {'input_tokens': 10, 'output_tokens': 2}}


def service(policy=None, response=None):
    settings = Settings(typesafe_enabled=True, typesafe_online_timeout=.02)
    store = SimpleNamespace(policy=AsyncMock(return_value=(policy or JevPolicy(), 1)),
        enqueue=AsyncMock(return_value='job'), record_online=AsyncMock(), finish=AsyncMock())
    client = SimpleNamespace(evaluate=AsyncMock(return_value=response or result()), aclose=AsyncMock())
    svc = JevService(settings, client=client, store=store)
    svc.registry = SimpleNamespace(scope_execution_allowed=AsyncMock(return_value=True))
    return svc


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -1, 2, None, 'bad'])
def test_confidence_is_finite_and_bounded(value):
    assert confidence(result(confidence_value=value)) == 0
    assert probability({'answers': {'supported': {'noul': value}}}, 'supported', fallback=0) == 0


def test_redaction_and_fingerprint():
    masked = redact('wxid_someone https://secret.test apikey_secret cx1:p:abcd person Alice', {'Alice': 'person_1'})
    assert all(x not in masked for x in ('wxid_someone', 'secret.test', 'apikey_secret', 'cx1:p:abcd', 'Alice'))
    item = {'value': {'relation': {'predicate': 'friend'}}, 'status': 'active'}
    audited = {**item, 'value': {'jev': result(), 'relation': {'predicate': 'friend', 'typesafe_shadow': result()}}}
    assert memory_fingerprint(item) == memory_fingerprint(audited)
    assert memory_fingerprint(item) != memory_fingerprint({**item, 'pinned': True})


async def test_shadow_queues_without_calling_upstream():
    svc = service()
    assert await svc.online(tenant_id='t', session_id='s', domain='intent', state={'message':'hi'}, trace_id='tr') == (None, False)
    svc.store.enqueue.assert_awaited_once()
    svc.client.evaluate.assert_not_awaited()


async def test_timeout_falls_back_and_is_audited():
    svc = service(JevPolicy(shadow_only=False))
    async def slow(**kwargs):
        await asyncio.sleep(10)
    svc.client.evaluate.side_effect = slow
    assert await asyncio.wait_for(svc.online(tenant_id='t', session_id='s', domain='intent', state={}, trace_id='tr'), .2) == (None, False)
    assert svc.store.record_online.call_args.kwargs['error'] == 'TimeoutError'


async def test_policy_change_during_request_prevents_apply():
    svc = service(JevPolicy(shadow_only=False), result('flag'))
    svc.store.policy.side_effect = [(JevPolicy(shadow_only=False), 1), (JevPolicy(shadow_only=True), 2)]
    _, active = await svc.online(tenant_id='t', session_id='s', domain='moderation', state={}, trace_id='tr')
    assert not active
    assert not svc.store.record_online.call_args.kwargs['applied']


async def test_owner_denied_and_zero_sampling_skip():
    svc = service()
    svc.registry.scope_execution_allowed.return_value = False
    await svc.online(tenant_id='t', session_id='s', domain='moderation', state={}, trace_id='tr')
    svc.store.enqueue.assert_not_awaited()
    svc.client.evaluate.assert_not_awaited()
    svc = service(JevPolicy(sample_rate=0))
    await svc.online(tenant_id='t', session_id='s', domain='intent', state={}, trace_id='tr')
    svc.store.enqueue.assert_not_awaited()


async def test_jev_can_correct_assistant_request_but_never_invent_tool():
    primary = StaticIntentClassifier(IntentDecision.from_dict({'domain':'handoff','action':'request','confidence':.95}))
    svc = service(JevPolicy(shadow_only=False))
    response = result('abstain')
    response['answers']['domain'] = {'choice':'chitchat','confidence':.95}
    svc.online = AsyncMock(return_value=(response, True))
    classifier = JevIntentClassifier(primary, svc)
    decision = await classifier.classify('你现在是我的助理，帮我解答群里的问题', context={'tenant_id':'t','mentioned_me':True})
    assert decision.domain is IntentDomain.CHITCHAT
    assert not decision.needs_tool
    response['answers']['domain']['choice'] = 'draw'
    decision = await classifier.classify('请帮我处理这个请求', context={'tenant_id':'t','mentioned_me':True})
    assert decision.domain is IntentDomain.NONE
    assert not decision.needs_tool


async def test_slow_jev_does_not_block_window_persistence():
    from plugins.memory.store import MemoryStore
    store = MemoryStore(SimpleNamespace(typesafe_enabled=True, typesafe_group_graph_shadow_timeout_seconds=.1))
    async def slow(**kwargs):
        await asyncio.sleep(10)
    store.typesafe_client = SimpleNamespace(evaluate_group_relationship=AsyncMock(side_effect=slow))
    store._load_group_relationship_events = AsyncMock(return_value=[{'id':1}])
    store._build_group_relationship_windows = lambda *a, **k: [{'index':0,'event_ids':[1],'sender_ids':['a','b'],'first_event_id':1,'last_event_id':1,'rows':[]}]
    store._load_group_member_directory = AsyncMock(return_value={})
    store._resolve_group_graph_session_ids = AsyncMock(return_value=['room@chatroom'])
    store._build_deterministic_group_window_candidates = lambda *a, **k: [{'subject':'a','object':f'b{i}','predicate':'replied_to','signals':{'quote':1},'evidence_event_ids':[1]} for i in range(20)]
    store._apply_group_relationship_window_candidate = AsyncMock(return_value={'id':1})
    for name in ('_refresh_legacy_cache_for_item_scope','_sync_memory_graph_for_item_safe','_sync_memory_vector_for_item_safe'):
        setattr(store, name, AsyncMock())
    response = await store.run_group_relationship_window_catchup(tenant_id='demo',channel='wechat',source_key='wxbot',session_id='room@chatroom',date='2026-09-20',max_windows_per_run=1,include_llm=False,time_budget_seconds=1)
    assert store._apply_group_relationship_window_candidate.await_count == 20
    assert response['next_cursor_event_id'] == 1
    store.typesafe_client.evaluate_group_relationship.assert_not_awaited()


async def test_help_seeking_has_independent_shadow_and_group_scope():
    policy = JevPolicy(shadow_only=True, participation_shadow_only=False, help_sessions=['room@chatroom'])
    svc = service(policy, result('reply'))
    assert await svc.online(tenant_id='t',session_id='other@chatroom',domain='participation',state={},trace_id='t') == (None,False)
    svc.client.evaluate.assert_not_awaited()
    _, active = await svc.online(tenant_id='t',session_id='room@chatroom',domain='participation',state={},trace_id='t')
    assert active


def test_help_group_ids_trim_deduplicate_and_reject_non_groups():
    from pydantic import ValidationError
    policy = JevPolicy(help_sessions=[' room@chatroom ', '', 'room@chatroom', 'other@chatroom'])
    assert policy.help_sessions == ['room@chatroom', 'other@chatroom']
    for invalid in [['user'], ['has spaces@chatroom'], [None]]:
        with pytest.raises(ValidationError):
            JevPolicy(help_sessions=invalid)


async def test_low_confidence_reply_records_reason_and_trace_without_applying():
    svc = service(JevPolicy(participation_shadow_only=False, help_sessions=['room@chatroom']), result('reply', .61))
    response, active = await svc.online(tenant_id='t', session_id='room@chatroom', domain='participation',
                                       state={'message':'how?'}, trace_id='trace-help')
    assert not active
    assert response['_audit'] == {'reason':'low_confidence', 'mode':'active', 'trace_id':'trace-help',
                                  'min_confidence':.8, 'effective_decision':''}
    assert svc.store.record_online.call_args.kwargs['result']['_audit'] == response['_audit']
    assert '_audit' not in svc.client.evaluate.call_args.kwargs['state']


async def test_whole_online_deadline_bounds_slow_owner_lookup():
    svc = service(JevPolicy(shadow_only=False))
    async def blocked(*args, **kwargs):
        await asyncio.sleep(10)
    svc.registry.scope_execution_allowed.side_effect = blocked
    response = await asyncio.wait_for(svc.online(tenant_id='t',session_id='s',domain='moderation',
                                                state={},trace_id='tr'), .15)
    assert response == (None, False)
    svc.client.evaluate.assert_not_awaited()
    assert svc.store.record_online.call_args.kwargs['result']['_audit']['reason'] == 'online_timeout'


async def test_whole_online_deadline_keeps_cancellation_visible():
    svc = service()
    svc.store.policy.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await svc.online(tenant_id='t',session_id='s',domain='intent',state={},trace_id='tr')
    svc.store.record_online.assert_not_awaited()


async def test_queued_help_revocation_blocks_upstream_even_with_cached_policy():
    enabled = JevPolicy(help_sessions=['room@chatroom'])
    svc = service(enabled)
    await svc.policy('t')
    svc.store.policy.return_value = (JevPolicy(), 2)
    await svc._process({'tenant_id':'t', 'session_id':'room@chatroom', 'domain':'participation',
                       'state':{'message':'help', '_audit_context':{'trace_id':'tr'}},
                       'target_id':None, 'attempts':1})
    svc.client.evaluate.assert_not_awaited()
    assert svc.store.finish.call_args.kwargs['status'] == 'skipped'
    assert svc.store.finish.call_args.kwargs['error'] == 'group_removed'


async def test_queued_shadow_preserves_trace_without_sending_it_upstream():
    svc = service()
    await svc.online(tenant_id='t',session_id='s',domain='intent',state={'message':'hello'},trace_id='trace-intent')
    state = svc.store.enqueue.call_args.kwargs['state']
    await svc._process({'tenant_id':'t','session_id':'s','domain':'intent', 'state':state,
                       'target_id':None, 'attempts':1})
    assert svc.client.evaluate.call_args.kwargs['state'] == {'message':'hello'}
    assert svc.store.finish.call_args.kwargs['result']['_audit']['trace_id'] == 'trace-intent'
    assert state['_audit_context']['trace_id'] == 'trace-intent'


async def test_participation_context_preserves_speaker_without_identifiers():
    svc = service()
    svc.store.recent_participation_messages = AsyncMock(return_value=[
        {'message_id':'current','sender_wxid':'wxid_a','sender_name':'小甲','content':'还是不行','is_self_sent':False},
        {'message_id':'reply','sender_wxid':'wxid_bot','sender_name':'机器人','content':'试试更新证书','is_self_sent':True},
        {'message_id':'question','sender_wxid':'wxid_a','sender_name':'小甲','content':'小甲的证书报错','is_self_sent':False},
    ])
    state = await svc.participation_state(tenant_id='t',session_id='room@chatroom',message='还是不行',sender_id='wxid_a',message_id='current')
    assert state['message']=='还是不行'
    assert len(state['recent_messages'])==2
    assert state['recent_messages'][0]['speaker']=='current_speaker'
    assert state['recent_messages'][1]['speaker']=='assistant'
    assert '小甲' not in str(state) and 'wxid_a' not in str(state)


async def test_participation_context_failure_keeps_current_message():
    svc = service()
    svc.store.recent_participation_messages=AsyncMock(side_effect=RuntimeError('db unavailable'))
    state=await svc.participation_state(tenant_id='t',session_id='room@chatroom',message='请帮忙')
    assert state['message']=='请帮忙'
    assert 'recent_messages' not in state
