from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.agent.scopes import MESSAGE_EXPORT_SCOPE
from app.channel.identity import (
    LEGACY_WXBOT_CONNECTION_ID,
    canonical_conversation_id,
    canonical_message_id,
)
from app.common.config import Settings
from app.common.intent import IntentDecision, IntentDomain
from app.common.intent_runtime import persist_decision
from app.common.types import Channel, InboundEvent, Message, Role, Session, Turn
from app.orchestrator.effects import EFFECT_STATUS_DUPLICATE, InMemoryEffectCommitter
from app.orchestrator.flow import MessageEffect
from app.orchestrator.pipeline import PipelineContext
from app.social.contracts import (
    GroupParticipationPolicyDocument,
    KillSwitches,
    ParticipationPolicyValues,
)
from plugins.wxbot.agent_tools import (
    WxbotAgentToolService,
    build_wxbot_file_analysis_agent_tools,
    build_wxbot_message_export_agent_tools,
)


class _FakeStore:
    def __init__(
        self,
        *,
        observations: list[dict[str, Any]] | None = None,
    ) -> None:
        self.enqueued: list[dict[str, Any]] = []
        self.active_file_path_reads = 0
        self.observations = list(observations or [])
        self.observation_period_calls: list[dict[str, Any]] = []

    async def enqueue_reply(self, **kwargs: Any) -> int:
        self.enqueued.append(dict(kwargs))
        return len(self.enqueued)

    async def list_active_outbound_file_paths(self) -> list[str]:
        self.active_file_path_reads += 1
        return []

    async def list_group_observations_for_period(
        self,
        tenant_id: str,
        session_id: str,
        *,
        start_occurred_ts: int,
        end_occurred_ts: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        self.observation_period_calls.append(
            {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "start_occurred_ts": start_occurred_ts,
                "end_occurred_ts": end_occurred_ts,
                "limit": limit,
            }
        )
        return list(self.observations[:limit])


class _FakeReportService:
    def __init__(
        self,
        *,
        report: str = "大家主要讨论了项目排期。",
        messages: list[dict[str, Any]] | None = None,
        messages_by_date: dict[str, list[dict[str, Any]]] | None = None,
        period: str = "2026-07-29",
        message_error: str = "",
    ) -> None:
        self.report = report
        self.messages = messages or [
            {
                "timestamp": "2026-07-29 09:30:00",
                "sender_wxid": "wxid_a",
                "sender_name": "张三",
                "msg_type": "text",
                "text": "今天确认项目排期。",
            }
        ]
        self.messages_by_date = {
            str(key): list(value) for key, value in (messages_by_date or {}).items()
        }
        self.period = period
        self.message_error = message_error
        self.message_calls: list[dict[str, Any]] = []

    async def fetch_report_messages_payload(
        self,
        session_id: str,
        *,
        session_name: str,
        report_type: str,
        date: str = "",
        year_month: str = "",
    ) -> dict[str, Any]:
        self.message_calls.append(
            {
                "session_id": session_id,
                "session_name": session_name,
                "report_type": report_type,
                "date": date,
                "year_month": year_month,
            }
        )
        if self.message_error:
            return {"ok": False, "error": self.message_error}
        return {
            "ok": True,
            "period": self.period,
            "messages": list(self.messages_by_date.get(date, self.messages)),
        }


class _FakeSocialPolicyStore:
    def __init__(self, *, file_send_enabled: bool = True) -> None:
        self.file_send_enabled = file_send_enabled

    async def get_group_policy(
        self,
        tenant_id: str,
        session_id: str,
    ) -> GroupParticipationPolicyDocument:
        return GroupParticipationPolicyDocument(
            tenant_id=tenant_id,
            session_id=session_id,
            version=1,
            kill_switches=KillSwitches(),
            effective_enabled=True,
            policy=ParticipationPolicyValues(
                file_send_enabled=self.file_send_enabled,
            ),
        )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        customer_service_prompt_enabled=False,
        wxbot_default_tenant_id="demo",
        wxbot_outbound_file_dir=str(tmp_path),
        wxbot_outbound_file_max_bytes=1024 * 1024,
    )


def _persist_file_intent(session: Session, action: str, **slots: object) -> Session:
    persist_decision(
        IntentDecision(
            domain=IntentDomain.FILE,
            action=action,
            confidence=0.95,
            slots=dict(slots),
        ),
        session=session,
    )
    return session


