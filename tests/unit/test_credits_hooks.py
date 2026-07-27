from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.common.types import (
    CapabilityResult,
    Channel,
    InboundEvent,
    Message,
    RouteDecision,
    RouteType,
    Session,
    ToolCall,
)
from app.orchestrator.effect_handlers import EffectDispatcher, EffectHandlerRegistry
from app.orchestrator.effects import (
    EFFECT_STATUS_DUPLICATE,
    EFFECT_STATUS_RECORDED,
    InMemoryEffectCommitter,
)
from app.orchestrator.pipeline import PipelineContext
from app.plugin.hooks import HookAbort, HookPoint, HookRunner
from plugins.credits.hooks import (
    CreditAuditEffectHandler,
    CreditAutoCheckinHook,
    CreditDeductionHook,
    CreditNaturalLanguageHook,
    CreditQueryCommandStep,
    CreditReserveStep,
    CreditSettlementEffectHandler,
    CreditSettlementHook,
    CreditSettleStep,
    build_credit_command_definitions,
)
from plugins.credits.store import CreditStore


class _FakeCreditStore:
    def __init__(
        self,
        *,
        enabled: bool = True,
        cost: int = 3,
        balance: int = 10,
        checkin_reward: int = 0,
        checkin_mode: int = 1,
        amap_search_cost: int = 2,
        amap_map_cost: int = 8,
        amap_route_map_cost: int = 12,
    ) -> None:
        self.enabled = enabled
        self.cost = cost
        self.balance = balance
        self.checkin_reward = checkin_reward
        self.checkin_mode = checkin_mode
        self.settings = SimpleNamespace(
            amap_search_credit_cost=amap_search_cost,
            amap_map_credit_cost=amap_map_cost,
            amap_route_map_credit_cost=amap_route_map_cost,
        )
        self.adjust_calls: list[dict[str, object]] = []
        self.checkin_calls: list[dict[str, object]] = []
        self.reserve_calls: list[dict[str, object]] = []
        self.capture_calls: list[dict[str, object]] = []
        self.release_calls: list[str] = []
        self.config_calls: list[dict[str, str]] = []
        self.member_detail_calls: list[dict[str, str]] = []
        self.members: list[dict[str, object]] = [
            {
                "user_id": "u1",
                "display_name": "张三",
                "credits": self.balance,
                "rank": 2,
                "checked_in_today": self.checkin_reward > 0,
                "today_reward": self.checkin_reward,
                "today_streak": 1 if self.checkin_reward > 0 else 0,
            },
            {
                "user_id": "wxid_jingluo",
                "display_name": "鲸落",
                "credits": 88,
                "rank": 3,
                "checked_in_today": True,
                "today_reward": 10,
                "today_streak": 4,
            },
            {
                "user_id": "wxid_laoye",
                "display_name": "老叶",
                "credits": 66,
                "rank": 4,
                "checked_in_today": False,
                "today_reward": 0,
                "today_streak": 0,
            },
        ]
        self.top_rows: list[dict[str, object]] = [
            {
                "user_id": "u2",
                "display_name": "李四",
                "credits": 42,
            },
            {
                "user_id": "u1",
                "display_name": "张三",
                "credits": self.balance,
            },
        ]

    async def get_config(self, tenant_id: str, session_id: str) -> dict:
        self.config_calls.append(
            {"tenant_id": tenant_id, "session_id": session_id}
        )
        labels = {
            1: "命令签到",
            2: "当前发言签到（静默）",
            3: "@ 机器人时静默签到",
        }
        return {
            "enabled": self.enabled,
            "credit_name": "积分",
            "cost_per_chat": self.cost,
            "checkin_mode": self.checkin_mode,
            "checkin_mode_label": labels.get(self.checkin_mode, f"模式 {self.checkin_mode}"),
            "amap_search_credit_cost": self.settings.amap_search_credit_cost,
            "amap_map_credit_cost": self.settings.amap_map_credit_cost,
            "amap_route_map_credit_cost": self.settings.amap_route_map_credit_cost,
        }

    async def get_balance(self, tenant_id: str, session_id: str, user_id: str) -> int:
        return self.balance

    async def peek_balance(self, tenant_id: str, session_id: str, user_id: str, display_name: str = "") -> int:
        return self.balance

    async def checkin(
        self,
        tenant_id: str,
        session_id: str,
        user_id: str,
        *,
        display_name: str = "",
    ) -> dict[str, object]:
        self.checkin_calls.append(
            {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "user_id": user_id,
                "display_name": display_name,
            }
        )
        if self.checkin_reward > 0:
            self.balance += self.checkin_reward
            return {
                "checked_in": True,
                "already_checked_in": False,
                "reward": self.checkin_reward,
                "balance": self.balance,
            }
        return {
            "checked_in": False,
            "already_checked_in": True,
            "reward": 0,
            "balance": self.balance,
        }

    async def adjust(
        self,
        tenant_id: str,
        session_id: str,
        user_id: str,
        delta: int,
        reason: str,
        actor: str = "",
        reference: str = "",
        display_name: str = "",
    ) -> int:
        self.adjust_calls.append(
            {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "user_id": user_id,
                "delta": delta,
                "reason": reason,
                "actor": actor,
                "reference": reference,
                "display_name": display_name,
            }
        )
        self.balance += delta
        return self.balance

    async def reserve_charge(
        self,
        tenant_id: str,
        session_id: str,
        user_id: str,
        amount: int,
        *,
        reason: str,
        reference: str = "",
        display_name: str = "",
        metadata: dict[str, object] | None = None,
        idempotency_key: str = "",
    ) -> dict[str, object]:
        self.reserve_calls.append(
            {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "user_id": user_id,
                "amount": amount,
                "reason": reason,
                "reference": reference,
                "display_name": display_name,
                "metadata": dict(metadata or {}),
                "idempotency_key": idempotency_key,
            }
        )
        if self.balance < amount:
            raise ValueError("余额不足")
        self.balance -= amount
        return {"reservation_id": f"reservation-{len(self.reserve_calls)}", "amount": amount}

    async def capture_reservation(
        self,
        reservation_id: str,
        *,
        amount: int | None = None,
        reference: str = "",
        display_name: str = "",
    ) -> dict[str, object]:
        self.capture_calls.append(
            {
                "reservation_id": reservation_id,
                "amount": amount,
                "reference": reference,
                "display_name": display_name,
            }
        )
        return {"reservation_id": reservation_id, "amount": amount or 0, "balance": self.balance}

    async def release_reservation(self, reservation_id: str) -> None:
        self.release_calls.append(reservation_id)

    async def get_member_detail(
        self,
        tenant_id: str,
        session_id: str,
        user_id: str,
        *,
        ledger_limit: int = 5,
    ) -> dict[str, object]:
        self.member_detail_calls.append(
            {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "user_id": user_id,
            }
        )
        matched_member = next(
            (item for item in self.members if str(item.get("user_id") or "") == user_id),
            None,
        )
        has_record = matched_member is not None
        if user_id == "u1":
            member = {
                "user_id": "u1",
                "display_name": "张三",
                "credits": self.balance,
                "rank": 2,
                "checked_in_today": self.checkin_reward > 0 or bool(self.checkin_calls),
                "today_reward": self.checkin_reward if self.checkin_calls else 0,
                "today_streak": 1 if self.checkin_calls else 0,
            }
            has_record = True
        elif matched_member is None:
            member = {
                "user_id": user_id,
                "display_name": "",
                "credits": self.balance,
                "rank": 2,
                "checked_in_today": self.checkin_reward > 0 or bool(self.checkin_calls),
                "today_reward": self.checkin_reward if self.checkin_calls else 0,
                "today_streak": 1 if self.checkin_calls else 0,
            }
        else:
            member = matched_member
        return {
            "user_id": user_id,
            "display_name": str(member.get("display_name") or ""),
            "credits": int(member.get("credits") or 0),
            "rank": member.get("rank"),
            "has_balance_record": has_record,
            "config": {
                "credit_name": "积分",
                "checkin_mode": self.checkin_mode,
                "checkin_mode_label": "静默签到",
            },
            "checkin_status": {
                "checked_in_today": bool(member.get("checked_in_today")),
                "today_reward": int(member.get("today_reward") or 0),
                "today_streak": int(member.get("today_streak") or 0),
                "next_reward": self.checkin_reward or 3,
                "checkin_mode_label": "静默签到",
            },
        }

    async def list_members(
        self,
        tenant_id: str,
        session_id: str,
        *,
        limit: int = 200,
        query: str = "",
    ) -> dict[str, object]:
        query_value = str(query or "").strip().lower()
        items = [
            item for item in self.members
            if not query_value
            or query_value in str(item.get("user_id") or "").lower()
            or query_value in str(item.get("display_name") or "").lower()
        ]
        return {"items": items[:limit], "count": len(items), "summary": {}}

    async def get_top(
        self,
        tenant_id: str,
        session_id: str,
        limit: int = 10,
    ) -> list[dict[str, object]]:
        return self.top_rows[:limit]


