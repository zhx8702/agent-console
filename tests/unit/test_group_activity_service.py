
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.channel import ChannelTarget
from app.channel.identity import canonical_conversation_id
from app.common.types import CapabilityResult, RouteType
from app.social import (
    GroupParticipationPolicyDocument,
    KillSwitches,
    ParticipationPolicyValues,
    VoiceProfile,
)
from plugins.group_activity.service import GroupActivityDecision, GroupActivityService
from plugins.group_activity.store import GroupActivityStore

_MANAGED_CONNECTION_ID = "connection-managed"
_MANAGED_EXTERNAL_SESSION_ID = "external-room@chatroom"
_MANAGED_SESSION_ID = canonical_conversation_id(
    _MANAGED_CONNECTION_ID,
    _MANAGED_EXTERNAL_SESSION_ID,
)


class _FakeStore:
    def __init__(self) -> None:
        self.event_created = False
        self.completed: list[dict] = []
        self.marked: list[dict] = []
        self.recent_exists = False
        self.completed_today = 0
        self.has_completed = False
        self.latest_completed: dict | None = None
        self.recent_texts: list[str] = []
        self.configs: list[dict] | None = None

    async def get_config(self, tenant_id: str, session_id: str):
        return _config(tenant_id=tenant_id, session_id=session_id)

    async def list_enabled_configs(self, limit: int = 200):
        return list(self.configs) if self.configs is not None else [_config()]

    async def recent_event_exists(self, tenant_id: str, session_id: str, *, minutes: int) -> bool:
        assert minutes >= 60
        return self.recent_exists

    async def count_completed_today(self, tenant_id: str, session_id: str, *, timezone: str) -> int:
        return self.completed_today

    async def has_completed_event(self, tenant_id: str, session_id: str) -> bool:
        return self.has_completed

    async def latest_completed_event(self, tenant_id: str, session_id: str):
        return self.latest_completed

    async def list_recent_generated_texts(
        self,
        tenant_id: str,
        session_id: str,
        *,
        minutes: int,
        limit: int = 20,
    ) -> list[str]:
        return list(self.recent_texts[:limit])

    async def try_create_event(self, **kwargs):
        if self.event_created:
            return None
        self.event_created = True
        return {"id": 1, **kwargs}

    async def try_start_event(self, event_id: int):
        return {"id": event_id, "slot_key": "slot", "trace_id": "trace"}

    async def complete_event(self, event_id: int, **kwargs):
        self.completed.append({"event_id": event_id, **kwargs})
        return {"id": event_id, "status": "completed"}

    async def mark_event(self, event_id: int, **kwargs) -> None:
        self.marked.append({"event_id": event_id, **kwargs})


class _FakeReader:
    def __init__(self, messages: list[dict]) -> None:
        self.messages = messages
        self.calls = 0

    async def load_group_text_messages(self, *args, **kwargs):
        self.calls += 1
        return list(self.messages)


class _FakeQueryReader:
    def __init__(self) -> None:
        self.calls = 0

    async def query_rows(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return [{"ok": 1}]
        body = b"wxid_member:\nhello".hex()
        return [
            {
                "server_id": "external-message-1",
                "create_time": int(time.time()) - 190 * 60,
                "real_sender_id": "wxid_member",
                "local_type": 1,
                "message_content_hex": body,
                "compression_type": 0,
            }
        ]


class _FakeObservationStore:
    def __init__(self, observations: list[dict]) -> None:
        self.observations = observations
        self.calls: list[tuple[str, str, int]] = []

    async def list_recent_group_observations(
        self,
        tenant_id: str,
        session_id: str,
        *,
        limit: int = 50,
        before_id: int | None = None,
    ) -> list[dict]:
        assert before_id is None
        self.calls.append((tenant_id, session_id, limit))
        return list(self.observations)


class _FakeAgent:
    def __init__(self, reply: str = "刚才那个部署日志有人继续看吗？") -> None:
        self.reply = reply
        self.calls: list[dict] = []

    async def answer(self, pre, session, hints=None):
        self.calls.append({"pre": pre, "session": session, "hints": hints})
        return CapabilityResult(route=RouteType.AGENT, reply_text=self.reply)


class _BlockingAgent(_FakeAgent):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def answer(self, pre, session, hints=None):
        self.calls.append({"pre": pre, "session": session, "hints": hints})
        self.started.set()
        await self.release.wait()
        return CapabilityResult(route=RouteType.AGENT, reply_text=self.reply)


class _ScopeGate:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.calls: list[tuple[tuple[str, ...], str, str]] = []

    async def __call__(
        self,
        owners: tuple[str, ...],
        *,
        tenant_id: str,
        session_id: str,
    ) -> bool:
        self.calls.append((owners, tenant_id, session_id))
        return self.enabled


class _FakeOutbound:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, target, text, options=None):
        self.sent.append({"target": target, "text": text, "options": options})
        return SimpleNamespace(metadata={"reply_queue_id": 42})


class _BlockingOutbound(_FakeOutbound):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def send_text(self, target, text, options=None):
        self.sent.append({"target": target, "text": text, "options": options})
        self.started.set()
        await self.release.wait()
        return SimpleNamespace(metadata={"reply_queue_id": 42})