def _session(
    *,
    external_session_id: str,
    session_kind: str,
    session_name: str,
    source_message_id: str,
) -> Session:
    contract: dict[str, Any] = {
        "participation_status": "must_reply",
        "source_message_id": source_message_id,
        "participation_policy_version": 17,
        "send_revalidation_enabled": True,
    }
    return Session(
        session_id="canonical-session",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
        adapter_id="wechat-sdk",
        connection_id=LEGACY_WXBOT_CONNECTION_ID,
        external_conversation_id=external_session_id,
        canonical_conversation_id="canonical-session",
        metadata={
            "session_name": session_name,
            "session_kind": session_kind,
            "_wxbot_delivery_contract": dict(contract),
        },
        turns=[
            Turn(
                session_id="canonical-session",
                role=Role.USER,
                content="汇总今天的消息，发文件给我",
                metadata={
                    "session_name": session_name,
                    "session_kind": session_kind,
                    "sender_name": "当前用户",
                    "sender_wxid": "wxid_current",
                    "msg_svr_id": source_message_id,
                    "_wxbot_delivery_contract": dict(contract),
                },
            )
        ],
    )


def _service(
    tmp_path: Path,
    *,
    report_service: _FakeReportService,
    store: _FakeStore | None = None,
    effect_reply_enabled: bool = True,
    file_send_enabled: bool = True,
) -> tuple[WxbotAgentToolService, _FakeStore]:
    resolved_store = store or _FakeStore()
    return (
        WxbotAgentToolService(
            _settings(tmp_path),
            wxbot_store=resolved_store,  # type: ignore[arg-type]
            report_service=report_service,  # type: ignore[arg-type]
            effect_reply_enabled=effect_reply_enabled,
            message_export_root=tmp_path,
            message_export_max_bytes=1024 * 1024,
            social_policy_store=_FakeSocialPolicyStore(
                file_send_enabled=file_send_enabled,
            ),
        ),
        resolved_store,
    )


def test_message_export_definition_is_current_session_only_and_group_switch_gated() -> None:
    service, _store = _service(
        Path("C:/tmp/wxbot-export-definition"),
        report_service=_FakeReportService(),
    )

    tools = build_wxbot_message_export_agent_tools(service)

    assert len(tools) == 1
    tool = tools[0]
    assert tool.scope == MESSAGE_EXPORT_SCOPE
    assert tool.name == "export_current_messages_file"
    assert tool.metadata["channels"] == ["wechat"]
    assert tool.metadata["session_kinds"] == ["group", "private"]
    assert "required_group_role" not in tool.metadata
    assert tool.metadata["requires_group_file_send"] is True
    properties = tool.parameters["properties"]
    assert set(properties) == {"report_type", "minutes", "date", "year_month", "format"}
    assert properties["report_type"]["enum"] == ["recent", "daily", "monthly"]
    assert properties["minutes"]["minimum"] == 1
    assert properties["minutes"]["maximum"] == 1440
    assert "session_id" not in properties
    assert "target" not in properties


def test_file_delivery_tools_require_group_master_switch_without_admin_role() -> None:
    service, _store = _service(
        Path("C:/tmp/wxbot-file-definition"),
        report_service=_FakeReportService(),
    )

    tools = {tool.name: tool for tool in build_wxbot_file_analysis_agent_tools(service)}

    assert "required_group_role" not in tools["inspect_current_file"].metadata
    for name in ("convert_current_file", "generate_text_file"):
        assert "required_group_role" not in tools[name].metadata
        assert tools[name].metadata["requires_group_file_send"] is True


@pytest.mark.asyncio
async def test_group_daily_export_binds_effects_to_current_external_session(
    tmp_path: Path,
) -> None:
    report_service = _FakeReportService(period="2026-07-29")
    service, store = _service(tmp_path, report_service=report_service)
    session = _session(
        external_session_id="room@chatroom",
        session_kind="group",
        session_name="测试群",
        source_message_id="msg-group-1",
    )

    result = await service.export_current_messages_file(
        session,
        {"report_type": "daily", "date": "2026-07-29", "format": "txt"},
    )

    assert store.enqueued == []
    assert result["self_enqueued_reply"] is True
    assert result["suppress_final_reply"] is True
    assert result["message_count"] == 1
    assert report_service.message_calls[0]["session_id"] == "room@chatroom"
    assert report_service.message_calls[0]["date"] == "2026-07-29"

    effects = result["channel_reply_effects"]
    assert len(effects) == 2
    assert [effect["type"] for effect in effects] == [
        "enqueue_channel_reply",
        "enqueue_channel_reply",
    ]
    assert [effect["payload"]["session_id"] for effect in effects] == [
        "room@chatroom",
        "room@chatroom",
    ]
    assert all(
        effect["payload"]["external_conversation_id"] == "room@chatroom" for effect in effects
    )
    assert effects[0]["payload"]["body"]["text"].endswith("共 1 条，文件已排队发送。")
    assert effects[0]["payload"]["delivery"]["source_message_id"] == "msg-group-1"
    assert effects[0]["payload"]["delivery"]["participation_policy_version"] == 17
    assert (
        effects[0]["payload"]["source_message"]["_wxbot_delivery_contract"]["source_message_id"]
        == "msg-group-1"
    )
    file_payload = effects[1]["payload"]["file"]
    export_path = Path(file_payload["file_path"])
    assert export_path.is_file()
    assert file_payload["file_name"].endswith(".txt")
    assert export_path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert "二、原始消息记录" in export_path.read_text(encoding="utf-8-sig")


