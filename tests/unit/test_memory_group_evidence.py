from __future__ import annotations

import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.jev.models import JevPolicy
from plugins.memory import store as runtime
from plugins.memory.group_evidence import (
    EVIDENCE_POLICY_VERSION,
    assess_group_evidence,
    group_recall_block_reason,
    relation_review_fingerprint,
)
from plugins.memory.store import MemoryStore, _is_prompt_eligible_memory_item
from plugins.memory.store_group_graph import _group_relation_acceptance_decision


def relation(**changes):
    return {
        "subject": "member-a",
        "predicate": "interested_in",
        "object": "ModelX",
        "object_type": "tool",
        **changes,
    }


def row(number=1, **changes):
    return {
        "source": "observation",
        "id": number,
        "member": "member-a",
        "text": "我一直偏好 ModelX，以后也优先选它。",
        "type": "text",
        **changes,
    }


def item(**changes):
    return {
        "id": 10,
        "tenant_id": "demo",
        "session_id": "room@chatroom",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "__group__",
        "source_type": "llm_group_window",
        "status": "active",
        "sensitivity": "normal",
        "pinned": False,
        "content": "member-a interested_in ModelX",
        "value": {
            "relation": relation(evidence_observation_ids=[1, 2]),
            "source_observation_ids": [1, 2],
            "acceptance": {"status": "accepted", "reviewed_by": "system/auto"},
        },
        **changes,
    }


@pytest.mark.parametrize(
    "rows,reason",
    [
        ([], "missing_evidence"),
        ([row(text="[图片]")], "non_text_or_missing_evidence"),
        ([row(type="image")], "non_text_or_missing_evidence"),
        ([row(is_self_sent=True)], "non_text_or_missing_evidence"),
        ([row(member="member-b")], "no_subject_statement"),
        ([row(text="这个版本怎么又出问题了")], "object_not_grounded"),
        ([row(text="myModelXclone")], "object_not_grounded"),
        ([row(), row()], "ambiguous_evidence"),
    ],
)
def test_evidence_requires_attributable_text_and_grounded_object(rows, reason):
    quality = assess_group_evidence(relation(), rows)
    assert quality["status"] == "needs_review"
    assert reason in quality["reasons"]
    assert "member-a" not in repr(quality)
    assert "ModelX" not in repr(quality)


def test_repeated_text_is_one_statement_and_other_speaker_cannot_supply_object():
    quality = assess_group_evidence(relation(), [row(), row(2), row(3, member="member-b")])
    assert quality["subject_statement_count"] == 1
    quality = assess_group_evidence(
        relation(), [row(text="我最近在试一个服务"), row(2, member="member-b")]
    )
    assert quality["object_grounded"] is False


@pytest.mark.parametrize("days,count", [(1, 3), (2, 10), (100, 10000)])
@pytest.mark.parametrize("predicate", ["interested_in", "works_on", "collaborated_with", "asked"])
def test_message_volume_and_days_never_establish_model_claim(days, count, predicate):
    assert _group_relation_acceptance_decision(
        {"llm": count},
        day_count=days,
        auto_accept=True,
        predicate=predicate,
    ) == ("needs_review", "group_window_semantic_review_required")


@pytest.mark.parametrize("text", ["ModelX 怎么安装？", "ModelX 真难用", "今天临时试下 ModelX"])
def test_grounded_mentions_still_do_not_establish_a_preference(text):
    candidate = item()
    candidate["value"]["relation"]["evidence_quality"] = assess_group_evidence(
        relation(), [row(text=text)]
    )
    assert candidate["value"]["relation"]["evidence_quality"]["status"] == "grounded"
    assert not _is_prompt_eligible_memory_item(candidate)


def test_old_inferred_preference_stays_reviewable_but_not_prompt_eligible():
    candidate = item()
    original = copy.deepcopy(candidate)
    assert group_recall_block_reason(candidate) == "group_preference_requires_review"
    assert not _is_prompt_eligible_memory_item(candidate)
    assert candidate == original
    candidate["value"]["acceptance"]["reviewed_by"] = "operator@example.test"
    assert _is_prompt_eligible_memory_item(candidate)


