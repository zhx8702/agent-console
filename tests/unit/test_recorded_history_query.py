from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.channel.identity import canonical_conversation_id
from app.common.types import Channel, Role, Session, Turn
from plugins.wxbot.agent_tool_service import WxbotAgentToolService
from plugins.wxbot.history_query import search_recorded_history


def session() -> Session:
    sid = canonical_conversation_id("connection-a", "room@chatroom")
    return Session(
        tenant_id="demo", session_id=sid, user_id="canonical-member", channel=Channel.WECHAT,
        connection_id="connection-a", external_conversation_id="room@chatroom",
        variables={"semantic_intent": {
            "operation": "retrieve", "source": "local_history", "domain": "group_info",
            "action": "query", "confidence": 0.9,
            "slots": {"sender_scope": "self", "period": "yesterday"},
        }},
        turns=[Turn(session_id=sid, role=Role.USER, content="我昨天说过什么",
                    metadata={"sender_wxid": "requester", "canonical_message_id": "current-message"})],
    )


class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 2, 4, 2, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_history_uses_calendar_day_and_server_requester_not_model_sender(monkeypatch):
    import plugins.wxbot.history_query as module

    monkeypatch.setattr(module, "datetime", FixedClock)
    store = SimpleNamespace(search_recorded_group_messages=AsyncMock(return_value={
        "total": 22, "truncated": True, "messages": [{"observation_id": 1, "text": "部署问题"}],
    }))
    service = object.__new__(WxbotAgentToolService)
    service._wxbot_store = store
    service._social_policy_store = SimpleNamespace(get_group_policy=AsyncMock(return_value=SimpleNamespace(
        policy=SimpleNamespace(timezone="Asia/Shanghai"),
    )))
    current = session()
    result = await search_recorded_history(service, current, {
        "sender_wxid": "somebody-else", "sender_name": "Other", "sender_scope": "group",
        "period": "recent", "hours": 336,
    })
    query = store.search_recorded_group_messages.call_args.kwargs
    assert query["tenant_id"] == "demo"
    assert query["session_id"] == current.session_id
    assert query["sender_wxid"] == "requester"
    assert query["sender_name"] == ""
    assert query["exclude_message_id"] == "current-message"
    assert query["start_ts"] == int(datetime(2026, 2, 2, 16, tzinfo=UTC).timestamp())
    assert query["end_ts"] == int(datetime(2026, 2, 3, 16, tzinfo=UTC).timestamp())
    assert result["truncated"] is True
    assert result["coverage"] == "captured_records_only"


@pytest.mark.asyncio
async def test_managed_history_never_falls_back_to_global_sdk():
    service = object.__new__(WxbotAgentToolService)
    service._wxbot_store = SimpleNamespace(search_recorded_group_messages=AsyncMock(return_value={"total": 0, "messages": []}))
    service._social_policy_store = None
    service._load_group_context = AsyncMock(side_effect=AssertionError("must not call SDK"))
    service._load_group_text_messages = AsyncMock(side_effect=AssertionError("must not call SDK"))
    result = await service.search_group_messages(session(), {"sender_scope": "self"})
    assert result["source"] == "recorded_group_observations"
    service._load_group_context.assert_not_called()
    service._load_group_text_messages.assert_not_called()


@pytest.mark.asyncio
async def test_mismatched_managed_connection_cannot_read_history():
    current = session()
    current.connection_id = "connection-b"
    service = object.__new__(WxbotAgentToolService)
    service._wxbot_store = SimpleNamespace(search_recorded_group_messages=AsyncMock())
    with pytest.raises(ValueError, match="scope_mismatch"):
        await search_recorded_history(service, current, {})
    service._wxbot_store.search_recorded_group_messages.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_self_identity_cannot_expand_to_whole_group():
    current = session()
    current.turns = []
    service = object.__new__(WxbotAgentToolService)
    service._wxbot_store = SimpleNamespace(search_recorded_group_messages=AsyncMock())
    with pytest.raises(ValueError, match="requester_identity_unavailable"):
        await search_recorded_history(service, current, {"sender_scope": "self"})
    service._wxbot_store.search_recorded_group_messages.assert_not_called()
