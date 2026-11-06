"""Automatic disposition, policy replay and bounded evidence lifecycle regressions."""
from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.jev.models import JevPolicy, memory_fingerprint
from plugins.memory import store as runtime
from plugins.memory.group_evidence import assess_group_evidence
from plugins.memory.store import MemoryStore
from tests.unit.test_jev_service import service
from tests.unit.test_memory_group_evidence import item, relation, row


@pytest.mark.parametrize('mode,global_shadow,expected', [
    ('auto', True, False), ('auto', False, False), ('shadow', False, True),
    ('shadow', True, True), ('inherit', True, True), ('inherit', False, False),
])
def test_memory_mode_is_independent(mode, global_shadow, expected):
    policy = JevPolicy(memory_mode=mode, shadow_only=global_shadow)
    assert policy.memory_shadow_only() is expected
    assert policy.shadow_only is global_shadow
    assert policy.participation_shadow_only is True


async def test_shadow_history_replays_once_when_automation_or_threshold_changes():
    svc = service(JevPolicy(memory_mode='auto'))
    current = item()
    current['value']['jev'] = {
        'content_hash': memory_fingerprint(current),
        'policy_key': JevPolicy(memory_mode='shadow').memory_review_key(svc.settings.typesafe_model),
    }
    assert await svc.enqueue_memory(current, run=AsyncMock())
    first = svc.store.enqueue.call_args.kwargs
    current['value']['jev']['policy_key'] = JevPolicy(memory_mode='auto').memory_review_key(svc.settings.typesafe_model)
    assert not await svc.enqueue_memory(current, run=AsyncMock())
    svc.store.policy.return_value = (JevPolicy(memory_mode='auto', min_confidence=.9), 2)
    svc._policies.clear()
    assert await svc.enqueue_memory(current, run=AsyncMock())
    assert svc.store.enqueue.call_args.kwargs['fingerprint'] != first['fingerprint']
    current['value']['jev']['policy_key'] = JevPolicy(memory_mode='auto', min_confidence=.9).memory_review_key(svc.settings.typesafe_model)
    current['value']['relation']['evidence_observation_ids'].append(3)
    assert await svc.enqueue_memory(current, run=AsyncMock())


@pytest.mark.parametrize('changes,actor', [
    ({'pinned': True}, ''), ({'source_type': 'manual'}, ''),
    ({'status': 'archived'}, ''), ({'status': 'invalidated'}, ''),
    ({}, 'admin/operator'),
])
async def test_replay_preserves_corrections_and_retirement(changes, actor):
    svc = service(JevPolicy(memory_mode='auto'))
    current = item(**changes)
    current['value']['acceptance']['reviewed_by'] = actor
    assert not await svc.enqueue_memory(current, run=AsyncMock())
    svc.store.enqueue.assert_not_awaited()


@pytest.mark.parametrize('choice,score,truncated,actor,pinned,mode,expected', [
    ('accepted', .99, False, 'system/auto', False, 'auto', 'accept'),
    ('rejected', .99, False, 'system/auto', False, 'auto', 'reject'),
    ('accepted', .4, False, 'system/auto', False, 'auto', 'needs_review'),
    ('rejected', .4, False, 'system/auto', False, 'auto', 'needs_review'),
    ('needs_review', .99, False, 'system/auto', False, 'auto', 'needs_review'),
    ('rejected', .99, True, 'system/auto', False, 'auto', 'needs_review'),
    ('accepted', .99, False, 'admin/operator', False, 'auto', None),
    ('accepted', .99, False, 'system/auto', True, 'auto', None),
    ('accepted', .99, False, 'system/auto', False, 'shadow', None),
    ('bad-choice', .99, False, 'system/auto', False, 'auto', None),
])
async def test_automatic_disposition(monkeypatch, choice, score, truncated, actor, pinned, mode, expected):
    current = item(pinned=pinned)
    current['value']['acceptance']['reviewed_by'] = actor
    store = MemoryStore(SimpleNamespace())
    @asynccontextmanager
    async def transaction():
        yield
    store._mutation_transaction = transaction
    store._jev_current = AsyncMock(return_value=current)
    store.jev_state = AsyncMock(return_value={
        'evidence_quality': assess_group_evidence(relation(), [row()]),
        'evidence_truncated': truncated,
    })
    store.get_memory_item = AsyncMock(side_effect=lambda *a, **k: copy.deepcopy(current))
    async def update(_id, **kwargs):
        current['value'] = kwargs['value_json']
        return current
    store.update_memory_item = AsyncMock(side_effect=update)
    store.review_memory_item_acceptance = AsyncMock()
    store.jev_service = SimpleNamespace(store=SimpleNamespace(finish=AsyncMock()))
    monkeypatch.setattr(runtime, '_exec', AsyncMock(return_value=[{'id': 1}]))
    result = {'model': 'test', 'answers': {
        'decision': {'choice': choice, 'confidence': score},
        'supported': {'noul': .99}, 'sensitive': {'noul': 0}, 'durable_preference': {'noul': .99},
    }}
    await store.apply_jev_result({'id': 1, 'lease_token': 'test', 'domain': 'relationship'},
                                 result, policy=JevPolicy(memory_mode=mode), duration_ms=1)
    if expected:
        assert store.review_memory_item_acceptance.call_args.kwargs['action'] == expected
        assert current['value']['jev']['applied'] is True
        if expected == 'needs_review':
            assert current['value']['jev']['lifecycle'] == 'observing'
    else:
        store.review_memory_item_acceptance.assert_not_awaited()