@pytest.mark.asyncio
async def test_legacy_group_recent_export_uses_user_window_and_filters_daily_payload(
    tmp_path: Path,
) -> None:
    report_service = _FakeReportService(
        messages=[
            {
                "timestamp": "2026-07-29 09:49:59",
                "sender_wxid": "wxid_old",
                "sender_name": "旧消息",
                "msg_type": "text",
                "text": "范围外旧消息",
            },
            {
                "timestamp": "2026-07-29 09:50:00",
                "sender_wxid": "wxid_a",
                "sender_name": "张三",
                "msg_type": "text",
                "text": "范围起点消息",
            },
            {
                "timestamp": "2026-07-29 09:59:59",
                "sender_wxid": "wxid_b",
                "sender_name": "李四",
                "msg_type": "text",
                "text": "范围内最新消息",
            },
            {
                "timestamp": "2026-07-29 10:00:00",
                "sender_wxid": "wxid_command",
                "sender_name": "当前用户",
                "msg_type": "text",
                "text": "导出命令本身",
            },
        ],
    )
    service, store = _service(tmp_path, report_service=report_service)
    session = _session(
        external_session_id="room@chatroom",
        session_kind="group",
        session_name="测试群",
        source_message_id="msg-recent-legacy",
    )
    session.turns[0].content = "整理十分钟群里话题 以文件方式发给我"
    _persist_file_intent(session, "export_history", recent_minutes=10)
    session.turns[0].created_at = datetime(2026, 7, 29, 1, 0, tzinfo=UTC)
    session.turns[0].metadata["occurred_ts"] = int(
        datetime(2026, 7, 29, 2, 0, tzinfo=UTC).timestamp()
    )

    result = await service.export_current_messages_file(
        session,
        {"report_type": "daily", "date": "2026-07-29", "format": "txt"},
    )

    assert store.enqueued == []
    assert result["report_type"] == "recent"
    assert result["minutes"] == 10
    assert result["message_count"] == 2
    assert result["period"] == ("最近 10 分钟（2026-07-29 09:50:00 至 2026-07-29 10:00:00）")
    assert report_service.message_calls == [
        {
            "session_id": "room@chatroom",
            "session_name": "测试群",
            "report_type": "daily",
            "date": "2026-07-29",
            "year_month": "",
        }
    ]
    export_path = Path(result["channel_reply_effects"][1]["payload"]["file"]["file_path"])
    exported = export_path.read_text(encoding="utf-8-sig")
    assert "范围起点消息" in exported
    assert "范围内最新消息" in exported
    assert "范围外旧消息" not in exported
    assert "导出命令本身" not in exported


@pytest.mark.asyncio
async def test_legacy_group_recent_export_fetches_both_dates_across_midnight(
    tmp_path: Path,
) -> None:
    report_service = _FakeReportService(
        messages_by_date={
            "2026-07-29": [
                {
                    "timestamp": "2026-07-29 23:56:00",
                    "sender_wxid": "wxid_a",
                    "sender_name": "张三",
                    "msg_type": "text",
                    "text": "午夜前消息",
                }
            ],
            "2026-07-30": [
                {
                    "timestamp": "2026-07-30 00:04:00",
                    "sender_wxid": "wxid_b",
                    "sender_name": "李四",
                    "msg_type": "text",
                    "text": "午夜后消息",
                }
            ],
        }
    )
    service, _store = _service(tmp_path, report_service=report_service)
    session = _session(
        external_session_id="room@chatroom",
        session_kind="group",
        session_name="测试群",
        source_message_id="msg-recent-midnight",
    )
    session.turns[0].content = "整理十分钟群里话题，以文件方式发给我"
    _persist_file_intent(session, "export_history", recent_minutes=10)
    session.turns[0].created_at = datetime(2026, 7, 29, 16, 5, tzinfo=UTC)

    result = await service.export_current_messages_file(
        session,
        {"report_type": "recent", "minutes": 10},
    )

    assert [item["date"] for item in report_service.message_calls] == [
        "2026-07-29",
        "2026-07-30",
    ]
    assert result["message_count"] == 2
    export_path = Path(result["channel_reply_effects"][1]["payload"]["file"]["file_path"])
    exported = export_path.read_text(encoding="utf-8-sig")
    assert "午夜前消息" in exported
    assert "午夜后消息" in exported