def _make_ctx(
    *,
    channel: Channel = Channel.WEB,
    session_id: str = "s1",
    external_conversation_id: str = "",
    content: str = "hello",
    metadata: dict[str, object] | None = None,
) -> PipelineContext:
    event = InboundEvent(
        message_id="m-1",
        tenant_id="demo",
        channel=channel,
        user_id="u1",
        session_id=session_id,
        external_conversation_id=external_conversation_id,
        message=Message(content=content),
        trace_id="trace-1",
        metadata=metadata or {},
    )
    session = Session(
        session_id=session_id,
        tenant_id="demo",
        user_id="u1",
        channel=channel,
        external_conversation_id=external_conversation_id,
    )
    return PipelineContext(event=event, trace_id="trace-1", session=session)


def _make_amap_ctx(content: str) -> PipelineContext:
    ctx = _make_ctx(
        channel=Channel.WECHAT,
        session_id="g1@chatroom",
        content=content,
        metadata={
            "sender_name": "张三",
            "wxbot_normalized_content": content,
        },
    )
    ctx.route = RouteDecision(
        type=RouteType.LLM,
        hints={"agent_tool_scope": "group_personal_map"},
    )
    ctx.extras["agent_tool_scope"] = "group_personal_map"
    return ctx


@pytest.mark.asyncio
async def test_credit_query_command_step_returns_canned_balance_result() -> None:
    store = _FakeCreditStore(cost=0, balance=7)
    step = CreditQueryCommandStep(store)
    ctx = _make_ctx(
        content="我还有多少积分",
        metadata={"wxbot_normalized_content": "我还有多少积分"},
    )
    ctx.route = RouteDecision(type=RouteType.LLM)

    result = await step.run(ctx)

    assert result.action == "stop"
    assert result.finalize is True
    assert result.skip_output_safety is True
    assert result.reason == "credit_balance_query"
    assert result.result is not None
    assert result.result.route == RouteType.CANNED
    assert "你当前有 7 积分。" in result.result.reply_text
    assert ctx.signals["credits"]["query"]["handled"] is True
    assert ctx.signals["credits"]["query"]["reason"] == "credit_balance_query"


@pytest.mark.asyncio
async def test_credit_query_command_step_handles_bare_my_credits_phrase() -> None:
    store = _FakeCreditStore(cost=0, balance=7)
    step = CreditQueryCommandStep(store)
    ctx = _make_ctx(
        content="@zzz 我的积分",
        metadata={
            "mentioned_me": True,
            "wxbot_normalized_content": "我的积分",
        },
    )
    ctx.route = RouteDecision(type=RouteType.LLM)

    result = await step.run(ctx)

    assert result.action == "stop"
    assert result.reason == "credit_balance_query"
    assert result.result is not None
    assert "你当前有 7 积分。" in result.result.reply_text


