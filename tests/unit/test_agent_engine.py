from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest

from app.agent.engine import AgentCapabilityEngine
from app.agent.registry import AgentToolDefinition, AgentToolRegistry
from app.billing import BillingCapture, BillingCoordinator, BillingQuote, BillingReservation
from app.billing.models import BillingResource, BillingSubject
from app.common.config import Settings as RuntimeSettings
from app.common.context import clear_context, set_trace_id
from app.common.types import (
    Channel,
    ChatResponse,
    ChatUsage,
    Role,
    RouteType,
    Session,
    ToolCall,
    Turn,
)

from ._fake_llm import make_preprocessed


def Settings(**kwargs: object) -> RuntimeSettings:
    """Build a pure engine-test configuration with policy bypass explicit."""

    values = dict(kwargs)
    values.setdefault("agent_tools_require_explicit_policy", False)
    return RuntimeSettings(**values)  # type: ignore[arg-type]


def _strict_allow_tool_owner_gate(
    *,
    expected_owners: frozenset[str] = frozenset({"test"}),
    expected_tenant_id: str = "demo",
    expected_session_id: str = "room@chatroom",
) -> Callable[[str, Session], Awaitable[bool]]:
    """Allow a test-owned tool only for the exact expected execution scope."""

    async def _gate(owner: str, session: Session) -> bool:
        assert owner in expected_owners
        assert session.tenant_id == expected_tenant_id
        assert session.session_id == expected_session_id
        return True

    return _gate


class _FakeWxbotToolService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def get_group_info(self, session, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append(("get_group_info", dict(arguments)))
        return {
            "session_id": session.session_id,
            "session_name": "测试群",
            "member_count": 3,
            "members_sample": [
                {"display_name": "张三", "wxid": "wxid_1"},
                {"display_name": "李四", "wxid": "wxid_2"},
            ],
        }

    async def list_group_members(self, session, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append(("list_group_members", dict(arguments)))
        return {
            "session_id": session.session_id,
            "session_name": "测试群",
            "total": 2,
            "members": [
                {"display_name": "张三", "wxid": "wxid_1"},
                {"display_name": "李四", "wxid": "wxid_2"},
            ],
        }

    async def get_group_member_avatar(self, session, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append(("get_group_member_avatar", dict(arguments)))
        return {
            "session_id": session.session_id,
            "session_name": "测试群",
            "display_name": "张三",
            "wxid": "wxid_1",
            "avatar_url": "http://127.0.0.1:5080/ext/roster/avatars/wxid_1",
            "avatar_cached": True,
        }

    async def search_group_messages(self, session, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append(("search_group_messages", dict(arguments)))
        return {
            "session_id": session.session_id,
            "session_name": "测试群",
            "query": str(arguments.get("query") or ""),
            "total": 1,
            "matched_senders": [{"display_name": "张三", "message_count": 1}],
            "messages": [
                {
                    "sender_name": "张三",
                    "sender_wxid": "wxid_1",
                    "text": "今天有人提到 draw 功能",
                    "timestamp": "2026-04-23 10:00:00",
                    "ts": 1,
                }
            ],
        }

    async def research_group_messages(self, session, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append(("research_group_messages", dict(arguments)))
        return {
            "session_id": session.session_id,
            "session_name": "测试群",
            "question": str(arguments.get("question") or ""),
            "found": True,
            "total": 1,
            "summary": "最近 24 小时内查到 1 条相关消息。",
            "messages": [
                {
                    "sender_name": "张三",
                    "sender_wxid": "wxid_1",
                    "text": "今天有人提到 draw 功能",
                    "timestamp": "2026-04-23 10:00:00",
                    "ts": 1,
                }
            ],
        }

    async def get_group_public_facts(self, session, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append(("get_group_public_facts", dict(arguments)))
        return {
            "session_id": session.session_id,
            "session_name": "测试群",
            "member_count": 3,
            "recent_message_count": 12,
            "active_member_count": 2,
            "top_speakers": [{"display_name": "张三", "wxid": "wxid_1", "message_count": 7}],
            "feature_labels": ["积分", "审核", "复读机"],
            "features": {
                "credits": {"enabled": True, "credit_name": "积分"},
                "moderation": {"enabled": True},
                "repeater": {"enabled": True},
            },
        }

    async def get_group_reply_policy(self, session, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append(("get_group_reply_policy", dict(arguments)))
        return {
            "session_id": session.session_id,
            "reply_mode": "contains",
            "mention_sender": True,
            "trigger_keywords": ["报价"],
        }

    async def get_group_credits_status(self, session, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append(("get_group_credits_status", dict(arguments)))
        return {
            "session_id": session.session_id,
            "enabled": True,
            "credit_name": "积分",
            "summary": {"member_count": 2},
            "top_members": [{"display_name": "张三", "credits": 120}],
        }

    async def get_group_credits_member(self, session, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append(("get_group_credits_member", dict(arguments)))
        return {
            "session_id": session.session_id,
            "user_id": "wxid_1",
            "display_name": "张三",
            "credits": 120,
            "rank": 1,
        }

    async def get_group_moderation_status(self, session, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append(("get_group_moderation_status", dict(arguments)))
        return {
            "session_id": session.session_id,
            "enabled": True,
            "keyword_count": 2,
            "recent_events": [],
        }

    async def get_group_repeater_status(self, session, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append(("get_group_repeater_status", dict(arguments)))
        return {
            "session_id": session.session_id,
            "enabled": True,
            "cooldown_seconds": 300,
            "recent_events": [],
        }

    async def get_group_welcome_status(self, session, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append(("get_group_welcome_status", dict(arguments)))
        return {
            "session_id": session.session_id,
            "enabled": True,
            "mention": True,
        }

    async def get_group_report_status(self, session, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append(("get_group_report_status", dict(arguments)))
        return {
            "session_id": session.session_id,
            "daily_enabled": True,
            "monthly_enabled": False,
        }

    async def get_group_credits_leaderboard(self, session, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append(("get_group_credits_leaderboard", dict(arguments)))
        return {
            "session_id": session.session_id,
            "count": 2,
            "items": [{"display_name": "张三", "credits": 120}],
        }

    async def get_group_recent_moderation_events(self, session, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append(("get_group_recent_moderation_events", dict(arguments)))
        return {
            "session_id": session.session_id,
            "count": 1,
            "items": [{"sender_name": "张三", "matched_keyword_list": ["代言"]}],
        }

    async def get_group_activity_ranking(self, session, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append(("get_group_activity_ranking", dict(arguments)))
        return {
            "session_id": session.session_id,
            "active_member_count": 2,
            "items": [{"display_name": "张三", "message_count": 7}],
        }


class _CapturingAgentLLM:
    def __init__(self) -> None:
        self.requests = []
        self._round = 0

    async def chat(self, request):
        self.requests.append(request)
        self._round += 1
        if self._round == 1:
            return ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="list_group_members",
                        arguments={"limit": 5},
                    )
                ],
                model="fake-chat",
                finish_reason="tool_use",
                usage=ChatUsage(input_tokens=20, output_tokens=5),
                latency_ms=1,
            )
        return ChatResponse(
            content="这个群现在主要有张三、李四这些成员。",
            tool_calls=[],
            model="fake-chat",
            finish_reason="stop",
            usage=ChatUsage(input_tokens=15, output_tokens=8),
            latency_ms=1,
        )


class _CountAgentLLM:
    def __init__(self) -> None:
        self.requests = []
        self._round = 0

    async def chat(self, request):
        self.requests.append(request)
        self._round += 1
        if self._round == 1:
            return ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_count_1",
                        name="get_group_info",
                        arguments={},
                    )
                ],
                model="fake-chat",
                finish_reason="tool_use",
                usage=ChatUsage(input_tokens=18, output_tokens=4),
                latency_ms=1,
            )
        return ChatResponse(
            content="这个群现在有 3 个人。",
            tool_calls=[],
            model="fake-chat",
            finish_reason="stop",
            usage=ChatUsage(input_tokens=10, output_tokens=6),
            latency_ms=1,
        )


class _FakeAgentStore:
    def __init__(self, *, enabled: bool = True, effective_tools: list[str] | None = None) -> None:
        self.enabled = enabled
        self.effective_tools = effective_tools
        self.audit_rows: list[dict[str, object]] = []

    async def get_session_policy(
        self,
        tenant_id: str,
        session_id: str,
        *,
        scope: str = "group_info",
        available_tools: list[str] | None = None,
    ) -> dict[str, object]:
        available = list(available_tools or [])
        effective = (
            list(self.effective_tools)
            if self.effective_tools is not None
            else available
        )
        return {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "scope": scope,
            "enabled": self.enabled,
            "allowed_tools": [] if self.effective_tools is None else list(self.effective_tools),
            "available_tools": available,
            "effective_tools": effective,
            "inherits_default_tools": self.effective_tools is None,
            "denial_reason": "" if self.enabled else "policy_disabled",
        }

    async def create_tool_audit(self, **kwargs) -> int:
        self.audit_rows.append(dict(kwargs))
        return len(self.audit_rows)


class _FakeBillingProvider:
    name = "credits"

    def __init__(self, amount: int = 2) -> None:
        self.amount = amount
        self.reservations: list[BillingReservation] = []
        self.captures: list[BillingReservation] = []
        self.releases: list[BillingReservation] = []

    async def quote(self, subject: BillingSubject, resource: BillingResource) -> BillingQuote:
        return BillingQuote(
            provider=self.name,
            subject=subject,
            resource=resource,
            amount=self.amount,
            currency="积分",
        )

    async def reserve(self, subject: BillingSubject, resource: BillingResource) -> BillingReservation:
        reservation = BillingReservation(
            provider=self.name,
            subject=subject,
            resource=resource,
            amount=self.amount,
            currency="积分",
            reservation_id=f"reservation-{len(self.reservations) + 1}",
        )
        self.reservations.append(reservation)
        return reservation

    async def capture(
        self,
        reservation: BillingReservation,
        *,
        amount: int | None = None,
    ) -> BillingCapture:
        self.captures.append(reservation)
        return BillingCapture(
            provider=self.name,
            subject=reservation.subject,
            resource=reservation.resource,
            amount=amount or reservation.amount,
            currency=reservation.currency,
        )

    async def release(self, reservation: BillingReservation) -> None:
        self.releases.append(reservation)


def _fake_billing(provider: _FakeBillingProvider | None = None) -> tuple[BillingCoordinator, _FakeBillingProvider]:
    billing = BillingCoordinator()
    provider = provider or _FakeBillingProvider()
    billing.register_provider(provider)
    return billing, provider


class _SearchAgentLLM:
    def __init__(self) -> None:
        self.requests = []
        self._round = 0

    async def chat(self, request):
        self.requests.append(request)
        self._round += 1
        if self._round == 1:
            return ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_search_1",
                        name="search_group_messages",
                        arguments={"query": "draw", "hours": 24, "limit": 5},
                    )
                ],
                model="fake-chat",
                finish_reason="tool_use",
                usage=ChatUsage(input_tokens=18, output_tokens=4),
                latency_ms=1,
            )
        return ChatResponse(
            content="刚才提到 draw 的是张三。",
            tool_calls=[],
            model="fake-chat",
            finish_reason="stop",
            usage=ChatUsage(input_tokens=12, output_tokens=6),
            latency_ms=1,
        )


class _FactsAgentLLM:
    def __init__(self) -> None:
        self.requests = []
        self._round = 0

    async def chat(self, request):
        self.requests.append(request)
        self._round += 1
        if self._round == 1:
            return ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_facts_1",
                        name="get_group_public_facts",
                        arguments={"hours": 72},
                    )
                ],
                model="fake-chat",
                finish_reason="tool_use",
                usage=ChatUsage(input_tokens=18, output_tokens=4),
                latency_ms=1,
            )
        return ChatResponse(
            content="这个群最近挺活跃，积分、审核和复读机都开着。",
            tool_calls=[],
            model="fake-chat",
            finish_reason="stop",
            usage=ChatUsage(input_tokens=12, output_tokens=6),
            latency_ms=1,
        )


class _RegistryAgentLLM:
    def __init__(self) -> None:
        self.requests = []
        self._round = 0

    async def chat(self, request):
        self.requests.append(request)
        self._round += 1
        if self._round == 1:
            return ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_registry_1",
                        name="registered_group_tool",
                        arguments={"topic": "积分"},
                    )
                ],
                model="fake-chat",
                finish_reason="tool_use",
                usage=ChatUsage(input_tokens=10, output_tokens=3),
                latency_ms=1,
            )
        return ChatResponse(
            content="注册表工具已执行。",
            tool_calls=[],
            model="fake-chat",
            finish_reason="stop",
            usage=ChatUsage(input_tokens=8, output_tokens=5),
            latency_ms=1,
        )


class _SuppressingToolAgentLLM:
    def __init__(self) -> None:
        self.requests = []
        self._round = 0

    async def chat(self, request):
        self.requests.append(request)
        self._round += 1
        if self._round == 1:
            return ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_suppress_1",
                        name="registered_suppressing_tool",
                        arguments={"topic": "地图"},
                    )
                ],
                model="fake-chat",
                finish_reason="tool_use",
                usage=ChatUsage(input_tokens=10, output_tokens=3),
                latency_ms=1,
            )
        return ChatResponse(
            content="",
            tool_calls=[],
            model="fake-chat",
            finish_reason="stop",
            usage=ChatUsage(input_tokens=8, output_tokens=0),
            latency_ms=1,
        )


