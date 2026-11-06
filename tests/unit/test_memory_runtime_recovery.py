from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.engine import AgentCapabilityEngine
from app.channel.identity import canonical_conversation_id
from app.common.config import Settings
from app.common.intent import IntentDecision, is_local_history_lookup
from app.common.types import Channel, ChatResponse, InboundEvent, Message, Session, ToolCall
from app.orchestrator.pipeline import PipelineContext
from app.orchestrator.runner import _decision_trace
from app.orchestrator.trace_store import _safe_decision_trace
from app.social.contracts import MemberPrivacyValues, ParticipationPolicyValues
from plugins.memory.hooks import MemoryControlStep, MemoryLoadStep
from plugins.memory.router import _memory_management_diagnostics
from plugins.memory.runtime_legacy import retrieve_legacy_group_memory
from plugins.memory.store import GROUP_HISTORY_USER_ID_SCOPE
from plugins.wxbot.hook_context import _resolve_group_agent_scope
from tests.unit._fake_llm import make_preprocessed
from tests.unit.test_memory_hooks import _FakeMemoryStore


def context() -> PipelineContext:
    event = InboundEvent(
        tenant_id="demo", channel=Channel.WECHAT, session_id="room@chatroom",
        user_id="member", message_id="message", message=Message(content="我今天问过哪些问题"),
        metadata={"source": "wxbot", "session_kind": "group"},
    )
    return PipelineContext(
        event=event, trace_id="trace", session=Session(
            tenant_id="demo", channel=Channel.WECHAT, session_id=event.session_id, user_id="member",
        ),
    )


def history_decision(**changes) -> IntentDecision:
    return IntentDecision.from_dict({
        "domain": "group_info", "action": "query", "operation": "retrieve",
        "source": "local_history", "confidence": 0.95,
        "slots": {"sender_scope": "self", "period": "today", "keywords": ""},
        **changes,
    })


@pytest.mark.asyncio
@pytest.mark.parametrize("public", [False, True])
async def test_public_group_recall_does_not_require_or_enable_member_memory(public):
    ctx = context()
    store = _FakeMemoryStore()
    store.get_group_member_privacy_policy = AsyncMock(return_value=MemberPrivacyValues())
    store.group_public_memory_enabled = AsyncMock(return_value=public)
    result = await MemoryLoadStep(store).run(ctx)
    assert "user_memory" not in ctx.session.variables
    if public:
        assert result.reason == "loaded"
        assert [call["user_id"] for call in store.get_calls] == [GROUP_HISTORY_USER_ID_SCOPE]
        assert "group_memory" in ctx.session.variables
    else:
        assert result.reason == "member_privacy_blocked"
        assert not store.get_calls
        assert "group_memory" not in ctx.session.variables
    assert ctx.signals["memory"]["runtime"]["load"]["scopes"]["member"]["status"] == "blocked"


@pytest.mark.asyncio
async def test_unavailable_public_policy_keeps_group_closed():
    ctx = context()
    store = _FakeMemoryStore()
    store.get_group_member_privacy_policy = AsyncMock(return_value=MemberPrivacyValues())
    store.group_public_memory_enabled = AsyncMock(side_effect=RuntimeError("offline"))
    assert (await MemoryLoadStep(store).run(ctx)).reason == "member_privacy_blocked"
    assert not store.get_calls


def test_new_group_permission_defaults_closed_and_is_not_a_participation_parameter():
    policy = ParticipationPolicyValues(group_public_memory_enabled=True)
    assert ParticipationPolicyValues().group_public_memory_enabled is False
    assert policy.to_domain(enabled=True).enabled is True


@pytest.mark.asyncio
async def test_history_lookup_bypasses_memory_mutations_and_uses_group_tool_scope():
    ctx = context()
    decision = history_decision(domain="memory", action="search")
    ctx.pre = make_preprocessed("我今天问过哪些问题")
    ctx.pre.semantic_intent = decision.model_dump(mode="json")
    store = SimpleNamespace()
    assert (await MemoryControlStep(store).run(ctx)).reason == "history_lookup"
    assert _resolve_group_agent_scope(decision=decision) == "group_info"


@pytest.mark.parametrize("changes", [
    {"action": "forget_all"}, {"action": "remember"}, {"operation": "update"},
    {"confidence": 0.2}, {"source": "none"}, {"domain": "file", "action": "export_history"},
])
def test_mutations_and_low_confidence_cannot_become_history_reads(changes):
    assert not is_local_history_lookup(history_decision(**changes))


