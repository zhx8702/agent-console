from __future__ import annotations

import asyncio
import json
import time

import pytest

from app.common.config import Settings
from app.common.types import Channel, Session
from plugins.wxbot.agent_tool_service import _gather_cancel_on_error
from plugins.wxbot.agent_tools import (
    WxbotAgentToolService,
    build_wxbot_core_agent_tools,
    build_wxbot_core_plugin_status_agent_tools,
    build_wxbot_credits_agent_tools,
    build_wxbot_credits_plugin_status_agent_tools,
    build_wxbot_group_agent_tools,
    build_wxbot_group_plugin_status_agent_tools,
    build_wxbot_moderation_agent_tools,
    build_wxbot_moderation_plugin_status_agent_tools,
    build_wxbot_repeater_agent_tools,
    build_wxbot_repeater_plugin_status_agent_tools,
)


@pytest.mark.asyncio
async def test_concurrent_tool_reads_cancel_siblings_after_one_failure() -> None:
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()

    async def fail_after_sibling_starts() -> None:
        await sibling_started.wait()
        raise RuntimeError("read failed")

    async def wait_forever() -> None:
        sibling_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            sibling_cancelled.set()

    with pytest.raises(RuntimeError, match="read failed"):
        await _gather_cancel_on_error(fail_after_sibling_starts(), wait_forever())

    assert sibling_cancelled.is_set()


class _FakeWxbotStore:
    def __init__(self) -> None:
        self.member_event_connections: list[str] = []
        self.session_policy_ids: list[str] = []

    async def list_member_events(
        self,
        tenant_id: str,
        limit: int = 50,
        *,
        connection_id: str = "",
    ) -> list[dict]:
        assert tenant_id == "demo"
        self.member_event_connections.append(connection_id)
        return [
            {
                "session_id": "room@chatroom",
                "event_type": "group.member.joined",
                "entity_name": "新成员",
                "created_at": "2026-04-23T10:00:00",
            }
        ]

    async def get_session_policy(self, tenant_id: str, session_id: str) -> dict[str, object]:
        assert tenant_id == "demo"
        self.session_policy_ids.append(session_id)
        return {
            "effective_mode": "contains",
            "effective_mention_sender": True,
            "trigger_keywords": ["报价"],
        }

    async def get_report_subscription(self, tenant_id: str, session_id: str) -> dict[str, object]:
        assert tenant_id == "demo"
        assert session_id == "room@chatroom"
        return {
            "daily_enabled": True,
            "monthly_enabled": False,
            "daily_hour": 9,
            "monthly_day": 1,
        }


class _FakeCreditsStore:
    async def get_config(self, tenant_id: str, session_id: str) -> dict[str, object]:
        return {
            "enabled": True,
            "credit_name": "积分",
            "cost_per_chat": 2,
            "checkin_mode": 2,
            "checkin_mode_label": "当前发言签到（静默）",
            "daily_checkin": 10,
            "streak_bonus": 5,
            "streak_cap": 50,
        }

    async def list_members(self, tenant_id: str, session_id: str, *, limit: int = 200, query: str = "") -> dict[str, object]:
        return {
            "items": [
                {
                    "user_id": "wxid_a",
                    "display_name": "张三",
                    "credits": 120,
                    "rank": 1,
                    "checked_in_today": True,
                },
                {
                    "user_id": "wxid_b",
                    "display_name": "李四",
                    "credits": 90,
                    "rank": 2,
                    "checked_in_today": False,
                },
            ][:limit],
            "summary": {
                "member_count": 2,
                "checked_in_today_count": 1,
                "total_credits": 210,
                "today": "2026-04-23",
            },
        }

    async def get_member_detail(
        self,
        tenant_id: str,
        session_id: str,
        user_id: str,
        *,
        ledger_limit: int = 20,
    ) -> dict[str, object]:
        assert user_id == "wxid_a"
        return {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "user_id": user_id,
            "display_name": "张三",
            "credits": 120,
            "rank": 1,
            "checkin_status": {
                "checked_in_today": True,
                "streak": 3,
            },
            "recent_ledger": [
                {"delta": 10, "reason": "checkin"},
            ],
        }


