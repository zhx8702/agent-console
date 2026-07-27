from __future__ import annotations

import asyncio

import pytest

from app.common.types import (
    CapabilityResult,
    Channel,
    PreprocessedMessage,
    RouteType,
    Session,
)
from app.plugin.runtime import (
    CapabilityOwnerExecutionDenied,
    GatedCapabilityEngine,
)


class _Capability:
    name = "plugin-capability"

    def __init__(self) -> None:
        self.calls = 0

    async def answer(self, pre, session, hints=None) -> CapabilityResult:
        _ = pre, session, hints
        self.calls += 1
        return CapabilityResult(route=RouteType.AGENT, reply_text="ok")


def _session() -> Session:
    return Session(
        session_id="room@chatroom",
        tenant_id="tenant-1",
        user_id="user-1",
        channel=Channel.WECHAT,
    )


def _pre() -> PreprocessedMessage:
    return PreprocessedMessage(original_text="hello", cleaned_text="hello")


@pytest.mark.asyncio
async def test_gated_capability_allows_compatible_and_enabled_execution() -> None:
    calls: list[tuple[str, str, str]] = []

    async def allow(owner: str, session: Session) -> bool:
        calls.append((owner, session.tenant_id, session.session_id))
        return True

    delegate = _Capability()
    wrapped = GatedCapabilityEngine("plugin", delegate, allow)

    result = await wrapped.answer(_pre(), _session(), {"source": "test"})

    assert result.reply_text == "ok"
    assert delegate.calls == 1
    assert calls == [
        ("plugin", "tenant-1", "room@chatroom"),
        ("plugin", "tenant-1", "room@chatroom"),
    ]
    assert wrapped.name == delegate.name
    assert wrapped.owner == "plugin"
    assert wrapped.delegate is delegate

    missing_gate = _Capability()
    with pytest.raises(CapabilityOwnerExecutionDenied):
        await GatedCapabilityEngine("plugin", missing_gate, None).answer(
            _pre(),
            _session(),
        )
    assert missing_gate.calls == 0


@pytest.mark.asyncio
async def test_gated_capability_revalidates_after_delegate_returns() -> None:
    decisions = iter((True, False))

    async def gate(owner: str, session: Session) -> bool:
        _ = owner, session
        return next(decisions)

    delegate = _Capability()
    wrapped = GatedCapabilityEngine("plugin", delegate, gate)

    with pytest.raises(CapabilityOwnerExecutionDenied) as raised:
        await wrapped.answer(_pre(), _session())

    assert raised.value.reason == "owner_disabled_after_capability"
    assert delegate.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["denied", "error", "invalid", "timeout"])
async def test_gated_capability_fails_closed_without_calling_delegate(mode: str) -> None:
    async def gate(owner: str, session: Session):
        _ = owner, session
        if mode == "error":
            raise RuntimeError("sensitive backend detail")
        if mode == "invalid":
            return "yes"
        if mode == "timeout":
            await asyncio.sleep(0.1)
        return False

    delegate = _Capability()
    wrapped = GatedCapabilityEngine(
        "plugin",
        delegate,
        gate,
        gate_timeout_seconds=0.01,
    )

    with pytest.raises(CapabilityOwnerExecutionDenied) as raised:
        await wrapped.answer(_pre(), _session())

    assert str(raised.value) == "capability_owner_execution_denied"
    assert raised.value.owner == "plugin"
    assert delegate.calls == 0


@pytest.mark.asyncio
async def test_gated_capability_never_blocks_core_owner() -> None:
    gate_calls = 0

    async def deny(owner: str, session: Session) -> bool:
        nonlocal gate_calls
        _ = owner, session
        gate_calls += 1
        return False

    delegate = _Capability()
    result = await GatedCapabilityEngine("core", delegate, deny).answer(
        _pre(),
        _session(),
    )

    assert result.reply_text == "ok"
    assert delegate.calls == 1
    assert gate_calls == 0
