from __future__ import annotations

import pytest

from plugins.memory.store import MemoryStore
from plugins.wxbot.plugin import _OwnerGatedMemoryFeedbackStore


def _memory_feedback_store(gate) -> _OwnerGatedMemoryFeedbackStore:
    store = object.__new__(_OwnerGatedMemoryFeedbackStore)
    store._owners_scope_execution_allowed = gate
    return store


@pytest.mark.asyncio
async def test_memory_feedback_correction_rechecks_combined_owners_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decisions = iter((True, False))
    gate_calls: list[tuple[tuple[str, ...], str, str]] = []
    events: list[str] = []

    async def gate(
        owners: tuple[str, ...],
        tenant_id: str,
        session_id: str,
    ) -> bool:
        gate_calls.append((owners, tenant_id, session_id))
        return next(decisions)

    async def resolve(_self, **_kwargs: str):
        await _self._before_member_fact_correction_mutation(
            tenant_id=_kwargs["tenant_id"],
            session_id=_kwargs["session_id"],
            user_id=_kwargs["user_id"],
        )
        events.append("mutation")
        return {"status": "applied", "changed": 1}

    monkeypatch.setattr(MemoryStore, "resolve_member_fact_correction", resolve)
    store = _memory_feedback_store(gate)

    with pytest.raises(RuntimeError, match="memory_plugin_runtime_disabled"):
        await store.resolve_member_fact_correction(
            tenant_id="demo",
            session_id="room@chatroom",
            user_id="wxid-member",
            correction_text="你记错了",
            idempotency_key="feedback-correction-1",
        )

    assert events == []
    assert gate_calls == [
        (("wxbot", "memory"), "demo", "room@chatroom"),
        (("wxbot", "memory"), "demo", "room@chatroom"),
    ]


@pytest.mark.asyncio
async def test_memory_feedback_privacy_forget_remains_ungated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate_calls: list[tuple[tuple[str, ...], str, str]] = []

    async def deny(
        owners: tuple[str, ...],
        tenant_id: str,
        session_id: str,
    ) -> bool:
        gate_calls.append((owners, tenant_id, session_id))
        return False

    async def forget(_self, **_kwargs: str) -> int:
        return 2

    monkeypatch.setattr(MemoryStore, "forget_member", forget)
    store = _memory_feedback_store(deny)

    changed = await store.forget_member(
        tenant_id="demo",
        session_id="room@chatroom",
        user_id="wxid-member",
        idempotency_key="feedback-forget-1",
    )

    assert changed == 2
    assert gate_calls == []