@pytest.mark.asyncio
async def test_credit_query_command_step_uses_external_managed_group_scope() -> None:
    store = _FakeCreditStore(cost=0, balance=420)
    step = CreditQueryCommandStep(store)
    external_session_id = "00000000000@chatroom"
    ctx = _make_ctx(
        channel=Channel.WECHAT,
        session_id="cx1:c:managed@chatroom",
        external_conversation_id=external_session_id,
        content="@zzz 我多少积分了",
        metadata={
            "mentioned_me": True,
            "wxbot_normalized_content": "我多少积分了",
            "sender_wxid": "u1",
        },
    )
    ctx.route = RouteDecision(type=RouteType.LLM)

    result = await step.run(ctx)

    assert result.action == "stop"
    assert result.reason == "credit_balance_query"
    assert result.result is not None
    assert "你当前有 420 积分。" in result.result.reply_text
    assert store.config_calls[-1]["session_id"] == external_session_id
    assert store.member_detail_calls[-1]["session_id"] == external_session_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "normalized"),
    [
        ("积分排名", "积分排名"),
        ("@zzz 积分排名", "积分排名"),
        ("查积分排名", "查积分排名"),
    ],
)
async def test_credit_query_command_step_routes_rank_phrases_to_leaderboard(
    content: str,
    normalized: str,
) -> None:
    store = _FakeCreditStore(cost=0, balance=7)
    step = CreditQueryCommandStep(store)
    ctx = _make_ctx(
        content=content,
        metadata={
            "mentioned_me": content.startswith("@"),
            "wxbot_normalized_content": normalized,
        },
    )
    ctx.route = RouteDecision(type=RouteType.LLM)

    result = await step.run(ctx)

    assert result.action == "stop"
    assert result.reason == "credits_command"
    assert result.result is not None
    assert "积分 榜 Top 10：" in result.result.reply_text
    assert "李四" in result.result.reply_text
    assert "发积分排名试试" not in result.result.reply_text


@pytest.mark.asyncio
async def test_credit_query_command_step_routes_rank_phrase_before_faq() -> None:
    store = _FakeCreditStore(cost=0, balance=7)
    step = CreditQueryCommandStep(store)
    ctx = _make_ctx(
        content="积分排名",
        metadata={"wxbot_normalized_content": "积分排名"},
    )
    ctx.route = RouteDecision(type=RouteType.FAQ)

    result = await step.run(ctx)

    assert result.action == "stop"
    assert result.reason == "credits_command"
    assert result.result is not None
    assert result.result.route == RouteType.CANNED
    assert "积分 榜 Top 10：" in result.result.reply_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "normalized"),
    [
        ("积分", "积分"),
        ("@zzz 积分", "积分"),
        ("我的积分", "我的积分"),
        ("@zzz 我的积分", "我的积分"),
        ("积分余额", "积分余额"),
        ("查积分", "查积分"),
    ],
)
async def test_credit_query_command_step_routes_balance_phrases(
    content: str,
    normalized: str,
) -> None:
    store = _FakeCreditStore(cost=0, balance=7)
    step = CreditQueryCommandStep(store)
    ctx = _make_ctx(
        content=content,
        metadata={
            "mentioned_me": content.startswith("@"),
            "wxbot_normalized_content": normalized,
        },
    )
    ctx.route = RouteDecision(type=RouteType.LLM)

    result = await step.run(ctx)

    assert result.action == "stop"
    assert result.reason == "credit_balance_query"
    assert result.result is not None
    assert "你当前有 7 积分。" in result.result.reply_text
    assert "发积分排名试试" not in result.result.reply_text


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["@zzz 我的积分", "@zzz 积分", "@zzz 查积分"])
async def test_credit_query_command_step_routes_raw_mentioned_balance_phrases(content: str) -> None:
    store = _FakeCreditStore(cost=0, balance=7)
    step = CreditQueryCommandStep(store)
    ctx = _make_ctx(
        content=content,
        metadata={"mentioned_me": True},
    )
    ctx.route = RouteDecision(type=RouteType.LLM)

    result = await step.run(ctx)

    assert result.action == "stop"
    assert result.reason == "credit_balance_query"
    assert result.result is not None
    assert "你当前有 7 积分。" in result.result.reply_text


@pytest.mark.asyncio
async def test_credit_reserve_step_reserves_chat_cost_and_sets_signal() -> None:
    store = _FakeCreditStore(cost=4, balance=12)
    step = CreditReserveStep(store)
    ctx = _make_ctx(metadata={"sender_name": "张三"})

    result = await step.run(ctx)

    assert result.reason == "reserved"
    assert len(result.effects) == 1
    assert result.effects[0].type == "reserve_credits"
    assert result.effects[0].owner == "credits"
    assert result.effects[0].payload["reservation_id"] == "reservation-1"
    assert result.effects[0].payload["amount"] == 4
    assert result.effects[0].idempotency_key == "credits:reserve:reservation-1"
    assert store.reserve_calls == [
        {
            "tenant_id": "demo",
            "session_id": "s1",
            "user_id": "u1",
            "amount": 4,
            "reason": "chat_cost",
            "reference": "trace-1",
            "display_name": "张三",
            "metadata": {"resource_kind": "chat", "resource_operation": "llm"},
            "idempotency_key": "chat:llm:trace-1",
        }
    ]
    assert ctx.signals["billing"]["reservation"] == {
        "reserved": True,
        "reservation_id": "reservation-1",
        "amount": 4,
        "reason": "reserved",
        "enabled": True,
        "credit_name": "积分",
        "cost_per_chat": 4,
    }


@pytest.mark.asyncio
async def test_credit_reserve_step_stops_when_balance_is_insufficient() -> None:
    store = _FakeCreditStore(cost=2, balance=0)
    step = CreditReserveStep(store)
    ctx = _make_ctx()

    result = await step.run(ctx)

    assert result.action == "stop"
    assert result.finalize is True
    assert result.reason == "insufficient_credits"
    assert result.result is not None
    assert result.result.route == RouteType.CANNED
    assert result.result.reply_text == "积分不足，请先签到。"
    assert store.reserve_calls == []
    assert ctx.signals["billing"]["reservation"]["reserved"] is False
    assert ctx.signals["billing"]["reservation"]["reason"] == "insufficient_credits"


@pytest.mark.asyncio
async def test_credit_settle_step_captures_successful_llm_result() -> None:
    store = _FakeCreditStore(cost=4, balance=12)
    reserve = CreditReserveStep(store)
    settle = CreditSettleStep(store)
    ctx = _make_ctx(metadata={"sender_name": "张三"})

    await reserve.run(ctx)
    ctx.result = CapabilityResult(route=RouteType.LLM, reply_text="ok")
    result = await settle.run(ctx)

    assert result.reason == "captured"
    assert len(result.effects) == 1
    assert result.effects[0].type == "capture_credits"
    assert result.effects[0].payload["reservation_id"] == "reservation-1"
    assert result.effects[0].payload["amount"] == 4
    assert result.effects[0].idempotency_key == "credits:capture_credits:reservation-1"
    assert store.capture_calls == [
        {
            "reservation_id": "reservation-1",
            "amount": 4,
            "reference": "trace-1",
            "display_name": "张三",
        }
    ]
    assert ctx.signals["billing"]["settlement"] == {
        "settled": True,
        "released": False,
        "reservation_id": "reservation-1",
        "amount": 4,
        "result_route": "llm",
        "reason": "captured",
    }


