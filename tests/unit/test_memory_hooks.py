from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.common.types import (
    Channel,
    InboundEvent,
    Message,
    OutboundReply,
    PreprocessedMessage,
    ReplySegment,
    Session,
)
from app.orchestrator.effect_handlers import (
    EffectDispatcher,
    EffectHandlerRegistry,
    register_memory_save_handler,
)
from app.orchestrator.effects import EFFECT_STATUS_RECORDED, InMemoryEffectCommitter
from app.orchestrator.pipeline import PipelineContext
from app.social.contracts import MemberPrivacyValues
from plugins.memory.hooks import (
    MemoryContextHook,
    MemoryLoadStep,
    MemoryPersistenceHook,
    MemorySaveStep,
)
from plugins.memory.store import (
    GROUP_HISTORY_USER_ID_SCOPE,
    MemoryStore,
    _rank_retrieved_memory_items,
    _update_session_state,
)


def test_session_state_does_not_turn_questions_or_assistant_advice_into_decisions() -> None:
    state = _update_session_state(
        {},
        session_id="room@chatroom",
        user_text="知识库如何使用？下一步怎么做",
        assistant_text="建议采用向量检索，并决定稍后继续处理。",
        created_at="2026-07-13T00:00:00Z",
    )

    assert state["open_items"] == []
    assert state["decisions"] == []


def test_session_state_keeps_explicit_user_commitments() -> None:
    state = _update_session_state(
        {},
        session_id="room@chatroom",
        user_text="决定采用混合检索。下一步：补充退款文档",
        assistant_text="收到",
        created_at="2026-07-13T00:00:00Z",
    )

    assert len(state["decisions"]) == 1
    assert len(state["open_items"]) == 1


def test_memory_retrieval_filters_unrelated_vector_noise() -> None:
    base = {
        "user_id": "wxid_a",
        "source_key": "wxbot",
        "scope_type": "identity",
        "content": "记忆内容",
        "normalized_key": "note:key",
        "status": "active",
        "acceptance_status": "accepted",
        "origin_session_kind": "group",
        "audience_scope": "session",
        "allowed_session_ids": ["room@chatroom"],
        "sensitivity_category": "normal",
    }
    ranked = _rank_retrieved_memory_items(
        [
            {**base, "id": 1, "match_count": 0, "vector_score": 0.12},
            {**base, "id": 2, "normalized_key": "note:relevant", "vector_score": 0.62},
        ],
        source_key="wxbot",
        user_id="wxid_a",
        session_id="room@chatroom",
        has_query=True,
        limit=10,
    )

    assert [item["id"] for item in ranked] == [2]


def test_group_relation_does_not_infer_reply_from_plain_adjacency() -> None:
    store = object.__new__(MemoryStore)
    plain = store._build_deterministic_group_window_candidates(
        {
            "rows": [
                {"id": 1, "user_text": "alice: 大家好"},
                {"id": 2, "user_text": "bob: 今天天气不错"},
            ]
        }
    )
    explicit = store._build_deterministic_group_window_candidates(
        {
            "rows": [
                {"id": 1, "user_text": "alice: 大家好"},
                {"id": 2, "user_text": "bob: 回复：你好"},
            ]
        }
    )

    assert not any(item["predicate"] == "replied_to" for item in plain)
    assert any(item["predicate"] == "replied_to" for item in explicit)