class _FakeModerationStore:
    async def get_config(self, tenant_id: str, session_id: str) -> dict[str, object]:
        return {
            "enabled": True,
            "webhook_enabled": True,
            "reminder_mode": "append",
            "reminder_text": "检测到敏感词",
        }

    async def get_keywords(self, tenant_id: str, session_id: str, enabled_only: bool = False) -> list[dict[str, object]]:
        return [
            {"id": 1, "keyword": "代言", "enabled": True, "created_at": "2026-04-22T10:00:00"},
            {"id": 2, "keyword": "推广", "enabled": True, "created_at": "2026-04-22T10:00:00"},
        ]

    async def get_events(
        self,
        tenant_id: str,
        session_id: str | None = None,
        *,
        action: str = "",
        webhook_status: str = "",
        keyword: str = "",
        limit: int = 50,
    ) -> list[dict[str, object]]:
        return [
            {
                "id": 11,
                "session_id": session_id or "room@chatroom",
                "session_name": "测试群",
                "sender_name": "张三",
                "message_preview": "有人提到代言",
                "matched_keyword_list": ["代言"],
                "created_at": "2026-04-23T10:00:00",
            }
        ][:limit]


class _FakeRepeaterStore:
    def __init__(self) -> None:
        self.config_session_ids: list[str] = []
        self.event_session_ids: list[str] = []

    async def get_config(self, tenant_id: str, session_id: str) -> dict[str, object]:
        self.config_session_ids.append(session_id)
        return {
            "enabled": True,
            "cooldown_seconds": 300,
        }

    async def list_events(
        self,
        tenant_id: str,
        session_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        self.event_session_ids.append(str(session_id or ""))
        return [
            {
                "id": 21,
                "session_id": session_id or "room@chatroom",
                "content_text": "[旺柴]",
                "trace_id": "trace-repeater-1",
                "created_at": "2026-04-23T10:00:00",
            }
        ][:limit]


async def _allow_data_owner_scope(
    owner: str,
    tenant_id: str,
    session_id: str,
) -> bool:
    assert owner in {"credits", "moderation", "repeater"}
    assert tenant_id == "demo"
    assert session_id == "room@chatroom"
    return True


async def _allow_data_owners_scope(
    owners: tuple[str, ...],
    tenant_id: str,
    session_id: str,
) -> bool:
    assert owners == ("wxbot", "credits", "moderation", "repeater")
    assert tenant_id == "demo"
    assert session_id == "room@chatroom"
    return True


class _TestWxbotAgentToolService(WxbotAgentToolService):
    def __init__(
        self,
        *,
        data_owner_scope_execution_allowed=_allow_data_owner_scope,
        data_owners_scope_execution_allowed=_allow_data_owners_scope,
    ) -> None:
        super().__init__(
            Settings(customer_service_prompt_enabled=False),
            wxbot_store=_FakeWxbotStore(),
            credits_store=_FakeCreditsStore(),
            moderation_store=_FakeModerationStore(),
            repeater_store=_FakeRepeaterStore(),
            data_owner_scope_execution_allowed=data_owner_scope_execution_allowed,
            data_owners_scope_execution_allowed=data_owners_scope_execution_allowed,
        )

    async def _sdk_get(self, path: str) -> dict[str, object]:
        if path == "/ext/roster/groups":
            return {
                "sessions": [
                    {"session_id": "room@chatroom", "session_name": "测试群"},
                ]
            }
        if path == "/ext/roster/groups/room@chatroom/members":
            return {
                "session_name": "测试群",
                "members": [
                    {
                        "wxid": "wxid_a",
                        "display_name": "张三",
                        "avatar": {
                            "cache_url": "/ext/roster/avatars/wxid_a",
                            "cached": True,
                            "size": 4773,
                            "content_type": "image/jpeg",
                            "small_head_url": "https://wx.qlogo.cn/a/132",
                            "big_head_url": "https://wx.qlogo.cn/a/0",
                        },
                    },
                    {"wxid": "wxid_b", "display_name": "李四"},
                ],
            }
        raise AssertionError(path)

    async def _sdk_get_optional(self, path: str) -> dict[str, object] | None:
        if path == "/group-members/settings/room@chatroom":
            return {
                "welcome_enabled": True,
                "welcome_mention": True,
            }
        return None

    async def _cache_avatar_file(self, avatar_url: str, *, wxid: str, content_type: str = "") -> str:
        assert wxid == "wxid_a"
        assert avatar_url.endswith("/ext/roster/avatars/wxid_a")
        return "/tmp/wxbot-avatars/wxid_a.jpg"

    async def _sdk_query_rows(
        self,
        *,
        database: str,
        sql: str,
        params: list[object] | dict[str, object] | None = None,
        limit: int = 200,
    ) -> list[dict[str, object]]:
        assert database == "message"
        if "sqlite_master" in sql:
            return [{"ok": 1}]
        now = int(time.time())
        return [
            {
                "create_time": now - 60,
                "message_content_hex": "wxid_a:\n今天有人提到 draw 功能".encode().hex(),
                "compression_type": 0,
            },
            {
                "create_time": now - 30,
                "message_content_hex": "wxid_b:\n积分功能也有人在讨论".encode().hex(),
                "compression_type": 0,
            },
            {
                "create_time": now - 10,
                "message_content_hex": "wxid_a:\n这个群最近挺热闹".encode().hex(),
                "compression_type": 0,
            },
        ]


class _CandidatesWxbotAgentToolService(_TestWxbotAgentToolService):
    async def _sdk_get(self, path: str) -> dict[str, object]:
        if path == "/ext/roster/groups":
            return {
                "sessions": [
                    {"session_id": "room@chatroom", "session_name": "测试群"},
                ]
            }
        if path == "/ext/roster/groups/room@chatroom/members":
            return {
                "ok": True,
                "session_id": "room@chatroom",
                "session_name": "测试群",
                "source_kind": "authoritative_group_roster",
                "candidates": [
                    {"wxid": "wxid_a", "nick_name": "张三", "alias": "", "remark": ""},
                    {"wxid": "wxid_b", "nick_name": "", "alias": "李四", "remark": ""},
                ],
            }
        return await super()._sdk_get(path)


class _EmptyRosterWxbotAgentToolService(_TestWxbotAgentToolService):
    async def _sdk_get(self, path: str) -> dict[str, object]:
        if path == "/ext/roster/groups":
            return {
                "sessions": [
                    {"session_id": "room@chatroom", "session_name": "测试群"},
                ]
            }
        if path == "/ext/roster/groups/room@chatroom/members":
            return {
                "ok": True,
                "session_id": "room@chatroom",
                "session_name": "测试群",
                "members": [],
            }
        return await super()._sdk_get(path)


class _ResearchQualityWxbotAgentToolService(_TestWxbotAgentToolService):
    async def _sdk_query_rows(
        self,
        *,
        database: str,
        sql: str,
        params: list[object] | dict[str, object] | None = None,
        limit: int = 200,
    ) -> list[dict[str, object]]:
        assert database == "message"
        if "sqlite_master" in sql:
            return [{"ok": 1}]
        now = int(time.time())
        rows = [
            {
                "create_time": now - 300,
                "message_content_hex": "wxid_current:\n/research 帮我找一下gpt5.5 怎么配到codex里面用".encode().hex(),
                "compression_type": 0,
            },
            {
                "create_time": now - 260,
                "message_content_hex": "wxid_b:\n不是可以codex -m gpt5.5吗？".encode().hex(),
                "compression_type": 0,
            },
            {
                "create_time": now - 220,
                "message_content_hex": "wxid_a:\ngpt5.5启动了".encode().hex(),
                "compression_type": 0,
            },
            {
                "create_time": now - 180,
                "message_content_hex": "wxid_b:\n你把 codex 的模型配置切到 gpt5.5 就行".encode().hex(),
                "compression_type": 0,
            },
            {
                "create_time": now - 160,
                "message_content_hex": "wxid_a:\n这个 json, 放在 .codex 下面".encode().hex(),
                "compression_type": 0,
            },
            {
                "create_time": now - 120,
                "message_content_hex": "wxid_a:\n这个群最近挺热闹".encode().hex(),
                "compression_type": 0,
            },
        ]
        return rows[:limit]


class _ResearchSemanticWxbotAgentToolService(_TestWxbotAgentToolService):
    async def _sdk_query_rows(
        self,
        *,
        database: str,
        sql: str,
        params: list[object] | dict[str, object] | None = None,
        limit: int = 200,
    ) -> list[dict[str, object]]:
        assert database == "message"
        if "sqlite_master" in sql:
            return [{"ok": 1}]
        now = int(time.time())
        rows = [
            {
                "create_time": now - 80,
                "message_content_hex": "wxid_b:\n我开了两个pro 20x".encode().hex(),
                "compression_type": 0,
            },
            {
                "create_time": now - 120,
                "message_content_hex": "wxid_a:\n老叶说CDK那边封号了，pro先别碰".encode().hex(),
                "compression_type": 0,
            },
            {
                "create_time": now - 160,
                "message_content_hex": "wxid_b:\nCDK最近封号风险挺高，老叶刚提醒过".encode().hex(),
                "compression_type": 0,
            },
            {
                "create_time": now - 200,
                "message_content_hex": "wxid_a:\n普通pro套餐今天有人问".encode().hex(),
                "compression_type": 0,
            },
        ]
        return rows[:limit]


class _ProfileWxbotAgentToolService(_TestWxbotAgentToolService):
    async def _sdk_get(self, path: str) -> dict[str, object]:
        if path == "/ext/roster/groups":
            return {
                "sessions": [
                    {"session_id": "room@chatroom", "session_name": "测试群"},
                ]
            }
        if path == "/ext/roster/groups/room@chatroom/members":
            return {
                "session_name": "测试群",
                "members": [
                    {"wxid": "wxid_linzhou", "display_name": "示例开发者-LinZhou"},
                    {"wxid": "wxid_b", "display_name": "李四"},
                ],
            }
        return await super()._sdk_get(path)

    async def _sdk_query_rows(
        self,
        *,
        database: str,
        sql: str,
        params: list[object] | dict[str, object] | None = None,
        limit: int = 200,
    ) -> list[dict[str, object]]:
        assert database == "message"
        if "sqlite_master" in sql:
            return [{"ok": 1}]
        now = int(time.time())
        rows = [
            {
                "create_time": now - 600,
                "message_content_hex": "wxid_linzhou:\n我最近在看 ExampleCrawler 和 AI Agent，邮箱 linzhou@example.test，手机 13800138000".encode().hex(),
                "compression_type": 0,
            },
            {
                "create_time": now - 500,
                "message_content_hex": "wxid_b:\nLinZhou 昨天提到 Python 爬虫和 sample-lab，身份证 110101199001011234".encode().hex(),
                "compression_type": 0,
            },
            {
                "create_time": now - 400,
                "message_content_hex": "wxid_linzhou:\nsecret=abcdef1234567890abcdef1234567890 ExampleCrawler 后面会整理一下".encode().hex(),
                "compression_type": 0,
            },
            {
                "create_time": now - 300,
                "message_content_hex": "wxid_b:\n张三在聊别的事情".encode().hex(),
                "compression_type": 0,
            },
        ]
        return rows[:limit]


def _make_session() -> Session:
    return Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
        metadata={"session_name": "测试群"},
    )