class _ExhaustedToolRoundsLLM:
    def __init__(self) -> None:
        self.requests = []

    async def chat(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            return ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_exhausted_1",
                        name="get_group_info",
                        arguments={},
                    )
                ],
                model="fake-chat",
                finish_reason="tool_use",
                usage=ChatUsage(input_tokens=10, output_tokens=3),
                latency_ms=1,
            )
        return ChatResponse(
            content="这个群现在有 3 个人。",
            tool_calls=[],
            model="fake-chat",
            finish_reason="stop",
            usage=ChatUsage(input_tokens=8, output_tokens=5),
            latency_ms=1,
        )


class _MapSearchOnlyLLM:
    def __init__(self) -> None:
        self.requests = []

    async def chat(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            return ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_map_search_1",
                        name="amap_text_search",
                        arguments={"keywords": "长沙美食 五一广场", "city": "长沙", "limit": 5},
                    )
                ],
                model="fake-chat",
                finish_reason="tool_use",
                usage=ChatUsage(input_tokens=10, output_tokens=3),
                latency_ms=1,
            )
        return ChatResponse(
            content="应该不会走到这里",
            tool_calls=[],
            model="fake-chat",
            finish_reason="stop",
            usage=ChatUsage(input_tokens=8, output_tokens=5),
            latency_ms=1,
        )