def test_stale_or_shadow_evaluation_cannot_authorize_preference_recall():
    candidate = item()
    value = candidate["value"]
    value["relation"]["evidence_quality"] = assess_group_evidence(relation(), [row()])
    value["acceptance"]["reviewed_by"] = "system/jev"
    value["jev"] = {
        "applied": True,
        "effective_decision": "accepted",
        "evidence_policy_version": EVIDENCE_POLICY_VERSION,
        "relation_hash": relation_review_fingerprint(value),
    }
    assert _is_prompt_eligible_memory_item(candidate)
    value["jev"]["applied"] = False
    assert not _is_prompt_eligible_memory_item(candidate)
    value["jev"]["applied"] = True
    value["relation"]["object"] = "DifferentProduct"
    assert not _is_prompt_eligible_memory_item(candidate)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actor", ["system/auto", "system/auto-review", "system/new-sweep", "system/jev"]
)
async def test_automated_review_api_cannot_bypass_semantic_review(monkeypatch, actor):
    store = MemoryStore(SimpleNamespace())
    monkeypatch.setattr(
        runtime, "_ACTIVE_MUTATION_CONNECTION", SimpleNamespace(get=lambda: object())
    )
    store.get_memory_item = AsyncMock(return_value=item(status="pending"))
    store.update_memory_item = AsyncMock(return_value={"id": 10})
    store.append_memory_acceptance_audit = AsyncMock()
    await store.review_memory_item_acceptance(10, action="accept", reviewed_by=actor)
    update = store.update_memory_item.call_args.kwargs
    assert update["status"] == "pending"
    assert update["value_json"]["acceptance"]["status"] == "needs_review"


@pytest.mark.asyncio
async def test_current_semantic_review_can_accept_through_shared_store(monkeypatch):
    store = MemoryStore(SimpleNamespace())
    current = item(status="pending")
    value = current["value"]
    value["relation"]["evidence_quality"] = assess_group_evidence(relation(), [row()])
    value["jev"] = {
        "applied": True,
        "effective_decision": "accepted",
        "evidence_policy_version": EVIDENCE_POLICY_VERSION,
        "relation_hash": relation_review_fingerprint(value),
    }
    monkeypatch.setattr(
        runtime, "_ACTIVE_MUTATION_CONNECTION", SimpleNamespace(get=lambda: object())
    )
    store.get_memory_item = AsyncMock(return_value=current)
    store.update_memory_item = AsyncMock(return_value={"id": 10})
    store.append_memory_acceptance_audit = AsyncMock()
    await store.review_memory_item_acceptance(10, action="accept", reviewed_by="system/jev")
    assert store.update_memory_item.call_args.kwargs["status"] == "active"