@pytest.mark.asyncio
async def test_managed_group_tools_never_fall_through_to_legacy_sdk_account() -> None:
    wxbot_store = _FakeWxbotStore()
    service = WxbotAgentToolService(
        Settings(customer_service_prompt_enabled=False),
        wxbot_store=wxbot_store,
        credits_store=_FakeCreditsStore(),
        moderation_store=_FakeModerationStore(),
        repeater_store=_FakeRepeaterStore(),
    )
    session = _make_session()
    session.connection_id = "wechat-main"
    session.external_conversation_id = "external-room@chatroom"

    # Store-backed policy uses the operator-facing external group ID without
    # touching an SDK.
    policy = await service.get_group_reply_policy(session, {})
    assert policy["session_id"] == "external-room@chatroom"
    assert wxbot_store.session_policy_ids == ["external-room@chatroom"]

    # SDK-only features fail closed instead of querying the global legacy SDK.
    with pytest.raises(ValueError, match="connection-scoped bridge RPC"):
        await service.get_group_welcome_status(session, {})


@pytest.mark.asyncio
async def test_managed_repeater_status_uses_external_group_scope() -> None:
    gate_calls: list[tuple[str, str, str]] = []

    async def owner_gate(owner: str, tenant_id: str, session_id: str) -> bool:
        gate_calls.append((owner, tenant_id, session_id))
        return True

    repeater_store = _FakeRepeaterStore()
    service = WxbotAgentToolService(
        Settings(customer_service_prompt_enabled=False),
        wxbot_store=_FakeWxbotStore(),
        credits_store=_FakeCreditsStore(),
        moderation_store=_FakeModerationStore(),
        repeater_store=repeater_store,
        data_owner_scope_execution_allowed=owner_gate,
    )
    session = _make_session()
    session.connection_id = "wechat-main"
    session.external_conversation_id = "external-room@chatroom"

    result = await service.get_group_repeater_status(session, {"event_limit": 3})

    assert result["session_id"] == "external-room@chatroom"
    assert repeater_store.config_session_ids == ["external-room@chatroom"]
    assert repeater_store.event_session_ids == ["external-room@chatroom"]
    assert gate_calls == [
        ("repeater", "demo", "external-room@chatroom"),
        ("repeater", "demo", "external-room@chatroom"),
    ]


