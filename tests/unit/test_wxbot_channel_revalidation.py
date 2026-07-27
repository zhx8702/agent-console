from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.channel import ChannelSendOptions, ChannelTarget
from app.social.contracts import (
    GroupParticipationPolicyDocument,
    KillSwitches,
    ParticipationPolicyValues,
)
from plugins.wxbot.channel import WxbotChannelOutbound


class _QueueStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def enqueue_reply(self, **kwargs: Any) -> int:
        self.calls.append(dict(kwargs))
        return len(self.calls)


class _PolicyStore:
    def __init__(self, document: GroupParticipationPolicyDocument) -> None:
        self.document = document

    async def get_group_policy(
        self,
        tenant_id: str,
        session_id: str,
    ) -> GroupParticipationPolicyDocument:
        assert (tenant_id, session_id) == ("demo", self.document.session_id)
        return self.document


def _document(
    *,
    version: int = 7,
    enabled: bool = True,
) -> GroupParticipationPolicyDocument:
    return GroupParticipationPolicyDocument(
        tenant_id="demo",
        session_id="room@chatroom",
        version=version,
        kill_switches=KillSwitches(group_enabled=enabled),
        effective_enabled=enabled,
        policy=ParticipationPolicyValues(rollout_stage="contextual"),
    )


def _target() -> ChannelTarget:
    return ChannelTarget(
        tenant_id="demo",
        channel="wechat",
        session_id="room@chatroom",
        session_kind="group",
        reply_to_message_id="question-1",
    )


def _managed_target() -> ChannelTarget:
    return ChannelTarget(
        tenant_id="demo",
        channel="wechat",
        session_id="cx1:c:managed-room@chatroom",
        external_conversation_id="room@chatroom",
        canonical_conversation_id="cx1:c:managed-room@chatroom",
        session_kind="group",
        reply_to_message_id="question-managed-1",
    )


class _ConnectionStore:
    def __init__(self, *, desired_state: str = "enabled") -> None:
        self.desired_state = desired_state

    async def get(self, tenant_id: str, connection_id: str) -> SimpleNamespace:
        assert (tenant_id, connection_id) == ("demo", "wechat-main")
        return SimpleNamespace(
            adapter_id="wechat-sdk",
            desired_state=self.desired_state,
        )


@pytest.mark.asyncio
async def test_channel_outbound_adds_revalidation_contract_to_group_tool_result() -> None:
    store = _QueueStore()
    channel = WxbotChannelOutbound(
        store,  # type: ignore[arg-type]
        social_policy_store=_PolicyStore(_document()),
    )

    contract = await channel.capture_group_delivery_contract(
        _target(),
        source_message_id="question-1",
    )
    await channel.send_text(
        _target(),
        "地图生成好了",
        ChannelSendOptions(
            source_message={"message_id": "question-1"},
            delivery_metadata=contract,
        ),
    )

    delivery = store.calls[0]["delivery"]
    assert delivery["participation_status"] == "must_reply"
    assert delivery["source_message_id"] == "question-1"
    assert delivery["participation_policy_version"] == 7
    assert delivery["send_revalidation_enabled"] is True
    assert delivery["speech_class"] == "obligation"


@pytest.mark.asyncio
async def test_managed_channel_policy_fences_use_external_conversation_scope() -> None:
    store = _QueueStore()
    channel = WxbotChannelOutbound(
        store,  # type: ignore[arg-type]
        social_policy_store=_PolicyStore(_document()),
    )

    contract = await channel.capture_group_delivery_contract(
        _managed_target(),
        source_message_id="question-managed-1",
    )
    result = await channel.send_text(
        _managed_target(),
        "外部群策略已命中",
        ChannelSendOptions(
            source_message={"message_id": "question-managed-1"},
            delivery_metadata=contract,
        ),
    )

    assert result.provider == "wxbot"
    assert store.calls[0]["delivery"]["external_conversation_id"] == "room@chatroom"
    assert (
        store.calls[0]["delivery"]["canonical_conversation_id"]
        == "cx1:c:managed-room@chatroom"
    )


