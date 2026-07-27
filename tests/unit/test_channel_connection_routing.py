from __future__ import annotations

import pytest

from app.channel.identity import (
    LEGACY_WXBOT_CONNECTION_ID,
    canonical_conversation_id,
    canonical_message_id,
    canonical_participant_id,
)
from app.channel.models import (
    ChannelMedia,
    ChannelSendOptions,
    ChannelSendResult,
    ChannelTarget,
)
from app.channel.registry import ChannelOutboundExecutionDenied, ChannelRegistry
from app.common.types import Channel, InboundEvent, Message


class _Provider:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple[str, ChannelTarget]] = []

    async def get_session_policy(self, target: ChannelTarget) -> dict[str, str]:
        self.calls.append(("get_session_policy", target))
        return {"provider": self.name}

    async def send_text(
        self,
        target: ChannelTarget,
        text: str,
        options: ChannelSendOptions | None = None,
    ) -> ChannelSendResult:
        _ = text, options
        self.calls.append(("send_text", target))
        return ChannelSendResult(provider=self.name)

    async def send_image(
        self,
        target: ChannelTarget,
        media: ChannelMedia,
        options: ChannelSendOptions | None = None,
    ) -> ChannelSendResult:
        _ = media, options
        self.calls.append(("send_image", target))
        return ChannelSendResult(provider=self.name)


class _CapturingProvider(_Provider):
    async def capture_group_delivery_contract(
        self,
        target: ChannelTarget,
        *,
        source_message_id: str,
        response_kind: str = "tool_result",
    ) -> dict[str, str]:
        self.calls.append(("capture_group_delivery_contract", target))
        return {
            "source_message_id": source_message_id,
            "response_kind": response_kind,
        }


async def _allow_owner(_owner: str, _target: ChannelTarget) -> bool:
    return True


def _target(
    *,
    tenant_id: str = "tenant-a",
    session_id: str = "room-a",
    connection_id: str = "",
    adapter_id: str = "",
) -> ChannelTarget:
    return ChannelTarget(
        tenant_id=tenant_id,
        channel="wechat",
        session_id=session_id,
        connection_id=connection_id,
        adapter_id=adapter_id,
    )


@pytest.mark.asyncio
async def test_same_platform_connections_route_to_their_own_provider() -> None:
    registry = ChannelRegistry(owner_gate=_allow_owner)
    fallback = _Provider("fallback")
    first = _Provider("first")
    second = _Provider("second")
    registry.register_outbound("wechat", fallback, owner="legacy")
    registry.register_connection_outbound(
        "tenant-a",
        "wechat-a",
        first,
        channel="wechat",
        owner="plugin-a",
    )
    registry.register_connection_outbound(
        "tenant-a",
        "wechat-b",
        second,
        channel="wechat",
        owner="plugin-b",
    )

    first_target = _target(connection_id="wechat-a")
    second_target = _target(connection_id="wechat-b")

    first_outbound = registry.require_outbound_for_target(first_target)
    second_outbound = registry.require_outbound_for_target(second_target)
    assert await first_outbound.get_session_policy(first_target) == {"provider": "first"}
    assert await second_outbound.get_session_policy(second_target) == {"provider": "second"}
    assert (
        registry.outbound_for(
            "wechat", tenant_id="tenant-a", connection_id="unknown-connection"
        )
        is None
    )
    fallback_target = _target()
    assert await registry.require_outbound("wechat").get_session_policy(
        fallback_target
    ) == {"provider": "fallback"}


def test_owner_overwrite_cannot_unregister_the_new_provider() -> None:
    registry = ChannelRegistry(owner_gate=_allow_owner)
    first = _Provider("first")
    second = _Provider("second")
    registry.register_outbound("wechat", first, owner="plugin-a")
    registry.register_outbound("wechat", second, owner="plugin-b")

    assert registry.unregister_owner("plugin-a") == 0
    assert registry.owner_for("wechat") == "plugin-b"
    assert registry.outbound_for("wechat") is not None
    assert registry.unregister_owner("plugin-b") == 1
    assert registry.outbound_for("wechat") is None


@pytest.mark.asyncio
async def test_scoped_target_uses_only_its_adapter_dispatcher_when_exact_route_is_absent() -> None:
    registry = ChannelRegistry(owner_gate=_allow_owner)
    legacy = _Provider("legacy")
    wechat_adapter = _Provider("wechat-adapter")
    registry.register_outbound("wechat", legacy, owner="legacy")
    registry.register_adapter_outbound(
        "wechat-sdk",
        wechat_adapter,
        channel="wechat",
        owner="wxbot",
    )
    target = ChannelTarget(
        tenant_id="tenant-a",
        channel="wechat",
        adapter_id="wechat-sdk",
        connection_id="wechat-main",
        session_id="conversation-a",
    )

    assert await registry.require_outbound_for_target(target).get_session_policy(target) == {
        "provider": "wechat-adapter"
    }
    assert (
        registry.outbound_for(
            "wechat",
            tenant_id="tenant-a",
            connection_id="wechat-main",
            adapter_id="different-adapter",
        )
        is None
    )
    assert registry.owner_for("wechat") == "legacy"


def test_unregister_owner_removes_adapter_and_channel_routes() -> None:
    registry = ChannelRegistry(owner_gate=_allow_owner)
    provider = _Provider("wxbot")
    registry.register_outbound("wechat", provider, owner="wxbot")
    registry.register_adapter_outbound("wechat-sdk", provider, owner="wxbot")

    assert registry.unregister_owner("wxbot") == 2
    assert registry.outbound_for("wechat") is None
    assert (
        registry.outbound_for(
            "wechat",
            tenant_id="tenant-a",
            connection_id="wechat-main",
            adapter_id="wechat-sdk",
        )
        is None
    )