@pytest.mark.asyncio
async def test_credit_settle_step_can_defer_capture_to_effect_handler() -> None:
    store = _FakeCreditStore(cost=4, balance=12)
    reserve = CreditReserveStep(store)
    settle = CreditSettleStep(store, effect_handler_enabled=True)
    ctx = _make_ctx(metadata={"sender_name": "张三"})

    await reserve.run(ctx)
    ctx.result = CapabilityResult(route=RouteType.LLM, reply_text="ok")
    result = await settle.run(ctx)

    assert result.reason == "captured"
    assert len(result.effects) == 1
    effect = result.effects[0]
    assert effect.type == "capture_credits"
    assert effect.owner == "credits"
    assert effect.payload["reservation_id"] == "reservation-1"
    assert effect.payload["amount"] == 4
    assert store.capture_calls == []
    assert ctx.signals["billing"]["settlement"] == {
        "settled": True,
        "released": False,
        "reservation_id": "reservation-1",
        "amount": 4,
        "result_route": "llm",
        "reason": "captured",
        "settle_as_effect": True,
    }


@pytest.mark.asyncio
async def test_credit_settlement_effect_handler_captures_once_after_commit() -> None:
    store = _FakeCreditStore(cost=4, balance=12)
    reserve = CreditReserveStep(store)
    settle = CreditSettleStep(store, effect_handler_enabled=True)
    ctx = _make_ctx(metadata={"sender_name": "张三"})
    registry = EffectHandlerRegistry()
    registry.register("capture_credits", "credits", CreditSettlementEffectHandler(store))
    dispatcher = EffectDispatcher(registry, InMemoryEffectCommitter())

    await reserve.run(ctx)
    ctx.result = CapabilityResult(route=RouteType.LLM, reply_text="ok")
    step_result = await settle.run(ctx)
    first = await dispatcher.dispatch(step_result.effects[0], ctx)
    second = await dispatcher.dispatch(step_result.effects[0], ctx)

    assert first.status == EFFECT_STATUS_RECORDED
    assert second.status == EFFECT_STATUS_DUPLICATE
    assert store.capture_calls == [
        {
            "reservation_id": "reservation-1",
            "amount": 4,
            "reference": "trace-1",
            "display_name": "张三",
        }
    ]
    assert ctx.extras["_credits_deducted"] is True
    assert ctx.signals["effects"]["credits"][0]["status"] == "captured"


@pytest.mark.asyncio
async def test_credit_settle_step_releases_canned_result_reservation() -> None:
    store = _FakeCreditStore(cost=4, balance=12)
    reserve = CreditReserveStep(store)
    settle = CreditSettleStep(store)
    ctx = _make_ctx()

    await reserve.run(ctx)
    ctx.result = CapabilityResult(route=RouteType.CANNED, reply_text="busy")
    result = await settle.run(ctx)

    assert result.reason == "released"
    assert len(result.effects) == 1
    assert result.effects[0].type == "release_credits"
    assert result.effects[0].payload["reservation_id"] == "reservation-1"
    assert result.effects[0].idempotency_key == "credits:release_credits:reservation-1"
    assert store.release_calls == ["reservation-1"]
    assert store.capture_calls == []
    assert ctx.signals["billing"]["settlement"]["released"] is True
    assert ctx.signals["billing"]["settlement"]["settled"] is False


@pytest.mark.asyncio
async def test_credit_settle_step_can_defer_release_to_effect_handler() -> None:
    store = _FakeCreditStore(cost=4, balance=12)
    reserve = CreditReserveStep(store)
    settle = CreditSettleStep(store, effect_handler_enabled=True)
    ctx = _make_ctx()

    await reserve.run(ctx)
    ctx.result = CapabilityResult(route=RouteType.CANNED, reply_text="busy")
    result = await settle.run(ctx)

    assert result.reason == "released"
    assert len(result.effects) == 1
    assert result.effects[0].type == "release_credits"
    assert result.effects[0].payload["reservation_id"] == "reservation-1"
    assert store.release_calls == []
    assert ctx.signals["billing"]["settlement"]["released"] is True
    assert ctx.signals["billing"]["settlement"]["settle_as_effect"] is True


@pytest.mark.asyncio
async def test_credit_audit_effect_handler_records_auto_checkin_without_store_write() -> None:
    registry = EffectHandlerRegistry()
    registry.register("auto_checkin", "credits", CreditAuditEffectHandler())
    dispatcher = EffectDispatcher(registry, InMemoryEffectCommitter())
    store = _FakeCreditStore(cost=0, balance=12, checkin_reward=3, checkin_mode=2)
    step = CreditReserveStep(store)
    ctx = _make_ctx(
        session_id="g1@chatroom",
        content="hello",
        metadata={"wxbot_normalized_content": "hello"},
    )

    step_result = await step.run(ctx)
    result = await dispatcher.dispatch(step_result.effects[0], ctx)

    assert result.status == EFFECT_STATUS_RECORDED
    assert ctx.signals["effects"]["credits"][0]["type"] == "auto_checkin"
    assert ctx.signals["effects"]["credits"][0]["status"] == "audited"


@pytest.mark.asyncio
async def test_credit_settlement_deducts_after_successful_capability() -> None:
    store = _FakeCreditStore(cost=4, balance=12)
    before = CreditDeductionHook(store)
    after = CreditSettlementHook(store)
    ctx = _make_ctx()

    await before.run(ctx)
    ctx.result = CapabilityResult(route=RouteType.LLM, reply_text="ok")
    await after.run(ctx)

    assert store.reserve_calls == [
        {
            "tenant_id": "demo",
            "session_id": "s1",
            "user_id": "u1",
            "amount": 4,
            "reason": "chat_cost",
            "reference": "trace-1",
            "display_name": "",
            "metadata": {"resource_kind": "chat", "resource_operation": "llm"},
            "idempotency_key": "chat:llm:trace-1",
        }
    ]
    assert store.capture_calls == [
        {
            "reservation_id": "reservation-1",
            "amount": 4,
            "reference": "trace-1",
            "display_name": "",
        }
    ]
    assert store.adjust_calls == []