class _FakeMemoryStore:
    def __init__(self) -> None:
        self.get_calls: list[dict[str, str]] = []
        self.remember_calls: list[dict[str, str]] = []
        self.retrieve_calls: list[dict[str, str]] = []
        self.hybrid_retrieve_calls: list[dict[str, object]] = []
        self.graph_retrieve_calls: list[dict[str, object]] = []
        self.settings = SimpleNamespace(
            memory_retrieval_enabled=True,
            memory_retrieval_top_k=6,
            memory_group_identity_memory_enabled=True,
        )

    async def get_group_member_privacy_policy(self, **kwargs):
        del kwargs
        return MemberPrivacyValues(
            memory_enabled=True,
            allow_group_recall=True,
            audience_scope="session",
        )

    async def get_runtime_profile(self, **kwargs):
        self.get_calls.append(kwargs)
        if kwargs["user_id"] == GROUP_HISTORY_USER_ID_SCOPE:
            return {
                "tenant_id": kwargs["tenant_id"],
                "channel": kwargs["channel"],
                "source_key": kwargs["source_key"],
                "user_id": kwargs["user_id"],
                "session_id": kwargs["session_id"],
                "short_term_memory": "群里最近在讨论发货窗口",
                "long_term_memory": "已知群聊事实:\n- 群默认讨论订单履约",
                "manual_notes": "群共享备注:\n群里默认短答",
                "identity_manual_notes": "群里默认短答",
                "session_manual_notes": "",
                "message_count": 9,
                "identity_message_count": 9,
                "session_message_count": 5,
                "imported_message_count": 7,
                "last_session_id": kwargs["session_id"],
                "session_summary": "Group context: fulfillment window discussion.",
                "open_items": [],
                "decisions": [],
                "recent_turns": [],
                "last_compacted_at": datetime(2026, 4, 21, 12, 7, tzinfo=UTC),
                "summary_version": 2,
                "identity_profile": {
                    "user_id": kwargs["user_id"],
                    "updated_at": datetime(2026, 4, 21, 12, 0, tzinfo=UTC),
                },
                "session_profile": {
                    "session_id": kwargs["session_id"],
                    "updated_at": datetime(2026, 4, 21, 12, 1, tzinfo=UTC),
                },
                "memory_items": {
                    "identity": [
                        {
                            "source_type": "manual",
                            "status": "active",
                            "content": "群共享规则：默认短答",
                            "created_at": datetime(2026, 4, 21, 12, 0, tzinfo=UTC),
                        }
                    ],
                    "session": [],
                },
            }
        return {
            "tenant_id": kwargs["tenant_id"],
            "channel": kwargs["channel"],
            "source_key": kwargs["source_key"],
            "user_id": kwargs["user_id"],
            "session_id": kwargs["session_id"],
            "short_term_memory": "用户最近问了退货进度",
            "long_term_memory": "已知用户事实与偏好:\n- 偏好微信联系",
            "manual_notes": "全局记忆备注:\nVIP 客户",
            "identity_manual_notes": "VIP 客户",
            "session_manual_notes": "",
            "message_count": 3,
            "identity_message_count": 3,
            "session_message_count": 1,
            "imported_message_count": 2,
            "last_session_id": "group-1@chatroom",
            "session_summary": "Recent context: user is checking a return shipment.",
            "open_items": [
                {
                    "text": "Follow up on return shipment",
                    "created_at": datetime(2026, 4, 21, 12, 0, tzinfo=UTC),
                }
            ],
            "decisions": [
                {
                    "text": "Use concise shipment updates",
                    "created_at": datetime(2026, 4, 21, 12, 0, tzinfo=UTC),
                }
            ],
            "recent_turns": [
                {
                    "user_text": "查一下物流",
                    "assistant_text": "我来帮你查",
                    "created_at": datetime(2026, 4, 21, 12, 1, tzinfo=UTC),
                }
            ],
            "last_compacted_at": datetime(2026, 4, 21, 12, 5, tzinfo=UTC),
            "summary_version": 2,
            "identity_profile": {
                "user_id": kwargs["user_id"],
                "updated_at": datetime(2026, 4, 21, 12, 0, tzinfo=UTC),
            },
            "session_profile": {
                "session_id": kwargs["session_id"],
                "updated_at": datetime(2026, 4, 21, 12, 1, tzinfo=UTC),
            },
            "memory_items": {
                "identity": [
                    {
                        "source_type": "manual",
                        "status": "active",
                        "content": "人工标记为 VIP",
                        "created_at": datetime(2026, 4, 21, 12, 0, tzinfo=UTC),
                    }
                ],
                "session": [
                    {
                        "source_type": "auto",
                        "status": "active",
                        "content": "本轮在查物流",
                    }
                ],
            },
        }

    async def retrieve_memory_items(self, **kwargs):
        self.retrieve_calls.append(kwargs)
        if kwargs["user_id"] == GROUP_HISTORY_USER_ID_SCOPE:
            return [
                {
                    "id": 17,
                    "source_type": "auto",
                    "status": "active",
                    "content": "群共享上下文：正在讨论发货窗口",
                    "sensitivity": "normal",
                }
            ]
        return [
            {
                "id": 7,
                "source_type": "auto",
                "status": "active",
                "content": "用户正在查物流",
                "sensitivity": "normal",
            }
        ]

    async def retrieve_memory_graph(self, **kwargs):
        self.graph_retrieve_calls.append(kwargs)
        if kwargs["user_id"] == GROUP_HISTORY_USER_ID_SCOPE:
            return {
                "facts": [
                    {
                        "memory_item_id": 18,
                        "subject_name": "当前群聊",
                        "predicate": "topic",
                        "object_value": "发货窗口",
                        "score": 120,
                        "reason": "query_match",
                    }
                ],
                "episodes": [],
                "budget_chars": kwargs.get("budget_chars") or 600,
            }
        return {
            "facts": [
                {
                    "memory_item_id": 8,
                    "subject_name": "用户",
                    "predicate": "asked_about",
                    "object_value": "物流",
                    "score": 120,
                    "reason": "query_match",
                }
            ],
            "episodes": [],
            "budget_chars": kwargs.get("budget_chars") or 600,
        }

    async def retrieve_memory_hybrid(self, **kwargs):
        self.hybrid_retrieve_calls.append(kwargs)
        if kwargs["user_id"] == GROUP_HISTORY_USER_ID_SCOPE:
            return {
                "items": [
                    {
                        "id": 17,
                        "source_type": "auto",
                        "status": "active",
                        "content": "群共享上下文：正在讨论发货窗口",
                        "sensitivity": "normal",
                    }
                ],
                "facts": [
                    {
                        "memory_item_id": 18,
                        "subject_name": "当前群聊",
                        "predicate": "topic",
                        "object_value": "发货窗口",
                        "score": 120,
                        "reason": "query_match",
                    }
                ],
                "episodes": [],
                "budget_chars": kwargs.get("budget_chars") or 600,
            }
        return {
            "items": [
                {
                    "id": 7,
                    "source_type": "auto",
                    "status": "active",
                    "content": "用户正在查物流",
                    "sensitivity": "normal",
                }
            ],
            "facts": [
                {
                    "memory_item_id": 8,
                    "subject_name": "用户",
                    "predicate": "asked_about",
                    "object_value": "物流",
                    "score": 120,
                    "reason": "query_match",
                }
            ],
            "episodes": [],
            "budget_chars": kwargs.get("budget_chars") or 600,
        }

    async def remember_interaction(self, **kwargs):
        self.remember_calls.append(kwargs)
        return {
            "tenant_id": kwargs["tenant_id"],
            "channel": kwargs["channel"],
            "source_key": kwargs["source_key"],
            "user_id": kwargs["user_id"],
            "session_id": kwargs["session_id"],
            "short_term_memory": "用户最近说: 我要看物流",
            "long_term_memory": "已知用户事实与偏好:\n- 偏好微信联系",
            "manual_notes": "全局记忆备注:\nVIP 客户",
            "identity_manual_notes": "VIP 客户",
            "session_manual_notes": "",
            "message_count": 4,
            "identity_message_count": 4,
            "session_message_count": 2,
            "imported_message_count": 2,
            "last_session_id": kwargs["session_id"],
            "session_summary": "Recent context: user asked to see logistics.",
            "open_items": [
                {
                    "text": "Check updated logistics status",
                    "created_at": datetime(2026, 4, 21, 12, 2, tzinfo=UTC),
                }
            ],
            "decisions": [
                {
                    "text": "Confirmed logistics reply should be concise",
                    "created_at": datetime(2026, 4, 21, 12, 2, tzinfo=UTC),
                }
            ],
            "recent_turns": [
                {
                    "user_text": kwargs["user_text"],
                    "assistant_text": kwargs["assistant_text"],
                    "created_at": datetime(2026, 4, 21, 12, 3, tzinfo=UTC),
                }
            ],
            "last_compacted_at": datetime(2026, 4, 21, 12, 6, tzinfo=UTC),
            "summary_version": 2,
            "identity_profile": {
                "user_id": kwargs["user_id"],
                "updated_at": datetime(2026, 4, 21, 12, 2, tzinfo=UTC),
            },
            "session_profile": {
                "session_id": kwargs["session_id"],
                "updated_at": datetime(2026, 4, 21, 12, 3, tzinfo=UTC),
            },
            "memory_items": {
                "identity": [
                    {
                        "source_type": "manual",
                        "status": "active",
                        "content": "人工标记为 VIP",
                        "created_at": datetime(2026, 4, 21, 12, 2, tzinfo=UTC),
                    }
                ],
                "session": [
                    {
                        "source_type": "auto",
                        "status": "active",
                        "content": "用户刚刚要求看物流",
                    }
                ],
            },
        }