@pytest.mark.asyncio
async def test_search_group_messages_returns_filtered_hits() -> None:
    service = _TestWxbotAgentToolService()

    result = await service.search_group_messages(
        _make_session(),
        {"query": "draw", "limit": 5, "hours": 24},
    )

    assert result["session_name"] == "测试群"
    assert result["total"] == 1
    assert result["matched_senders"] == [{"display_name": "张三", "message_count": 1}]
    assert result["messages"][0]["sender_name"] == "张三"
    assert result["messages"][0]["text"] == "今天有人提到 draw 功能"


@pytest.mark.asyncio
async def test_research_group_messages_returns_summary_and_hits() -> None:
    service = _TestWxbotAgentToolService()

    result = await service.research_group_messages(
        _make_session(),
        {"question": "最近谁提到 draw 功能", "limit": 3, "hours": 24},
    )

    assert result["session_name"] == "测试群"
    assert result["found"] is True
    assert result["total"] >= 1
    assert "draw" in [str(item).lower() for item in result["keywords"]]
    assert result["messages"][0]["sender_name"] == "张三"
    assert "查到" in str(result["summary"])


@pytest.mark.asyncio
async def test_research_group_messages_ignores_command_echo_and_prefers_real_discussion() -> None:
    service = _ResearchQualityWxbotAgentToolService()

    result = await service.research_group_messages(
        _make_session(),
        {"question": "帮我找一下gpt5.5 怎么配到codex里面用", "limit": 3, "hours": 24},
    )

    assert result["found"] is True
    assert "gpt5.5" in [str(item).lower() for item in result["keywords"]]
    assert "codex" in [str(item).lower() for item in result["keywords"]]
    assert all(not str(item["text"]).startswith("/research") for item in result["messages"])
    assert "codex" in str(result["messages"][0]["text"]).lower()
    assert "最相关线索" in str(result["summary"])
    assert len(result["solution_hints"]) >= 1
    solution_texts = [str(item.get("text") or "") for item in result["solution_hints"]]
    assert any("codex -m gpt5.5" in text.lower() for text in solution_texts)
    assert any(".codex" in text.lower() for text in solution_texts)