@pytest.mark.asyncio
async def test_legacy_group_items_require_all_observations_in_current_connection(monkeypatch):
    import plugins.memory.store as store_module

    canonical = canonical_conversation_id("connection-a", "room@chatroom")
    store = SimpleNamespace(
        settings=SimpleNamespace(memory_graph_retrieval_enabled=True),
        combined_history_scope_execution_allowed=AsyncMock(return_value=True),
        retrieve_memory_hybrid=AsyncMock(return_value={
            "items": [{"id": 1}, {"id": 2}, {"id": 3}],
            "facts": [{"id": 8, "memory_item_id": 1}, {"id": 9, "memory_item_id": 2}],
        }),
    )
    async def execute(sql, params):
        assert params["tid"] == "demo"
        if "FROM plugin_memory_item" in sql:
            assert params["sid"] == "room@chatroom"
            assert params["uid"] == GROUP_HISTORY_USER_ID_SCOPE
            return [
                {"id": 1, "value_json": '{"source_observation_ids": [10, 11]}'},
                {"id": 2, "value_json": '{"source_observation_ids": [10, 99]}'},
                {"id": 3, "value_json": '{"source_observation_ids": []}'},
            ]
        assert params["sid"] == canonical
        return [{"id": 10}, {"id": 11}]
    monkeypatch.setattr(store_module, "_exec", execute)
    kwargs = dict(tenant_id="demo", connection_id="connection-a", session_id=canonical,
                  external_session_id="room@chatroom", source_key="wxbot", query="部署")
    result = await retrieve_legacy_group_memory(store, **kwargs)
    assert result == {"items": [{"id": 1}], "facts": [{"id": 8, "memory_item_id": 1}]}
    store.retrieve_memory_hybrid.reset_mock()
    assert await retrieve_legacy_group_memory(store, **(kwargs | {"connection_id": "connection-b"})) == {"items": [], "facts": []}
    store.retrieve_memory_hybrid.assert_not_called()
    store.combined_history_scope_execution_allowed.return_value = False
    assert await retrieve_legacy_group_memory(store, **kwargs) == {"items": [], "facts": []}
    store.retrieve_memory_hybrid.assert_not_called()


def test_memory_trace_retains_reason_and_counts_without_raw_memory():
    ctx = context()
    ctx.signals["memory"] = {"runtime": {
        "load": {"status": "blocked", "reason": "member_privacy_blocked", "candidate_count": 3,
                 "content": "private-memory-body", "user_id": "private-user"},
        "save": {"status": "skipped", "reason": "member_privacy_blocked"},
    }}
    trace = _safe_decision_trace(_decision_trace(ctx))
    assert trace["memory"]["load_reason"] == "member_privacy_blocked"
    assert trace["memory"]["candidate_count"] == 3
    assert "private-" not in repr(trace)


def test_management_status_distinguishes_fake_vectors_from_semantic_embedding():
    diagnostics = _memory_management_diagnostics(
        config={"llm_embed_provider": "fake", "memory_vector_index_enabled": True},
        runtime_scope={}, job_stats={}, acceptance_stats={}, items=[], session_id="room@chatroom",
    )
    assert any(row["code"] == "non_semantic_embedding" for row in diagnostics)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["ok", "error", "empty", "unavailable", "synthesis_error", "unexecuted_tool"])
async def test_history_answer_requires_observed_tool_result(monkeypatch, outcome):
    pre = make_preprocessed("我今天问过哪些问题")
    pre.semantic_intent = history_decision().model_dump(mode="json")
    ctx = context()
    llm = SimpleNamespace(chat=AsyncMock(return_value=ChatResponse(content="你问了部署方式。[10]", model="test")))
    if outcome == "synthesis_error":
        llm.chat.side_effect = RuntimeError("provider unavailable")
    elif outcome == "unexecuted_tool":
        llm.chat.return_value.tool_calls = [ToolCall(id="unexpected", name="web_search", arguments={})]
    engine = AgentCapabilityEngine(llm, settings=Settings(app_env="test"))
    monkeypatch.setattr(engine, "_compose_system_prompt", lambda *args, **kwargs: pytest.fail("history must not load persona or memory"))
    monkeypatch.setattr(engine, "_available_tools", AsyncMock(return_value=(
        {} if outcome == "unavailable" else {"search_group_messages": object()}, {},
    )))
    async def execute(session, call, tools):
        llm.chat.assert_not_called()
        assert call.name == "search_group_messages"
        assert call.arguments["sender_scope"] == "self"
        return ToolCall(id=call.id, name=call.name, error="offline" if outcome == "error" else None,
                        result={"messages": [] if outcome == "empty" else [{"observation_id": 10, "text": "怎么部署？"}]})
    monkeypatch.setattr(engine, "_execute_tool_call", execute)
    result = await engine.answer(pre, ctx.session, {"agent_tool_scope": "group_info"})
    if outcome == "ok":
        assert result.metadata["history_evidence_available"] is True
        assert len(result.tool_calls) == 1
        assert llm.chat.call_count == 1
    elif outcome in {"synthesis_error", "unexecuted_tool"}:
        assert llm.chat.call_count == 1
        assert "未能生成" in result.reply_text
        assert len(result.tool_calls) == 1
    else:
        llm.chat.assert_not_called()
        assert result.reply_text