@pytest.mark.asyncio
async def test_memory_context_hook_uses_event_user_id_for_group_member_scope() -> None:
    store = _FakeMemoryStore()
    hook = MemoryContextHook(store)
    event = InboundEvent(
        message_id="m-1",
        tenant_id="demo",
        channel=Channel.WECHAT,
        user_id="wxid_member_a",
        session_id="group-1@chatroom",
        message=Message(content="查一下物流"),
        metadata={"source": "wxbot"},
    )
    session = Session(
        session_id="group-1@chatroom",
        tenant_id="demo",
        user_id="group-1@chatroom",
        channel=Channel.WECHAT,
    )
    ctx = PipelineContext(event=event, trace_id="trace-1", session=session)

    await hook.run(ctx)

    assert store.get_calls == [
        {
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "session_id": "group-1@chatroom",
            "user_id": "wxid_member_a",
            "request_session_kind": "group",
        },
        {
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "session_id": "group-1@chatroom",
            "user_id": GROUP_HISTORY_USER_ID_SCOPE,
            "request_session_kind": "group",
        },
    ]
    assert session.variables["user_memory"]["user_id"] == "wxid_member_a"
    assert session.variables["group_memory"]["user_id"] == GROUP_HISTORY_USER_ID_SCOPE
    assert session.variables["group_memory"]["session_summary"] == (
        "Group context: fulfillment window discussion."
    )
    assert session.variables["user_memory"]["identity_manual_notes"] == "VIP 客户"
    assert session.variables["user_memory"]["session_summary"] == (
        "Recent context: user is checking a return shipment."
    )
    assert session.variables["user_memory"]["open_items"] == [
        {
            "text": "Follow up on return shipment",
            "created_at": "2026-04-21T12:00:00+00:00",
        }
    ]
    assert session.variables["user_memory"]["decisions"] == [
        {
            "text": "Use concise shipment updates",
            "created_at": "2026-04-21T12:00:00+00:00",
        }
    ]
    assert session.variables["user_memory"]["recent_turns"] == [
        {
            "user_text": "查一下物流",
            "assistant_text": "我来帮你查",
            "created_at": "2026-04-21T12:01:00+00:00",
        }
    ]
    assert session.variables["user_memory"]["last_compacted_at"] == "2026-04-21T12:05:00+00:00"
    assert session.variables["user_memory"]["summary_version"] == 2
    assert (
        session.variables["user_memory"]["relevant_memory_items"][0]["content"] == "用户正在查物流"
    )
    assert session.variables["user_memory"]["relevant_graph_facts"] == []
    assert session.variables["user_memory"]["relevant_graph_episodes"] == []
    assert (
        session.variables["user_memory"]["identity_profile"]["updated_at"]
        == "2026-04-21T12:00:00+00:00"
    )
    assert (
        session.variables["user_memory"]["memory_items"]["identity"][0]["content"]
        == "人工标记为 VIP"
    )
    assert session.variables["user_memory"]["memory_items"]["identity"][0]["created_at"] == (
        "2026-04-21T12:00:00+00:00"
    )
    assert store.retrieve_calls == [
        {
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "session_id": "group-1@chatroom",
            "user_id": "wxid_member_a",
            "query": "查一下物流",
            "limit": 6,
            "request_session_kind": "group",
        },
        {
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "session_id": "group-1@chatroom",
            "user_id": GROUP_HISTORY_USER_ID_SCOPE,
            "query": "查一下物流",
            "limit": 6,
            "request_session_kind": "group",
        },
    ]
    assert store.graph_retrieve_calls == []