@pytest.mark.asyncio
async def test_research_group_messages_prefers_semantic_matches_over_generic_ascii() -> None:
    service = _ResearchSemanticWxbotAgentToolService()

    result = await service.research_group_messages(
        _make_session(),
        {"question": "老叶说他封了几个pro", "limit": 3, "hours": 6},
    )

    assert result["found"] is True
    top_text = str(result["messages"][0]["text"])
    assert "老叶" in top_text
    assert "封" in top_text
    assert "我开了两个pro 20x" not in top_text
    assert "老叶" in result["messages"][0]["matched_keywords"]
    assert any("封" in str(keyword) for keyword in result["messages"][0]["matched_keywords"])
    pro_only_index = [
        str(item["text"]) for item in result["messages"]
    ].index("我开了两个pro 20x")
    assert pro_only_index > 0


@pytest.mark.asyncio
async def test_research_group_messages_quantity_question_requires_quantity_evidence() -> None:
    service = _ResearchSemanticWxbotAgentToolService()

    result = await service.research_group_messages(
        _make_session(),
        {"question": "老叶说他封了几个pro", "limit": 3, "hours": 6},
    )

    assert "未找到明确数量" in str(result["summary"])
    assert "两个pro 20x" not in str(result["summary"])


@pytest.mark.asyncio
async def test_group_info_supports_candidates_roster_payload() -> None:
    service = _CandidatesWxbotAgentToolService()

    info = await service.get_group_info(_make_session(), {})
    members = await service.list_group_members(_make_session(), {"limit": 10})

    assert info["session_name"] == "测试群"
    assert info["member_count"] == 2
    assert info["members_sample"] == []
    assert members["total"] == 2
    assert members["members"][0]["wxid"] == "wxid_a"
    assert members["members"][1]["wxid"] == "wxid_b"


@pytest.mark.asyncio
async def test_group_info_marks_empty_roster_as_unavailable() -> None:
    service = _EmptyRosterWxbotAgentToolService()

    info = await service.get_group_info(_make_session(), {})
    members = await service.list_group_members(_make_session(), {"limit": 10})

    assert info["member_count"] == 0
    assert info["member_count_known"] is False
    assert info["roster_available"] is False
    assert info["source"] == "sdk_roster_empty"
    assert "不能按 0 人处理" in info["note"]
    assert members["total"] == 0
    assert members["roster_available"] is False
    assert members["source"] == "sdk_roster_empty"
    assert "无法列出成员列表" in members["note"]