class _AmapToolLLM:
    def __init__(self, tool_calls: list[ToolCall], final_text: str = "查好了。") -> None:
        self.requests = []
        self._tool_calls = tool_calls
        self._final_text = final_text

    async def chat(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            return ChatResponse(
                content="",
                tool_calls=self._tool_calls,
                model="fake-chat",
                finish_reason="tool_use",
                usage=ChatUsage(input_tokens=10, output_tokens=3),
                latency_ms=1,
            )
        return ChatResponse(
            content=self._final_text,
            tool_calls=[],
            model="fake-chat",
            finish_reason="stop",
            usage=ChatUsage(input_tokens=8, output_tokens=5),
            latency_ms=1,
        )


class _AddressPromptAmapLLM:
    def __init__(self) -> None:
        self.requests = []

    async def chat(self, request):
        self.requests.append(request)
        if len(self.requests) == 1:
            return ChatResponse(
                content="请提供具体地址或位置, 我再帮你查询。",
                tool_calls=[],
                model="fake-chat",
                finish_reason="stop",
                usage=ChatUsage(input_tokens=10, output_tokens=8),
                latency_ms=1,
            )
        return ChatResponse(
            content="查到了: 群硕软件开发(武汉)有限公司在武汉市洪山区。",
            tool_calls=[],
            model="fake-chat",
            finish_reason="stop",
            usage=ChatUsage(input_tokens=12, output_tokens=10),
            latency_ms=1,
        )


class _PrivateFallbackLLM:
    def __init__(self) -> None:
        self.requests = []

    async def chat(self, request):
        self.requests.append(request)
        return ChatResponse(
            content="私聊里我拿不到群资料。",
            tool_calls=[],
            model="fake-chat",
            finish_reason="stop",
            usage=ChatUsage(input_tokens=9, output_tokens=8),
            latency_ms=1,
        )


class _NoToolAgentLLM:
    def __init__(self) -> None:
        self.requests = []

    async def chat(self, request):
        self.requests.append(request)
        return ChatResponse(
            content="不用工具也能回复。",
            tool_calls=[],
            model="fake-chat",
            finish_reason="stop",
            usage=ChatUsage(input_tokens=9, output_tokens=8),
            latency_ms=1,
        )


class _ToolErrorAgentLLM:
    def __init__(self) -> None:
        self.requests = []
        self._round = 0

    async def chat(self, request):
        self.requests.append(request)
        self._round += 1
        if self._round == 1:
            return ChatResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_error_1",
                        name="get_group_info",
                        arguments={},
                    )
                ],
                model="fake-chat",
                finish_reason="tool_use",
                usage=ChatUsage(input_tokens=12, output_tokens=3),
                latency_ms=1,
            )
        return ChatResponse(
            content="当前群资料暂时取不到，你稍后再试。",
            tool_calls=[],
            model="fake-chat",
            finish_reason="stop",
            usage=ChatUsage(input_tokens=10, output_tokens=7),
            latency_ms=1,
        )


class _FailingWxbotToolService(_FakeWxbotToolService):
    async def get_group_info(self, session, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append(("get_group_info", dict(arguments)))
        raise RuntimeError("sdk roster unavailable")


@pytest.mark.asyncio
async def test_agent_capability_executes_group_tools_and_returns_final_reply() -> None:
    llm = _CapturingAgentLLM()
    tools = _FakeWxbotToolService()
    settings = Settings(customer_service_prompt_enabled=False)
    engine = AgentCapabilityEngine(llm, settings=settings, wxbot_tools=tools)
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="@zzz 这个群有哪些人",
            trace_id="trace-agent-current",
            metadata={
                "sender_name": "小石",
                "sender_wxid": "wxid_current",
                "sender_is_group_admin": True,
                "mentioned_me": True,
                "cleaned_content": "这个群有哪些人",
            },
        ),
    ]

    set_trace_id("trace-agent-current")
    try:
        result = await engine.answer(
            make_preprocessed("这个群有哪些人"),
            session,
            {"agent_tool_scope": "group_info"},
        )
    finally:
        clear_context()

    assert result.route.value == "agent"
    assert result.reply_text == "这个群现在主要有张三、李四这些成员。"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "list_group_members"
    assert result.tool_calls[0].result["total"] == 2
    assert tools.calls == [("list_group_members", {"limit": 5})]
    assert len(llm.requests) == 2
    assert {tool.name for tool in llm.requests[0].tools} == {
        "get_group_info",
        "list_group_members",
        "get_group_member_avatar",
        "search_group_messages",
        "research_group_messages",
        "get_group_public_facts",
        "get_group_reply_policy",
        "get_group_credits_status",
        "get_group_credits_member",
        "get_group_moderation_status",
        "get_group_repeater_status",
        "get_group_welcome_status",
        "get_group_report_status",
        "get_group_credits_leaderboard",
        "get_group_recent_moderation_events",
        "get_group_activity_ranking",
    }
    assert llm.requests[0].messages[-1].content == (
        "当前发言人[小石]（明确 @ 了你；消息里的机器人称呼指你本人）：这个群有哪些人"
    )
    assert "你当前处于群聊/频道信息查询 Agent 模式" in (llm.requests[0].system or "")


@pytest.mark.asyncio
async def test_agent_capability_hides_privacy_sensitive_tools_from_regular_member() -> None:
    engine = AgentCapabilityEngine(
        _NoToolAgentLLM(),
        settings=Settings(customer_service_prompt_enabled=False),
        wxbot_tools=_FakeWxbotToolService(),
    )
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="@zzz 群里有什么信息",
            metadata={
                "sender_name": "普通成员",
                "sender_wxid": "wxid_member",
                "mentioned_me": True,
            },
        )
    ]

    tools, _policy = await engine._available_tools(
        session,
        {"agent_tool_scope": "group_info"},
        make_preprocessed("群里有什么信息"),
    )

    assert "get_group_info" in tools
    assert "get_group_public_facts" in tools
    assert "list_group_members" not in tools
    assert "get_group_member_avatar" not in tools
    assert "search_group_messages" not in tools
    assert "research_group_messages" not in tools
    assert "build_group_member_profile_report" not in tools


def test_group_admin_cannot_bypass_disabled_group_file_master_switch() -> None:
    definition = AgentToolDefinition(
        scope="file_analysis",
        name="generate_text_file",
        description="generate file",
        parameters={"type": "object", "properties": {}},
        handler=lambda *_args, **_kwargs: None,
        metadata={
            "channels": ["wechat"],
            "session_kinds": ["group", "private"],
            "required_group_role": "admin",
            "requires_group_file_send": True,
        },
    )
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_admin",
        channel=Channel.WECHAT,
        metadata={"session_kind": "group", "group_file_send_enabled": False},
        turns=[
            Turn(
                session_id="room@chatroom",
                role=Role.USER,
                content="生成文件发我",
                metadata={"sender_is_group_admin": True},
            )
        ],
    )

    assert AgentCapabilityEngine._definition_matches_session(definition, session) is False
    session.metadata["group_file_send_enabled"] = True
    assert AgentCapabilityEngine._definition_matches_session(definition, session) is True


@pytest.mark.asyncio
async def test_agent_capability_can_answer_group_member_count_queries() -> None:
    llm = _CountAgentLLM()
    tools = _FakeWxbotToolService()
    settings = Settings(customer_service_prompt_enabled=False)
    engine = AgentCapabilityEngine(llm, settings=settings, wxbot_tools=tools)
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="@zzz 群里有多少人",
            trace_id="trace-agent-count",
            metadata={
                "sender_name": "小石",
                "sender_wxid": "wxid_current",
                "mentioned_me": True,
                "cleaned_content": "群里有多少人",
            },
        ),
    ]

    set_trace_id("trace-agent-count")
    try:
        result = await engine.answer(
            make_preprocessed("群里有多少人"),
            session,
            {
                "agent_tool_scope": "group_info",
                "_llm_model_tier": "tier-3",
                "_llm_temperature": 0.85,
            },
        )
    finally:
        clear_context()

    assert result.reply_text == "这个群现在有 3 个人。"
    assert result.tool_calls[0].name == "get_group_info"
    assert result.tool_calls[0].result["member_count"] == 3
    assert tools.calls == [("get_group_info", {})]
    assert llm.requests[0].messages[-1].content == (
        "当前发言人[小石]（明确 @ 了你；消息里的机器人称呼指你本人）：群里有多少人"
    )


@pytest.mark.asyncio
async def test_agent_capability_falls_back_to_plain_chat_for_private_session() -> None:
    llm = _PrivateFallbackLLM()
    tools = _FakeWxbotToolService()
    settings = Settings(customer_service_prompt_enabled=False)
    engine = AgentCapabilityEngine(llm, settings=settings, wxbot_tools=tools)
    session = Session(
        session_id="wxid_private_1",
        tenant_id="demo",
        user_id="wxid_private_1",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="群里有多少人",
            trace_id="trace-agent-private",
            metadata={"sender_name": "小石", "cleaned_content": "群里有多少人"},
        ),
    ]

    set_trace_id("trace-agent-private")
    try:
        result = await engine.answer(
            make_preprocessed("群里有多少人"),
            session,
            {
                "agent_tool_scope": "group_info",
                "_llm_model_tier": "tier-3",
                "_llm_temperature": 0.85,
            },
        )
    finally:
        clear_context()

    assert result.reply_text == "私聊里我拿不到群资料。"
    assert result.tool_calls == []
    assert tools.calls == []
    assert llm.requests[0].tools == []
    assert llm.requests[0].model_tier == "tier-3"
    assert llm.requests[0].temperature == 0.85


