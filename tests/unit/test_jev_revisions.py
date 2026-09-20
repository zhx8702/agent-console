from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.jev.knowledge import KnowledgeEvidenceChanged
from app.jev.knowledge_publish import publish_candidate
from app.jev.models import fingerprint
from app.jev.quality import QualityFinding, quality_disposition
from app.kb.ingest import KB_MUTATION_LOCK_KEY
from app.kb.service import InMemoryKBStore

from .test_jev_knowledge import messages, result, row, service


async def test_kb_scope_reenters_only_in_own_task():
    store = InMemoryKBStore()
    entered, attempted = asyncio.Event(), asyncio.Event()

    async def child():
        attempted.set()
        async with store.resource_lock("t", "g", KB_MUTATION_LOCK_KEY):
            entered.set()

    async with store.resource_lock("t", "g", KB_MUTATION_LOCK_KEY):
        async with asyncio.timeout(1):
            async with store.resource_lock("t", "g", KB_MUTATION_LOCK_KEY):
                pass
        task = asyncio.create_task(child())
        await attempted.wait()
        assert not entered.is_set()
    await asyncio.wait_for(task, 1)
    assert entered.is_set()


def baseline():
    return SimpleNamespace(
        id=7,
        session_id="g@chatroom",
        title="旧知识",
        content="仅适用于测试环境",
        content_hash="a" * 64,
        source="manual",
        url="https://example.org",
        meta={"source_members": ["original"]},
    )


async def test_revision_keeps_baseline_and_reviews_operator_edits_independently():
    svc = service()
    svc.jev.client.evaluate.return_value = result("accept")
    edited = {
        **row(),
        "source_members": ["a"],
        "revision": {
            "id": "rev",
            "target_doc_id": 7,
            "base_hash": "a" * 64,
            "title": "新知识",
            "content": "仅适用于测试环境：更新证书",
            "before": {"content": "旧内容"},
            "origin": "operator",
        },
    }
    revision = await svc.build_revision(edited, baseline(), messages(), threshold=0.9)
    svc.llm.chat.assert_not_awaited()
    assert revision["status"] == "ready"
    assert revision["source_members"] == ["a", "original"]
    svc.jev.client.evaluate.assert_awaited_once()
    svc.jev.client.evaluate.return_value = result("reject")
    assert (await svc.build_revision(edited, baseline(), messages(), threshold=0.9))[
        "status"
    ] == "needs_review"
    edited["revision"]["base_hash"] = "b" * 64
    rebased = await svc.build_revision(edited, baseline(), messages(), threshold=.9)
    assert rebased["base_hash"] == "a"*64 and rebased["previous_revision_id"] == "rev"
    assert rebased["id"] != "rev" and rebased["status"] == "needs_review"
    assert rebased["before"]["content"] == baseline().content


async def publication_fixture():
    svc = service()
    doc = baseline()

    @asynccontextmanager
    async def scope(*args):
        yield

    svc.kb.mutation_scope = scope
    svc.kb.get_document = AsyncMock(return_value=doc)
    svc.kb.update_document = AsyncMock(return_value=doc.id)
    svc.kb.add_document = AsyncMock(return_value=doc.id)
    review = result()
    review.update(
        _knowledge_snapshot="snapshot", _evidence_hash=fingerprint(svc.message_payload(messages()))
    )
    item = {
        **row(),
        "review": review,
        "source_members": ["a", "original"],
        "comparisons": [],
        "revision": {
            "id": "rev",
            "target_doc_id": 7,
            "base_hash": doc.content_hash,
            "title": "新知识",
            "content": "更新证书",
            "status": "ready",
            "evaluation": result("accept"),
            "source_members": ["a", "original"],
            "evidence_hash": fingerprint(svc.message_payload(messages())),
        },
    }
    return svc, item, doc


@pytest.mark.parametrize("failure", ["source", "snapshot", "base", "sensitive", "permission"])
async def test_publication_revalidates_evidence_and_revision(failure):
    svc, item, doc = await publication_fixture()
    if failure == "source":
        svc.store.evidence.return_value[1]["content"] = "实际上没有恢复"
    if failure == "snapshot":
        svc.store.knowledge_snapshot.return_value = "changed"
    if failure == "base":
        doc.content_hash = "b" * 64
    if failure == "sensitive":
        item["revision"]["evaluation"] = result("accept", sensitive={"noul": 0.9})
    if failure == "permission":
        svc.store.blocked_members.return_value = {"original"}
    with pytest.raises(KnowledgeEvidenceChanged):
        await publish_candidate(svc, item, actor="admin", reason="reviewed", apply_revision=True)
    svc.kb.update_document.assert_not_awaited()