@pytest.mark.asyncio
async def test_get_group_member_avatar_returns_cached_sdk_url() -> None:
    service = _TestWxbotAgentToolService()

    result = await service.get_group_member_avatar(
        _make_session(),
        {"display_name": "张三"},
    )

    assert result["session_name"] == "测试群"
    assert result["display_name"] == "张三"
    assert result["wxid"] == "wxid_a"
    assert result["avatar_url"] == f"{service._settings.wxbot_sdk_url.rstrip('/')}/ext/roster/avatars/wxid_a"
    assert result["avatar_file_path"] == "/tmp/wxbot-avatars/wxid_a.jpg"
    assert result["avatar_cached"] is True
    assert result["avatar_content_type"] == "image/jpeg"
    assert result["small_head_url"] == "https://wx.qlogo.cn/a/132"
    assert result["big_head_url"] == "https://wx.qlogo.cn/a/0"


@pytest.mark.asyncio
async def test_get_group_public_facts_summarizes_plugin_state() -> None:
    service = _TestWxbotAgentToolService()

    result = await service.get_group_public_facts(
        _make_session(),
        {"hours": 72},
    )

    assert result["member_count"] == 2
    assert result["recent_message_count"] == 3
    assert result["active_member_count"] == 2
    assert result["top_speakers"] == []
    assert result["recent_samples"] == []
    assert all("entity_name" not in item for item in result["recent_member_events"])
    assert result["feature_labels"] == ["积分", "审核", "复读机", "欢迎语", "日报月报"]
    assert result["features"]["credits"]["enabled"] is True
    assert result["features"]["moderation"]["enabled"] is True
    assert result["features"]["repeater"]["enabled"] is True
    assert result["features"]["welcome"]["enabled"] is True
    assert result["features"]["reports"]["daily_enabled"] is True


@pytest.mark.asyncio
async def test_group_public_facts_rechecks_one_atomic_owner_snapshot_before_return() -> None:
    decisions = iter((True, False))
    calls: list[tuple[tuple[str, ...], str, str]] = []

    async def owners_gate(
        owners: tuple[str, ...],
        tenant_id: str,
        session_id: str,
    ) -> bool:
        calls.append((owners, tenant_id, session_id))
        return next(decisions)

    service = _TestWxbotAgentToolService(
        data_owners_scope_execution_allowed=owners_gate,
    )

    with pytest.raises(RuntimeError, match="plugin_owner_snapshot_disabled"):
        await service.get_group_public_facts(_make_session(), {"hours": 72})

    assert calls == [
        (("wxbot", "credits", "moderation", "repeater"), "demo", "room@chatroom"),
        (("wxbot", "credits", "moderation", "repeater"), "demo", "room@chatroom"),
    ]


@pytest.mark.asyncio
async def test_get_group_public_facts_reads_member_events_from_session_connection() -> None:
    service = _TestWxbotAgentToolService()
    session = _make_session()
    session.connection_id = "wechat-main"

    with pytest.raises(ValueError, match="connection-scoped bridge RPC"):
        await service.get_group_public_facts(session, {"hours": 72})

    assert service._wxbot_store.member_event_connections == ["wechat-main"]


@pytest.mark.asyncio
async def test_group_plugin_tools_return_independent_status_payloads() -> None:
    service = _TestWxbotAgentToolService()
    session = _make_session()

    credits = await service.get_group_credits_status(session, {"limit": 2})
    member = await service.get_group_credits_member(session, {"display_name": "张三"})
    moderation = await service.get_group_moderation_status(session, {"keyword_limit": 5, "event_limit": 3})
    repeater = await service.get_group_repeater_status(session, {"event_limit": 3})
    welcome = await service.get_group_welcome_status(session, {})
    reports = await service.get_group_report_status(session, {})
    reply_policy = await service.get_group_reply_policy(session, {})

    assert credits["enabled"] is True
    assert credits["summary"]["member_count"] == 2
    assert credits["top_members"][0]["display_name"] == "张三"

    assert member["display_name"] == "张三"
    assert member["credits"] == 120
    assert member["checkin_status"]["checked_in_today"] is True

    assert moderation["enabled"] is True
    assert moderation["keyword_count"] == 2
    assert moderation["recent_events"][0]["matched_keyword_list"] == ["代言"]

    assert repeater["enabled"] is True
    assert repeater["recent_events"][0]["content_text"] == "[旺柴]"

    assert welcome["enabled"] is True
    assert welcome["mention"] is True

    assert reports["daily_enabled"] is True
    assert reports["daily_hour"] == 9

    assert reply_policy["reply_mode"] == "contains"
    assert reply_policy["mention_sender"] is True


