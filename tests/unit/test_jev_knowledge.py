from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.common.config import Settings
from app.jev.knowledge import JevKnowledgeService, KnowledgeEvidenceChanged, KnowledgeScopeDisabled
from app.jev.knowledge_models import (
    KnowledgeDraft,
    check_evidence,
    day_bounds,
    due_days,
    parse_extraction,
    review_disposition,
)
from app.jev.models import JevPolicy
from app.jev.service import JevService


def draft(**changes):
    return KnowledgeDraft.model_validate({"title": "修复连接", "question": "连接失败怎么办", "solution": "更新证书后重试", "outcome": "群友确认已恢复", "evidence_ids": [1, 2], "resolution_ids": [2], **changes})


def messages():
    return [{"id": 1, "content": "连接失败怎么办", "sender_wxid": "a", "sender_name": "成员甲", "is_self_sent": False, "occurred_ts": 10},
            {"id": 2, "content": "更新证书后恢复了", "sender_wxid": "a", "sender_name": "成员甲", "is_self_sent": False, "occurred_ts": 20}]


def result(choice="retain", **changes):
    answers = {"decision": {"choice": choice, "confidence": .95}, "supported": {"noul": .96}, "resolved": {"noul": .95}, "sensitive": {"noul": .01}}
    answers.update(changes)
    return {"answers": answers, "model": "jev-test"}


def service():
    policy = JevPolicy(knowledge=True, knowledge_sessions=["g@chatroom"])
    jev = JevService(Settings(typesafe_enabled=True), client=SimpleNamespace(evaluate=AsyncMock(return_value=result())))
    jev.store = SimpleNamespace(policy=AsyncMock(return_value=(policy, 1)), record_online=AsyncMock())
    jev.registry = SimpleNamespace(scope_execution_allowed=AsyncMock(return_value=True))
    store = SimpleNamespace(runtime_evidence=AsyncMock(return_value=[]), open_candidates=AsyncMock(return_value=[]), candidate=AsyncMock(), knowledge_snapshot=AsyncMock(return_value="snapshot"), blocked_members=AsyncMock(return_value=set()), page=AsyncMock(return_value=messages()),
        context=AsyncMock(return_value=[]), save_page=AsyncMock(return_value=True), evidence=AsyncMock(return_value=messages()),
        save_review=AsyncMock(return_value=True), policies=AsyncMock(return_value=[('t', policy)]), schedule=AsyncMock())
    llm = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(content='{"candidates":['+draft().model_dump_json()+']}')))
    kb = SimpleNamespace(list_documents=AsyncMock(side_effect=lambda *a, **k: [1] if k.get("session_id") else []), search_documents=AsyncMock(return_value=[]), get_document=AsyncMock())
    return JevKnowledgeService(jev, llm=llm, kb=kb, store=store)


def row():
    return {"id": "j", "tenant_id": "t", "session_id": "g@chatroom", "cursor_id": 0, "attempts": 1, "draft": draft().model_dump()}


def test_default_policy_disabled_and_invalid_timezones_rejected():
    assert not JevPolicy().knowledge
    assert JevPolicy(knowledge_sessions=['  g@chatroom ', '', 'g@chatroom']).knowledge_sessions == ['g@chatroom']
    with pytest.raises(ValidationError):
        JevPolicy(knowledge_timezone='No/Such_Zone')


def test_daily_period_is_previous_local_day_and_dst_aware():
    days = due_days(datetime(2026, 9, 20, 20, tzinfo=UTC), 'Asia/Shanghai', 3)
    assert days[-1] == date(2026, 9, 20) and len(days) == 7
    assert not due_days(datetime(2026, 9, 20, 17, tzinfo=UTC), 'Asia/Shanghai', 3)
    start, end = day_bounds(date(2026, 3, 8), 'America/New_York')
    assert end-start == 23*3600


@pytest.mark.parametrize('changes', [{'evidence_ids':[9]}, {'resolution_ids':[9]}, {'evidence_ids':[]}])
def test_invalid_evidence_cannot_be_stored(changes):
    with pytest.raises((ValueError, ValidationError)):
        check_evidence(draft(**changes), messages())