class _FakeChannelRegistry:
    def __init__(self, outbound: _FakeOutbound | None) -> None:
        self.outbound = outbound
        self.targets: list = []

    def outbound_for_target(self, target):
        self.targets.append(target)
        return self.outbound


class _FakeSocialPolicyStore:
    def __init__(
        self,
        *,
        effective_enabled: bool = True,
        proactive_enabled: bool = True,
        voice_profile: VoiceProfile | None = None,
    ) -> None:
        current_hour = datetime.now(UTC).hour
        self.document = GroupParticipationPolicyDocument(
            tenant_id="demo",
            session_id="room@chatroom",
            version=7,
            kill_switches=KillSwitches(
                global_enabled=effective_enabled,
                tenant_enabled=True,
                group_enabled=True,
            ),
            effective_enabled=effective_enabled,
            policy=ParticipationPolicyValues(
                timezone="UTC",
                quiet_start_hour=(current_hour + 1) % 24,
                quiet_end_hour=(current_hour + 2) % 24,
                proactive_enabled=proactive_enabled,
                rollout_stage="proactive",
                rollout_opt_in=True,
                proactive_rollout_percent=100,
            ),
            voice_profile=voice_profile,
        )
        self.events: list[dict] = []

    async def get_group_policy(self, tenant_id: str, session_id: str):
        return self.document.model_copy(
            update={"tenant_id": tenant_id, "session_id": session_id}
        )

    async def record_participation_event(self, **kwargs):
        self.events.append(kwargs)
        return SimpleNamespace(**kwargs)


def _config(**overrides):
    data = {
        "tenant_id": "demo",
        "session_id": "room@chatroom",
        "session_name": "测试群",
        "channel_id": "wechat",
        "adapter_id": "wechat-sdk",
        "connection_id": "legacy-wechat-default",
        "external_session_id": "room@chatroom",
        "enabled": True,
        "active_start": "00:00",
        "active_end": "23:59",
        "quiet_start": "00:00",
        "quiet_end": "00:00",
        "timezone": "Asia/Shanghai",
        "idle_minutes": 180,
        "lookback_minutes": 60,
        "min_send_interval_minutes": 60,
        "max_per_day": 8,
        "topic_repeat_window_minutes": 1440,
        "llm_model_tier": "tier-2",
        "temperature": 0.9,
        "agent_tool_scope": "group_info",
    }
    data.update(overrides)
    return data


def _messages(*ages: int) -> list[dict]:
    now = int(time.time())
    return [
        {
            "message_id": f"msg-{index}",
            "sender_wxid": f"wxid_{index}",
            "sender_name": f"用户{index}",
            "text": f"消息{index}",
            "timestamp": "2026-04-30 10:00:00",
            "ts": now - age,
        }
        for index, age in enumerate(ages, start=1)
    ]


def _observations(*ages: int) -> list[dict]:
    now = int(time.time())
    return [
        {
            "id": index,
            "message_id": f"canonical-msg-{index}",
            "sender_wxid": f"canonical-user-{index}",
            "sender_name": f"用户{index}",
            "msg_type": "text",
            "content": f"消息{index}",
            "is_self_sent": False,
            "occurred_ts": now - age,
            "metadata": {"external_message_id": f"external-msg-{index}"},
        }
        for index, age in enumerate(ages, start=1)
    ]


def _service(
    messages: list[dict],
    *,
    agent: _FakeAgent | None = None,
    store: _FakeStore | None = None,
    outbound: _FakeOutbound | None = None,
    social_policy_store: _FakeSocialPolicyStore | None = None,
    scope_gate: _ScopeGate | None = None,
    wxbot_store: _FakeObservationStore | None = None,
    channel_registry: _FakeChannelRegistry | None = None,
    patch_identity: bool = True,
):
    fake_store = store or _FakeStore()
    fake_agent = agent or _FakeAgent()
    fake_outbound = outbound or _FakeOutbound()
    service = GroupActivityService(
        store=fake_store,
        settings=SimpleNamespace(
            wxbot_sdk_url="http://127.0.0.1:5080",
            wxbot_self_wxid="wxid_bot",
            wxbot_default_tenant_id="demo",
        ),
        agent_engine=fake_agent,
        outbound=fake_outbound,
        message_reader=_FakeReader(messages),
        wxbot_store=wxbot_store,
        social_policy_store=social_policy_store or _FakeSocialPolicyStore(),
        owners_scope_execution_allowed=scope_gate or _ScopeGate(),
        channel_registry=channel_registry,
        execution_owner_versions={
            "group_activity": "0.1.0",
            "wxbot": "0.2.0",
        },
    )

    async def verified_identity(_messages, **_kwargs):
        return {"wxid_bot"}

    if patch_identity:
        service._resolve_bot_wxids = verified_identity
    return service, fake_store, fake_agent, fake_outbound


@pytest.mark.asyncio
async def test_group_activity_due_session_scope_disable_blocks_work() -> None:
    gate = _ScopeGate(enabled=False)
    service, _, agent, outbound = _service(
        _messages(190 * 60, 200 * 60),
        scope_gate=gate,
    )

    result = await service.process_due_sessions()

    assert result["items"][0]["status"] == "skipped"
    assert result["items"][0]["reason"] == "scope_execution_denied"
    assert gate.calls == [
        (("group_activity", "wxbot"), "demo", "room@chatroom")
    ]
    assert service._message_reader.calls == 0
    assert agent.calls == []
    assert outbound.sent == []