@pytest.mark.asyncio
async def test_credit_settlement_skips_canned_replies() -> None:
    store = _FakeCreditStore(cost=4, balance=12)
    before = CreditDeductionHook(store)
    after = CreditSettlementHook(store)
    ctx = _make_ctx()

    await before.run(ctx)
    ctx.result = CapabilityResult(route=RouteType.CANNED, reply_text="busy")
    await after.run(ctx)

    assert store.release_calls == ["reservation-1"]
    assert store.capture_calls == []
    assert store.adjust_calls == []


@pytest.mark.asyncio
async def test_credit_deduction_blocks_zero_balance_before_llm() -> None:
    store = _FakeCreditStore(cost=2, balance=0)
    hook = CreditDeductionHook(store)
    ctx = _make_ctx()

    with pytest.raises(HookAbort) as exc_info:
        await hook.run(ctx)

    assert exc_info.value.reason == "insufficient_credits"
    assert exc_info.value.reply_text == "积分不足，请先签到。"
    assert ctx.extras.get("_credits_cost") is None


@pytest.mark.asyncio
async def test_amap_search_deducts_search_cost_without_chat_cost() -> None:
    store = _FakeCreditStore(cost=9, balance=10)
    before = CreditDeductionHook(store)
    after = CreditSettlementHook(store)
    ctx = _make_amap_ctx("找一下上海人民广场附近 3 家咖啡店")

    await before.run(ctx)
    ctx.result = CapabilityResult(
        route=RouteType.LLM,
        reply_text="ok",
        tool_calls=[
            ToolCall(
                id="call-1",
                name="amap_text_search",
                arguments={"keywords": "人民广场 咖啡店", "city": "上海"},
                result={"pois": [{"name": "阿拉比卡咖啡"}]},
            )
        ],
        metadata={"agent_tool_scope": "group_personal_map"},
    )
    await after.run(ctx)

    assert ctx.extras["_credits_cost"] == 2
    assert ctx.extras["_credits_agent_billing"] == "amap"
    assert store.reserve_calls == []
    assert store.adjust_calls == []


@pytest.mark.asyncio
async def test_amap_agent_billing_uses_credit_config_before_chat_or_env_cost() -> None:
    store = _FakeCreditStore(cost=9, balance=10, amap_search_cost=4)
    before = CreditDeductionHook(store)
    after = CreditSettlementHook(store)
    ctx = _make_amap_ctx("找一下上海人民广场附近 3 家咖啡店")

    await before.run(ctx)
    ctx.result = CapabilityResult(
        route=RouteType.LLM,
        reply_text="ok",
        tool_calls=[
            ToolCall(
                id="call-1",
                name="amap_text_search",
                arguments={"keywords": "人民广场 咖啡店", "city": "上海"},
                result={"pois": [{"name": "阿拉比卡咖啡"}]},
            )
        ],
        metadata={"agent_tool_scope": "group_personal_map"},
    )
    await after.run(ctx)

    assert ctx.extras["_credits_cost"] == 4
    assert ctx.extras["_credits_agent_billing"] == "amap"
    assert store.reserve_calls == []
    assert store.adjust_calls == []


@pytest.mark.asyncio
async def test_amap_search_blocks_when_balance_below_search_cost() -> None:
    store = _FakeCreditStore(cost=0, balance=1)
    hook = CreditDeductionHook(store)
    ctx = _make_amap_ctx("找一下上海人民广场附近 3 家咖啡店")

    with pytest.raises(HookAbort) as exc_info:
        await hook.run(ctx)

    assert exc_info.value.reason == "insufficient_credits"
    assert store.adjust_calls == []


@pytest.mark.asyncio
async def test_amap_map_generation_deducts_map_cost_without_chat_cost() -> None:
    store = _FakeCreditStore(cost=9, balance=20)
    before = CreditDeductionHook(store)
    after = CreditSettlementHook(store)
    ctx = _make_amap_ctx("找一下上海人民广场附近 3 家咖啡店，生成高德地图")

    await before.run(ctx)
    ctx.result = CapabilityResult(
        route=RouteType.LLM,
        reply_text="ok",
        tool_calls=[
            ToolCall(
                id="call-1",
                name="amap_create_personal_map",
                arguments={"map_name": "人民广场咖啡店地图", "scene_type": 2},
                result={
                    "map_name": "人民广场咖啡店地图",
                    "scene_type": 2,
                    "point_count": 3,
                    "schema_url": "amapuri://workInAmap/createWithToken?polymericId=mcp_1",
                    "qr_image_sent": True,
                },
            )
        ],
        metadata={"agent_tool_scope": "group_personal_map"},
    )
    await after.run(ctx)

    assert ctx.extras["_credits_cost"] == 8
    assert ctx.extras["_credits_agent_billing"] == "amap"
    assert store.reserve_calls == []
    assert store.adjust_calls == []


@pytest.mark.asyncio
async def test_amap_route_map_generation_deducts_route_map_cost() -> None:
    store = _FakeCreditStore(cost=9, balance=20)
    before = CreditDeductionHook(store)
    after = CreditSettlementHook(store)
    ctx = _make_amap_ctx("帮我安排长沙一日游，生成高德地图")

    await before.run(ctx)
    ctx.result = CapabilityResult(
        route=RouteType.LLM,
        reply_text="ok",
        tool_calls=[
            ToolCall(
                id="call-1",
                name="amap_create_personal_map",
                arguments={"map_name": "长沙一日游地图", "scene_type": 1},
                result={
                    "map_name": "长沙一日游地图",
                    "scene_type": 1,
                    "point_count": 5,
                    "schema_url": "amapuri://workInAmap/createWithToken?polymericId=mcp_2",
                    "qr_image_sent": True,
                },
            )
        ],
        metadata={"agent_tool_scope": "group_personal_map"},
    )
    await after.run(ctx)

    assert ctx.extras["_credits_cost"] == 12
    assert ctx.extras["_credits_agent_billing"] == "amap"
    assert store.reserve_calls == []
    assert store.adjust_calls == []