@pytest.mark.asyncio
async def test_group_memory_hides_identity_wide_private_facts_by_default() -> None:
    store = _FakeMemoryStore()
    store.settings = SimpleNamespace(
        memory_retrieval_enabled=True,
        memory_retrieval_top_k=6,
        memory_group_identity_memory_enabled=False,
    )
    hook = MemoryContextHook(store)
    event = InboundEvent(
        message_id="m-private-boundary",
        tenant_id="demo",
        channel=Channel.WECHAT,
        user_id="wxid_member_a",
        session_id="group-1@chatroom",
        message=Message(content="查一下物流"),
        metadata={"source": "wxbot"},
    )
    session = Session(
        session_id="group-1@chatroom",
        tenant_id="demo",
        user_id="group-1@chatroom",
        channel=Channel.WECHAT,
    )
    ctx = PipelineContext(event=event, trace_id="trace-private-boundary", session=session)

    await hook.run(ctx)

    personal = session.variables["user_memory"]
    assert personal["long_term"] == ""
    assert personal["identity_manual_notes"] == ""
    assert personal["memory_items"]["identity"] == []
    assert personal["relevant_graph_facts"] == []
    assert personal["session_summary"] == "Recent context: user is checking a return shipment."
    assert ctx.signals["memory"]["audience_scope"] == "group_session_only"


@pytest.mark.asyncio
async def test_memory_context_hook_attaches_graph_when_enabled() -> None:
    store = _FakeMemoryStore()
    store.settings = SimpleNamespace(
        memory_retrieval_enabled=True,
        memory_retrieval_top_k=6,
        memory_graph_retrieval_enabled=True,
        memory_graph_retrieval_fact_top_k=2,
        memory_graph_retrieval_episode_top_k=1,
        memory_graph_retrieval_budget_chars=400,
        memory_group_identity_memory_enabled=True,
    )
    hook = MemoryContextHook(store)
    event = InboundEvent(
        message_id="m-1",
        tenant_id="demo",
        channel=Channel.WECHAT,
        user_id="wxid_member_a",
        session_id="group-1@chatroom",
        message=Message(content="查一下物流"),
        metadata={"source": "wxbot"},
    )
    session = Session(
        session_id="group-1@chatroom",
        tenant_id="demo",
        user_id="group-1@chatroom",
        channel=Channel.WECHAT,
    )
    ctx = PipelineContext(event=event, trace_id="trace-1", session=session)

    await hook.run(ctx)

    assert store.graph_retrieve_calls == [
        {
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "session_id": "group-1@chatroom",
            "user_id": "wxid_member_a",
            "query": "查一下物流",
            "fact_top_k": 2,
            "episode_top_k": 1,
            "budget_chars": 400,
            "exclude_memory_item_ids": [7],
            "request_session_kind": "group",
        },
        {
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "session_id": "group-1@chatroom",
            "user_id": GROUP_HISTORY_USER_ID_SCOPE,
            "query": "查一下物流",
            "fact_top_k": 2,
            "episode_top_k": 1,
            "budget_chars": 400,
            "exclude_memory_item_ids": [17],
            "request_session_kind": "group",
        },
    ]
    assert session.variables["user_memory"]["relevant_graph_facts"][0]["object_value"] == "物流"
    assert (
        session.variables["group_memory"]["relevant_graph_facts"][0]["object_value"] == "发货窗口"
    )
    assert session.variables["user_memory"]["memory_graph_budget_chars"] == 400