@pytest.mark.asyncio
async def test_group_activity_missing_scope_gate_fails_closed() -> None:
    service, _, agent, outbound = _service(_messages(190 * 60, 200 * 60))
    service._owners_scope_execution_allowed = None

    decision = await service.process_session(_config(), dry_run=False)

    assert decision.status == "skipped"
    assert decision.reason == "scope_execution_denied"
    assert service._message_reader.calls == 0
    assert agent.calls == []
    assert outbound.sent == []


@pytest.mark.asyncio
async def test_group_activity_missing_execution_owner_version_fails_closed() -> None:
    gate = _ScopeGate()
    service, _, agent, outbound = _service(
        _messages(190 * 60, 200 * 60),
        scope_gate=gate,
    )
    service._execution_owner_versions["wxbot"] = ""

    decision = await service.process_session(_config(), dry_run=False)

    assert decision.status == "skipped"
    assert decision.reason == "scope_execution_denied"
    assert gate.calls == []
    assert agent.calls == []
    assert outbound.sent == []


@pytest.mark.asyncio
async def test_group_activity_revalidates_scope_after_generation_before_send() -> None:
    gate = _ScopeGate()
    agent = _BlockingAgent()
    service, store, _, outbound = _service(
        _messages(190 * 60, 200 * 60),
        agent=agent,
        scope_gate=gate,
    )

    processing = asyncio.create_task(
        service.process_session(_config(), dry_run=False)
    )
    await asyncio.wait_for(agent.started.wait(), timeout=1)
    gate.enabled = False
    agent.release.set()
    decision = await asyncio.wait_for(processing, timeout=1)

    assert decision.status == "skipped"
    assert decision.reason == "scope_execution_denied"
    assert gate.calls == [
        (("group_activity", "wxbot"), "demo", "room@chatroom"),
        (("group_activity", "wxbot"), "demo", "room@chatroom"),
        (("group_activity", "wxbot"), "demo", "room@chatroom"),
        (("group_activity", "wxbot"), "demo", "room@chatroom"),
        (("group_activity", "wxbot"), "demo", "room@chatroom"),
    ]
    assert outbound.sent == []
    assert store.marked[-1]["reason_code"] == "scope_execution_denied"


def test_group_activity_default_policy_is_conservative() -> None:
    config = GroupActivityStore(SimpleNamespace()).default_config(
        "demo",
        "room@chatroom",
    )

    assert config["quiet_start"] == "23:00"
    assert config["idle_minutes"] == 180
    assert config["quiet_end"] == "08:00"
    assert config["min_send_interval_minutes"] == 180
    assert config["max_per_day"] == 1
    assert config["topic_repeat_window_minutes"] == 1440


@pytest.mark.asyncio
async def test_group_activity_accepts_only_resolved_bot_identity_markers() -> None:
    service, _, _, _ = _service(_messages(190 * 60, 200 * 60))

    resolved = await GroupActivityService._resolve_bot_wxids(
        service,
        [
            {
                "identity_resolved": True,
                "self_wxid": "wxid_bot",
                "self_rowid": 17,
            }
        ],
        cfg=_config(),
        history_source="legacy_sdk",
    )

    assert resolved == {"wxid_bot"}


@pytest.mark.asyncio
async def test_group_activity_skips_when_group_not_idle_for_three_hours() -> None:
    service, _, _, outbound = _service(_messages(30 * 60, 70 * 60))

    decision = await service.process_session(_config(), dry_run=False)

    assert decision.status == "skipped"
    assert decision.reason == "group_not_idle"
    assert outbound.sent == []


@pytest.mark.asyncio
async def test_group_activity_dry_run_after_three_hours_idle() -> None:
    service, _, _, _ = _service(_messages(190 * 60, 200 * 60))

    decision = await service.process_session(_config(), dry_run=True)

    assert decision.status == "dry_run"
    assert decision.reason == "would_trigger"
    assert "群里已经约 180 分钟没人发言" in decision.prompt


@pytest.mark.asyncio
async def test_group_activity_uses_agent_skill_and_wxbot_queue() -> None:
    agent = _FakeAgent("刚才部署那个点要不要继续看看？")
    outbound = _FakeOutbound()
    service, store, _, _ = _service(
        _messages(190 * 60, 200 * 60),
        agent=agent,
        outbound=outbound,
    )

    decision = await service.process_session(
        _config(
            agent_tool_scope="group_plugin_status",
            llm_model_tier="tier-3",
            temperature=0.0,
        ),
        dry_run=False,
    )

    assert decision.status == "completed"
    assert agent.calls[0]["hints"] == {
        "agent_tool_scope": "group_plugin_status",
        "_llm_model_tier": "tier-3",
        "_llm_temperature": 0.0,
    }
    assert outbound.sent[0]["text"] == "刚才部署那个点要不要继续看看？"
    assert outbound.sent[0]["target"].session_kind == "group"
    assert outbound.sent[0]["options"].idempotency_key.startswith("group_activity:demo:room@chatroom:")
    assert outbound.sent[0]["options"].delivery_metadata["automated"] is True
    assert outbound.sent[0]["options"].delivery_metadata["ai_generated"] is True
    assert outbound.sent[0]["options"].delivery_metadata["identity_disclosed"] is False
    assert outbound.sent[0]["options"].delivery_metadata["speech_class"] == "scheduled"
    assert outbound.sent[0]["options"].delivery_metadata["speech_budget_enabled"] is True
    assert outbound.sent[0]["options"].delivery_metadata["humanization_stage"] == "proactive"
    delivery = outbound.sent[0]["options"].delivery_metadata
    assert delivery["participation_status"] == "may_reply"
    assert delivery["participation_score"] > 0
    assert delivery["requested_proactive"] is True
    assert delivery["source_message_id"] == "msg-1"
    assert delivery["send_revalidation_enabled"] is True
    assert delivery["deferred_candidate"] is False
    not_before = datetime.fromisoformat(delivery["not_before"])
    expires_at = datetime.fromisoformat(delivery["expires_at"])
    assert not_before < expires_at
    assert outbound.sent[0]["options"].source_message["message_id"] == "msg-1"
    assert store.completed[0]["reply_queue_id"] == 42
    assert "这是由 AI 助手执行" in agent.calls[0]["pre"].cleaned_text
    assert "不要说自己是 AI 或机器人" not in agent.calls[0]["pre"].cleaned_text