def test_bot_reply_cannot_prove_success():
    msgs = messages()
    msgs[1]['is_self_sent'] = True
    with pytest.raises(ValueError, match='bot_cannot_confirm'):
        check_evidence(draft(), msgs)
    for msg in msgs:
        msg['is_self_sent'] = True
    with pytest.raises(ValueError, match='bot_only'):
        check_evidence(draft(resolution_ids=[]), msgs)


@pytest.mark.parametrize(('response', 'expected'), [
    (result(), 'ready'), (result('unresolved'), 'unresolved'), (result('reject'), 'rejected'),
    (result(supported={'noul':float('nan')}), 'needs_review'),
    (result(sensitive={'noul':.8}), 'needs_review'),
    (result(resolved={'noul':.5}), 'needs_review'),
    (result(decision={'choice':'retain','confidence':.7}), 'needs_review'),
])
def test_review_gates(response, expected):
    assert review_disposition(draft(), response, .9)[0] == expected
    assert review_disposition(draft(resolution_ids=[]), result(), .9)[0] == 'needs_review'


def test_parser_rejects_prose_and_accepts_empty_candidates():
    assert parse_extraction('```json\n{"candidates":[]}\n```').candidates == []
    with pytest.raises(ValueError):
        parse_extraction('Here are some ideas')


async def test_extract_without_bot_interaction_keeps_original_evidence():
    svc = service()
    await svc.extract_page(row())
    submitted = svc.llm.chat.call_args.args[0]
    assert '连接失败怎么办' in submitted.messages[0].content
    assert '成员甲' not in submitted.messages[0].content
    assert svc.store.save_page.call_args.args[2][0]['resolution_ids'] == [2]


async def test_invalid_model_evidence_does_not_advance_cursor():
    svc = service()
    svc.llm.chat.return_value.content = '{"candidates":['+draft(evidence_ids=[999],resolution_ids=[]).model_dump_json()+']}'
    with pytest.raises(ValueError):
        await svc.extract_page(row())
    svc.store.save_page.assert_not_awaited()


async def test_scope_removed_during_extraction_prevents_candidate_write():
    svc = service()
    async def removed(request):
        svc.jev.store.policy.return_value = (JevPolicy(), 2)
        return SimpleNamespace(content='{"candidates":['+draft().model_dump_json()+']}')
    svc.llm.chat.side_effect = removed
    with pytest.raises(KnowledgeScopeDisabled):
        await svc.extract_page(row())
    svc.store.save_page.assert_not_awaited()


async def test_member_opt_out_excludes_messages_before_egress():
    svc = service()
    svc.store.blocked_members.return_value = {'a'}
    await svc.extract_page(row())
    svc.llm.chat.assert_not_awaited()
    assert svc.store.save_page.call_args.args[2] == []


async def test_removed_evidence_prevents_review():
    svc = service()
    svc.store.evidence.return_value = messages()[:1]
    with pytest.raises(KnowledgeEvidenceChanged):
        await svc.review_candidate(row())
    svc.jev.client.evaluate.assert_not_awaited()


async def test_ready_is_not_automatically_published():
    svc = service()
    await svc.review_candidate(row())
    assert svc.store.save_review.call_args.kwargs['status'] == 'ready'
    assert svc.kb.search_documents.call_args.kwargs['raise_on_error'] is True


@pytest.mark.parametrize(('relation','expected'), [('duplicate','duplicate'),('conflict','needs_review'),('supplement','needs_review'),('review','needs_review'),('unrelated','ready')])
async def test_semantic_comparison_controls_disposition(relation, expected):
    svc = service()
    svc.kb.search_documents.return_value = [SimpleNamespace(doc_id=8)]
    svc.kb.get_document.return_value = SimpleNamespace(id=8,session_id='g@chatroom',title='旧经验',content='旧步骤',content_hash='v1',meta={},source='manual',url=None)
    svc.jev.client.evaluate.side_effect = [result(), result(relation)]
    svc.build_revision = AsyncMock(return_value={})
    await svc.review_candidate(row())
    assert svc.store.save_review.call_args.kwargs['status'] == expected
    assert svc.store.save_review.call_args.kwargs['comparisons'][0]['content_hash'] == 'v1'