@pytest.mark.asyncio
async def test_memory_context_hook_uses_hybrid_once_when_enabled() -> None:
    store = _FakeMemoryStore()
    store.settings = SimpleNamespace(
        memory_retrieval_enabled=True,
        memory_hybrid_retrieval_enabled=True,
        memory_retrieval_top_k=5,
        memory_graph_retrieval_enabled=True,
        memory_graph_retrieval_fact_top_k=2,
        memory_graph_retrieval_episode_top_k=1,
        memory_graph_retrieval_budget_chars=400,
        memory_group_identity_memory_enabled=True,
    )
    hook = MemoryContextHook(store)
    event = InboundEvent(
        message_id="m-1",
        tenant_id="demo",
        channel=Channel.WECHAT,
        user_id="wxid_member_a",
        session_id="group-1@chatroom",
        message=Message(content="查一下物流"),
        metadata={"source": "wxbot"},
    )
    session = Session(
        session_id="group-1@chatroom",
        tenant_id="demo",
        user_id="group-1@chatroom",
        channel=Channel.WECHAT,
    )
    ctx = PipelineContext(event=event, trace_id="trace-1", session=session)

    await hook.run(ctx)

    assert store.retrieve_calls == []
    assert store.graph_retrieve_calls == []
    assert store.hybrid_retrieve_calls == [
        {
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "session_id": "group-1@chatroom",
            "user_id": "wxid_member_a",
            "query": "查一下物流",
            "limit": 5,
            "fact_top_k": 2,
            "episode_top_k": 1,
            "budget_chars": 400,
            "include_graph": True,
            "request_session_kind": "group",
        },
        {
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "session_id": "group-1@chatroom",
            "user_id": GROUP_HISTORY_USER_ID_SCOPE,
            "query": "查一下物流",
            "limit": 5,
            "fact_top_k": 2,
            "episode_top_k": 1,
            "budget_chars": 400,
            "include_graph": True,
            "request_session_kind": "group",
        },
    ]
    assert session.variables["user_memory"]["relevant_memory_items"][0]["id"] == 7
    assert session.variables["user_memory"]["relevant_graph_facts"][0]["object_value"] == "物流"
    assert session.variables["group_memory"]["relevant_memory_items"][0]["id"] == 17


@pytest.mark.asyncio
async def test_memory_context_hook_does_not_load_other_member_memory_in_group() -> None:
    store = _FakeMemoryStore()
    hook = MemoryContextHook(store)
    event = InboundEvent(
        message_id="m-1b",
        tenant_id="demo",
        channel=Channel.WECHAT,
        user_id="wxid_member_b",
        session_id="group-1@chatroom",
        message=Message(content="查一下物流"),
        metadata={"source": "wxbot", "sender_wxid": "wxid_member_b", "sender_name": "群友B"},
    )
    session = Session(
        session_id="group-1@chatroom",
        tenant_id="demo",
        user_id="group-1@chatroom",
        channel=Channel.WECHAT,
    )
    ctx = PipelineContext(event=event, trace_id="trace-1b", session=session)

    await hook.run(ctx)

    loaded_user_ids = [call["user_id"] for call in store.get_calls]
    retrieved_user_ids = [call["user_id"] for call in store.retrieve_calls]
    assert loaded_user_ids == ["wxid_member_b", GROUP_HISTORY_USER_ID_SCOPE]
    assert retrieved_user_ids == ["wxid_member_b", GROUP_HISTORY_USER_ID_SCOPE]
    assert "wxid_member_a" not in loaded_user_ids
    assert "wxid_member_a" not in retrieved_user_ids
    assert session.variables["user_memory"]["user_id"] == "wxid_member_b"
    assert session.variables["group_memory"]["user_id"] == GROUP_HISTORY_USER_ID_SCOPE


@pytest.mark.asyncio
async def test_memory_load_step_sets_memory_signal() -> None:
    store = _FakeMemoryStore()
    step = MemoryLoadStep(store)
    event = InboundEvent(
        message_id="m-1",
        tenant_id="demo",
        channel=Channel.DISCORD,
        user_id="discord-user-a",
        session_id="discord-channel-1",
        message=Message(content="查一下物流"),
        metadata={"source": "discord"},
    )
    session = Session(
        session_id="discord-channel-1",
        tenant_id="demo",
        user_id="discord-channel-1",
        channel=Channel.DISCORD,
    )
    ctx = PipelineContext(event=event, trace_id="trace-1", session=session)

    result = await step.run(ctx)

    assert result.action == "continue"
    assert result.reason == "loaded"
    assert store.get_calls == [
        {
            "tenant_id": "demo",
            "channel": "discord",
            "source_key": "discord",
            "session_id": "discord-channel-1",
            "user_id": "discord-user-a",
            "request_session_kind": "private",
        }
    ]
    assert ctx.signals["memory"]["user_profile"]["user_id"] == "discord-user-a"
    assert session.variables["user_memory"]["user_id"] == "discord-user-a"


@pytest.mark.asyncio
async def test_memory_context_hook_respects_retrieval_disabled() -> None:
    store = _FakeMemoryStore()
    store.settings.memory_retrieval_enabled = False
    hook = MemoryContextHook(store)
    event = InboundEvent(
        message_id="m-1",
        tenant_id="demo",
        channel=Channel.WECHAT,
        user_id="wxid_member_a",
        session_id="group-1@chatroom",
        message=Message(content="查一下物流"),
        metadata={"source": "wxbot"},
    )
    session = Session(
        session_id="group-1@chatroom",
        tenant_id="demo",
        user_id="group-1@chatroom",
        channel=Channel.WECHAT,
    )
    ctx = PipelineContext(event=event, trace_id="trace-1", session=session)

    await hook.run(ctx)

    assert store.retrieve_calls == []
    assert session.variables["user_memory"]["relevant_memory_items"] == []