@pytest.mark.asyncio
async def test_group_activity_persists_auditable_proactive_decision_before_queue() -> None:
    policy_store = _FakeSocialPolicyStore()
    service, _, _, _ = _service(
        _messages(190 * 60, 200 * 60),
        social_policy_store=policy_store,
    )

    decision = await service.process_session(_config(), dry_run=False)

    assert decision.status == "completed"
    assert len(policy_store.events) == 1
    event = policy_store.events[0]
    assert event["event_kind"] == "runtime"
    assert event["runtime_stage"] == "decision"
    assert event["delivery_stage"] == "queue_requested"
    assert event["decision"].status.value == "may_reply"
    assert event["trace_id"]
    assert event["signal_summary"]["requested_proactive"] is True
    assert event["signal_summary"]["source_message_bound"] is True
    assert event["signal_summary"]["send_revalidation_enabled"] is True
    assert event["signal_summary"]["duplicate_guard_outcome"] == "topic_guard_passed"


@pytest.mark.asyncio
async def test_group_activity_skips_when_new_message_arrives_before_send() -> None:
    reader_messages = _messages(190 * 60, 200 * 60)
    service, store, _, outbound = _service(reader_messages)
    service._message_reader.messages = reader_messages

    async def still_not_idle(cfg, *, idle_minutes, bot_wxids):
        return False

    service._still_idle = still_not_idle

    decision = await service.process_session(_config(), dry_run=False)

    assert decision.status == "skipped"
    assert decision.reason == "new_message_before_send"
    assert outbound.sent == []
    assert store.marked[0]["status"] == "skipped"


@pytest.mark.asyncio
async def test_group_activity_ignores_its_own_recent_message_for_idle_check() -> None:
    messages = _messages(190 * 60, 200 * 60)
    messages.append(
        {
            "sender_wxid": "wxid_bot",
            "sender_name": "AI 助手",
            "text": "（AI 助手自动暖场）上个话题",
            "timestamp": "2026-04-30 10:59:00",
            "ts": int(time.time()) - 60,
        }
    )
    service, _, _, _ = _service(messages)

    decision = await service.process_session(_config(), dry_run=True)

    assert decision.status == "dry_run"
    assert decision.reason == "would_trigger"
    assert len(decision.messages) == 2
    assert all(item["sender_wxid"] != "wxid_bot" for item in decision.messages)


@pytest.mark.asyncio
async def test_group_activity_fails_closed_when_bot_identity_is_unknown() -> None:
    service, _, agent, outbound = _service(_messages(190 * 60, 200 * 60))
    service._settings = SimpleNamespace(
        wxbot_sdk_url="http://127.0.0.1:5080",
        wxbot_default_tenant_id="demo",
    )

    async def no_bot_identity(messages, **_kwargs):
        return None

    service._resolve_bot_wxids = no_bot_identity

    decision = await service.process_session(_config(), dry_run=False)

    assert decision.status == "skipped"
    assert decision.reason == "bot_identity_unavailable"
    assert decision.as_dict()["reason_code"] == "bot_identity_unavailable"
    assert agent.calls == []
    assert outbound.sent == []


@pytest.mark.asyncio
async def test_group_activity_quiet_hours_are_not_bypassed_by_force() -> None:
    service, _, agent, outbound = _service(_messages(190 * 60, 200 * 60))
    service._in_quiet_window = lambda cfg: True

    decision = await service.process_session(_config(), dry_run=False, force=True)

    assert decision.status == "skipped"
    assert decision.reason == "quiet_hours"
    assert agent.calls == []
    assert outbound.sent == []


@pytest.mark.asyncio
async def test_group_activity_uses_stable_cooldown_reason_code() -> None:
    store = _FakeStore()
    store.recent_exists = True
    service, _, agent, outbound = _service(
        _messages(190 * 60, 200 * 60),
        store=store,
    )

    decision = await service.process_session(_config(), dry_run=False)

    assert decision.status == "skipped"
    assert decision.reason == "cooldown_active"
    assert agent.calls == []
    assert outbound.sent == []