@pytest.mark.asyncio
async def test_legacy_group_recent_export_fails_closed_on_missing_timestamp(
    tmp_path: Path,
) -> None:
    report_service = _FakeReportService(
        messages=[
            {
                "timestamp": "not-a-time",
                "sender_wxid": "wxid_a",
                "sender_name": "张三",
                "msg_type": "text",
                "text": "无法证明是否在范围内",
            }
        ]
    )
    service, store = _service(tmp_path, report_service=report_service)
    session = _session(
        external_session_id="room@chatroom",
        session_kind="group",
        session_name="测试群",
        source_message_id="msg-recent-invalid-ts",
    )
    session.turns[0].content = "整理十分钟群里话题，以文件方式发给我"
    _persist_file_intent(session, "export_history", recent_minutes=10)
    session.turns[0].created_at = datetime(2026, 7, 29, 2, 0, tzinfo=UTC)

    with pytest.raises(RuntimeError, match="valid timestamp"):
        await service.export_current_messages_file(
            session,
            {"report_type": "recent", "minutes": 10},
        )

    assert store.enqueued == []
    assert list(tmp_path.rglob("*.txt")) == []


@pytest.mark.asyncio
async def test_group_export_is_denied_when_group_file_switch_is_closed(
    tmp_path: Path,
) -> None:
    report_service = _FakeReportService(period="2026-07-29")
    service, store = _service(
        tmp_path,
        report_service=report_service,
        file_send_enabled=False,
    )
    session = _session(
        external_session_id="room@chatroom",
        session_kind="group",
        session_name="测试群",
        source_message_id="msg-group-disabled",
    )

    with pytest.raises(RuntimeError, match="group_file_send_disabled"):
        await service.export_current_messages_file(
            session,
            {"report_type": "daily", "date": "2026-07-29", "format": "txt"},
        )

    assert report_service.message_calls == []
    assert store.enqueued == []


@pytest.mark.asyncio
async def test_generate_text_file_requires_explicit_file_request_and_queues_artifact(
    tmp_path: Path,
) -> None:
    service, store = _service(
        tmp_path,
        report_service=_FakeReportService(),
        effect_reply_enabled=True,
    )
    session = _session(
        external_session_id="wxid_friend",
        session_kind="private",
        session_name="好友",
        source_message_id="msg-generate-1",
    )
    session.turns[0].content = "把上面的内容整理成 md 文件发我"
    _persist_file_intent(
        session,
        "generate",
        format="md",
        delivery_required=True,
    )

    result = await service.generate_text_file(
        session,
        {"content": "# 已整理\n\n这是文件正文。", "format": "md"},
    )

    assert result["ok"] is True
    assert result["delivery_status"] == "queued"
    assert store.enqueued == []
    assert len(result["channel_reply_effects"]) == 1
    file_payload = result["channel_reply_effects"][0]["payload"]["file"]
    assert file_payload["file_name"].endswith(".md")
    assert "# 已整理" in Path(file_payload["file_path"]).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_generated_file_retry_keeps_one_delivery_identity(tmp_path: Path) -> None:
    service, _store = _service(
        tmp_path,
        report_service=_FakeReportService(),
        effect_reply_enabled=True,
    )
    session = _session(
        external_session_id="wxid_friend",
        session_kind="private",
        session_name="好友",
        source_message_id="msg-generate-retry-1",
    )
    session.turns[0].content = "把热点新闻整理成文件发我"
    _persist_file_intent(session, "generate", delivery_required=True)

    first = await service.generate_text_file(
        session,
        {
            "content": "第一次模型措辞",
            "format": "txt",
            "file_name": "第一次名称",
        },
    )
    second = await service.generate_text_file(
        session,
        {
            "content": "重试后的不同措辞",
            "format": "md",
            "file_name": "第二次名称",
        },
    )

    first_effect = first["channel_reply_effects"][0]
    second_effect = second["channel_reply_effects"][0]
    assert first_effect == second_effect
    assert first_effect["idempotency_key"] == second_effect["idempotency_key"]
    assert first_effect["payload"]["command_id"] == second_effect["payload"]["command_id"]
    assert (
        first_effect["payload"]["file"]["file_path"]
        == (second_effect["payload"]["file"]["file_path"])
    )
    assert first_effect["payload"]["file"]["file_name"] == "第一次名称.txt"
    artifact_path = Path(first_effect["payload"]["file"]["file_path"])
    assert artifact_path.read_text(encoding="utf-8") == "第一次模型措辞"

    event = InboundEvent(
        message_id="msg-generate-retry-1",
        tenant_id="demo",
        channel=Channel.WECHAT,
        user_id="wxid_friend",
        session_id="wxid_friend",
        message=Message(content=session.turns[0].content),
        trace_id="trace-generate-retry-1",
    )
    ctx = PipelineContext(event=event, trace_id=event.trace_id)
    committer = InMemoryEffectCommitter()
    await committer.commit(MessageEffect(**first_effect), ctx)
    duplicate = await committer.commit(MessageEffect(**second_effect), ctx)
    assert duplicate.status == EFFECT_STATUS_DUPLICATE