async def test_revision_recovers_commit_without_reapplying():
    svc, item, doc = await publication_fixture()
    doc.meta["jev_revision_id"] = "rev"
    doc.title = item["revision"]["title"]
    doc.content = item["revision"]["content"]
    doc.content_hash = "b" * 64
    svc.store.knowledge_snapshot.return_value = "changed-by-previous-commit"
    doc_id, revision = await publish_candidate(
        svc, item, actor="admin", reason="retry", apply_revision=True
    )
    assert doc_id == 7 and revision["result_hash"] == "b" * 64
    svc.kb.update_document.assert_not_awaited()


async def test_revision_preserves_manual_source_and_combined_provenance():
    svc, item, doc = await publication_fixture()
    await publish_candidate(svc, item, actor="admin", reason="reviewed", apply_revision=True)
    args = svc.kb.update_document.call_args.kwargs
    assert args["source"] == "manual" and args["url"] == doc.url
    assert args["metadata"]["source_members"] == ["a", "original"]
    assert args["metadata"]["previous_content_hash"] == "a" * 64


@pytest.mark.parametrize(
    ("runtime", "expected"),
    [
        ([], "needs_review"),
        ([{"trace_id": "t", "processing_status": "completed"}], "needs_review"),
        ([{"trace_id": "t", "processing_status": "permanent_failure"}], "supported"),
        (
            [
                {
                    "trace_id": "t",
                    "processing_status": "permanent_failure",
                    "deliveries": [{"status": "sent"}],
                }
            ],
            "needs_review",
        ),
        (
            [{"trace_id": "t", "deliveries": [{"status": "failed"}, {"status": "pending"}]}],
            "needs_review",
        ),
    ],
)
def test_missing_answer_requires_actual_runtime_evidence(runtime, expected):
    finding = QualityFinding(
        kind="missed_help", title="漏答", explanation="求助未回答", evidence_ids=[1]
    )
    assert quality_disposition(finding, result("supported"), runtime, 0.9)[0] == expected
    assert quality_disposition(finding, result("reject"), runtime, 0.9)[0] == "rejected"


async def test_quality_findings_unknown_evidence_does_not_checkpoint():
    svc = service()
    svc.llm.chat.return_value.content = '{"candidates":[],"findings":[{"kind":"missed_help","title":"漏答","explanation":"未答","evidence_ids":[999]}]}'
    with pytest.raises(ValueError, match="unknown_quality_evidence"):
        await svc.extract_page(row())
    svc.store.save_page.assert_not_awaited()


@pytest.mark.parametrize('reason',['quiet_hours','answered_by_member','soft_budget_hour_exhausted','member_opt_out'])
async def test_expected_policy_behavior_cannot_be_confirmed_as_missed_help(reason):
    finding=QualityFinding(kind='missed_help',title='漏答',explanation='未答',evidence_ids=[1])
    runtime=[{'trace_id':'t','processing_status':'intentionally_suppressed','decisions':[{'reasons':[reason]}]}]
    assert quality_disposition(finding,result('supported'),runtime,.9)==('rejected','expected_policy_behavior')


@pytest.mark.parametrize('kind',['unhelpful_answer','unnecessary_reply','good_resolution'])
def test_answer_quality_requires_sent_reply(kind):
    finding=QualityFinding(kind=kind,title='回答复盘',explanation='评价回答',evidence_ids=[1])
    assert quality_disposition(finding,result('supported'),[],.9)==('needs_review','delivered_answer_missing')
    assert quality_disposition(finding,result('supported'),[{'deliveries':[{'status':'sent'}]}],.9)[0]=='supported'


async def test_oversized_knowledge_comparison_stays_for_review():
    svc=service()
    doc=baseline()
    doc.content='长正文'*8001
    svc.kb.get_document.return_value=doc
    svc.kb.search_documents.return_value=[SimpleNamespace(doc_id=7)]
    await svc.review_candidate(row())
    saved=svc.store.save_review.call_args.kwargs
    assert saved['status']=='needs_review'
    assert saved['comparisons'][0]['result']['reason']=='comparison_document_too_large'
    assert svc.jev.client.evaluate.await_count==1  # No judgment on a silently truncated document.


async def test_single_revision_does_not_clear_conflicts_with_other_knowledge():
    svc=service()
    first=baseline()
    second=SimpleNamespace(**{**vars(first),'id':8,'session_id':''})
    svc.kb.search_documents.return_value=[SimpleNamespace(doc_id=7),SimpleNamespace(doc_id=8)]
    svc.kb.get_document.side_effect=[first,second]
    svc.jev.client.evaluate.side_effect=[result(),result('supplement'),result('conflict')]
    svc.build_revision=AsyncMock(return_value={'target_doc_id':7,'status':'ready'})
    await svc.review_candidate(row())
    saved=svc.store.save_review.call_args.kwargs
    assert saved['status']=='needs_review' and saved['reason']=='other_knowledge_requires_review'