@pytest.mark.asyncio
async def test_group_activity_waits_for_human_response_before_another_warmup() -> None:
    messages = _messages(190 * 60, 200 * 60)
    store = _FakeStore()
    store.has_completed = True
    store.latest_completed = {
        "id": 7,
        "last_user_message_ts": max(int(item["ts"]) for item in messages),
    }
    service, _, agent, outbound = _service(messages, store=store)

    decision = await service.process_session(_config(), dry_run=False)

    assert decision.status == "skipped"
    assert decision.reason == "awaiting_human_response"
    assert agent.calls == []
    assert outbound.sent == []


@pytest.mark.asyncio
async def test_group_activity_skips_repeated_topic_inside_configured_window() -> None:
    store = _FakeStore()
    store.has_completed = True
    store.recent_texts = ["（AI 助手自动暖场）刚才部署那个点要不要继续看看？"]
    agent = _FakeAgent("刚才部署那个点要不要继续看看？")
    service, _, _, outbound = _service(
        _messages(190 * 60, 200 * 60),
        store=store,
        agent=agent,
    )

    decision = await service.process_session(
        _config(topic_repeat_window_minutes=720),
        dry_run=False,
    )

    assert decision.status == "skipped"
    assert decision.reason == "duplicate_topic"
    assert outbound.sent == []
    assert store.marked[0]["reason_code"] == "duplicate_topic"


@pytest.mark.asyncio
async def test_group_activity_discloses_identity_when_recent_context_asks() -> None:
    store = _FakeStore()
    store.has_completed = True
    messages = _messages(190 * 60, 200 * 60)
    messages[-1]["text"] = "你是真人吗？"
    service, _, _, outbound = _service(messages, store=store)

    decision = await service.process_session(_config(), dry_run=False)

    assert decision.status == "completed"
    assert outbound.sent[0]["text"].startswith("（AI 助手自动暖场）")
    assert outbound.sent[0]["options"].delivery_metadata["identity_disclosed"] is True


@pytest.mark.asyncio
async def test_group_activity_allows_truthful_ai_disclosure_without_duplicate_prefix() -> None:
    store = _FakeStore()
    store.has_completed = True
    agent = _FakeAgent("我是 AI 助手，刚才那个部署问题还需要继续看吗？")
    service, _, _, outbound = _service(
        _messages(190 * 60, 200 * 60),
        store=store,
        agent=agent,
    )

    decision = await service.process_session(_config(), dry_run=False)

    assert decision.status == "completed"
    assert outbound.sent[0]["text"] == "我是 AI 助手，刚才那个部署问题还需要继续看吗？"
    assert outbound.sent[0]["options"].delivery_metadata["identity_disclosed"] is False


@pytest.mark.asyncio
async def test_group_activity_does_not_repeat_identity_prefix_after_first_activity() -> None:
    store = _FakeStore()
    store.has_completed = True
    service, _, _, outbound = _service(
        _messages(190 * 60, 200 * 60),
        store=store,
    )

    decision = await service.process_session(_config(), dry_run=False)

    assert decision.status == "completed"
    assert outbound.sent[0]["text"] == "刚才那个部署日志有人继续看吗？"
    assert outbound.sent[0]["options"].delivery_metadata["identity_disclosed"] is False


@pytest.mark.asyncio
async def test_group_activity_voice_profile_guides_prompt_without_overriding_safety() -> None:
    policy_store = _FakeSocialPolicyStore(
        voice_profile=VoiceProfile(
            profile_id="room-natural",
            version=3,
            enabled=True,
            tone="轻松克制",
            phrase_preferences=["接着聊聊"],
        )
    )
    service, _, agent, outbound = _service(
        _messages(190 * 60, 200 * 60),
        social_policy_store=policy_store,
    )

    decision = await service.process_session(_config(), dry_run=False)

    assert decision.status == "completed"
    assert "语气 轻松克制" in agent.calls[0]["pre"].cleaned_text
    assert "接着聊聊" in agent.calls[0]["pre"].cleaned_text
    assert "不得覆盖安全、隐私、身份透明或事实约束" in agent.calls[0]["pre"].cleaned_text
    assert "不得生成或猜测付款、授权、身份核验" in agent.calls[0]["pre"].cleaned_text
    assert outbound.sent[0]["options"].delivery_metadata["voice_profile"]["version"] == 3
    assert (
        "authorized_sample_session_ids"
        not in outbound.sent[0]["options"].delivery_metadata["voice_profile"]
    )
    assert (
        "authorization_reference"
        not in outbound.sent[0]["options"].delivery_metadata["voice_profile"]
    )
    assert (
        outbound.sent[0]["options"].delivery_metadata["voice_profile_reason"]
        == "voice_profile_active"
    )
    assert len(policy_store.events) == 1
    assert policy_store.events[0]["signal_summary"]["speech_class"] == "scheduled"