@pytest.mark.asyncio
async def test_channel_outbound_distinguishes_soft_group_output_from_task_obligation() -> None:
    store = _QueueStore()
    channel = WxbotChannelOutbound(
        store,  # type: ignore[arg-type]
        social_policy_store=_PolicyStore(_document()),
    )

    contract = await channel.capture_group_delivery_contract(
        _target(),
        source_message_id="question-1",
    )
    contract.update(
        {
            "participation_status": "may_reply",
            "response_kind": "short",
            "speech_class": "soft",
        }
    )
    await channel.send_text(
        _target(),
        "顺带补充一句",
        ChannelSendOptions(
            source_message={"message_id": "question-1"},
            delivery_metadata=contract,
        ),
    )

    delivery = store.calls[0]["delivery"]
    assert delivery["participation_status"] == "may_reply"
    assert delivery["speech_class"] == "soft"
    assert delivery["send_revalidation_enabled"] is True


@pytest.mark.asyncio
async def test_channel_outbound_fails_closed_without_policy_store_or_captured_contract() -> None:
    store = _QueueStore()
    channel = WxbotChannelOutbound(store)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="policy_store_required"):
        await channel.send_text(
            _target(),
            "不能绕过策略",
            ChannelSendOptions(
                source_message={"message_id": "question-1"},
                delivery_metadata={
                    "participation_status": "must_reply",
                    "source_message_id": "question-1",
                    "participation_policy_version": 7,
                    "send_revalidation_enabled": True,
                },
            ),
        )

    assert store.calls == []


@pytest.mark.asyncio
async def test_channel_outbound_does_not_replace_missing_request_version_at_send_time() -> None:
    store = _QueueStore()
    channel = WxbotChannelOutbound(
        store,  # type: ignore[arg-type]
        social_policy_store=_PolicyStore(_document()),
    )

    with pytest.raises(RuntimeError, match="request_contract_required"):
        await channel.send_text(
            _target(),
            "缺少请求时版本，不能用发送时版本补齐",
            ChannelSendOptions(
                source_message={"message_id": "question-1"},
                delivery_metadata={"response_kind": "tool_result"},
            ),
        )

    assert store.calls == []


@pytest.mark.asyncio
async def test_managed_channel_outbound_fails_closed_when_connection_is_disabled() -> None:
    store = _QueueStore()
    channel = WxbotChannelOutbound(
        store,  # type: ignore[arg-type]
        connection_store=_ConnectionStore(desired_state="disabled"),
    )
    target = ChannelTarget(
        tenant_id="demo",
        channel="wechat",
        adapter_id="wechat-sdk",
        connection_id="wechat-main",
        session_id="private-user",
    )

    with pytest.raises(RuntimeError, match="wxbot_connection_not_enabled"):
        await channel.send_text(target, "should not queue")

    assert store.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("document", "delivery", "reason"),
    [
        (
            _document(version=8),
            {
                "participation_policy_version": 7,
                "send_revalidation_enabled": True,
                "participation_status": "must_reply",
                "source_message_id": "question-1",
            },
            "participation_policy_version_changed",
        ),
        (
            _document(enabled=False),
            {
                "participation_policy_version": 7,
                "send_revalidation_enabled": True,
                "participation_status": "must_reply",
                "source_message_id": "question-1",
            },
            "participation_disabled",
        ),
    ],
)
async def test_channel_outbound_suppresses_stale_or_disabled_group_result(
    document: GroupParticipationPolicyDocument,
    delivery: dict[str, Any],
    reason: str,
) -> None:
    store = _QueueStore()
    channel = WxbotChannelOutbound(
        store,  # type: ignore[arg-type]
        social_policy_store=_PolicyStore(document),
    )

    result = await channel.send_text(
        _target(),
        "这条不能发送",
        ChannelSendOptions(
            source_message={"message_id": "question-1"},
            delivery_metadata=delivery,
        ),
    )

    assert result.metadata == {"suppressed": True, "reason": reason}
    assert store.calls == []
