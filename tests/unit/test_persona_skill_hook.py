from __future__ import annotations

import pytest

from app.common.types import Channel, InboundEvent, Message, Session
from app.orchestrator.pipeline import PipelineContext
from plugins.persona_extract.hooks import PersonaSkillEnrichStep, PersonaSkillHook


class _FakePersonaStore:
    def __init__(
        self,
        profile: dict | None,
        *,
        profile_session_id: str = "",
    ) -> None:
        self._profile = profile
        self._profile_session_id = profile_session_id
        self.calls: list[dict[str, str]] = []

    async def resolve_profile(
        self,
        *,
        tenant_id: str,
        session_id: str,
        channel: str,
        source_key: str,
    ) -> dict | None:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "channel": channel,
                "source_key": source_key,
            }
        )
        if self._profile_session_id and session_id != self._profile_session_id:
            return None
        return self._profile


@pytest.mark.asyncio
async def test_persona_skill_hook_injects_profile_by_channel_and_source() -> None:
    store = _FakePersonaStore(
        {
            "id": 1,
            "profile_name": "wechat-default",
            "channel": "wechat",
            "source_key": "wxbot",
            "source_label": "微信机器人",
            "prompt_text": "请模仿这个人的表达风格回复。",
            "job_id": 12,
        }
    )
    hook = PersonaSkillHook(store)
    event = InboundEvent(
        message_id="m-1",
        tenant_id="demo",
        channel=Channel.WECHAT,
        user_id="u1",
        session_id="s1",
        message=Message(content="hello"),
        metadata={"source": "wxbot"},
    )
    session = Session(
        session_id="s1",
        tenant_id="demo",
        user_id="u1",
        channel=Channel.WECHAT,
    )
    ctx = PipelineContext(event=event, trace_id="trace-1", session=session)

    await hook.run(ctx)

    assert store.calls == [
        {
            "tenant_id": "demo",
            "session_id": "s1",
            "channel": "wechat",
            "source_key": "wxbot",
        }
    ]
    assert session.variables["persona_skill"] == "请模仿这个人的表达风格回复。"
    assert session.variables["persona_profile"]["name"] == "wechat-default"


@pytest.mark.asyncio
async def test_persona_skill_hook_uses_external_session_for_managed_channel() -> None:
    external_session_id = "00000000000@chatroom"
    canonical_session_id = "cx1:c:managed@chatroom"
    store = _FakePersonaStore(
        {
            "id": 1,
            "profile_name": "小海",
            "channel": "wechat",
            "source_key": "wxbot",
            "source_label": "微信机器人",
            "prompt_text": "使用小海的人格风格。",
            "job_id": 12,
            "skill_slug": "xiaohai",
        },
        profile_session_id=external_session_id,
    )
    event = InboundEvent(
        message_id="m-managed",
        tenant_id="demo",
        channel=Channel.WECHAT,
        adapter_id="wechat-sdk",
        connection_id="wechat-main",
        user_id="cx1:p:user",
        session_id=canonical_session_id,
        external_conversation_id=external_session_id,
        canonical_conversation_id=canonical_session_id,
        message=Message(content="@zzz hello"),
        metadata={"source": "wxbot"},
    )
    session = Session(
        session_id=canonical_session_id,
        tenant_id="demo",
        user_id="cx1:p:user",
        channel=Channel.WECHAT,
        external_conversation_id=external_session_id,
        canonical_conversation_id=canonical_session_id,
    )
    ctx = PipelineContext(event=event, trace_id="trace-managed", session=session)

    await PersonaSkillHook(store).run(ctx)

    assert store.calls[0]["session_id"] == external_session_id
    assert session.variables["persona_skill"] == "使用小海的人格风格。"
    assert session.variables["persona_profile"]["skill_slug"] == "xiaohai"


@pytest.mark.asyncio
async def test_persona_skill_enrich_step_sets_signal() -> None:
    store = _FakePersonaStore(
        {
            "id": 2,
            "profile_name": "discord-default",
            "channel": "discord",
            "source_key": "discord",
            "source_label": "Discord",
            "prompt_text": "Use concise Discord-style replies.",
            "job_id": 13,
            "artifact": {
                "slug": "discord-default",
                "files": {"skill_prompt": "Use concise Discord-style replies."},
                "target": {"user_id": "u1", "name": "User One"},
                "meta": {"impression": "concise"},
            },
        }
    )
    step = PersonaSkillEnrichStep(store)
    event = InboundEvent(
        message_id="m-1",
        tenant_id="demo",
        channel=Channel.DISCORD,
        user_id="u1",
        session_id="discord-channel-1",
        message=Message(content="hello"),
        metadata={"source": "discord"},
    )
    session = Session(
        session_id="discord-channel-1",
        tenant_id="demo",
        user_id="u1",
        channel=Channel.DISCORD,
    )
    ctx = PipelineContext(event=event, trace_id="trace-1", session=session)

    result = await step.run(ctx)

    assert result.action == "continue"
    assert result.reason == "loaded"
    assert session.variables["persona_skill"] == "Use concise Discord-style replies."
    assert ctx.signals["persona"]["skill"]["matched"] is True
    assert ctx.signals["persona"]["skill"]["profile"]["name"] == "discord-default"
    assert ctx.signals["persona"]["skill"]["profile"]["target_name"] == "User One"


@pytest.mark.asyncio
async def test_persona_skill_hook_clears_stale_profile_when_missing() -> None:
    store = _FakePersonaStore(None)
    hook = PersonaSkillHook(store)
    event = InboundEvent(
        message_id="m-2",
        tenant_id="demo",
        channel=Channel.WEB,
        user_id="u1",
        session_id="s1",
        message=Message(content="hello"),
        metadata={},
    )
    session = Session(
        session_id="s1",
        tenant_id="demo",
        user_id="u1",
        channel=Channel.WEB,
        variables={
            "persona_skill": "old skill",
            "persona_profile": {"name": "old"},
        },
    )
    ctx = PipelineContext(event=event, trace_id="trace-2", session=session)

    await hook.run(ctx)

    assert "persona_skill" not in session.variables
    assert "persona_profile" not in session.variables


@pytest.mark.asyncio
async def test_persona_skill_enrich_step_clears_signal_when_missing() -> None:
    store = _FakePersonaStore(None)
    step = PersonaSkillEnrichStep(store)
    event = InboundEvent(
        message_id="m-2",
        tenant_id="demo",
        channel=Channel.WEB,
        user_id="u1",
        session_id="s1",
        message=Message(content="hello"),
        metadata={},
    )
    session = Session(
        session_id="s1",
        tenant_id="demo",
        user_id="u1",
        channel=Channel.WEB,
        variables={
            "persona_skill": "old skill",
            "persona_profile": {"name": "old"},
        },
    )
    ctx = PipelineContext(event=event, trace_id="trace-2", session=session)

    result = await step.run(ctx)

    assert result.action == "continue"
    assert result.reason == "not_found"
    assert "persona_skill" not in session.variables
    assert "persona_profile" not in session.variables
    assert ctx.signals["persona"]["skill"]["matched"] is False