@pytest.mark.asyncio
async def test_generated_file_fallback_identity_uses_stable_trace(tmp_path: Path) -> None:
    service, _store = _service(
        tmp_path,
        report_service=_FakeReportService(),
        effect_reply_enabled=True,
    )
    first_session = _session(
        external_session_id="wxid_friend",
        session_kind="private",
        session_name="好友",
        source_message_id="",
    )
    second_session = _session(
        external_session_id="wxid_friend",
        session_kind="private",
        session_name="好友",
        source_message_id="",
    )
    for session in (first_session, second_session):
        session.turns[0].content = "把热点新闻整理成文件发我"
        session.turns[0].trace_id = "trace-stable-file-request"
        _persist_file_intent(session, "generate", delivery_required=True)

    first = await service.generate_text_file(
        first_session,
        {"content": "第一次正文", "file_name": "第一次名称"},
    )
    second = await service.generate_text_file(
        second_session,
        {"content": "重试正文", "file_name": "第二次名称"},
    )

    first_effect = first["channel_reply_effects"][0]
    second_effect = second["channel_reply_effects"][0]
    assert first_effect == second_effect
    assert first_effect["payload"]["delivery"]["source_message_id"].startswith(
        "message-export-source-"
    )


@pytest.mark.asyncio
async def test_artifact_cleanup_retention_covers_effect_idempotency_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _store = _service(
        tmp_path,
        report_service=_FakeReportService(),
        effect_reply_enabled=True,
    )
    service._settings = service._settings.model_copy(
        update={
            "wxbot_outbound_file_retention_seconds": 5 * 60,
            "wxbot_outbound_file_cleanup_grace_seconds": 0,
            "orchestrator_flow_effect_commit_ttl_seconds": 7 * 24 * 60 * 60,
        }
    )
    captured: dict[str, Any] = {}

    def _capture_cleanup(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"errors": []}

    monkeypatch.setattr(
        "plugins.wxbot.agent_tool_service.cleanup_message_exports",
        _capture_cleanup,
    )

    await service._cleanup_message_export_artifacts()

    assert captured["retention_seconds"] == 7 * 24 * 60 * 60
    assert captured["cleanup_grace_seconds"] == 0


@pytest.mark.asyncio
async def test_managed_private_file_generation_canonicalizes_source_and_target(
    tmp_path: Path,
) -> None:
    service, store = _service(
        tmp_path,
        report_service=_FakeReportService(),
        effect_reply_enabled=True,
    )
    connection_id = "managed-wechat-account"
    external_session_id = "wxid_friend"
    external_source_message_id = "5665164121400123687"
    canonical_session_id = canonical_conversation_id(
        connection_id,
        external_session_id,
    )
    canonical_source_message_id = canonical_message_id(
        connection_id,
        external_source_message_id,
    )
    session = _session(
        external_session_id=external_session_id,
        session_kind="private",
        session_name="好友",
        source_message_id=external_source_message_id,
    )
    session.connection_id = connection_id
    session.session_id = canonical_session_id
    session.canonical_conversation_id = canonical_session_id
    session.metadata.pop("_wxbot_delivery_contract")
    session.turns[0].metadata.pop("_wxbot_delivery_contract")
    session.turns[0].metadata["external_message_id"] = external_source_message_id
    session.turns[0].content = "把上面的内容整理成 txt 文件发我"
    _persist_file_intent(session, "generate", format="txt", delivery_required=True)

    result = await service.generate_text_file(
        session,
        {"content": "这是文件正文。", "format": "txt"},
    )

    assert store.enqueued == []
    payload = result["channel_reply_effects"][0]["payload"]
    assert payload["session_id"] == canonical_session_id
    assert payload["external_conversation_id"] == external_session_id
    assert payload["canonical_conversation_id"] == canonical_session_id
    assert payload["reply_to_message_id"] == external_source_message_id
    assert payload["source_message"]["message_id"] == canonical_source_message_id
    assert payload["delivery"]["source_message_id"] == canonical_source_message_id


@pytest.mark.asyncio
async def test_generate_json_file_preserves_structured_content(tmp_path: Path) -> None:
    service, _store = _service(
        tmp_path,
        report_service=_FakeReportService(),
        effect_reply_enabled=True,
    )
    session = _session(
        external_session_id="wxid_friend",
        session_kind="private",
        session_name="好友",
        source_message_id="msg-generate-json-1",
    )
    session.turns[0].content = "把上面的内容整理成 json 文件发我"
    _persist_file_intent(session, "generate", format="json", delivery_required=True)

    result = await service.generate_text_file(
        session,
        {"content": '{"ok": true, "items": [1]}', "format": "json"},
    )

    assert result["ok"] is True
    file_payload = result["channel_reply_effects"][0]["payload"]["file"]
    assert Path(file_payload["file_path"]).read_text(encoding="utf-8") == (
        '{\n  "ok": true,\n  "items": [\n    1\n  ]\n}\n'
    )