@pytest.mark.parametrize(
    ("owner", "method_name", "arguments"),
    [
        ("credits", "get_group_credits_status", {"limit": 2}),
        (
            "moderation",
            "get_group_moderation_status",
            {"keyword_limit": 5, "event_limit": 3},
        ),
        ("repeater", "get_group_repeater_status", {"event_limit": 3}),
    ],
)
@pytest.mark.asyncio
async def test_cross_plugin_agent_reads_recheck_fresh_owner_after_store_read(
    owner: str,
    method_name: str,
    arguments: dict[str, object],
) -> None:
    gate_calls: list[tuple[str, str, str]] = []

    async def gate(actual_owner: str, tenant_id: str, session_id: str) -> bool:
        gate_calls.append((actual_owner, tenant_id, session_id))
        assert actual_owner == owner
        return len(gate_calls) == 1

    service = _TestWxbotAgentToolService(
        data_owner_scope_execution_allowed=gate,
    )

    with pytest.raises(RuntimeError, match=rf"{owner}_plugin_runtime_disabled"):
        await getattr(service, method_name)(_make_session(), arguments)

    assert gate_calls == [
        (owner, "demo", "room@chatroom"),
        (owner, "demo", "room@chatroom"),
    ]


@pytest.mark.asyncio
async def test_cross_plugin_agent_read_fails_before_store_when_owner_gate_missing() -> None:
    class _UnreadableCreditsStore(_FakeCreditsStore):
        async def get_config(self, tenant_id: str, session_id: str) -> dict[str, object]:
            raise AssertionError("credits store must not be read without its owner gate")

        async def list_members(
            self,
            tenant_id: str,
            session_id: str,
            *,
            limit: int = 200,
            query: str = "",
        ) -> dict[str, object]:
            raise AssertionError("credits store must not be read without its owner gate")

    service = _TestWxbotAgentToolService(
        data_owner_scope_execution_allowed=None,
    )
    service._credits_store = _UnreadableCreditsStore()

    with pytest.raises(RuntimeError, match="credits_plugin_scope_unavailable"):
        await service.get_group_credits_status(_make_session(), {"limit": 2})


@pytest.mark.asyncio
async def test_group_operational_tools_return_rankings_and_events() -> None:
    service = _TestWxbotAgentToolService()
    session = _make_session()

    leaderboard = await service.get_group_credits_leaderboard(
        session,
        {"limit": 2, "checked_in_today_only": True},
    )
    moderation_events = await service.get_group_recent_moderation_events(
        session,
        {"limit": 3, "keyword": "代言"},
    )
    activity = await service.get_group_activity_ranking(
        session,
        {"hours": 24, "limit": 2},
    )

    assert leaderboard["checked_in_today_only"] is True
    assert leaderboard["count"] == 1
    assert leaderboard["items"][0]["display_name"] == "张三"

    assert moderation_events["count"] == 1
    assert moderation_events["items"][0]["matched_keyword_list"] == ["代言"]

    assert activity["active_member_count"] == 2
    assert activity["items"][0]["display_name"] == "张三"
    assert activity["items"][0]["message_count"] == 2


@pytest.mark.asyncio
async def test_build_group_member_profile_report_matches_member_and_redacts_sensitive_text() -> None:
    service = _ProfileWxbotAgentToolService()

    result = await service.build_group_member_profile_report(
        _make_session(),
        {
            "query": "示例开发者-LinZhou",
            "hours": 168,
            "limit": 8,
            "external_candidates": [
                {
                    "platform": "github",
                    "display_name": "FictionalCoder",
                    "url": "https://code.example.test/users/fictional-coder?token=PRIVATE_SENTINEL_TOKEN_BETA",
                    "public_summary": "I am LinZhou, 示例开发者-LinZhou, ExampleCrawler author. email linzhou@example.test",
                },
                {
                    "platform": "bilibili",
                    "display_name": "示例开发者-LinZhou",
                    "url": "https://video.example.test/users/fictional-coder",
                    "public_summary": "公开简介提到 ExampleCrawler、sample-lab、AI Agent 和爬虫技术，手机 13800138000",
                },
            ],
        },
    )

    assert result["found"] is True
    assert result["member"]["display_name"] == "示例开发者-LinZhou"
    assert result["profile"]["status"] == "candidate"
    assert result["profile"]["review"]["state"] == "needs_review"
    assert result["facets"]
    assert result["evidence_refs"]
    assert result["external_candidates"]
    assert result["review"]["state"] == "needs_review"
    assert any(item["type"] == "alias" for item in result["facets"])
    assert any(item["type"] == "topic_interest" for item in result["facets"])
    assert any(item["type"] == "public_identity_candidate" for item in result["facets"])

    serialized = json.dumps(result, ensure_ascii=False)
    assert "13800138000" not in serialized
    assert "linzhou@example.test" not in serialized
    assert "110101199001011234" not in serialized
    assert "abcdef1234567890abcdef1234567890" not in serialized
    assert "PRIVATE_SENTINEL_TOKEN_BETA" not in serialized
    assert "[redacted-phone]" in serialized
    assert "[redacted-email]" in serialized
    assert "[redacted-id]" in serialized
    assert "[redacted-token]" in serialized