@pytest.mark.asyncio
async def test_agent_capability_can_search_recent_group_messages() -> None:
    llm = _SearchAgentLLM()
    tools = _FakeWxbotToolService()
    settings = Settings(customer_service_prompt_enabled=False)
    engine = AgentCapabilityEngine(llm, settings=settings, wxbot_tools=tools)
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="@zzz 刚才谁提到 draw",
            trace_id="trace-agent-search",
            metadata={
                "sender_name": "小石",
                "sender_wxid": "wxid_current",
                "sender_is_group_admin": True,
                "mentioned_me": True,
                "cleaned_content": "刚才谁提到 draw",
            },
        ),
    ]

    set_trace_id("trace-agent-search")
    try:
        result = await engine.answer(
            make_preprocessed("刚才谁提到 draw"),
            session,
            {"agent_tool_scope": "group_info"},
        )
    finally:
        clear_context()

    assert result.reply_text == "刚才提到 draw 的是张三。"
    assert result.tool_calls[0].name == "search_group_messages"
    assert result.tool_calls[0].result["messages"][0]["sender_name"] == "张三"
    assert tools.calls == [("search_group_messages", {"query": "draw", "hours": 24, "limit": 5})]


@pytest.mark.asyncio
async def test_agent_capability_can_read_group_public_facts() -> None:
    llm = _FactsAgentLLM()
    tools = _FakeWxbotToolService()
    settings = Settings(customer_service_prompt_enabled=False)
    engine = AgentCapabilityEngine(llm, settings=settings, wxbot_tools=tools)
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="@zzz 这个群开了哪些功能",
            trace_id="trace-agent-facts",
            metadata={
                "sender_name": "小石",
                "sender_wxid": "wxid_current",
                "mentioned_me": True,
                "cleaned_content": "这个群开了哪些功能",
            },
        ),
    ]

    set_trace_id("trace-agent-facts")
    try:
        result = await engine.answer(
            make_preprocessed("这个群开了哪些功能"),
            session,
            {"agent_tool_scope": "group_info"},
        )
    finally:
        clear_context()

    assert result.reply_text == "这个群最近挺活跃，积分、审核和复读机都开着。"
    assert result.tool_calls[0].name == "get_group_public_facts"
    assert result.tool_calls[0].result["feature_labels"] == ["积分", "审核", "复读机"]
    assert tools.calls == [("get_group_public_facts", {"hours": 72})]


@pytest.mark.asyncio
async def test_agent_capability_filters_tools_by_session_policy_and_audits_calls() -> None:
    llm = _SearchAgentLLM()
    tools = _FakeWxbotToolService()
    agent_store = _FakeAgentStore(effective_tools=["search_group_messages"])
    settings = Settings(customer_service_prompt_enabled=False)
    engine = AgentCapabilityEngine(
        llm,
        settings=settings,
        wxbot_tools=tools,
        agent_store=agent_store,
    )
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="@zzz 刚才谁提到 draw",
            trace_id="trace-agent-audit",
            metadata={
                "sender_name": "小石",
                "sender_wxid": "wxid_current",
                "sender_is_group_admin": True,
                "mentioned_me": True,
                "cleaned_content": "刚才谁提到 draw",
            },
        ),
    ]

    set_trace_id("trace-agent-audit")
    try:
        result = await engine.answer(
            make_preprocessed("刚才谁提到 draw"),
            session,
            {"agent_tool_scope": "group_info"},
        )
    finally:
        clear_context()

    assert [tool.name for tool in llm.requests[0].tools] == ["search_group_messages"]
    assert result.metadata["effective_tools"] == ["search_group_messages"]
    assert len(agent_store.audit_rows) == 1
    assert agent_store.audit_rows[0]["tool_name"] == "search_group_messages"
    assert agent_store.audit_rows[0]["final_reply_text"] == "刚才提到 draw 的是张三。"


@pytest.mark.asyncio
async def test_agent_capability_audits_tool_error_without_breaking_reply() -> None:
    llm = _ToolErrorAgentLLM()
    tools = _FailingWxbotToolService()
    agent_store = _FakeAgentStore()
    settings = Settings(customer_service_prompt_enabled=False)
    engine = AgentCapabilityEngine(
        llm,
        settings=settings,
        wxbot_tools=tools,
        agent_store=agent_store,
    )
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="@zzz 群里有多少人",
            trace_id="trace-agent-tool-error",
            metadata={
                "sender_name": "小石",
                "sender_wxid": "wxid_current",
                "mentioned_me": True,
                "cleaned_content": "群里有多少人",
            },
        ),
    ]

    set_trace_id("trace-agent-tool-error")
    try:
        result = await engine.answer(
            make_preprocessed("群里有多少人"),
            session,
            {"agent_tool_scope": "group_info"},
        )
    finally:
        clear_context()

    assert result.reply_text == "当前群资料暂时取不到，你稍后再试。"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "get_group_info"
    assert result.tool_calls[0].error == "sdk roster unavailable"
    assert tools.calls == [("get_group_info", {})]
    assert len(agent_store.audit_rows) == 1
    assert agent_store.audit_rows[0]["tool_name"] == "get_group_info"
    assert agent_store.audit_rows[0]["tool_error"] == "sdk roster unavailable"
    assert agent_store.audit_rows[0]["final_reply_text"] == "当前群资料暂时取不到，你稍后再试。"


@pytest.mark.asyncio
async def test_agent_capability_fails_closed_without_policy_store() -> None:
    llm = _CapturingAgentLLM()
    engine = AgentCapabilityEngine(
        llm,
        settings=RuntimeSettings(customer_service_prompt_enabled=False),
        wxbot_tools=_FakeWxbotToolService(),
    )
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )

    result = await engine.answer(
        make_preprocessed("这个群有哪些人"),
        session,
        {"agent_tool_scope": "group_info"},
    )

    assert result.reply_text == "当前群未启用群资料 Agent 查询。"
    assert result.metadata["agent_disabled"] is True
    assert llm.requests == []


@pytest.mark.asyncio
async def test_agent_capability_returns_disabled_message_when_group_agent_policy_off() -> None:
    llm = _CapturingAgentLLM()
    tools = _FakeWxbotToolService()
    agent_store = _FakeAgentStore(enabled=False, effective_tools=[])
    settings = Settings(customer_service_prompt_enabled=False)
    engine = AgentCapabilityEngine(
        llm,
        settings=settings,
        wxbot_tools=tools,
        agent_store=agent_store,
    )
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )

    result = await engine.answer(
        make_preprocessed("这个群有哪些人"),
        session,
        {"agent_tool_scope": "group_info"},
    )

    assert result.reply_text == "当前群未启用群资料 Agent 查询。"
    assert result.metadata["agent_disabled"] is True
    assert llm.requests == []


@pytest.mark.asyncio
async def test_agent_capability_returns_scope_specific_disabled_message() -> None:
    llm = _CapturingAgentLLM()
    tools = _FakeWxbotToolService()
    agent_store = _FakeAgentStore(enabled=False, effective_tools=[])
    settings = Settings(customer_service_prompt_enabled=False)
    engine = AgentCapabilityEngine(
        llm,
        settings=settings,
        wxbot_tools=tools,
        agent_store=agent_store,
    )
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )

    result = await engine.answer(
        make_preprocessed("这个群积分怎么配"),
        session,
        {"agent_tool_scope": "group_plugin_status"},
    )

    assert result.reply_text == "当前群未启用群插件状态 Agent 查询。"
    assert result.metadata["agent_tool_scope"] == "group_plugin_status"
    assert llm.requests == []