@pytest.mark.asyncio
async def test_private_monthly_export_uses_current_private_target(
    tmp_path: Path,
) -> None:
    report_service = _FakeReportService(period="2026-07")
    service, _store = _service(tmp_path, report_service=report_service)
    session = _session(
        external_session_id="wxid_friend",
        session_kind="private",
        session_name="好友",
        source_message_id="msg-private-1",
    )

    result = await service.export_current_messages_file(
        session,
        {"report_type": "monthly", "year_month": "2026-07"},
    )

    assert report_service.message_calls == []
    for effect in result["channel_reply_effects"]:
        assert effect["payload"]["session_id"] == "wxid_friend"
        assert effect["payload"]["session_kind"] == "private"
        assert effect["payload"]["connection_id"] == LEGACY_WXBOT_CONNECTION_ID


@pytest.mark.asyncio
async def test_group_export_uses_connection_scoped_managed_observations(
    tmp_path: Path,
) -> None:
    report_service = _FakeReportService(period="2026-07-29")
    connection_id = "managed-wechat-account"
    external_source_message_id = "5665164121400123687"
    canonical_source_message_id = canonical_message_id(
        connection_id,
        external_source_message_id,
    )
    canonical_session_id = canonical_conversation_id(
        connection_id,
        "room@chatroom",
    )
    occurred_ts = int(datetime(2026, 7, 29, 1, 30, tzinfo=UTC).timestamp())
    store = _FakeStore(
        observations=[
            {
                "message_id": "managed-msg-1",
                "sender_wxid": "wxid_a",
                "sender_name": "张三",
                "msg_type": "text",
                "content": "托管连接里的当天消息",
                "is_self_sent": False,
                "occurred_ts": occurred_ts,
                "metadata": {},
            }
        ]
    )
    service, store = _service(
        tmp_path,
        report_service=report_service,
        store=store,
    )
    session = _session(
        external_session_id="room@chatroom",
        session_kind="group",
        session_name="测试群",
        source_message_id=canonical_source_message_id,
    )
    session.connection_id = connection_id
    session.session_id = canonical_session_id
    session.canonical_conversation_id = canonical_session_id
    session.turns[0].metadata["external_message_id"] = external_source_message_id
    session.turns[0].metadata["msg_svr_id"] = external_source_message_id

    result = await service.export_current_messages_file(
        session,
        {"report_type": "daily", "date": "2026-07-29"},
    )

    assert report_service.message_calls == []
    assert result["message_count"] == 1
    assert store.observation_period_calls == [
        {
            "tenant_id": "demo",
            "session_id": canonical_session_id,
            "start_occurred_ts": int(datetime(2026, 7, 28, 16, 0, tzinfo=UTC).timestamp()),
            "end_occurred_ts": int(datetime(2026, 7, 29, 16, 0, tzinfo=UTC).timestamp()),
            "limit": 10001,
        }
    ]
    assert store.enqueued == []
    for effect in result["channel_reply_effects"]:
        payload = effect["payload"]
        assert payload["session_id"] == canonical_session_id
        assert payload["external_conversation_id"] == "room@chatroom"
        assert payload["canonical_conversation_id"] == canonical_session_id
        assert payload["reply_to_message_id"] == external_source_message_id
        assert payload["source_message"]["message_id"] == canonical_source_message_id
        assert payload["source_message"]["external_message_id"] == external_source_message_id
        assert payload["source_message"]["session_id"] == canonical_session_id
        assert payload["delivery"]["session_id"] == canonical_session_id
        assert payload["delivery"]["external_conversation_id"] == "room@chatroom"
        assert payload["delivery"]["canonical_conversation_id"] == canonical_session_id
        assert payload["delivery"]["source_message_id"] == canonical_source_message_id
    export_path = Path(result["channel_reply_effects"][1]["payload"]["file"]["file_path"])
    exported = export_path.read_text(encoding="utf-8-sig")
    assert "[2026-07-29 09:30:00] 张三: 托管连接里的当天消息" in exported