@pytest.mark.asyncio
async def test_memory_persistence_hook_persists_reply_by_event_user_id() -> None:
    store = _FakeMemoryStore()
    hook = MemoryPersistenceHook(store)
    event = InboundEvent(
        message_id="m-2",
        tenant_id="demo",
        channel=Channel.WECHAT,
        user_id="wxid_member_b",
        session_id="group-2@chatroom",
        message=Message(content="我要看物流"),
        metadata={"source": "wxbot"},
        trace_id="trace-2",
    )
    session = Session(
        session_id="group-2@chatroom",
        tenant_id="demo",
        user_id="group-2@chatroom",
        channel=Channel.WECHAT,
    )
    reply = OutboundReply(
        tenant_id="demo",
        channel=Channel.WECHAT,
        user_id="group-2@chatroom",
        session_id="group-2@chatroom",
        segments=[ReplySegment(content="物流今天会继续更新")],
    )
    ctx = PipelineContext(
        event=event,
        trace_id="trace-2",
        session=session,
        pre=PreprocessedMessage(original_text="我要看物流", cleaned_text="我要看物流"),
        reply=reply,
    )

    await hook.run(ctx)

    assert len(store.remember_calls) == 1
    call = dict(store.remember_calls[0])
    expiry = datetime.fromisoformat(str(call.pop("expires_at")))
    assert call == {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_member_b",
        "session_id": "group-2@chatroom",
        "user_text": "我要看物流",
        "assistant_text": "物流今天会继续更新",
        "trace_id": "trace-2",
        "origin_session_kind": "group",
        "audience_scope": "session",
        "allowed_session_ids": ["group-2@chatroom"],
        "sensitivity_category": "normal",
        "source_kind": "conversation",
    }
    assert datetime.now(UTC) + timedelta(days=29, hours=23) <= expiry
    assert expiry <= datetime.now(UTC) + timedelta(days=30, minutes=1)
    assert session.variables["user_memory"]["user_id"] == "wxid_member_b"
    assert session.variables["user_memory"]["message_count"] == 4
    assert session.variables["user_memory"]["session_message_count"] == 2
    assert session.variables["user_memory"]["session_summary"] == (
        "Recent context: user asked to see logistics."
    )
    assert session.variables["user_memory"]["open_items"] == [
        {
            "text": "Check updated logistics status",
            "created_at": "2026-04-21T12:02:00+00:00",
        }
    ]
    assert session.variables["user_memory"]["decisions"] == [
        {
            "text": "Confirmed logistics reply should be concise",
            "created_at": "2026-04-21T12:02:00+00:00",
        }
    ]
    assert session.variables["user_memory"]["recent_turns"] == [
        {
            "user_text": "我要看物流",
            "assistant_text": "物流今天会继续更新",
            "created_at": "2026-04-21T12:03:00+00:00",
        }
    ]
    assert session.variables["user_memory"]["last_compacted_at"] == "2026-04-21T12:06:00+00:00"
    assert session.variables["user_memory"]["summary_version"] == 2
    assert session.variables["user_memory"]["session_profile"]["updated_at"] == (
        "2026-04-21T12:03:00+00:00"
    )
    assert session.variables["user_memory"]["memory_items"]["session"][0]["content"] == (
        "用户刚刚要求看物流"
    )


@pytest.mark.asyncio
async def test_group_memory_persistence_captures_policy_audience_and_retention() -> None:
    store = _FakeMemoryStore()

    async def get_policy(**kwargs):
        assert kwargs == {
            "tenant_id": "demo",
            "session_id": "group-2@chatroom",
            "user_id": "wxid_member_b",
        }
        return MemberPrivacyValues(
            memory_enabled=True,
            allow_group_recall=True,
            audience_scope="session",
            retention_days=30,
        )

    store.get_group_member_privacy_policy = get_policy
    hook = MemoryPersistenceHook(store)
    event = InboundEvent(
        message_id="m-policy",
        tenant_id="demo",
        channel=Channel.WECHAT,
        user_id="wxid_member_b",
        session_id="group-2@chatroom",
        message=Message(content="记住本群的发货窗口"),
        metadata={"source": "wxbot"},
        trace_id="trace-policy",
    )
    session = Session(
        session_id="group-2@chatroom",
        tenant_id="demo",
        user_id="group-2@chatroom",
        channel=Channel.WECHAT,
    )
    ctx = PipelineContext(
        event=event,
        trace_id=event.trace_id,
        session=session,
        pre=PreprocessedMessage(
            original_text="记住本群的发货窗口",
            cleaned_text="记住本群的发货窗口",
        ),
        reply=OutboundReply(
            tenant_id="demo",
            channel=Channel.WECHAT,
            user_id=session.session_id,
            session_id=session.session_id,
            segments=[ReplySegment(content="记住了")],
        ),
    )
    earliest = datetime.now(UTC) + timedelta(days=29, hours=23)

    await hook.run(ctx)

    call = store.remember_calls[0]
    expiry = datetime.fromisoformat(str(call["expires_at"]))
    assert call["origin_session_kind"] == "group"
    assert call["audience_scope"] == "session"
    assert call["allowed_session_ids"] == ["group-2@chatroom"]
    assert call["sensitivity_category"] == "normal"
    assert call["source_kind"] == "conversation"
    assert earliest <= expiry <= datetime.now(UTC) + timedelta(days=30, minutes=1)