@pytest.mark.asyncio
async def test_agent_capability_uses_registered_agent_tools_when_available() -> None:
    llm = _RegistryAgentLLM()
    settings = Settings(customer_service_prompt_enabled=False)
    registry = AgentToolRegistry()
    captured_calls: list[dict[str, object]] = []

    async def _handler(session, arguments: dict[str, object]) -> dict[str, object]:
        captured_calls.append(dict(arguments))
        return {"topic": arguments.get("topic"), "ok": True}

    registry.register(
        AgentToolDefinition(
            scope="group_info",
            name="registered_group_tool",
            description="registry tool",
            parameters={
                "type": "object",
                "properties": {"topic": {"type": "string"}},
                "additionalProperties": False,
            },
            handler=_handler,
        ),
        owner="test",
    )
    engine = AgentCapabilityEngine(
        llm,
        settings=settings,
        agent_tool_registry=registry,
        tool_owner_gate=_strict_allow_tool_owner_gate(),
    )
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="@zzz 帮我查积分",
            trace_id="trace-agent-registry",
            metadata={
                "sender_name": "小石",
                "sender_wxid": "wxid_current",
                "mentioned_me": True,
                "cleaned_content": "帮我查积分",
            },
        ),
    ]

    set_trace_id("trace-agent-registry")
    try:
        result = await engine.answer(
            make_preprocessed("帮我查积分"),
            session,
            {"agent_tool_scope": "group_info"},
        )
    finally:
        clear_context()

    assert result.reply_text == "注册表工具已执行。"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "registered_group_tool"
    assert captured_calls == [{"topic": "积分"}]
    assert [tool.name for tool in llm.requests[0].tools] == ["registered_group_tool"]


@pytest.mark.parametrize(
    ("channel", "session_id", "metadata", "expected_tools"),
    [
        (Channel.WECHAT, "room@chatroom", {}, ["wechat_only_tool", "cross_channel_tool"]),
        (
            Channel.DISCORD,
            "discord-channel-1",
            {"session_kind": "group", "sender_name": "小石"},
            ["cross_channel_tool"],
        ),
        (
            Channel.FEISHU,
            "feishu-chat-1",
            {"session_kind": "group", "sender_name": "小石"},
            ["cross_channel_tool"],
        ),
        (
            Channel.WECHAT,
            "wxid-private-1",
            {"session_kind": "private", "sender_name": "小石"},
            [],
        ),
    ],
)
@pytest.mark.asyncio
async def test_agent_capability_filters_tools_by_channel_and_session_metadata(
    channel: Channel,
    session_id: str,
    metadata: dict[str, object],
    expected_tools: list[str],
) -> None:
    llm = _NoToolAgentLLM()
    registry = AgentToolRegistry()

    registry.register(
        AgentToolDefinition(
            scope="group_info",
            name="wechat_only_tool",
            description="wechat only",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=_noop_handler,
            metadata={"channels": ["wechat"], "session_kinds": ["group"]},
        ),
        owner="wxbot",
    )
    registry.register(
        AgentToolDefinition(
            scope="group_info",
            name="cross_channel_tool",
            description="cross channel",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=_noop_handler,
            metadata={"session_kinds": ["group"]},
        ),
        owner="test",
    )
    engine = AgentCapabilityEngine(
        llm,
        settings=Settings(customer_service_prompt_enabled=False),
        agent_tool_registry=registry,
        tool_owner_gate=_strict_allow_tool_owner_gate(
            expected_owners=frozenset({"test", "wxbot"}),
            expected_session_id=session_id,
        ),
    )
    session = Session(
        session_id=session_id,
        tenant_id="demo",
        user_id="user-1",
        channel=channel,
        metadata=metadata,
    )

    result = await engine.answer(
        make_preprocessed("群里有什么信息"),
        session,
        {"agent_tool_scope": "group_info"},
    )

    assert result.route == RouteType.AGENT
    assert [tool.name for tool in llm.requests[0].tools] == expected_tools


@pytest.mark.asyncio
async def test_agent_capability_keeps_descriptor_metadata_out_of_tool_schema() -> None:
    llm = _RegistryAgentLLM()
    settings = Settings(customer_service_prompt_enabled=False)
    registry = AgentToolRegistry()

    async def _handler(session, arguments: dict[str, object]) -> dict[str, object]:
        return {"topic": arguments.get("topic"), "ok": True}

    registry.register(
        AgentToolDefinition(
            scope="group_info",
            name="registered_group_tool",
            description="registry tool",
            parameters={
                "type": "object",
                "properties": {"topic": {"type": "string"}},
                "required": ["topic"],
                "additionalProperties": False,
            },
            handler=_handler,
            metadata={
                "channels": ["wechat"],
                "session_kinds": ["group"],
                "source_plugin": "test",
            },
            embed_text="embedding-only text",
            tree_text="tree-only text",
            required_params=["topic"],
            verb_type="query",
            scopes=["group_info"],
        ),
        owner="test",
    )
    engine = AgentCapabilityEngine(
        llm,
        settings=settings,
        agent_tool_registry=registry,
        tool_owner_gate=_strict_allow_tool_owner_gate(),
    )
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="@zzz 帮我查积分",
            trace_id="trace-agent-registry-metadata",
            metadata={
                "sender_name": "小石",
                "sender_wxid": "wxid_current",
                "mentioned_me": True,
                "cleaned_content": "帮我查积分",
            },
        ),
    ]

    result = await engine.answer(
        make_preprocessed("帮我查积分"),
        session,
        {"agent_tool_scope": "group_info"},
    )

    assert result.reply_text == "注册表工具已执行。"
    schema = llm.requests[0].tools[0]
    assert schema.name == "registered_group_tool"
    assert schema.description == "registry tool"
    assert set(schema.model_dump()) == {"name", "description", "parameters"}
    assert schema.parameters == {
        "type": "object",
        "properties": {"topic": {"type": "string"}},
        "required": ["topic"],
        "additionalProperties": False,
    }
    assert not hasattr(schema, "embed_text")
    assert not hasattr(schema, "tree_text")
    assert not hasattr(schema, "required_params")
    assert not hasattr(schema, "verb_type")
    assert not hasattr(schema, "scopes")
    assert not hasattr(schema, "channels")
    assert not hasattr(schema, "session_kinds")
    assert not hasattr(schema, "source_plugin")
    assert not hasattr(schema, "owner")


async def _noop_handler(session, arguments: dict[str, object]) -> dict[str, object]:
    return {"ok": True}


def _register_preselection_tool(
    registry: AgentToolRegistry,
    *,
    name: str,
    embed_text: str | None = None,
    tree_text: str | None = None,
    required_params: list[str] | None = None,
    verb_type: str | None = None,
) -> None:
    registry.register(
        AgentToolDefinition(
            scope="group_info",
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=_noop_handler,
            embed_text=embed_text,
            tree_text=tree_text,
            required_params=required_params,
            verb_type=verb_type,
        ),
        owner="test",
    )


def _preselection_session(text: str) -> Session:
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content=f"@zzz {text}",
            trace_id="trace-agent-preselection",
            metadata={"sender_name": "小石", "cleaned_content": text},
        )
    ]
    return session


@pytest.mark.asyncio
async def test_agent_capability_previews_effective_tools_without_invoking_llm() -> None:
    llm = _NoToolAgentLLM()
    registry = AgentToolRegistry()
    _register_preselection_tool(
        registry,
        name="credit_tool",
        embed_text="积分 查询",
        required_params=["积分"],
    )
    _register_preselection_tool(registry, name="moderation_tool", embed_text="审核 状态")
    engine = AgentCapabilityEngine(
        llm,
        settings=Settings(customer_service_prompt_enabled=False),
        agent_tool_registry=registry,
        tool_owner_gate=_strict_allow_tool_owner_gate(),
    )

    preview = await engine.preview_availability(
        make_preprocessed("帮我查积分"),
        _preselection_session("帮我查积分"),
        {"agent_tool_scope": "group_info"},
    )

    assert preview == {
        "effective_tool_count": 1,
        "policy_allowed": True,
        "denial_reason": "",
        "effective_tools": ["credit_tool"],
        "tool_preselection_verdict": "CLEAR",
    }
    assert llm.requests == []