@pytest.mark.asyncio
async def test_owned_outbound_facade_rechecks_target_scope_before_every_operation() -> None:
    allowed = True
    gate_calls: list[tuple[str, str, str]] = []

    async def gate(owner: str, target: ChannelTarget) -> bool:
        gate_calls.append((owner, target.tenant_id, target.session_id))
        return allowed

    provider = _Provider("wxbot")
    registry = ChannelRegistry(owner_gate=gate)
    registry.register_outbound("wechat", provider, owner="wxbot")
    first = _target(session_id="room-1")
    second = _target(session_id="room-2")
    third = _target(tenant_id="tenant-b", session_id="room-3")
    outbound = registry.outbound_for("wechat")

    assert outbound is not None
    assert outbound is not provider
    assert await outbound.get_session_policy(first) == {"provider": "wxbot"}
    assert (await registry.require_outbound("wechat").send_text(second, "hello")).provider == (
        "wxbot"
    )
    assert (
        await outbound.send_image(third, ChannelMedia(image_url="https://example.test/a.png"))
    ).provider == "wxbot"
    assert gate_calls == [
        ("wxbot", "tenant-a", "room-1"),
        ("wxbot", "tenant-a", "room-2"),
        ("wxbot", "tenant-b", "room-3"),
    ]

    allowed = False
    denied_target = _target(tenant_id="tenant-c", session_id="room-4")
    with pytest.raises(ChannelOutboundExecutionDenied) as exc_info:
        await outbound.send_text(denied_target, "must not send")

    assert exc_info.value.owner == "wxbot"
    assert exc_info.value.target is denied_target
    assert exc_info.value.reason == "owner_execution_denied"
    assert gate_calls[-1] == ("wxbot", "tenant-c", "room-4")
    assert provider.calls == [
        ("get_session_policy", first),
        ("send_text", second),
        ("send_image", third),
    ]


@pytest.mark.asyncio
async def test_owned_outbound_facade_preserves_optional_delivery_contract_gate() -> None:
    allowed = True
    gate_calls: list[tuple[str, str, str]] = []

    async def gate(owner: str, target: ChannelTarget) -> bool:
        gate_calls.append((owner, target.tenant_id, target.session_id))
        return allowed

    provider = _CapturingProvider("wxbot")
    registry = ChannelRegistry(owner_gate=gate)
    registry.register_outbound("wechat", provider, owner="wxbot")
    target = _target(session_id="room-contract")
    outbound = registry.require_outbound("wechat")
    capture = outbound.capture_group_delivery_contract  # type: ignore[attr-defined]

    assert await capture(
        target,
        source_message_id="source-1",
        response_kind="agent_tool",
    ) == {
        "source_message_id": "source-1",
        "response_kind": "agent_tool",
    }
    assert gate_calls == [("wxbot", "tenant-a", "room-contract")]

    allowed = False
    with pytest.raises(ChannelOutboundExecutionDenied):
        await capture(
            target,
            source_message_id="source-2",
        )
    assert provider.calls == [("capture_group_delivery_contract", target)]


@pytest.mark.asyncio
async def test_owned_outbound_fails_closed_without_gate_but_empty_owner_is_compatible() -> None:
    target = _target()
    owned_provider = _Provider("owned")
    owned_registry = ChannelRegistry()
    owned_registry.register_outbound("wechat", owned_provider, owner="plugin")

    with pytest.raises(ChannelOutboundExecutionDenied) as exc_info:
        await owned_registry.require_outbound("wechat").get_session_policy(target)

    assert exc_info.value.reason == "owner_gate_not_configured"
    assert owned_provider.calls == []

    async def reject_if_called(_owner: str, _target: ChannelTarget) -> bool:
        raise AssertionError("empty compatibility owners must bypass the plugin gate")

    legacy_provider = _Provider("legacy")
    legacy_registry = ChannelRegistry(owner_gate=reject_if_called)
    legacy_registry.register_outbound("wechat", legacy_provider)
    outbound = legacy_registry.require_outbound("wechat")

    assert outbound is legacy_provider
    assert await outbound.get_session_policy(target) == {"provider": "legacy"}


def test_dynamic_channel_and_connection_identity_propagate_to_target() -> None:
    event = InboundEvent(
        message_id="external-message",
        tenant_id="tenant-a",
        channel="feixin",
        adapter_id="feixin",
        connection_id="feixin-primary",
        user_id="external-user",
        session_id="canonical-session",
        external_conversation_id="external-room",
        canonical_conversation_id="canonical-session",
        external_participant_id="external-user",
        canonical_participant_id="canonical-user",
        message=Message(content="hello"),
    )

    target = ChannelTarget.from_event(event)

    assert event.channel == "feixin"
    assert not isinstance(event.channel, Channel)
    assert target.adapter_id == "feixin"
    assert target.connection_id == "feixin-primary"
    assert target.external_conversation_id == "external-room"
    assert target.canonical_conversation_id == "canonical-session"
    assert target.canonical_participant_id == "canonical-user"


def test_external_ids_are_isolated_by_connection_namespace() -> None:
    external_conversation = "same-room"
    external_participant = "same-user"
    external_message = "same-message"

    assert canonical_conversation_id("connection-a", external_conversation) != (
        canonical_conversation_id("connection-b", external_conversation)
    )
    assert canonical_participant_id("connection-a", external_participant) != (
        canonical_participant_id("connection-b", external_participant)
    )
    assert canonical_message_id("connection-a", external_message) != (
        canonical_message_id("connection-b", external_message)
    )
    assert (
        canonical_conversation_id(
            LEGACY_WXBOT_CONNECTION_ID,
            external_conversation,
        )
        == external_conversation
    )