@pytest.mark.parametrize('count,body_size,truncated', [(12, 900, False), (48, 400, False), (20, 1500, True)])
async def test_expanded_evidence_is_complete_or_explicitly_observing(monkeypatch, count, body_size, truncated):
    current = item()
    current['value']['relation']['evidence_observation_ids'] = list(range(1, count + 1))
    store = MemoryStore(SimpleNamespace())
    store._jev_current = AsyncMock(return_value=current)
    store._resolve_group_graph_session_ids = AsyncMock(return_value=['room@chatroom'])
    store._member_memory_write_blocked = AsyncMock(return_value=False)
    async def execute(sql, params):
        assert params['tid'] == 'demo'
        return [{'id': i, 'content': '我一直喜欢 ModelX。' + '字' * body_size,
                 'sender_wxid': 'member-a', 'sender_name': '', 'msg_type': 'text'} for i in params['ids']]
    monkeypatch.setattr(runtime, '_exec', execute)
    state = await store.jev_state({'domain': 'relationship'})
    assert state['evidence_truncated'] is truncated
    assert sum(len(entry['text']) for entry in state['evidence']) <= 24000
    assert state['evidence_quality']['status'] == ('needs_review' if truncated else 'grounded')


async def test_background_sweep_reaches_old_records_and_wraps():
    svc = service(JevPolicy(memory_mode='auto'))
    svc.memory_store = SimpleNamespace(get_memory_item=AsyncMock(return_value=item()))
    svc.store.maintain = AsyncMock()
    svc.store.memory_changes = AsyncMock(return_value=[])
    svc.store.memory_review_page = AsyncMock(return_value=[{'id': 10}])
    svc.store.claim = AsyncMock(return_value=[])
    svc.enqueue_memory = AsyncMock()
    svc._memory_review_cursor = 9
    await svc.run_batch()
    svc.store.memory_review_page.assert_awaited_once_with(9)
    svc.enqueue_memory.assert_awaited_once()
    assert svc._memory_review_cursor == 0


async def test_mode_roundtrip_has_a_new_job_identity_but_stable_records_do_not_requeue():
    svc = service(JevPolicy(memory_mode='auto'))
    current = item()
    current['value']['jev'] = {'evaluation_id': 'shadow-1'}
    assert await svc.enqueue_memory(current, run=AsyncMock())
    before = svc.store.enqueue.call_args.kwargs['fingerprint']
    current['value']['jev'] = {'evaluation_id': 'shadow-2'}
    assert await svc.enqueue_memory(current, run=AsyncMock())
    assert svc.store.enqueue.call_args.kwargs['fingerprint'] != before
    current['value']['jev'].update(
        content_hash=memory_fingerprint(current),
        policy_key=JevPolicy(memory_mode='auto').memory_review_key(svc.settings.typesafe_model),
    )
    assert not await svc.enqueue_memory(current, run=AsyncMock())


async def test_owner_disabled_never_enqueues_a_replay():
    svc = service(JevPolicy(memory_mode='auto'))
    svc.registry.scope_execution_allowed.return_value = False
    assert not await svc.enqueue_memory(item(), run=AsyncMock())
    svc.store.enqueue.assert_not_awaited()