@pytest.mark.asyncio
async def test_managed_group_recent_export_queries_exact_occurred_ts_window(
    tmp_path: Path,
) -> None:
    connection_id = "managed-wechat-account"
    external_session_id = "room@chatroom"
    canonical_session_id = canonical_conversation_id(connection_id, external_session_id)
    anchor = datetime(2026, 7, 29, 2, 0, tzinfo=UTC)
    store = _FakeStore(
        observations=[
            {
                "message_id": "managed-recent-1",
                "sender_wxid": "wxid_a",
                "sender_name": "张三",
                "msg_type": "text",
                "content": "最近十分钟的消息",
                "is_self_sent": False,
                "occurred_ts": int((anchor.timestamp()) - 120),
                "metadata": {},
            }
        ]
    )
    service, store = _service(
        tmp_path,
        report_service=_FakeReportService(),
        store=store,
    )
    session = _session(
        external_session_id=external_session_id,
        session_kind="group",
        session_name="测试群",
        source_message_id="managed-recent-command",
    )
    session.connection_id = connection_id
    session.session_id = canonical_session_id
    session.canonical_conversation_id = canonical_session_id
    session.turns[0].content = "把最近10分钟群消息汇总成文件发给我"
    _persist_file_intent(session, "export_history", recent_minutes=10)
    session.turns[0].created_at = anchor

    result = await service.export_current_messages_file(
        session,
        {"report_type": "recent", "minutes": 10},
    )

    assert result["report_type"] == "recent"
    assert result["minutes"] == 10
    assert result["message_count"] == 1
    assert store.observation_period_calls == [
        {
            "tenant_id": "demo",
            "session_id": canonical_session_id,
            "start_occurred_ts": int(anchor.timestamp()) - 600,
            "end_occurred_ts": int(anchor.timestamp()),
            "limit": 10001,
        }
    ]


@pytest.mark.asyncio
async def test_managed_group_export_fails_closed_on_canonical_scope_mismatch(
    tmp_path: Path,
) -> None:
    report_service = _FakeReportService(period="2026-07-29")
    service, store = _service(tmp_path, report_service=report_service)
    session = _session(
        external_session_id="room@chatroom",
        session_kind="group",
        session_name="测试群",
        source_message_id="msg-managed-mismatch",
    )
    session.connection_id = "managed-wechat-account"

    with pytest.raises(ValueError, match="managed_wxbot_conversation_scope_mismatch"):
        await service.export_current_messages_file(
            session,
            {"report_type": "daily", "date": "2026-07-29"},
        )

    assert report_service.message_calls == []
    assert store.observation_period_calls == []
    assert store.enqueued == []


@pytest.mark.asyncio
async def test_invalid_export_date_fails_before_sdk_or_send(tmp_path: Path) -> None:
    report_service = _FakeReportService()
    service, store = _service(tmp_path, report_service=report_service)

    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        await service.export_current_messages_file(
            _session(
                external_session_id="room@chatroom",
                session_kind="group",
                session_name="测试群",
                source_message_id="msg-invalid-date",
            ),
            {"report_type": "daily", "date": "2026-02-30"},
        )

    assert report_service.message_calls == []
    assert store.enqueued == []
    assert list(tmp_path.rglob("*.txt")) == []


@pytest.mark.asyncio
async def test_message_fetch_failure_does_not_stage_or_send_file(tmp_path: Path) -> None:
    report_service = _FakeReportService(message_error="messages unavailable")
    service, store = _service(tmp_path, report_service=report_service)

    with pytest.raises(RuntimeError, match="messages unavailable"):
        await service.export_current_messages_file(
            _session(
                external_session_id="room@chatroom",
                session_kind="group",
                session_name="测试群",
                source_message_id="msg-messages-failed",
            ),
            {"report_type": "daily", "date": "2026-07-29"},
        )

    assert len(report_service.message_calls) == 1
    assert store.enqueued == []
    assert list(tmp_path.rglob("*.txt")) == []


@pytest.mark.asyncio
async def test_compatibility_path_enqueues_confirmation_and_file(
    tmp_path: Path,
) -> None:
    report_service = _FakeReportService(period="2026-07-29")
    service, store = _service(
        tmp_path,
        report_service=report_service,
        effect_reply_enabled=False,
    )

    result = await service.export_current_messages_file(
        _session(
            external_session_id="wxid_friend",
            session_kind="private",
            session_name="好友",
            source_message_id="msg-compat-1",
        ),
        {"report_type": "daily", "date": "2026-07-29"},
    )

    assert result["channel_reply_effects"] == []
    assert [item["msg_type"] for item in store.enqueued] == ["text", "file"]
    assert [item["session_id"] for item in store.enqueued] == [
        "wxid_friend",
        "wxid_friend",
    ]
    assert store.enqueued[1]["file_path"]
    assert store.enqueued[1]["file_name"].endswith(".txt")
    assert store.enqueued[0]["delivery"]["source_message_id"] == "msg-compat-1"
    assert store.enqueued[0]["command_id"] != store.enqueued[1]["command_id"]
    assert store.active_file_path_reads == 1