@pytest.mark.asyncio
async def test_persistence_does_not_trust_model_supplied_evidence_quality():
    store = MemoryStore(SimpleNamespace(memory_group_graph_auto_accept=True))
    store._find_memory_item_by_normalized_key = AsyncMock(return_value=[])
    store._insert_or_touch_memory_item = AsyncMock(return_value={"id": 10})
    await store._apply_group_relationship_window_candidate(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="__group__",
        session_id="room@chatroom",
        target_date="2026-02-01",
        window={
            "index": 1,
            "first_event_id": 1,
            "last_event_id": 3,
            "rows": [
                {
                    "id": 1,
                    "_graph_source": "observation",
                    "user_text": "member-a: 这个平台怎么老出错",
                },
                {
                    "id": 2,
                    "_graph_source": "observation",
                    "user_text": "member-b: 你说的是 ModelX 吗",
                },
                {
                    "id": 3,
                    "_graph_source": "observation",
                    "user_text": "member-a: [图片]",
                    "msg_type": "image",
                },
            ],
        },
        candidate=relation(
            evidence_observation_ids=[1, 2, 3],
            confidence=0.99,
            evidence_quality={"status": "grounded"},
            signals={"llm": 3},
        ),
    )
    inserted = store._insert_or_touch_memory_item.call_args.kwargs
    assert inserted["status"] == "pending"
    quality = inserted["value_json"]["relation"]["evidence_quality"]
    assert quality["status"] == "needs_review"
    assert "object_not_grounded" in quality["reasons"]
    assert quality["subject_statement_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("vector_candidates", [None, [1]])
async def test_graph_fact_and_episode_cannot_bypass_preference_gate(monkeypatch, vector_candidates):
    candidate = item()
    value = candidate["value"]
    fact = {
        "id": 1,
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "__group__",
        "status": "active",
        "subject_name": "member-a",
        "predicate": "interested_in",
        "object_name": "ModelX",
        "memory_item_id": 10,
        "confidence": 0.9,
        "item_source_type": "llm_group_window",
        "item_value_json": json.dumps(value),
        "item_session_id": "room@chatroom",
        "item_sensitivity": "normal",
        "item_sensitivity_category": "normal",
        "item_origin_session_kind": "group",
        "item_audience_scope": "session",
        "item_allowed_session_ids": ["room@chatroom"],
    }
    episode = {
        "id": 2,
        "status": "active",
        "session_id": "room@chatroom",
        "title": "ModelX",
        "summary": "member-a likes ModelX",
        "memory_item_ids_json": "[10]",
    }

    async def execute(sql, params):
        if "FROM plugin_memory_fact fact" in sql:
            return [fact]
        if "FROM plugin_memory_episode" in sql:
            return [episode]
        return [
            {
                **candidate,
                "value_json": json.dumps(value),
                "audience_scope": "session",
                "origin_session_kind": "group",
                "sensitivity_category": "normal",
                "allowed_session_ids": ["room@chatroom"],
            }
        ]

    monkeypatch.setattr(runtime, "_exec", execute)
    store = MemoryStore(SimpleNamespace())
    kwargs = dict(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="__group__",
        session_id="room@chatroom",
        query="ModelX",
        candidate_fact_ids=vector_candidates,
        request_session_kind="group",
    )
    result = await store._retrieve_memory_graph_sql(**kwargs)
    assert result["facts"] == []
    assert result["episodes"] == []
    value["acceptance"]["reviewed_by"] = "operator@example.test"
    fact["item_value_json"] = json.dumps(value)
    result = await store._retrieve_memory_graph_sql(**kwargs)
    assert [entry["id"] for entry in result["facts"]] == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("media,missing", [(False, False), (True, False), (False, True)])
async def test_jev_reads_source_records_and_keeps_subject_identity(monkeypatch, media, missing):
    store = MemoryStore(SimpleNamespace())
    store._jev_current = AsyncMock(return_value=item())
    store._resolve_group_graph_session_ids = AsyncMock(return_value=["room@chatroom"])
    store._member_memory_write_blocked = AsyncMock(return_value=False)
    records = [
        {
            "id": i,
            "content": "我一直喜欢 ModelX",
            "sender_wxid": "member-a",
            "sender_name": "甲",
            "msg_type": "image" if media else "text",
        }
        for i in (1, 2)
    ]
    monkeypatch.setattr(
        runtime, "_exec", AsyncMock(return_value=records[:1] if missing else records)
    )
    state = await store.jev_state({"domain": "relationship"})
    if missing:
        assert state is None
    else:
        assert state["evidence_quality"]["status"] == ("needs_review" if media else "grounded")
        assert state["subject"] == state["evidence"][0]["speaker"]
        assert state["evidence_quality"]["subject_statement_count"] == (0 if media else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("too_many", [False, True])
async def test_truncated_review_cannot_certify_the_complete_claim(monkeypatch, too_many):
    current = item()
    if too_many:
        current["value"]["relation"]["evidence_observation_ids"] = list(range(1, 50))
    store = MemoryStore(SimpleNamespace())
    store._jev_current = AsyncMock(return_value=current)
    store._resolve_group_graph_session_ids = AsyncMock(return_value=["room@chatroom"])
    store._member_memory_write_blocked = AsyncMock(return_value=False)
    async def execute(sql, params):
        return [{"id": i, "content": "ModelX " + ("上下文" * 2200 if not too_many else "长期首选"),
                 "sender_wxid": "member-a", "sender_name": "", "msg_type": "text"}
                for i in params["ids"]]
    monkeypatch.setattr(runtime, "_exec", execute)
    state = await store.jev_state({"domain": "relationship"})
    assert state["evidence_truncated"] is True
    assert state["evidence_quality"]["status"] == "needs_review"
    assert "evidence_truncated" in state["evidence_quality"]["reasons"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "grounded,explicit,shadow,expected",
    [
        (True, 0.99, False, "accepted"),
        (True, 0, False, "needs_review"),
        (False, 0.99, False, "needs_review"),
        (True, 0.99, True, None),
    ],
)
async def test_jev_acceptance_requires_fresh_grounding_and_explicit_preference(
    monkeypatch,
    grounded,
    explicit,
    shadow,
    expected,
):
    store = MemoryStore(SimpleNamespace())
    current = item()
    quality = assess_group_evidence(
        relation(), [row(text="ModelX 是我的长期首选" if grounded else "没提产品")]
    )

    @asynccontextmanager
    async def transaction():
        yield

    store._mutation_transaction = transaction
    store._jev_current = AsyncMock(return_value=current)
    store.jev_state = AsyncMock(return_value={"evidence_quality": quality})
    store.get_memory_item = AsyncMock(side_effect=lambda *a, **k: copy.deepcopy(current))

    async def update(_item_id, **kwargs):
        current["value"] = kwargs["value_json"]
        return current

    store.update_memory_item = AsyncMock(side_effect=update)
    store.review_memory_item_acceptance = AsyncMock()
    store.jev_service = SimpleNamespace(store=SimpleNamespace(finish=AsyncMock()))
    monkeypatch.setattr(runtime, "_exec", AsyncMock(return_value=[{"id": 1}]))
    result = {
        "model": "test",
        "answers": {
            "decision": {"choice": "accepted", "confidence": 0.99},
            "supported": {"noul": 0.99},
            "sensitive": {"noul": 0},
            "durable_preference": {"noul": explicit},
        },
    }
    await store.apply_jev_result(
        {"id": 1, "lease_token": "test", "domain": "relationship"},
        result,
        policy=JevPolicy(shadow_only=shadow),
        duration_ms=1,
    )
    if expected is None:
        store.review_memory_item_acceptance.assert_not_awaited()
    else:
        assert store.review_memory_item_acceptance.call_args.kwargs["action"] == (
            "accept" if expected == "accepted" else expected
        )
    assert current["value"]["jev"]["evidence_quality"] == quality
    assert current["value"]["jev"]["applied"] is (not shadow)