@pytest.mark.asyncio
async def test_group_activity_voice_profile_always_discloses_identity() -> None:
    policy_store = _FakeSocialPolicyStore(
        voice_profile=VoiceProfile(
            enabled=True,
            identity_disclosure="always",
        )
    )
    service, _, agent, outbound = _service(
        _messages(190 * 60, 200 * 60),
        social_policy_store=policy_store,
    )

    decision = await service.process_session(_config(), dry_run=False)

    assert decision.status == "completed"
    assert "这次需要自然地明确 AI 助手身份" in agent.calls[0]["pre"].cleaned_text
    assert outbound.sent[0]["text"].startswith("（AI 助手自动暖场）")
    assert outbound.sent[0]["options"].delivery_metadata["identity_disclosed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("profile", "reason"),
    [
        (
            VoiceProfile(
                enabled=True,
                expires_at=datetime.now(UTC),
                tone="不得应用的过期语气",
            ),
            "voice_profile_expired",
        ),
        (
            VoiceProfile(
                enabled=True,
                sample_source="authorized_group_samples",
                sample_scope="current_group",
                authorized_sample_session_ids=["other@chatroom"],
                authorization_reference="approval-secret",
                tone="不得应用的越群语气",
            ),
            "voice_profile_sample_scope_invalid",
        ),
    ],
)
async def test_group_activity_voice_profile_fails_closed_outside_runtime_scope(
    profile: VoiceProfile,
    reason: str,
) -> None:
    service, _, agent, outbound = _service(
        _messages(190 * 60, 200 * 60),
        social_policy_store=_FakeSocialPolicyStore(voice_profile=profile),
    )

    decision = await service.process_session(_config(), dry_run=False)

    assert decision.status == "completed"
    assert decision.voice_profile_reason == reason
    assert "不得应用" not in decision.prompt
    assert "不得应用" not in agent.calls[0]["pre"].cleaned_text
    delivery = outbound.sent[0]["options"].delivery_metadata
    assert delivery["voice_profile"] == {}
    assert delivery["voice_profile_reason"] == reason
    assert decision.as_dict()["voice_profile_reason"] == reason


@pytest.mark.asyncio
async def test_group_activity_social_kill_switch_cannot_be_bypassed_by_force() -> None:
    service, _, agent, outbound = _service(
        _messages(190 * 60, 200 * 60),
        social_policy_store=_FakeSocialPolicyStore(effective_enabled=False),
    )

    decision = await service.process_session(
        _config(),
        dry_run=False,
        force=True,
    )

    assert decision.status == "skipped"
    assert decision.reason == "social_participation_disabled"
    assert agent.calls == []
    assert outbound.sent == []


@pytest.mark.asyncio
async def test_group_activity_social_silence_budget_cannot_be_bypassed_by_force() -> None:
    service, _, agent, outbound = _service(_messages(30 * 60))

    decision = await service.process_session(
        _config(),
        dry_run=False,
        force=True,
    )

    assert decision.status == "skipped"
    assert decision.reason == "proactive_group_not_silent_long_enough"
    assert agent.calls == []
    assert outbound.sent == []


@pytest.mark.asyncio
async def test_group_activity_social_daily_budget_cannot_be_bypassed_by_force() -> None:
    store = _FakeStore()
    store.completed_today = 1
    service, _, agent, outbound = _service(
        _messages(190 * 60),
        store=store,
    )

    decision = await service.process_session(
        _config(),
        dry_run=False,
        force=True,
    )

    assert decision.status == "skipped"
    assert decision.reason == "proactive_daily_budget_exhausted"
    assert agent.calls == []
    assert outbound.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        "我是真人，刚才那个部署问题还聊吗？",
        "我不是机器人，刚才那个部署问题还聊吗？",
        "已经转接人工，刚才那个部署问题还聊吗？",
    ],
)
async def test_group_activity_blocks_deceptive_identity_claims(reply: str) -> None:
    store = _FakeStore()
    store.has_completed = True
    agent = _FakeAgent(reply)
    service, _, _, outbound = _service(
        _messages(190 * 60, 200 * 60),
        store=store,
        agent=agent,
    )

    decision = await service.process_session(_config(), dry_run=False)

    assert decision.status == "skipped"
    assert decision.reason == "generation_identity_deception"
    assert outbound.sent == []


@pytest.mark.asyncio
async def test_group_activity_generation_failure_is_closed_and_audited() -> None:
    class _FailingAgent(_FakeAgent):
        async def answer(self, pre, session, hints=None):
            raise RuntimeError("model unavailable")

    store = _FakeStore()
    service, _, _, outbound = _service(
        _messages(190 * 60, 200 * 60),
        store=store,
        agent=_FailingAgent(),
    )

    decision = await service.process_session(_config(), dry_run=False)

    assert decision.status == "failed"
    assert decision.reason == "internal_error"
    assert outbound.sent == []
    assert store.marked[0]["status"] == "failed"
    assert store.marked[0]["reason_code"] == "internal_error"