async def test_failed_retrieval_cannot_imply_novel_knowledge():
    svc = service()
    svc.kb.search_documents.side_effect = RuntimeError('index unavailable')
    with pytest.raises(RuntimeError):
        await svc.review_candidate(row())
    svc.store.save_review.assert_not_awaited()


async def test_cancellation_does_not_commit_partial_batch():
    svc = service()
    svc.llm.chat.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await svc.extract_page(row())
    svc.store.save_page.assert_not_awaited()


async def test_changed_knowledge_snapshot_requires_fresh_review():
    svc=service()
    svc.store.knowledge_snapshot.side_effect=['before','after']
    with pytest.raises(RuntimeError,match='knowledge_changed'):
        await svc.review_candidate(row())
    svc.store.save_review.assert_not_awaited()


async def test_cross_day_question_requires_independent_resolution_judgment():
    svc=service()
    prior={'id':'prior', 'status':'unresolved', 'tenant_id':'t','session_id':'g@chatroom','draft':draft(solution='',outcome='',resolution_ids=[]).model_dump()}
    svc.store.candidate.return_value=prior
    current={**row(),'draft':draft(resolves_candidate_id='prior').model_dump()}
    await svc.review_candidate(current)
    assert svc.store.save_review.call_args.kwargs['reason']=='prior_resolution_uncertain'
    svc.jev.client.evaluate.return_value=result(resolves_prior={'noul':.98})
    await svc.review_candidate(current)
    assert svc.store.save_review.call_args.kwargs['status']=='ready'


async def test_erasure_requires_vector_cleanup_success():
    svc=service()
    run=AsyncMock(return_value=[{'id':7,'session_id':'g@chatroom'}])
    svc.kb.delete_document=AsyncMock(side_effect=RuntimeError('vector unavailable'))
    with pytest.raises(RuntimeError,match='vector unavailable'):
        await svc.erase_member(tenant_id='t',user_id='member',run=run)
    assert run.await_count==1  # Candidate provenance is retained for the durable retry.


async def test_rechecking_own_resolved_followup_is_allowed_but_not_another_candidates():
    svc=service()
    prior={'id':'prior','status':'resolved','tenant_id':'t','session_id':'g@chatroom',
        'draft':draft(solution='',outcome='',resolution_ids=[]).model_dump(),
        'review':{'resolution_candidate_id':'j'}}
    svc.store.candidate.return_value=prior
    svc.jev.client.evaluate.return_value=result(resolves_prior={'noul':.98})
    current={**row(),'draft':draft(resolves_candidate_id='prior').model_dump()}
    await svc.review_candidate(current)
    assert svc.store.save_review.call_args.kwargs['status']=='ready'
    prior['review']['resolution_candidate_id']='different'
    with pytest.raises(KnowledgeEvidenceChanged,match='prior_question_changed'):
        await svc.review_candidate(current)


async def test_graceful_shutdown_returns_claim_for_resumption():
    svc=service()
    svc._schedule_at=float('inf')
    job=row()
    svc.store.claim=AsyncMock(return_value=job)
    svc.store.finish=AsyncMock(return_value=True)
    svc.extract_page=AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await svc.tick()
    svc.store.finish.assert_awaited_once_with('job',job,status='pending',error='WorkerShutdown',retry=True,release_attempt=True)


async def test_offline_extraction_allows_configured_stream_retry_window(monkeypatch):
    svc=service()
    svc.jev.settings=svc.jev.settings.model_copy(update={'openai_responses_stream_max_duration_seconds':600})
    import app.jev.knowledge as module
    observed=[]
    async def wait(awaitable, *, timeout):
        observed.append(timeout)
        return await awaitable
    monkeypatch.setattr(module,'wait_for_llm_activity',wait)
    await svc.extract_page(row())
    assert len(observed)==1 and observed[0]>600