@pytest.mark.asyncio
async def test_agent_capability_preview_reports_policy_denial_without_tools() -> None:
    llm = _NoToolAgentLLM()
    registry = AgentToolRegistry()
    _register_preselection_tool(registry, name="credit_tool", embed_text="积分")
    engine = AgentCapabilityEngine(
        llm,
        settings=Settings(customer_service_prompt_enabled=False),
        agent_store=_FakeAgentStore(enabled=False, effective_tools=[]),
        agent_tool_registry=registry,
        tool_owner_gate=_strict_allow_tool_owner_gate(),
    )

    preview = await engine.preview_availability(
        make_preprocessed("积分"),
        _preselection_session("积分"),
        {"agent_tool_scope": "group_info"},
    )

    assert preview == {
        "effective_tool_count": 0,
        "policy_allowed": False,
        "denial_reason": "policy_disabled",
        "effective_tools": [],
        "tool_preselection_verdict": "LOW",
    }
    assert llm.requests == []


@pytest.mark.asyncio
async def test_agent_capability_preselects_tool_with_clear_verdict() -> None:
    llm = _NoToolAgentLLM()
    registry = AgentToolRegistry()
    _register_preselection_tool(registry, name="credit_tool", embed_text="积分 查询", required_params=["积分"])
    _register_preselection_tool(registry, name="moderation_tool", embed_text="审核 状态")
    engine = AgentCapabilityEngine(
        llm,
        settings=Settings(customer_service_prompt_enabled=False),
        agent_tool_registry=registry,
        tool_owner_gate=_strict_allow_tool_owner_gate(),
    )

    result = await engine.answer(
        make_preprocessed("帮我查积分"),
        _preselection_session("帮我查积分"),
        {"agent_tool_scope": "group_info"},
    )

    assert [tool.name for tool in llm.requests[0].tools] == ["credit_tool"]
    assert result.metadata["tool_preselection_verdict"] == "CLEAR"
    assert result.metadata["tool_preselection_selected"] == ["credit_tool"]


@pytest.mark.asyncio
async def test_agent_capability_preselection_marks_ambiguous_when_multiple_matches() -> None:
    llm = _NoToolAgentLLM()
    registry = AgentToolRegistry()
    _register_preselection_tool(registry, name="credit_summary_tool", embed_text="积分")
    _register_preselection_tool(registry, name="credit_member_tool", tree_text="积分")
    engine = AgentCapabilityEngine(
        llm,
        settings=Settings(customer_service_prompt_enabled=False),
        agent_tool_registry=registry,
        tool_owner_gate=_strict_allow_tool_owner_gate(),
    )

    result = await engine.answer(
        make_preprocessed("积分"),
        _preselection_session("积分"),
        {"agent_tool_scope": "group_info"},
    )

    assert {tool.name for tool in llm.requests[0].tools} == {"credit_summary_tool", "credit_member_tool"}
    assert result.metadata["tool_preselection_verdict"] == "AMBIGUOUS"


@pytest.mark.asyncio
async def test_agent_capability_preselection_keeps_close_runner_up_when_margin_is_small() -> None:
    llm = _NoToolAgentLLM()
    registry = AgentToolRegistry()
    _register_preselection_tool(
        registry,
        name="credit_summary_tool",
        embed_text="积分",
        required_params=["积分"],
    )
    _register_preselection_tool(registry, name="credit_member_tool", tree_text="积分")
    _register_preselection_tool(registry, name="moderation_tool", embed_text="审核")
    engine = AgentCapabilityEngine(
        llm,
        settings=Settings(customer_service_prompt_enabled=False),
        agent_tool_registry=registry,
        tool_owner_gate=_strict_allow_tool_owner_gate(),
    )

    result = await engine.answer(
        make_preprocessed("积分"),
        _preselection_session("积分"),
        {"agent_tool_scope": "group_info"},
    )

    assert [tool.name for tool in llm.requests[0].tools] == [
        "credit_summary_tool",
        "credit_member_tool",
    ]
    assert result.metadata["tool_preselection_verdict"] == "AMBIGUOUS"
    assert result.metadata["tool_preselection_selected"] == [
        "credit_summary_tool",
        "credit_member_tool",
    ]
    assert result.metadata["tool_preselection_scores"] == {
        "credit_summary_tool": 3,
        "credit_member_tool": 2,
        "moderation_tool": 0,
    }


@pytest.mark.asyncio
async def test_agent_capability_preselection_marks_insufficient_when_weak_match() -> None:
    llm = _NoToolAgentLLM()
    registry = AgentToolRegistry()
    _register_preselection_tool(registry, name="query_tool", verb_type="查")
    _register_preselection_tool(registry, name="status_tool", embed_text="状态")
    engine = AgentCapabilityEngine(
        llm,
        settings=Settings(customer_service_prompt_enabled=False),
        agent_tool_registry=registry,
        tool_owner_gate=_strict_allow_tool_owner_gate(),
    )

    result = await engine.answer(
        make_preprocessed("查"),
        _preselection_session("查"),
        {"agent_tool_scope": "group_info"},
    )

    assert [tool.name for tool in llm.requests[0].tools] == ["query_tool", "status_tool"]
    assert result.metadata["tool_preselection_verdict"] == "INSUFFICIENT"
    assert result.metadata["tool_preselection_selected"] == ["query_tool", "status_tool"]
    assert result.metadata["tool_preselection_scores"] == {
        "query_tool": 1,
        "status_tool": 0,
    }


@pytest.mark.asyncio
async def test_agent_capability_preselection_marks_low_when_no_metadata() -> None:
    llm = _NoToolAgentLLM()
    registry = AgentToolRegistry()
    registry.register(
        AgentToolDefinition(
            scope="group_info",
            name="plain_tool",
            description="plain tool",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=_noop_handler,
        ),
        owner="test",
    )
    engine = AgentCapabilityEngine(
        llm,
        settings=Settings(customer_service_prompt_enabled=False),
        agent_tool_registry=registry,
        tool_owner_gate=_strict_allow_tool_owner_gate(),
    )

    result = await engine.answer(
        make_preprocessed("完全不相关"),
        _preselection_session("完全不相关"),
        {"agent_tool_scope": "group_info"},
    )

    assert [tool.name for tool in llm.requests[0].tools] == ["plain_tool"]
    assert result.metadata["tool_preselection_verdict"] == "LOW"


@pytest.mark.asyncio
async def test_agent_capability_suppresses_final_reply_when_tool_self_enqueues() -> None:
    llm = _SuppressingToolAgentLLM()
    settings = Settings(customer_service_prompt_enabled=False)
    registry = AgentToolRegistry()
    agent_store = _FakeAgentStore()

    async def _handler(session, arguments: dict[str, object]) -> dict[str, object]:
        return {
            "topic": arguments.get("topic"),
            "self_enqueued_reply": True,
            "suppress_final_reply": True,
            "channel_reply_effects": [
                {
                    "type": "enqueue_channel_reply",
                    "owner": "wxbot",
                    "idempotency_key": "channel-reply:demo:trace-agent-suppress:1",
                    "payload": {
                        "channel": "wechat",
                        "session_id": session.session_id,
                        "body": {"type": "text", "text": "地图已发送"},
                    },
                }
            ],
        }

    registry.register(
        AgentToolDefinition(
            scope="group_personal_map",
            name="registered_suppressing_tool",
            description="registered suppressing tool",
            parameters={
                "type": "object",
                "properties": {"topic": {"type": "string"}},
                "additionalProperties": False,
            },
            handler=_handler,
        ),
        owner="test",
    )
    engine = AgentCapabilityEngine(
        llm,
        settings=settings,
        agent_store=agent_store,
        agent_tool_registry=registry,
        tool_owner_gate=_strict_allow_tool_owner_gate(),
    )
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="@zzz 生成高德地图",
            trace_id="trace-agent-suppress",
            metadata={
                "sender_name": "小石",
                "sender_wxid": "wxid_current",
                "mentioned_me": True,
                "cleaned_content": "生成高德地图",
            },
        ),
    ]

    set_trace_id("trace-agent-suppress")
    try:
        result = await engine.answer(
            make_preprocessed("生成高德地图"),
            session,
            {"agent_tool_scope": "group_personal_map"},
        )
    finally:
        clear_context()

    assert result.reply_text == ""
    assert result.metadata["suppress_final_reply"] is True
    assert result.metadata["suppress_outbound"] is True
    assert result.metadata["skip_assistant_turn"] is True
    assert result.metadata["channel_reply_effects"] == [
        {
            "type": "enqueue_channel_reply",
            "owner": "wxbot",
            "producer_owner": "test",
            "idempotency_key": "channel-reply:demo:trace-agent-suppress:1",
            "payload": {
                "channel": "wechat",
                "session_id": "room@chatroom",
                "body": {"type": "text", "text": "地图已发送"},
            },
        }
    ]
    tool_message = llm.requests[1].messages[-1]
    assert "channel_reply_effects" not in tool_message.content
    assert len(agent_store.audit_rows) == 1
    assert agent_store.audit_rows[0]["final_reply_text"] == ""