@pytest.mark.asyncio
async def test_amap_failed_map_generation_does_not_deduct_search_or_chat_cost() -> None:
    store = _FakeCreditStore(cost=9, balance=20)
    before = CreditDeductionHook(store)
    after = CreditSettlementHook(store)
    ctx = _make_amap_ctx("找一下上海人民广场附近 3 家咖啡店，生成高德地图")

    await before.run(ctx)
    ctx.result = CapabilityResult(
        route=RouteType.LLM,
        reply_text="高德地图这次生成失败了",
        tool_calls=[
            ToolCall(
                id="call-1",
                name="amap_text_search",
                arguments={"keywords": "人民广场 咖啡店", "city": "上海"},
                result={"pois": [{"name": "阿拉比卡咖啡"}]},
            ),
            ToolCall(
                id="call-2",
                name="amap_create_personal_map",
                arguments={"map_name": "人民广场咖啡店地图", "scene_type": 2},
                result={"error": "upstream_error", "message": "生成失败"},
            ),
        ],
        metadata={"agent_tool_scope": "group_personal_map"},
    )
    await after.run(ctx)

    assert ctx.extras["_credits_cost"] == 8
    assert ctx.extras["_credits_agent_billing"] == "amap"
    assert store.reserve_calls == []
    assert store.adjust_calls == []


@pytest.mark.asyncio
async def test_credit_deduction_auto_checkin_happens_before_balance_gate() -> None:
    store = _FakeCreditStore(cost=2, balance=0, checkin_reward=3, checkin_mode=2)
    hook = CreditDeductionHook(store)
    ctx = _make_ctx(
        channel=Channel.WECHAT,
        session_id="g1@chatroom",
        metadata={"sender_name": "张三"},
    )

    await hook.run(ctx)

    assert len(store.checkin_calls) == 1
    assert store.balance == 1
    assert ctx.extras["_credits_cost"] == 2
    assert ctx.extras["_credits_auto_checkin_result"]["checked_in"] is True


@pytest.mark.asyncio
async def test_credit_deduction_auto_checkin_uses_generic_group_metadata() -> None:
    store = _FakeCreditStore(cost=2, balance=0, checkin_reward=3, checkin_mode=2)
    hook = CreditDeductionHook(store)
    ctx = _make_ctx(
        channel=Channel.DISCORD,
        session_id="discord-channel-1",
        metadata={"session_kind": "group", "sender_name": "张三"},
    )

    await hook.run(ctx)

    assert len(store.checkin_calls) == 1
    assert store.checkin_calls[0]["session_id"] == "discord-channel-1"
    assert store.balance == 1
    assert ctx.extras["_credits_auto_checkin_result"]["checked_in"] is True


@pytest.mark.asyncio
async def test_credit_auto_checkin_runs_before_reply_policy_suppression() -> None:
    class _SuppressReplyHook:
        name = "wxbot.reply_policy"
        point = HookPoint.BEFORE_ROUTE
        priority = 20

        async def run(self, ctx: PipelineContext) -> None:
            ctx.extras["suppress_outbound"] = True
            ctx.extras["skip_assistant_turn"] = True
            raise HookAbort("", reason="reply_mode_contains_no_match")

    store = _FakeCreditStore(cost=0, balance=0, checkin_reward=3, checkin_mode=2)
    runner = HookRunner()
    runner.register(CreditAutoCheckinHook(store), owner="credits")
    runner.register(_SuppressReplyHook(), owner="wxbot")
    ctx = _make_ctx(
        channel=Channel.WECHAT,
        session_id="g1@chatroom",
        metadata={"sender_name": "张三"},
    )

    with pytest.raises(HookAbort) as exc_info:
        await runner.run(HookPoint.BEFORE_ROUTE, ctx)

    assert exc_info.value.reason == "reply_mode_contains_no_match"
    assert len(store.checkin_calls) == 1
    assert store.checkin_calls[0]["session_id"] == "g1@chatroom"
    assert store.balance == 3
    assert ctx.extras["_credits_auto_checkin_result"]["checked_in"] is True


@pytest.mark.asyncio
async def test_credit_auto_checkin_uses_sender_wxid_for_wechat_identity() -> None:
    store = _FakeCreditStore(cost=0, balance=0, checkin_reward=3, checkin_mode=2)
    hook = CreditAutoCheckinHook(store)
    ctx = _make_ctx(
        channel=Channel.WECHAT,
        session_id="g1@chatroom",
        metadata={"sender_name": "张三", "sender_wxid": "wxid_real_member"},
    )
    ctx.event.user_id = "display-or-legacy-id"

    await hook.run(ctx)

    assert len(store.checkin_calls) == 1
    assert store.checkin_calls[0]["user_id"] == "wxid_real_member"


@pytest.mark.asyncio
async def test_credit_auto_checkin_skips_command_mode() -> None:
    store = _FakeCreditStore(cost=0, balance=0, checkin_reward=3, checkin_mode=1)
    hook = CreditAutoCheckinHook(store)
    ctx = _make_ctx(
        channel=Channel.WECHAT,
        session_id="g1@chatroom",
        metadata={"sender_name": "张三"},
    )

    await hook.run(ctx)

    assert store.checkin_calls == []
    assert "_credits_auto_checkin_result" not in ctx.extras


@pytest.mark.asyncio
async def test_credit_deduction_blocks_when_auto_checkin_still_not_enough() -> None:
    store = _FakeCreditStore(cost=5, balance=0, checkin_reward=2, checkin_mode=2)
    hook = CreditDeductionHook(store)
    ctx = _make_ctx(
        channel=Channel.WECHAT,
        session_id="g1@chatroom",
        metadata={"sender_name": "张三"},
    )

    with pytest.raises(HookAbort) as exc_info:
        await hook.run(ctx)

    assert len(store.checkin_calls) == 1
    assert store.balance == 2
    assert exc_info.value.reply_text == "积分不足，请补充后再试。"


@pytest.mark.asyncio
async def test_credit_natural_language_balance_query_runs_silent_checkin_first() -> None:
    store = _FakeCreditStore(cost=0, balance=0, checkin_reward=3, checkin_mode=3)
    hook = CreditNaturalLanguageHook(store)
    ctx = _make_ctx(
        channel=Channel.WECHAT,
        session_id="g1@chatroom",
        metadata={
            "sender_name": "张三",
            "mentioned_me": True,
            "wxbot_normalized_content": "我有多少积分",
        },
    )
    ctx.route = RouteDecision(type=RouteType.LLM)

    with pytest.raises(HookAbort) as exc_info:
        await hook.run(ctx)

    assert len(store.checkin_calls) == 1
    assert ctx.extras["_credits_auto_checkin_result"]["checked_in"] is True
    assert "你当前有 3 积分。" in exc_info.value.reply_text
    assert "今日已签到，获得 3 积分。" in exc_info.value.reply_text