@pytest.mark.asyncio
async def test_private_daily_export_filters_only_current_session_turns_for_date(
    tmp_path: Path,
) -> None:
    report_service = _FakeReportService(period="unused")
    service, _store = _service(tmp_path, report_service=report_service)
    session = _session(
        external_session_id="wxid_friend",
        session_kind="private",
        session_name="好友",
        source_message_id="msg-private-filter",
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="当天用户消息",
            created_at=datetime(2026, 7, 28, 16, 30, tzinfo=UTC),
            metadata={"sender_name": "好友", "sender_wxid": "wxid_friend"},
        ),
        Turn(
            session_id=session.session_id,
            role=Role.ASSISTANT,
            content="当天助手回复",
            created_at=datetime(2026, 7, 29, 1, 0, tzinfo=UTC),
        ),
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="次日消息不应导出",
            created_at=datetime(2026, 7, 29, 16, 30, tzinfo=UTC),
            metadata={
                "sender_name": "好友",
                "sender_wxid": "wxid_friend",
                "msg_svr_id": "msg-private-filter",
            },
        ),
    ]

    result = await service.export_current_messages_file(
        session,
        {"report_type": "daily", "date": "2026-07-29"},
    )

    assert report_service.message_calls == []
    assert result["message_count"] == 2
    file_path = Path(result["channel_reply_effects"][1]["payload"]["file"]["file_path"])
    content = file_path.read_text(encoding="utf-8-sig")
    assert "好友: 当天用户消息" in content
    assert "助手: 当天助手回复" in content
    assert "次日消息不应导出" not in content


@pytest.mark.asyncio
async def test_private_recent_export_filters_turns_before_command_anchor(
    tmp_path: Path,
) -> None:
    service, _store = _service(
        tmp_path,
        report_service=_FakeReportService(period="unused"),
    )
    session = _session(
        external_session_id="wxid_friend",
        session_kind="private",
        session_name="好友",
        source_message_id="msg-private-recent",
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="范围外旧消息",
            created_at=datetime(2026, 7, 29, 1, 49, 59, tzinfo=UTC),
        ),
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="范围起点消息",
            created_at=datetime(2026, 7, 29, 1, 0, tzinfo=UTC),
            metadata={"occurred_ts": int(datetime(2026, 7, 29, 1, 50, tzinfo=UTC).timestamp())},
        ),
        Turn(
            session_id=session.session_id,
            role=Role.ASSISTANT,
            content="范围内助手消息",
            created_at=datetime(2026, 7, 29, 1, 59, 59, tzinfo=UTC),
        ),
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="整理十分钟聊天记录，以文件方式发给我",
            created_at=datetime(2026, 7, 29, 2, 0, tzinfo=UTC),
            metadata={"msg_svr_id": "msg-private-recent"},
        ),
    ]
    _persist_file_intent(session, "export_history", recent_minutes=10)

    result = await service.export_current_messages_file(
        session,
        {"report_type": "daily", "date": "2026-07-29"},
    )

    assert result["report_type"] == "recent"
    assert result["minutes"] == 10
    assert result["message_count"] == 2
    export_path = Path(result["channel_reply_effects"][1]["payload"]["file"]["file_path"])
    exported = export_path.read_text(encoding="utf-8-sig")
    assert "范围起点消息" in exported
    assert "范围内助手消息" in exported
    assert "范围外旧消息" not in exported
    assert "整理十分钟聊天记录" not in exported


@pytest.mark.asyncio
@pytest.mark.parametrize("minutes", [0, 1441])
async def test_recent_export_rejects_out_of_bounds_minutes_before_read_or_send(
    tmp_path: Path,
    minutes: int,
) -> None:
    report_service = _FakeReportService()
    service, store = _service(tmp_path, report_service=report_service)
    session = _session(
        external_session_id="room@chatroom",
        session_kind="group",
        session_name="测试群",
        source_message_id=f"msg-invalid-recent-{minutes}",
    )
    session.turns[0].content = f"汇总最近{minutes}分钟群消息并导出文件给我"
    _persist_file_intent(session, "export_history", recent_minutes=minutes)

    with pytest.raises(ValueError, match="无效或不明确"):
        await service.export_current_messages_file(
            session,
            {"report_type": "recent", "minutes": minutes},
        )

    assert report_service.message_calls == []
    assert store.enqueued == []
    assert list(tmp_path.rglob("*.txt")) == []


@pytest.mark.asyncio
async def test_recent_export_rejects_ambiguous_user_range_before_read_or_send(
    tmp_path: Path,
) -> None:
    report_service = _FakeReportService()
    service, store = _service(tmp_path, report_service=report_service)
    session = _session(
        external_session_id="room@chatroom",
        session_kind="group",
        session_name="测试群",
        source_message_id="msg-ambiguous-recent",
    )
    session.turns[0].content = "汇总最近10分钟还是20分钟群消息并输出文件给我"
    _persist_file_intent(session, "export_history", recent_minutes_invalid=True)

    with pytest.raises(ValueError, match="无效或不明确"):
        await service.export_current_messages_file(
            session,
            {"report_type": "daily"},
        )

    assert report_service.message_calls == []
    assert store.enqueued == []
    assert list(tmp_path.rglob("*.txt")) == []