@pytest.mark.asyncio
async def test_agent_capability_runs_final_synthesis_after_tool_round_limit() -> None:
    llm = _ExhaustedToolRoundsLLM()
    tools = _FakeWxbotToolService()
    settings = Settings(customer_service_prompt_enabled=False)
    engine = AgentCapabilityEngine(
        llm,
        settings=settings,
        wxbot_tools=tools,
        max_tool_rounds=1,
    )
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="@zzz 群里有多少人",
            trace_id="trace-agent-exhausted",
            metadata={
                "sender_name": "小石",
                "sender_wxid": "wxid_current",
                "mentioned_me": True,
                "cleaned_content": "群里有多少人",
            },
        ),
    ]

    set_trace_id("trace-agent-exhausted")
    try:
        result = await engine.answer(
            make_preprocessed("群里有多少人"),
            session,
            {"agent_tool_scope": "group_info"},
        )
    finally:
        clear_context()

    assert result.reply_text == "这个群现在有 3 个人。"
    assert len(result.tool_calls) == 1
    assert len(llm.requests) == 2
    assert llm.requests[1].tools == []
    assert llm.requests[1].metadata["agent_final_synthesis"] is True


def _amap_session(text: str) -> Session:
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content=f"@zzz {text}",
            trace_id="trace-agent-amap-billing",
            metadata={
                "sender_name": "小石",
                "sender_wxid": "wxid_current",
                "mentioned_me": True,
                "cleaned_content": text,
            },
        ),
    ]
    return session


def _register_amap_tool(
    registry: AgentToolRegistry,
    name: str,
    result: dict[str, object],
) -> None:
    async def _handler(session, arguments: dict[str, object]) -> dict[str, object]:
        return dict(result)

    registry.register(
        AgentToolDefinition(
            scope="group_personal_map",
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}, "additionalProperties": True},
            handler=_handler,
        ),
        owner="test",
    )


@pytest.mark.asyncio
async def test_agent_capability_falls_back_to_amap_search_when_model_prompts_for_address() -> None:
    llm = _AddressPromptAmapLLM()
    settings = Settings(customer_service_prompt_enabled=False)
    registry = AgentToolRegistry()
    _register_amap_tool(
        registry,
        "amap_text_search",
        {
            "items": [
                {
                    "name": "群硕软件开发(武汉)有限公司",
                    "address": "湖北省武汉市洪山区",
                    "longitude": 114.4,
                    "latitude": 30.5,
                }
            ]
        },
    )
    billing, provider = _fake_billing()
    engine = AgentCapabilityEngine(
        llm,
        settings=settings,
        agent_tool_registry=registry,
        billing=billing,
        tool_owner_gate=_strict_allow_tool_owner_gate(),
    )

    result = await engine.answer(
        make_preprocessed("群硕软件开发(武汉)有限公司在哪里, 精确到楼栋"),
        _amap_session("群硕软件开发(武汉)有限公司在哪里, 精确到楼栋"),
        {"agent_tool_scope": "group_personal_map"},
    )

    assert result.reply_text == "查到了: 群硕软件开发(武汉)有限公司在武汉市洪山区。"
    assert [item.name for item in result.tool_calls] == ["amap_text_search"]
    assert result.tool_calls[0].arguments == {"keywords": "群硕软件开发(武汉)有限公司", "city": "武汉", "limit": 10}
    assert result.metadata["agent_billing_operation"] == "amap_search"
    assert [item.resource.operation for item in provider.reservations] == ["amap_search"]
    assert [item.resource.operation for item in provider.captures] == ["amap_search"]
    assert len(llm.requests) == 2
    assert llm.requests[1].metadata["agent_fallback_search"] is True


@pytest.mark.asyncio
async def test_agent_capability_bills_amap_search_once_for_multiple_search_tools() -> None:
    llm = _AmapToolLLM(
        [
            ToolCall(id="geo-1", name="amap_geo", arguments={"address": "中控信息大厦"}),
            ToolCall(id="around-1", name="amap_around_search", arguments={"keywords": "咖啡", "location": "1,2"}),
        ],
    )
    settings = Settings(customer_service_prompt_enabled=False)
    registry = AgentToolRegistry()
    _register_amap_tool(registry, "amap_geo", {"name": "中控信息大厦", "longitude": 1, "latitude": 2})
    _register_amap_tool(registry, "amap_around_search", {"items": [{"name": "咖啡店", "longitude": 1, "latitude": 2}]})
    billing, provider = _fake_billing()
    engine = AgentCapabilityEngine(
        llm,
        settings=settings,
        agent_tool_registry=registry,
        billing=billing,
        tool_owner_gate=_strict_allow_tool_owner_gate(),
    )

    result = await engine.answer(
        make_preprocessed("中控信息大厦附近有几家咖啡店"),
        _amap_session("中控信息大厦附近有几家咖啡店"),
        {"agent_tool_scope": "group_personal_map"},
    )

    assert result.metadata["agent_billing_operation"] == "amap_search"
    assert [item.resource.operation for item in provider.reservations] == ["amap_search"]
    assert [item.resource.operation for item in provider.captures] == ["amap_search"]
    assert provider.releases == []


@pytest.mark.asyncio
async def test_agent_capability_bills_new_amap_query_tools_as_search() -> None:
    llm = _AmapToolLLM(
        [
            ToolCall(id="tip-1", name="amap_input_tips", arguments={"keywords": "中控"}),
            ToolCall(id="detail-1", name="amap_place_detail", arguments={"poi_id": "B1"}),
        ],
    )
    settings = Settings(customer_service_prompt_enabled=False)
    registry = AgentToolRegistry()
    _register_amap_tool(registry, "amap_input_tips", {"tips": [{"name": "中控信息大厦"}]})
    _register_amap_tool(registry, "amap_place_detail", {"detail": {"name": "中控信息大厦"}})
    billing, provider = _fake_billing()
    engine = AgentCapabilityEngine(
        llm,
        settings=settings,
        agent_tool_registry=registry,
        billing=billing,
        tool_owner_gate=_strict_allow_tool_owner_gate(),
    )

    result = await engine.answer(
        make_preprocessed("中控信息大厦在哪里"),
        _amap_session("中控信息大厦在哪里"),
        {"agent_tool_scope": "group_personal_map"},
    )

    assert result.metadata["agent_billing_operation"] == "amap_search"
    assert [item.resource.operation for item in provider.reservations] == ["amap_search"]
    assert [item.resource.operation for item in provider.captures] == ["amap_search"]