@pytest.mark.asyncio
@pytest.mark.parametrize("normalized", ["我的积分", "我有多少积分"])
async def test_credit_natural_language_self_balance_queries_still_work(normalized: str) -> None:
    store = _FakeCreditStore(cost=0, balance=7)
    hook = CreditNaturalLanguageHook(store)
    ctx = _make_ctx(
        channel=Channel.WECHAT,
        session_id="g1@chatroom",
        content=f"@zzz {normalized}",
        metadata={
            "sender_name": "张三",
            "mentioned_me": True,
            "wxbot_normalized_content": normalized,
        },
    )
    ctx.route = RouteDecision(type=RouteType.LLM)

    with pytest.raises(HookAbort) as exc_info:
        await hook.run(ctx)

    assert exc_info.value.reason == "credit_balance_query"
    assert "你当前有 7 积分。" in exc_info.value.reply_text
    assert ctx.signals["credits"]["intent"]["type"] == "balance_self"
    assert ctx.signals["credits"]["intent"]["should_handle"] is True
    assert "query_text" not in ctx.signals["credits"]["intent"]


@pytest.mark.asyncio
async def test_credit_natural_language_can_query_other_member_by_display_name() -> None:
    store = _FakeCreditStore(cost=0, balance=0, checkin_reward=3, checkin_mode=3)
    hook = CreditNaturalLanguageHook(store)
    ctx = _make_ctx(
        channel=Channel.WECHAT,
        session_id="g1@chatroom",
        metadata={
            "sender_name": "张三",
            "mentioned_me": True,
            "wxbot_normalized_content": "鲸落有多少积分",
        },
    )
    ctx.route = RouteDecision(type=RouteType.LLM)

    with pytest.raises(HookAbort) as exc_info:
        await hook.run(ctx)

    assert len(store.checkin_calls) == 1
    assert exc_info.value.reason == "credit_member_query"
    assert exc_info.value.reply_text == (
        "鲸落 当前有 88 积分。\n"
        "当前排名：第 3 名。\n"
        "今日已签到，获得 10 积分。\n"
        "当前连签：4 天。"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("normalized", ["查一下老叶积分", "老叶的积分多少"])
async def test_credit_natural_language_other_member_explicit_queries_still_work(
    normalized: str,
) -> None:
    store = _FakeCreditStore(cost=0, balance=0, checkin_reward=0, checkin_mode=3)
    hook = CreditNaturalLanguageHook(store)
    ctx = _make_ctx(
        channel=Channel.WECHAT,
        session_id="g1@chatroom",
        content=f"@zzz {normalized}",
        metadata={
            "sender_name": "张三",
            "mentioned_me": True,
            "wxbot_normalized_content": normalized,
        },
    )
    ctx.route = RouteDecision(type=RouteType.LLM)

    with pytest.raises(HookAbort) as exc_info:
        await hook.run(ctx)

    assert exc_info.value.reason == "credit_member_query"
    assert "老叶 当前有 66 积分。" in exc_info.value.reply_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "normalized",
    [
        "多少积分能跟海神共进晚餐",
        "唐三积分",
        "海神积分 可以的",
        "这个积分大部分号不会扣",
    ],
)
async def test_credit_natural_language_ignores_discussion_and_naked_member_phrases(
    normalized: str,
) -> None:
    store = _FakeCreditStore(cost=0, balance=7)
    hook = CreditNaturalLanguageHook(store)
    ctx = _make_ctx(
        channel=Channel.WECHAT,
        session_id="g1@chatroom",
        content=f"@zzz {normalized}",
        metadata={
            "sender_name": "张三",
            "mentioned_me": True,
            "wxbot_normalized_content": normalized,
        },
    )
    ctx.route = RouteDecision(type=RouteType.LLM)

    await hook.run(ctx)

    assert store.checkin_calls == []


@pytest.mark.asyncio
async def test_credit_natural_language_rejects_reverse_transfer_without_member_lookup() -> None:
    store = _FakeCreditStore(cost=0, balance=7)
    hook = CreditNaturalLanguageHook(store)
    ctx = _make_ctx(
        channel=Channel.WECHAT,
        session_id="g1@chatroom",
        content="@zzz 帮我划走千羽10积分到我账户",
        metadata={
            "sender_name": "张三",
            "mentioned_me": True,
            "wxbot_normalized_content": "帮我划走千羽10积分到我账户",
        },
    )
    ctx.route = RouteDecision(type=RouteType.LLM)

    with pytest.raises(HookAbort) as exc_info:
        await hook.run(ctx)

    assert exc_info.value.reason == "credit_transfer_unsupported"
    assert "不支持从别人账户划走或扣除积分" in exc_info.value.reply_text
    assert "没找到 划走千羽10 的积分记录" not in exc_info.value.reply_text


@pytest.mark.asyncio
@pytest.mark.parametrize("normalized", ["转 10 积分给千羽", "给千羽 10 积分"])
async def test_credit_natural_language_rejects_self_transfer_without_balance_lookup(
    normalized: str,
) -> None:
    store = _FakeCreditStore(cost=0, balance=7)
    hook = CreditNaturalLanguageHook(store)
    ctx = _make_ctx(
        channel=Channel.WECHAT,
        session_id="g1@chatroom",
        content=f"@zzz {normalized}",
        metadata={
            "sender_name": "张三",
            "mentioned_me": True,
            "wxbot_normalized_content": normalized,
        },
    )
    ctx.route = RouteDecision(type=RouteType.LLM)

    with pytest.raises(HookAbort) as exc_info:
        await hook.run(ctx)

    assert exc_info.value.reason == "credit_transfer_unsupported"
    assert "暂不支持用自然语言转账积分" in exc_info.value.reply_text
    assert "你当前有" not in exc_info.value.reply_text
    assert "没找到" not in exc_info.value.reply_text


@pytest.mark.asyncio
async def test_credit_natural_language_can_query_other_member_by_extra_mention() -> None:
    store = _FakeCreditStore(cost=0, balance=0, checkin_reward=3, checkin_mode=3)
    hook = CreditNaturalLanguageHook(store)
    ctx = _make_ctx(
        channel=Channel.WECHAT,
        session_id="g1@chatroom",
        metadata={
            "sender_name": "张三",
            "mentioned_me": True,
            "at_wxids": ["wxid_bot", "wxid_jingluo"],
            "wxbot_normalized_content": "@鲸落 有多少积分",
        },
    )
    ctx.route = RouteDecision(type=RouteType.LLM)

    with pytest.raises(HookAbort) as exc_info:
        await hook.run(ctx)

    assert len(store.checkin_calls) == 1
    assert exc_info.value.reason == "credit_member_query"
    assert "鲸落 当前有 88 积分。" in exc_info.value.reply_text