@pytest.mark.asyncio
async def test_build_group_member_profile_report_keeps_public_candidates_unaccepted() -> None:
    service = _ProfileWxbotAgentToolService()

    result = await service.build_group_member_profile_report(
        _make_session(),
        {
            "query": "LinZhou",
            "external_candidates": [
                {
                    "platform": "github",
                    "display_name": "FictionalCoder",
                    "url": "https://code.example.test/users/fictional-coder",
                    "public_summary": "LinZhou ExampleCrawler Python developer",
                },
                {
                    "platform": "blog",
                    "display_name": "示例开发者-LinZhou",
                    "url": "https://profile.example.test/linzhou",
                    "public_summary": "same public display name only",
                },
            ],
        },
    )

    statuses = {item["binding_status"] for item in result["external_candidates"]}
    assert statuses <= {"candidate", "needs_human_review"}
    assert "matched" not in statuses
    assert all(item["binding_status"] != "accepted" for item in result["external_candidates"])
    assert result["review"]["binding_policy"].startswith("公开搜索候选只能作为")


@pytest.mark.asyncio
async def test_build_group_member_profile_report_name_only_match_is_not_strong_binding() -> None:
    service = _ProfileWxbotAgentToolService()

    result = await service.build_group_member_profile_report(
        _make_session(),
        {
            "display_name": "示例开发者-LinZhou",
            "external_candidates": [
                {
                    "platform": "website",
                    "display_name": "示例开发者-LinZhou",
                    "url": "https://profile.example.test/name-only",
                    "public_summary": "公开名称相同，但没有群内项目或本人确认信号",
                },
            ],
        },
    )

    candidate = result["external_candidates"][0]
    assert candidate["confidence"] <= 0.45
    assert candidate["binding_status"] == "candidate"
    assert "exact_display_name" in candidate["match_signals"]


def test_wxbot_agent_tool_builders_can_be_split_by_plugin() -> None:
    service = _TestWxbotAgentToolService()
    full_tools = build_wxbot_group_agent_tools(service)
    full = {item.name for item in full_tools}
    split = (
        {item.name for item in build_wxbot_core_agent_tools(service)}
        | {item.name for item in build_wxbot_credits_agent_tools(service)}
        | {item.name for item in build_wxbot_moderation_agent_tools(service)}
        | {item.name for item in build_wxbot_repeater_agent_tools(service)}
    )

    assert len(full) == 17
    assert full == split
    assert "get_group_member_avatar" in split
    assert "get_group_credits_status" in split
    assert "get_group_recent_moderation_events" in split
    assert "get_group_repeater_status" in split
    assert "research_group_messages" in split
    assert "build_group_member_profile_report" in split
    assert all(item.metadata["channels"] == ["wechat"] for item in full_tools)
    assert all(item.metadata["session_kinds"] == ["group"] for item in full_tools)


def test_wxbot_plugin_status_agent_tool_builders_have_dedicated_scope() -> None:
    service = _TestWxbotAgentToolService()
    full = build_wxbot_group_plugin_status_agent_tools(service)
    split = (
        {item.name for item in build_wxbot_core_plugin_status_agent_tools(service)}
        | {item.name for item in build_wxbot_credits_plugin_status_agent_tools(service)}
        | {item.name for item in build_wxbot_moderation_plugin_status_agent_tools(service)}
        | {item.name for item in build_wxbot_repeater_plugin_status_agent_tools(service)}
    )

    assert {item.scope for item in full} == {"group_plugin_status"}
    assert all(item.metadata["channels"] == ["wechat"] for item in full)
    assert all(item.metadata["session_kinds"] == ["group"] for item in full)
    assert {item.name for item in full} == split
    assert split == {
        "get_group_reply_policy",
        "get_group_credits_status",
        "get_group_credits_member",
        "get_group_moderation_status",
        "get_group_repeater_status",
        "get_group_welcome_status",
        "get_group_report_status",
        "get_group_credits_leaderboard",
        "get_group_recent_moderation_events",
    }