@pytest.mark.asyncio
async def test_agent_capability_bills_successful_map_instead_of_search() -> None:
    llm = _AmapToolLLM(
        [
            ToolCall(id="search-1", name="amap_text_search", arguments={"keywords": "咖啡"}),
            ToolCall(
                id="map-1",
                name="amap_create_personal_map",
                arguments={"map_name": "咖啡地图", "points": [{"name": "咖啡店"}], "scene_type": 2},
            ),
        ],
    )
    settings = Settings(customer_service_prompt_enabled=False)
    registry = AgentToolRegistry()
    _register_amap_tool(registry, "amap_text_search", {"items": [{"name": "咖啡店", "longitude": 1, "latitude": 2}]})
    _register_amap_tool(
        registry,
        "amap_create_personal_map",
        {"map_name": "咖啡地图", "scene_type": 2, "point_count": 3, "qr_image_sent": True},
    )
    billing, provider = _fake_billing()
    engine = AgentCapabilityEngine(
        llm,
        settings=settings,
        agent_tool_registry=registry,
        billing=billing,
        tool_owner_gate=_strict_allow_tool_owner_gate(),
    )

    result = await engine.answer(
        make_preprocessed("中控信息大厦附近找 3 家咖啡店，生成高德地图"),
        _amap_session("中控信息大厦附近找 3 家咖啡店，生成高德地图"),
        {"agent_tool_scope": "group_personal_map"},
    )

    assert result.metadata["agent_billing_operation"] == "amap_map"
    assert [item.resource.operation for item in provider.reservations] == ["amap_map"]
    assert [item.resource.operation for item in provider.captures] == ["amap_map"]


@pytest.mark.asyncio
async def test_agent_capability_does_not_bill_search_when_map_generation_fails() -> None:
    llm = _AmapToolLLM(
        [
            ToolCall(id="search-1", name="amap_text_search", arguments={"keywords": "咖啡"}),
            ToolCall(
                id="map-1",
                name="amap_create_personal_map",
                arguments={"map_name": "咖啡地图", "points": [{"name": "咖啡店"}], "scene_type": 2},
            ),
        ],
        final_text="地图生成失败了。",
    )
    settings = Settings(customer_service_prompt_enabled=False)
    registry = AgentToolRegistry()
    _register_amap_tool(registry, "amap_text_search", {"items": [{"name": "咖啡店", "longitude": 1, "latitude": 2}]})
    _register_amap_tool(registry, "amap_create_personal_map", {"error": "upstream_error", "message": "生成失败"})
    billing, provider = _fake_billing()
    engine = AgentCapabilityEngine(
        llm,
        settings=settings,
        agent_tool_registry=registry,
        billing=billing,
        tool_owner_gate=_strict_allow_tool_owner_gate(),
    )

    result = await engine.answer(
        make_preprocessed("中控信息大厦附近找 3 家咖啡店，生成高德地图"),
        _amap_session("中控信息大厦附近找 3 家咖啡店，生成高德地图"),
        {"agent_tool_scope": "group_personal_map"},
    )

    assert result.metadata["agent_billing_operation"] == ""
    assert provider.reservations == []
    assert provider.captures == []
    assert provider.releases == []


@pytest.mark.asyncio
async def test_agent_capability_bills_route_map_for_complex_personal_map() -> None:
    llm = _AmapToolLLM(
        [
            ToolCall(
                id="map-1",
                name="amap_create_personal_map",
                arguments={
                    "map_name": "长沙一日游地图",
                    "points": [{"name": str(index)} for index in range(5)],
                    "scene_type": 1,
                },
            ),
        ],
    )
    settings = Settings(customer_service_prompt_enabled=False)
    registry = AgentToolRegistry()
    _register_amap_tool(
        registry,
        "amap_create_personal_map",
        {"map_name": "长沙一日游地图", "scene_type": 1, "point_count": 5, "qr_image_sent": True},
    )
    billing, provider = _fake_billing()
    engine = AgentCapabilityEngine(
        llm,
        settings=settings,
        agent_tool_registry=registry,
        billing=billing,
        tool_owner_gate=_strict_allow_tool_owner_gate(),
    )

    result = await engine.answer(
        make_preprocessed("帮我安排长沙一日游，生成高德地图"),
        _amap_session("帮我安排长沙一日游，生成高德地图"),
        {"agent_tool_scope": "group_personal_map"},
    )

    assert result.metadata["agent_billing_operation"] == "amap_route_map"
    assert [item.resource.operation for item in provider.reservations] == ["amap_route_map"]
    assert [item.resource.operation for item in provider.captures] == ["amap_route_map"]


@pytest.mark.asyncio
async def test_agent_capability_auto_creates_personal_map_when_search_exhausts_rounds() -> None:
    llm = _MapSearchOnlyLLM()
    settings = Settings(customer_service_prompt_enabled=False)
    registry = AgentToolRegistry()
    created_args: list[dict[str, object]] = []

    async def _search_handler(session, arguments: dict[str, object]) -> dict[str, object]:
        return {
            "items": [
                {
                    "poi_id": "food_1",
                    "name": "宴长沙(五一广场店)",
                    "longitude": 112.984519,
                    "latitude": 28.193257,
                    "address": "世茂环球金融中心5层",
                },
                {
                    "poi_id": "hotel_1",
                    "name": "长沙五一广场酒店",
                    "longitude": 112.985879,
                    "latitude": 28.180125,
                    "address": "芙蓉中路",
                },
                {
                    "poi_id": "food_2",
                    "name": "文和里大长沙美食城寨",
                    "longitude": 112.976544,
                    "latitude": 28.190204,
                    "address": "黄兴南路",
                },
            ]
        }

    async def _create_handler(session, arguments: dict[str, object]) -> dict[str, object]:
        created_args.append(dict(arguments))
        return {
            "qr_image_sent": True,
            "self_enqueued_reply": True,
            "suppress_final_reply": True,
        }

    registry.register(
        AgentToolDefinition(
            scope="group_personal_map",
            name="amap_text_search",
            description="search",
            parameters={"type": "object", "properties": {}, "additionalProperties": True},
            handler=_search_handler,
        ),
        owner="test",
    )
    registry.register(
        AgentToolDefinition(
            scope="group_personal_map",
            name="amap_create_personal_map",
            description="create",
            parameters={"type": "object", "properties": {}, "additionalProperties": True},
            handler=_create_handler,
        ),
        owner="test",
    )
    engine = AgentCapabilityEngine(
        llm,
        settings=settings,
        agent_tool_registry=registry,
        max_tool_rounds=1,
        tool_owner_gate=_strict_allow_tool_owner_gate(),
    )
    session = Session(
        session_id="room@chatroom",
        tenant_id="demo",
        user_id="wxid_current",
        channel=Channel.WECHAT,
    )
    session.turns = [
        Turn(
            session_id=session.session_id,
            role=Role.USER,
            content="@zzz 帮我规划长沙美食打卡路线，从五一广场出发，包含 2 个点，生成高德地图",
            trace_id="trace-agent-map-auto",
            metadata={
                "sender_name": "小石",
                "sender_wxid": "wxid_current",
                "mentioned_me": True,
                "cleaned_content": "帮我规划长沙美食打卡路线，从五一广场出发，包含 2 个点，生成高德地图",
            },
        ),
    ]

    set_trace_id("trace-agent-map-auto")
    try:
        result = await engine.answer(
            make_preprocessed("帮我规划长沙美食打卡路线，从五一广场出发，包含 2 个点，生成高德地图"),
            session,
            {"agent_tool_scope": "group_personal_map"},
        )
    finally:
        clear_context()

    assert result.reply_text == ""
    assert [item.name for item in result.tool_calls] == ["amap_text_search", "amap_create_personal_map"]
    assert result.metadata["suppress_outbound"] is True
    assert len(created_args) == 1
    points = created_args[0]["points"]
    assert [item["name"] for item in points] == ["宴长沙(五一广场店)", "文和里大长沙美食城寨"]


def test_agent_capability_sanitizes_unprofessional_final_text() -> None:
    assert AgentCapabilityEngine._sanitize_final_text("我靠，第一把寄了，地图没生成成。") == (
        "这次失败了，地图没有生成成功。"
    )