@pytest.mark.asyncio
async def test_credit_natural_language_raw_extra_mention_still_queries_other_member() -> None:
    store = _FakeCreditStore(cost=0, balance=0, checkin_reward=3, checkin_mode=3)
    hook = CreditNaturalLanguageHook(store)
    ctx = _make_ctx(
        channel=Channel.WECHAT,
        session_id="g1@chatroom",
        content="@zzz @鲸落 积分",
        metadata={
            "sender_name": "张三",
            "mentioned_me": True,
            "at_wxids": ["wxid_bot", "wxid_jingluo"],
        },
    )
    ctx.route = RouteDecision(type=RouteType.LLM)

    with pytest.raises(HookAbort) as exc_info:
        await hook.run(ctx)

    assert exc_info.value.reason == "credit_member_query"
    assert "鲸落 当前有 88 积分。" in exc_info.value.reply_text


@pytest.mark.asyncio
async def test_credit_natural_language_direct_checkin_uses_silent_checkin_result() -> None:
    store = _FakeCreditStore(cost=0, balance=0, checkin_reward=5, checkin_mode=3)
    hook = CreditNaturalLanguageHook(store)
    ctx = _make_ctx(
        channel=Channel.WECHAT,
        session_id="g1@chatroom",
        metadata={
            "sender_name": "张三",
            "mentioned_me": True,
            "wxbot_normalized_content": "签到",
        },
    )
    ctx.route = RouteDecision(type=RouteType.LLM)

    with pytest.raises(HookAbort) as exc_info:
        await hook.run(ctx)

    assert len(store.checkin_calls) == 1
    assert exc_info.value.reason == "credit_checkin_action"
    assert exc_info.value.reply_text == (
        "当前群签到模式为：@ 机器人时静默签到\n"
        "无需手动发送「@签到」，@ 机器人发言会自动签到。"
    )


@pytest.mark.asyncio
async def test_credit_direct_checkin_repeat_returns_no_repeat_tip() -> None:
    store = _FakeCreditStore(cost=0, balance=5, checkin_reward=0, checkin_mode=3)
    hook = CreditNaturalLanguageHook(store)
    ctx = _make_ctx(
        channel=Channel.WECHAT,
        session_id="g1@chatroom",
        metadata={
            "sender_name": "张三",
            "mentioned_me": True,
            "wxbot_normalized_content": "签到",
        },
    )
    ctx.route = RouteDecision(type=RouteType.LLM)

    with pytest.raises(HookAbort) as exc_info:
        await hook.run(ctx)

    assert len(store.checkin_calls) == 1
    assert exc_info.value.reason == "credit_checkin_action"
    assert exc_info.value.reply_text == (
        "当前群签到模式为：@ 机器人时静默签到\n"
        "无需手动发送「@签到」，@ 机器人发言会自动签到。"
    )


@pytest.mark.asyncio
async def test_credit_natural_language_direct_checkin_in_silent_any_mode_returns_mode_tip() -> None:
    store = _FakeCreditStore(cost=0, balance=0, checkin_reward=5, checkin_mode=2)
    hook = CreditNaturalLanguageHook(store)
    ctx = _make_ctx(
        channel=Channel.WECHAT,
        session_id="g1@chatroom",
        content="签到",
        metadata={
            "sender_name": "张三",
            "mentioned_me": False,
            "wxbot_normalized_content": "签到",
        },
    )
    ctx.route = RouteDecision(type=RouteType.LLM)

    with pytest.raises(HookAbort) as exc_info:
        await hook.run(ctx)

    assert len(store.checkin_calls) == 1
    assert exc_info.value.reason == "credit_checkin_action"
    assert exc_info.value.reply_text == (
        "当前群签到模式为：当前发言签到（静默）\n"
        "无需手动发送「签到」，普通发言会自动签到。"
    )


@pytest.mark.asyncio
async def test_credit_command_checkin_in_silent_modes_returns_mode_specific_tip() -> None:
    silent_any = _FakeCreditStore(cost=0, balance=0, checkin_reward=5, checkin_mode=2)
    mention_only = _FakeCreditStore(cost=0, balance=0, checkin_reward=5, checkin_mode=3)
    silent_any_checkin = build_credit_command_definitions(silent_any)[0]
    mention_only_checkin = build_credit_command_definitions(mention_only)[0]
    ctx = _make_ctx(channel=Channel.WECHAT, session_id="g1@chatroom", content="/签到")

    assert await silent_any_checkin.handler(ctx, []) == (
        "当前群签到模式为：当前发言签到（静默）\n"
        "无需手动发送「/签到」，普通发言会自动签到。"
    )
    assert await mention_only_checkin.handler(ctx, []) == (
        "当前群签到模式为：@ 机器人时静默签到\n"
        "无需手动发送「/签到」，@ 机器人发言会自动签到。"
    )
    assert silent_any.checkin_calls == []
    assert mention_only.checkin_calls == []


@pytest.mark.asyncio
async def test_get_checkin_status_handles_missing_today_row(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, object]]:
        if "FROM plugin_credits_checkin" in sql:
            return [
                {
                    "checkin_date": date(2026, 4, 20),
                    "streak": 3,
                    "reward": 15,
                    "created_at": None,
                }
            ]
        return []

    async def fake_get_config(self: CreditStore, tenant_id: str, session_id: str) -> dict[str, object]:
        return {
            "checkin_mode": 1,
            "checkin_mode_label": "命令签到",
            "daily_checkin": 10,
            "streak_bonus": 5,
            "streak_cap": 50,
        }

    monkeypatch.setattr("plugins.credits.store._today_cn", lambda: date(2026, 4, 22))
    monkeypatch.setattr("plugins.credits.store._exec", fake_exec)
    monkeypatch.setattr(CreditStore, "get_config", fake_get_config)

    store = CreditStore(settings=None)
    status = await store.get_checkin_status("demo", "group@chatroom", "u1")

    assert status["checked_in_today"] is False
    assert status["current_streak"] == 0
    assert status["next_reward"] == 10
