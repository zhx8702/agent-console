from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.channel.identity import canonical_conversation_id
from app.common.types import Channel, InboundEvent, Message, PreprocessedMessage, Session
from app.orchestrator.pipeline import PipelineContext
from plugins.group_activity.hooks import GroupActivityObserveHook

_MANAGED_CONNECTION_ID = "connection-a"
_EXTERNAL_SESSION_ID = "external-room@chatroom"
_CANONICAL_SESSION_ID = canonical_conversation_id(
    _MANAGED_CONNECTION_ID,
    _EXTERNAL_SESSION_ID,
)


class _FakeStore:
    def __init__(self) -> None:
        self.candidates = []
        self.settings = SimpleNamespace(wxbot_default_tenant_id="demo")

    async def upsert_candidate(self, tenant_id: str, session_id: str, **kwargs):
        self.candidates.append(
            {"tenant_id": tenant_id, "session_id": session_id, **kwargs}
        )
        return {
            "adapter_id": kwargs.get("adapter_id", ""),
            "connection_id": kwargs.get("connection_id", ""),
            "external_session_id": kwargs.get("external_session_id", ""),
        }


def _ctx(
    session_id: str = _CANONICAL_SESSION_ID,
    channel: Channel = Channel.WECHAT,
) -> PipelineContext:
    event = InboundEvent(
        message_id="m1",
        tenant_id="demo",
        channel=channel,
        adapter_id="wechat-sdk",
        connection_id=_MANAGED_CONNECTION_ID,
        user_id="wxid_a",
        session_id=session_id,
        external_conversation_id=_EXTERNAL_SESSION_ID,
        message=Message(content="hello"),
        metadata={
            "session_name": "测试群",
            "session_kind": "group" if session_id.endswith("@chatroom") else "private",
        },
    )
    return PipelineContext(
        event=event,
        trace_id="trace-1",
        session=Session(
            session_id=session_id,
            tenant_id="demo",
            user_id="wxid_a",
            channel=channel,
        ),
        pre=PreprocessedMessage(original_text="hello", cleaned_text="hello"),
    )


@pytest.mark.asyncio
async def test_group_activity_hook_records_group_candidate() -> None:
    store = _FakeStore()
    hook = GroupActivityObserveHook(store)
    ctx = _ctx()

    await hook.run(ctx)

    assert store.candidates == [
        {
            "tenant_id": "demo",
            "session_id": _CANONICAL_SESSION_ID,
            "session_name": "测试群",
            "connection_id": _MANAGED_CONNECTION_ID,
            "adapter_id": "wechat-sdk",
            "external_session_id": _EXTERNAL_SESSION_ID,
        }
    ]
    assert ctx.session is not None
    assert ctx.session.adapter_id == "wechat-sdk"
    assert ctx.session.connection_id == _MANAGED_CONNECTION_ID
    assert ctx.session.external_conversation_id == _EXTERNAL_SESSION_ID
    assert ctx.session.canonical_conversation_id == _CANONICAL_SESSION_ID
    assert ctx.session.metadata["external_session_id"] == _EXTERNAL_SESSION_ID
    assert ctx.session.metadata["canonical_conversation_id"] == _CANONICAL_SESSION_ID


@pytest.mark.asyncio
async def test_group_activity_hook_ignores_private_sessions() -> None:
    store = _FakeStore()
    hook = GroupActivityObserveHook(store)

    await hook.run(_ctx(session_id="private-room"))

    assert store.candidates == []


@pytest.mark.asyncio
async def test_group_activity_hook_ignores_self_sent_group_events() -> None:
    store = _FakeStore()
    hook = GroupActivityObserveHook(store)
    ctx = _ctx()
    ctx.event.metadata["is_self_sent"] = True

    await hook.run(ctx)

    assert store.candidates == []


@pytest.mark.asyncio
async def test_group_activity_hook_normalizes_metadata_fallback_into_session() -> None:
    store = _FakeStore()
    hook = GroupActivityObserveHook(store)
    event = InboundEvent(
        message_id="m-metadata-identity",
        tenant_id="demo",
        channel=Channel.WECHAT,
        user_id="wxid_a",
        session_id=_CANONICAL_SESSION_ID,
        message=Message(content="hello"),
        metadata={
            "session_name": "测试群",
            "session_kind": "group",
            "adapter_id": "wechat-sdk",
            "connection_id": _MANAGED_CONNECTION_ID,
            "external_session_id": _EXTERNAL_SESSION_ID,
        },
    )
    assert event.external_conversation_id == _CANONICAL_SESSION_ID
    ctx = PipelineContext(
        event=event,
        trace_id="trace-metadata-identity",
        session=Session(
            session_id=_CANONICAL_SESSION_ID,
            tenant_id="demo",
            user_id="wxid_a",
            channel=Channel.WECHAT,
        ),
        pre=PreprocessedMessage(original_text="hello", cleaned_text="hello"),
    )

    await hook.run(ctx)

    assert len(store.candidates) == 1
    assert ctx.event.connection_id == _MANAGED_CONNECTION_ID
    assert ctx.event.external_conversation_id == _EXTERNAL_SESSION_ID
    assert ctx.session is not None
    assert ctx.session.metadata["connection_id"] == _MANAGED_CONNECTION_ID