@pytest.mark.asyncio
async def test_group_memory_persistence_is_session_scoped_without_opt_in() -> None:
    store = _FakeMemoryStore()
    store.settings = SimpleNamespace(
        memory_retrieval_enabled=True,
        memory_retrieval_top_k=6,
        memory_group_identity_memory_enabled=False,
    )
    hook = MemoryPersistenceHook(store)
    event = InboundEvent(
        message_id="m-session-only",
        tenant_id="demo",
        channel=Channel.WECHAT,
        user_id="wxid_member_b",
        session_id="group-2@chatroom",
        message=Message(content="以后默认短答"),
        metadata={"source": "wxbot"},
        trace_id="trace-session-only",
    )
    session = Session(
        session_id="group-2@chatroom",
        tenant_id="demo",
        user_id="group-2@chatroom",
        channel=Channel.WECHAT,
    )
    ctx = PipelineContext(
        event=event,
        trace_id=event.trace_id,
        session=session,
        pre=PreprocessedMessage(
            original_text="以后默认短答",
            cleaned_text="以后默认短答",
        ),
        reply=OutboundReply(
            tenant_id="demo",
            channel=Channel.WECHAT,
            user_id=session.session_id,
            session_id=session.session_id,
            segments=[ReplySegment(content="好")],
        ),
    )

    await hook.run(ctx)

    assert store.remember_calls[0]["identity_scope"] is False
    assert session.variables["user_memory"]["long_term"] == ""
    assert session.variables["user_memory"]["memory_items"]["identity"] == []


@pytest.mark.asyncio
async def test_memory_persistence_skips_observation_only_group_message() -> None:
    store = _FakeMemoryStore()
    hook = MemoryPersistenceHook(store)
    event = InboundEvent(
        message_id="m-observed",
        tenant_id="demo",
        channel=Channel.WECHAT,
        user_id="wxid_member_b",
        session_id="group-2@chatroom",
        message=Message(content="群里普通聊天"),
        metadata={"source": "wxbot"},
        trace_id="trace-observed",
    )
    session = Session(
        session_id="group-2@chatroom",
        tenant_id="demo",
        user_id="group-2@chatroom",
        channel=Channel.WECHAT,
    )
    reply = OutboundReply(
        tenant_id="demo",
        channel=Channel.WECHAT,
        user_id="group-2@chatroom",
        session_id="group-2@chatroom",
    )
    ctx = PipelineContext(
        event=event,
        trace_id=event.trace_id,
        session=session,
        pre=PreprocessedMessage(original_text="群里普通聊天", cleaned_text="群里普通聊天"),
        reply=reply,
        extras={
            "interaction_mode": "observed",
            "wxbot_reply_policy": {"allowed": False},
        },
    )

    await hook.run(ctx)

    assert store.remember_calls == []
    assert ctx.signals["memory"]["observation_only_skipped"] is True


@pytest.mark.asyncio
async def test_memory_save_step_persists_reply_and_updates_signal() -> None:
    store = _FakeMemoryStore()
    step = MemorySaveStep(store)
    event = InboundEvent(
        message_id="m-2",
        tenant_id="demo",
        channel=Channel.DISCORD,
        user_id="discord-user-b",
        session_id="discord-channel-2",
        message=Message(content="我要看物流"),
        metadata={"source": "discord"},
        trace_id="trace-2",
    )
    session = Session(
        session_id="discord-channel-2",
        tenant_id="demo",
        user_id="discord-channel-2",
        channel=Channel.DISCORD,
    )
    reply = OutboundReply(
        tenant_id="demo",
        channel=Channel.DISCORD,
        user_id="discord-channel-2",
        session_id="discord-channel-2",
        segments=[ReplySegment(content="物流今天会继续更新")],
    )
    ctx = PipelineContext(
        event=event,
        trace_id="trace-2",
        session=session,
        pre=PreprocessedMessage(original_text="我要看物流", cleaned_text="我要看物流"),
        reply=reply,
    )

    result = await step.run(ctx)

    assert result.action == "continue"
    assert result.reason == "saved"
    assert len(result.effects) == 1
    assert result.effects[0].type == "save_memory"
    assert result.effects[0].owner == "memory"
    assert result.effects[0].idempotency_key == (
        "memory:save:demo:discord:discord:discord-channel-2:discord-user-b:trace-2"
    )
    assert result.effects[0].payload["message_count"] == 4
    assert store.remember_calls == [
        {
            "tenant_id": "demo",
            "channel": "discord",
            "source_key": "discord",
            "user_id": "discord-user-b",
            "session_id": "discord-channel-2",
            "user_text": "我要看物流",
            "assistant_text": "物流今天会继续更新",
            "trace_id": "trace-2",
            "origin_session_kind": "private",
            "audience_scope": "private",
            "allowed_session_ids": [],
            "sensitivity_category": "normal",
            "expires_at": None,
            "source_kind": "conversation",
        }
    ]
    assert ctx.signals["memory"]["user_profile"]["message_count"] == 4
    assert session.variables["user_memory"]["user_id"] == "discord-user-b"