@pytest.mark.asyncio
async def test_managed_observations_preserve_full_identity_without_legacy_sdk() -> None:
    gate = _ScopeGate()
    agent = _FakeAgent("刚才那个部署问题要不要继续看看？")
    outbound = _FakeOutbound()
    registry = _FakeChannelRegistry(outbound)
    observations = _FakeObservationStore(_observations(190 * 60, 200 * 60))
    service, store, _, _ = _service(
        [],
        agent=agent,
        outbound=outbound,
        scope_gate=gate,
        wxbot_store=observations,
        channel_registry=registry,
        patch_identity=False,
    )
    config = _config(
        session_id=_MANAGED_SESSION_ID,
        connection_id=_MANAGED_CONNECTION_ID,
        external_session_id=_MANAGED_EXTERNAL_SESSION_ID,
    )

    decision = await service.process_session(config, dry_run=False)

    assert decision.status == "completed"
    assert service._message_reader.calls == 0
    assert len(observations.calls) == 2
    generated_session = agent.calls[0]["session"]
    assert generated_session.adapter_id == "wechat-sdk"
    assert generated_session.connection_id == _MANAGED_CONNECTION_ID
    assert generated_session.external_conversation_id == _MANAGED_EXTERNAL_SESSION_ID
    assert generated_session.canonical_conversation_id == _MANAGED_SESSION_ID
    target = outbound.sent[0]["target"]
    assert target.adapter_id == "wechat-sdk"
    assert target.connection_id == _MANAGED_CONNECTION_ID
    assert target.external_conversation_id == _MANAGED_EXTERNAL_SESSION_ID
    assert target.canonical_conversation_id == _MANAGED_SESSION_ID
    options = outbound.sent[0]["options"]
    assert options.source_message["message_id"] == "canonical-msg-1"
    assert options.source_message["msg_svr_id"] == "external-msg-1"
    assert options.delivery_metadata["execution_owners"] == [
        "group_activity",
        "wxbot",
    ]
    assert options.delivery_metadata["execution_owner_versions"] == {
        "group_activity": "0.1.0",
        "wxbot": "0.2.0",
    }
    assert options.delivery_metadata["execution_tenant_id"] == "demo"
    assert options.delivery_metadata["execution_session_id"] == _MANAGED_SESSION_ID
    assert store.completed[0]["reply_queue_id"] == 42
    assert len(gate.calls) == 9
    assert all(call[0] == ("group_activity", "wxbot") for call in gate.calls)


@pytest.mark.asyncio
async def test_managed_connection_never_falls_back_to_raw_sdk_history() -> None:
    service, _, agent, outbound = _service([], patch_identity=False)
    config = _config(
        session_id=_MANAGED_SESSION_ID,
        connection_id=_MANAGED_CONNECTION_ID,
        external_session_id=_MANAGED_EXTERNAL_SESSION_ID,
    )

    decision = await service.process_session(config, dry_run=False)

    assert decision.status == "skipped"
    assert decision.reason == "connection_scoped_history_unavailable"
    assert service._message_reader.calls == 0
    assert agent.calls == []
    assert outbound.sent == []


@pytest.mark.asyncio
async def test_conflicting_managed_identity_fails_before_history_or_llm() -> None:
    observations = _FakeObservationStore(_observations(190 * 60, 200 * 60))
    service, _, agent, outbound = _service(
        [],
        wxbot_store=observations,
        patch_identity=False,
    )

    decision = await service.process_session(
        _config(
            session_id=_MANAGED_SESSION_ID,
            connection_id=_MANAGED_CONNECTION_ID,
            external_session_id="different-room@chatroom",
        ),
        dry_run=False,
    )

    assert decision.status == "skipped"
    assert decision.reason == "channel_identity_mismatch"
    assert observations.calls == []
    assert agent.calls == []
    assert outbound.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("external_session_id", "reason"),
    [
        ("", "external_session_identity_unavailable"),
        (_MANAGED_SESSION_ID, "channel_identity_mismatch"),
    ],
)
async def test_managed_identity_never_synthesizes_external_from_canonical_session(
    external_session_id: str,
    reason: str,
) -> None:
    observations = _FakeObservationStore(_observations(190 * 60, 200 * 60))
    service, _, agent, outbound = _service(
        [],
        wxbot_store=observations,
        patch_identity=False,
    )

    decision = await service.process_session(
        _config(
            session_id=_MANAGED_SESSION_ID,
            connection_id=_MANAGED_CONNECTION_ID,
            external_session_id=external_session_id,
        ),
        dry_run=False,
    )

    assert decision.status == "skipped"
    assert decision.reason == reason
    assert observations.calls == []
    assert agent.calls == []
    assert outbound.sent == []


@pytest.mark.asyncio
async def test_legacy_connection_rejects_managed_canonical_session_id() -> None:
    service, _, agent, outbound = _service([], patch_identity=False)

    decision = await service.process_session(
        _config(
            session_id=_MANAGED_SESSION_ID,
            connection_id="legacy-wechat-default",
            external_session_id=_MANAGED_SESSION_ID,
        ),
        dry_run=False,
    )

    assert decision.status == "skipped"
    assert decision.reason == "channel_identity_mismatch"
    assert service._message_reader.calls == 0
    assert agent.calls == []
    assert outbound.sent == []


@pytest.mark.asyncio
async def test_non_default_tenant_cannot_use_legacy_sdk_history() -> None:
    service, _, agent, outbound = _service([], patch_identity=False)

    decision = await service.process_session(
        _config(tenant_id="other-tenant"),
        dry_run=False,
    )

    assert decision.status == "skipped"
    assert decision.reason == "legacy_wxbot_history_tenant_unavailable"
    assert service._message_reader.calls == 0
    assert agent.calls == []
    assert outbound.sent == []


@pytest.mark.asyncio
async def test_each_legacy_sdk_query_has_fresh_pre_and_post_owner_gates() -> None:
    gate = _ScopeGate()
    service, _, _, _ = _service([], scope_gate=gate, patch_identity=False)
    query_reader = _FakeQueryReader()
    service._message_reader = query_reader

    history = await service._load_messages(_config(), idle_minutes=180)

    assert history.source == "legacy_sdk"
    assert [item["text"] for item in history.messages] == ["hello"]
    assert query_reader.calls == 2
    assert len(gate.calls) == 4
    assert all(call[0] == ("group_activity", "wxbot") for call in gate.calls)


