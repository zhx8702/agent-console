from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.common.types import Channel, InboundEvent, Message
from app.orchestrator.pipeline import PipelineContext
from plugins.speaker_portrait.hooks import SpeakerPortraitNoteStep, _external_session_id
from plugins.speaker_portrait.plugin import SpeakerPortraitPlugin


def _event(*, external_session_id: str = "53876528317@chatroom", content: str = "hello") -> InboundEvent:
    return InboundEvent(
        message_id="message-1",
        tenant_id="default",
        channel=Channel.WECHAT,
        adapter_id="wechat-sdk",
        connection_id="connection-1",
        user_id="cx1:p:user",
        session_id="cx1:c:canonical@chatroom",
        external_conversation_id=external_session_id,
        external_user_id="wxid_hai",
        message=Message(content=content),
        metadata={"sender_wxid": "wxid_hai", "sender_name": "小海"},
    )


def test_portrait_session_id_prefers_sdk_external_identity() -> None:
    ctx = PipelineContext(event=_event(), trace_id="trace-1")

    assert _external_session_id(ctx) == "53876528317@chatroom"


def test_portrait_session_id_does_not_persist_canonical_fallback() -> None:
    ctx = PipelineContext(event=_event(external_session_id=""), trace_id="trace-1")

    assert _external_session_id(ctx) == ""


class _NoteStore:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def note_speaker_message(self, **kwargs) -> bool:
        self.calls.append(kwargs)
        return True


@pytest.mark.asyncio
async def test_note_step_uses_external_session_and_only_counts_text() -> None:
    store = _NoteStore()
    step = SpeakerPortraitNoteStep(store)  # type: ignore[arg-type]

    result = await step.run(PipelineContext(event=_event(), trace_id="trace-1"))
    empty = await step.run(
        PipelineContext(event=_event(content=""), trace_id="trace-2")
    )

    assert result.reason == "noted"
    assert store.calls[0]["session_id"] == "53876528317@chatroom"
    assert empty.reason == "no_text"
    assert len(store.calls) == 1


class _HotUpdateStore:
    def __init__(self) -> None:
        self.created: list[dict] = []

    async def due_hot_updates(self, **_kwargs) -> list[dict]:
        return [
            {
                "id": 7,
                "tenant_id": "default",
                "session_id": "53876528317@chatroom",
                "speaker_id": "wxid_hai",
                "display_name": "小海",
                "pending_messages": 37,
                "last_distilled_message_at": "2026-08-27T07:00:00+00:00",
            }
        ]

    async def create_job(self, **kwargs) -> dict:
        self.created.append(kwargs)
        return {"id": 8, "status": "queued"}


@pytest.mark.asyncio
async def test_hot_update_keeps_pending_until_success_and_uses_distilled_cursor() -> None:
    store = _HotUpdateStore()
    plugin = SpeakerPortraitPlugin()
    plugin._store = store  # type: ignore[assignment]
    plugin._ctx = SimpleNamespace(
        settings=SimpleNamespace(
            speaker_portrait_hot_update_min_messages=40,
            speaker_portrait_hot_update_min_seconds=3600.0,
        )
    )

    await plugin._enqueue_hot_updates()

    assert store.created == [
        {
            "tenant_id": "default",
            "session_id": "53876528317@chatroom",
            "session_name": "53876528317@chatroom",
            "speaker_id": "wxid_hai",
            "speaker_name": "小海",
            "external_session_id": "53876528317@chatroom",
            "days_limit": 14,
            "max_messages": 800,
            "mode": "incremental",
            "since_timestamp": "2026-08-27T07:00:00+00:00",
            "portrait_id": 7,
            "claimed_pending_messages": 37,
        }
    ]
