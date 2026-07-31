from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.agent.scopes import MESSAGE_EXPORT_SCOPE
from app.channel.identity import LEGACY_WXBOT_CONNECTION_ID
from app.common.config import Settings
from app.common.types import Channel, Role, Session, Turn
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
    def __init__(self) -> None:
        self.enqueued: list[dict[str, Any]] = []
        self.active_file_path_reads = 0

    async def enqueue_reply(self, **kwargs: Any) -> int:
        self.enqueued.append(dict(kwargs))
        return len(self.enqueued)

    async def list_active_outbound_file_paths(self) -> list[str]:
        self.active_file_path_reads += 1
        return []


class _FakeReportService:
    def __init__(
        self,
        *,
        report: str = "大家主要讨论了项目排期。",
        messages: list[dict[str, Any]] | None = None,
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
            "messages": list(self.messages),
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


def test_message_export_definition_is_current_session_only_and_group_admin_gated() -> None:
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
    assert tool.metadata["required_group_role"] == "admin"
    assert tool.metadata["requires_group_file_send"] is True
    properties = tool.parameters["properties"]
    assert set(properties) == {"report_type", "date", "year_month", "format"}
    assert "session_id" not in properties
    assert "target" not in properties


def test_file_delivery_tools_require_group_admin_and_master_switch() -> None:
    service, _store = _service(
        Path("C:/tmp/wxbot-file-definition"),
        report_service=_FakeReportService(),
    )

    tools = {
        tool.name: tool
        for tool in build_wxbot_file_analysis_agent_tools(service)
    }

    assert "required_group_role" not in tools["inspect_current_file"].metadata
    for name in ("convert_current_file", "generate_text_file"):
        assert tools[name].metadata["required_group_role"] == "admin"
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
        effect["payload"]["external_conversation_id"] == "room@chatroom"
        for effect in effects
    )
    assert effects[0]["payload"]["body"]["text"].endswith("共 1 条，文件已排队发送。")
    assert effects[0]["payload"]["delivery"]["source_message_id"] == "msg-group-1"
    assert effects[0]["payload"]["delivery"]["participation_policy_version"] == 17
    assert (
        effects[0]["payload"]["source_message"]["_wxbot_delivery_contract"][
            "source_message_id"
        ]
        == "msg-group-1"
    )
    file_payload = effects[1]["payload"]["file"]
    export_path = Path(file_payload["file_path"])
    assert export_path.is_file()
    assert file_payload["file_name"].endswith(".txt")
    assert export_path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert "二、原始消息记录" in export_path.read_text(encoding="utf-8-sig")


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
async def test_group_export_rejects_managed_connection_history(
    tmp_path: Path,
) -> None:
    report_service = _FakeReportService(period="2026-07-29")
    service, store = _service(tmp_path, report_service=report_service)
    session = _session(
        external_session_id="room@chatroom",
        session_kind="group",
        session_name="测试群",
        source_message_id="msg-managed-group",
    )
    session.connection_id = "managed-wechat-account"

    with pytest.raises(ValueError, match="connection_scoped_history_unavailable"):
        await service.export_current_messages_file(
            session,
            {"report_type": "daily", "date": "2026-07-29"},
        )

    assert report_service.message_calls == []
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