def test_managed_target_cannot_use_legacy_outbound_fallback() -> None:
    service, _, _, _ = _service([])
    target = ChannelTarget(
        tenant_id="demo",
        channel="wechat",
        session_id=_MANAGED_SESSION_ID,
        adapter_id="wechat-sdk",
        connection_id=_MANAGED_CONNECTION_ID,
        external_conversation_id=_MANAGED_EXTERNAL_SESSION_ID,
        canonical_conversation_id=_MANAGED_SESSION_ID,
    )

    with pytest.raises(RuntimeError, match="connection_scoped_outbound_unavailable"):
        service._outbound_for_target(target)


@pytest.mark.asyncio
async def test_legacy_status_lookup_has_fresh_pre_and_post_owner_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plugins.group_activity.service as module

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "identity": {
                    "ready": True,
                    "self_wxid": "wxid_bot",
                    "self_rowid": 7,
                }
            }

    async def request(*_args, **_kwargs):
        return _Response()

    monkeypatch.setattr(module, "safe_trusted_service_request", request)
    gate = _ScopeGate()
    service, _, _, _ = _service([], scope_gate=gate, patch_identity=False)

    resolved = await service._resolve_bot_wxids(
        [],
        cfg=_config(),
        history_source="legacy_sdk",
    )

    assert resolved == {"wxid_bot"}
    assert len(gate.calls) == 2


@pytest.mark.asyncio
async def test_scope_is_revalidated_after_outbound_call() -> None:
    gate = _ScopeGate()
    outbound = _BlockingOutbound()
    registry = _FakeChannelRegistry(outbound)
    observations = _FakeObservationStore(_observations(190 * 60, 200 * 60))
    service, store, _, _ = _service(
        [],
        outbound=outbound,
        scope_gate=gate,
        wxbot_store=observations,
        channel_registry=registry,
        patch_identity=False,
    )
    processing = asyncio.create_task(
        service.process_session(
            _config(
                session_id=_MANAGED_SESSION_ID,
                connection_id=_MANAGED_CONNECTION_ID,
                external_session_id=_MANAGED_EXTERNAL_SESSION_ID,
            ),
            dry_run=False,
        )
    )
    await asyncio.wait_for(outbound.started.wait(), timeout=1)
    gate.enabled = False
    outbound.release.set()

    decision = await asyncio.wait_for(processing, timeout=1)

    assert decision.status == "skipped"
    assert decision.reason == "scope_execution_denied"
    assert store.completed == []
    assert store.marked[-1]["reason_code"] == "scope_execution_denied"
    assert len(gate.calls) == 9


@pytest.mark.asyncio
async def test_cancellation_marks_started_event_before_propagating() -> None:
    agent = _BlockingAgent()
    gate = _ScopeGate()
    service, store, _, outbound = _service(
        _messages(190 * 60, 200 * 60),
        agent=agent,
        scope_gate=gate,
    )
    processing = asyncio.create_task(
        service.process_session(_config(), dry_run=False)
    )
    await asyncio.wait_for(agent.started.wait(), timeout=1)

    processing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await processing

    assert store.marked[-1]["status"] == "failed"
    assert store.marked[-1]["reason_code"] == "cancelled"
    assert outbound.sent == []
    assert len(gate.calls) == 5


@pytest.mark.asyncio
async def test_due_batch_has_two_worker_bound_and_preserves_result_order() -> None:
    store = _FakeStore()
    store.configs = [
        _config(
            session_id=f"room-{index}@chatroom",
            external_session_id=f"room-{index}@chatroom",
        )
        for index in range(5)
    ]
    service, _, _, _ = _service([], store=store)
    active = 0
    maximum_active = 0
    two_started = asyncio.Event()
    release = asyncio.Event()

    async def process(config, **_kwargs):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        if active == 2:
            two_started.set()
        await release.wait()
        active -= 1
        return GroupActivityDecision("completed", "queued", config, [])

    service.process_session = process
    batch = asyncio.create_task(service.process_due_sessions())
    await asyncio.wait_for(two_started.wait(), timeout=1)
    assert maximum_active == 2
    release.set()

    result = await asyncio.wait_for(batch, timeout=1)

    assert result["processed"] == 5
    assert [item["config"]["session_id"] for item in result["items"]] == [
        f"room-{index}@chatroom" for index in range(5)
    ]


@pytest.mark.asyncio
async def test_due_batch_cancellation_cancels_and_awaits_active_workers() -> None:
    store = _FakeStore()
    store.configs = [
        _config(
            session_id=f"room-{index}@chatroom",
            external_session_id=f"room-{index}@chatroom",
        )
        for index in range(4)
    ]
    service, _, _, _ = _service([], store=store)
    active = 0
    cancelled = 0
    two_started = asyncio.Event()

    async def process(config, **_kwargs):
        nonlocal active, cancelled
        active += 1
        if active == 2:
            two_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled += 1
            raise
        finally:
            active -= 1

    service.process_session = process
    batch = asyncio.create_task(service.process_due_sessions())
    await asyncio.wait_for(two_started.wait(), timeout=1)
    batch.cancel()

    with pytest.raises(asyncio.CancelledError):
        await batch

    assert active == 0
    assert cancelled == 2