@pytest.mark.asyncio
async def test_memory_save_step_effect_handler_opt_in_only_emits_effect() -> None:
    store = _FakeMemoryStore()
    step = MemorySaveStep(store, effect_handler_enabled=True)
    event = InboundEvent(
        message_id="m-3",
        tenant_id="demo",
        channel=Channel.DISCORD,
        user_id="discord-user-c",
        session_id="discord-channel-3",
        message=Message(content="我要看物流"),
        metadata={"source": "discord"},
        trace_id="trace-3",
    )
    session = Session(
        session_id="discord-channel-3",
        tenant_id="demo",
        user_id="discord-channel-3",
        channel=Channel.DISCORD,
    )
    reply = OutboundReply(
        tenant_id="demo",
        channel=Channel.DISCORD,
        user_id="discord-channel-3",
        session_id="discord-channel-3",
        segments=[ReplySegment(content="物流今天会继续更新")],
    )
    ctx = PipelineContext(
        event=event,
        trace_id="trace-3",
        session=session,
        pre=PreprocessedMessage(original_text="我要看物流", cleaned_text="我要看物流"),
        reply=reply,
    )

    result = await step.run(ctx)

    assert result.action == "continue"
    assert result.reason == "effect_pending"
    assert len(result.effects) == 1
    assert result.effects[0].type == "save_memory"
    assert result.effects[0].owner == "memory"
    assert result.effects[0].payload == {
        "tenant_id": "demo",
        "channel": "discord",
        "source_key": "discord",
        "session_id": "discord-channel-3",
        "user_id": "discord-user-c",
        "user_text": "我要看物流",
        "assistant_text": "物流今天会继续更新",
        "trace_id": "trace-3",
        "origin_session_kind": "private",
        "audience_scope": "private",
        "allowed_session_ids": [],
        "sensitivity_category": "normal",
        "expires_at": None,
        "source_kind": "conversation",
        "message_count": 0,
        "identity_message_count": 0,
        "session_message_count": 0,
    }
    assert store.remember_calls == []
    assert "memory" not in ctx.signals


@pytest.mark.asyncio
async def test_memory_save_effect_handler_persists_and_updates_signal() -> None:
    store = _FakeMemoryStore()
    step = MemorySaveStep(store, effect_handler_enabled=True)
    registry = EffectHandlerRegistry()
    register_memory_save_handler(registry, store)
    dispatcher = EffectDispatcher(registry, InMemoryEffectCommitter())
    event = InboundEvent(
        message_id="m-4",
        tenant_id="demo",
        channel=Channel.WECHAT,
        user_id="wxid_member_c",
        session_id="group-4@chatroom",
        message=Message(content="我要看物流"),
        metadata={"source": "wxbot"},
        trace_id="trace-4",
    )
    session = Session(
        session_id="group-4@chatroom",
        tenant_id="demo",
        user_id="group-4@chatroom",
        channel=Channel.WECHAT,
    )
    reply = OutboundReply(
        tenant_id="demo",
        channel=Channel.WECHAT,
        user_id="group-4@chatroom",
        session_id="group-4@chatroom",
        segments=[ReplySegment(content="物流今天会继续更新")],
    )
    ctx = PipelineContext(
        event=event,
        trace_id="trace-4",
        session=session,
        pre=PreprocessedMessage(original_text="我要看物流", cleaned_text="我要看物流"),
        reply=reply,
    )

    step_result = await step.run(ctx)
    dispatch_results = await dispatcher.dispatch_all(step_result.effects, ctx)

    assert [result.status for result in dispatch_results] == [EFFECT_STATUS_RECORDED]
    assert len(store.remember_calls) == 1
    call = dict(store.remember_calls[0])
    expiry = datetime.fromisoformat(str(call.pop("expires_at")))
    assert call == {
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_member_c",
        "session_id": "group-4@chatroom",
        "user_text": "我要看物流",
        "assistant_text": "物流今天会继续更新",
        "trace_id": "trace-4",
        "origin_session_kind": "group",
        "audience_scope": "session",
        "allowed_session_ids": ["group-4@chatroom"],
        "sensitivity_category": "normal",
        "source_kind": "conversation",
    }
    assert datetime.now(UTC) + timedelta(days=29, hours=23) <= expiry
    assert expiry <= datetime.now(UTC) + timedelta(days=30, minutes=1)
    assert ctx.signals["memory"]["user_profile"]["message_count"] == 4
    assert session.variables["user_memory"]["user_id"] == "wxid_member_c"
