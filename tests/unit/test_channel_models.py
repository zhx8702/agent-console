from __future__ import annotations

from app.channel import ChannelTarget, apply_event_scope_to_session
from app.channel.models import configuration_session_id
from app.common.types import Channel, InboundEvent, Message, Session


def test_channel_target_from_event_prefers_generic_sender_and_reply_fields() -> None:
    event = InboundEvent(
        message_id="event-message-1",
        tenant_id="demo",
        channel=Channel.DISCORD,
        user_id="discord-user-fallback",
        session_id="discord-channel-1",
        message=Message(content="/draw poster"),
        metadata={
            "session_kind": "group",
            "session_name": "设计频道",
            "sender_id": "discord-user-1",
            "sender_name": "Alice",
            "reply_to_message_id": "discord-message-1",
            "sender_wxid": "wxid_should_not_win",
            "msg_svr_id": "wx-message-should-not-win",
        },
    )

    target = ChannelTarget.from_event(event)

    assert target.channel == "discord"
    assert target.session_kind == "group"
    assert target.session_name == "设计频道"
    assert target.sender_id == "discord-user-1"
    assert target.sender_name == "Alice"
    assert target.reply_to_message_id == "discord-message-1"


def test_channel_target_from_session_keeps_wechat_legacy_fields() -> None:
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_user",
        channel=Channel.WECHAT,
        metadata={
            "session_name": "微信群",
            "sender_wxid": "wxid_sender",
            "msg_svr_id": "wx-message-1",
        },
    )

    target = ChannelTarget.from_session(session)

    assert target.channel == "wechat"
    assert target.session_kind == "group"
    assert target.sender_id == "wxid_sender"
    assert target.reply_to_message_id == "wx-message-1"


def test_event_connection_scope_is_copied_to_session() -> None:
    event = InboundEvent(
        message_id="message-a",
        tenant_id="demo",
        channel="feixin",
        adapter_id="feixin-gateway",
        connection_id="feixin-primary",
        user_id="user-a",
        session_id="canonical-room-a",
        external_conversation_id="external-room-a",
        canonical_conversation_id="canonical-room-a",
        external_user_id="external-user-a",
        canonical_participant_id="canonical-user-a",
        message=Message(content="hello"),
    )
    session = Session(
        session_id=event.session_id,
        tenant_id=event.tenant_id,
        user_id=event.user_id,
        channel=event.channel,
    )

    result = apply_event_scope_to_session(session, event)

    assert result is session
    assert session.adapter_id == "feixin-gateway"
    assert session.connection_id == "feixin-primary"
    assert session.external_conversation_id == "external-room-a"
    assert session.canonical_conversation_id == "canonical-room-a"
    assert session.canonical_participant_id == "canonical-user-a"
    assert session.metadata["external_message_id"] == "message-a"


def test_configuration_session_id_uses_external_id_for_managed_event() -> None:
    event = InboundEvent(
        message_id="managed-message",
        tenant_id="demo",
        channel=Channel.WECHAT,
        user_id="cx1:p:user",
        session_id="cx1:c:managed@chatroom",
        external_conversation_id="00000000000@chatroom",
        canonical_conversation_id="cx1:c:managed@chatroom",
        message=Message(content="hello"),
    )

    assert configuration_session_id(event) == "00000000000@chatroom"


def test_configuration_session_id_recovers_external_id_from_metadata() -> None:
    event = InboundEvent(
        message_id="managed-message",
        tenant_id="demo",
        channel=Channel.WECHAT,
        user_id="cx1:p:user",
        session_id="cx1:c:managed@chatroom",
        message=Message(content="hello"),
        metadata={"external_conversation_id": "00000000000@chatroom"},
    )

    assert event.external_conversation_id == event.session_id
    assert configuration_session_id(event) == "00000000000@chatroom"


def test_configuration_session_id_keeps_legacy_event_id_authoritative() -> None:
    event = InboundEvent(
        message_id="legacy-message",
        tenant_id="demo",
        channel=Channel.WECHAT,
        user_id="wxid-user",
        session_id="00000000000@chatroom",
        external_conversation_id="unexpected-other-room@chatroom",
        message=Message(content="hello"),
    )

    assert configuration_session_id(event) == "00000000000@chatroom"
