from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.channel import get_reply_policy_override
from app.common.types import (
    Channel,
    InboundEvent,
    Message,
    PreprocessedMessage,
    Role,
    Session,
    Turn,
)
from app.orchestrator.pipeline import PipelineContext
from app.plugin.hooks import HookAbort
from plugins.tibo_reset.hooks import TiboResetIntentHook, TiboResetIntentStep
from plugins.tibo_reset.intent import (
    TiboResetIntentType,
    classify_tibo_reset_followup,
    classify_tibo_reset_intent,
    format_tibo_reset_reply,
    normalize_query_text,
)
from plugins.tibo_reset.store import TiboResetStore


def _stats() -> dict:
    return {
        "history_count": 9,
        "week_count": 5,
        "week_everyone_count": 4,
        "week_everyone_weekly_usage_count": 3,
        "week_everyone_banked_reset_count": 1,
        "week_subset_count": 1,
        "today_count": 1,
        "today_everyone_count": 1,
        "today_everyone_weekly_usage_count": 1,
        "today_everyone_banked_reset_count": 0,
        "today_subset_count": 0,
        "latest_reset_at": "2026-07-16T04:14:09+00:00",
        "latest_source_url": "https://x.com/thsottiaux/status/2077607697487188198",
        "timezone": "Asia/Shanghai",
    }


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Codex 本周重置多少次？", TiboResetIntentType.WEEK_COUNT),
        ("ChatGPT 今天是否重置", TiboResetIntentType.TODAY_STATUS),
        ("Codex 上次什么时候重置的", TiboResetIntentType.LATEST),
        ("ChatGPT Work 的重置历史会保留吗", TiboResetIntentType.RETENTION),
        ("Codex 额度重置情况", TiboResetIntentType.SUMMARY),
        ("这礼拜 Codex 额度放了几波", TiboResetIntentType.WEEK_COUNT),
        ("code x 今儿额度回血没", TiboResetIntentType.TODAY_STATUS),
        ("Codex 最近一次额度到账是几点", TiboResetIntentType.LATEST),
        ("ＣＯＤＸＥ 本周重置几回", TiboResetIntentType.WEEK_COUNT),
        ("怎么查 ChatGPT 本周重置了几次", TiboResetIntentType.WEEK_COUNT),
        ("cdoex这周reset几回", TiboResetIntentType.WEEK_COUNT),
        ("OpenAI Codex 啥时候重置的", TiboResetIntentType.LATEST),
        ("chat gpt 这周额度回满几次", TiboResetIntentType.WEEK_COUNT),
        ("重置", TiboResetIntentType.NONE),
        ("本周重置多少次？", TiboResetIntentType.NONE),
        ("今天是否重置", TiboResetIntentType.NONE),
        ("重置历史会保留吗", TiboResetIntentType.NONE),
        ("Tibo这礼拜放了几波", TiboResetIntentType.NONE),
        ("Codex 重置了", TiboResetIntentType.NONE),
        ("Codex 本周重置了两次", TiboResetIntentType.NONE),
        ("ChatGPT 重置", TiboResetIntentType.NONE),
        ("OpenAI 今天重置了吗？", TiboResetIntentType.NONE),
        ("Codex 重置没成功", TiboResetIntentType.NONE),
        ("Codex 重置情况已同步", TiboResetIntentType.NONE),
        ("Codex本周更新了什么", TiboResetIntentType.NONE),
        ("Codex最近哪个模型好", TiboResetIntentType.NONE),
        ("Codex历史聊天会保留吗", TiboResetIntentType.NONE),
        ("额度本周用了多少", TiboResetIntentType.NONE),
        ("今天重制了吗", TiboResetIntentType.NONE),
        ("今天重制海报了吗", TiboResetIntentType.NONE),
        ("今天怎么重置", TiboResetIntentType.NONE),
        ("重置要怎么操作", TiboResetIntentType.NONE),
        ("怎么重置数据库", TiboResetIntentType.NONE),
        ("数据库本周重置了几次", TiboResetIntentType.NONE),
        ("服务器收到重置通知了吗", TiboResetIntentType.NONE),
        ("设备号今天重置了吗", TiboResetIntentType.NONE),
        ("这周王者回血了几次", TiboResetIntentType.NONE),
        ("今天吃什么", TiboResetIntentType.NONE),
    ],
)
def test_classify_tibo_reset_intent(text: str, expected: TiboResetIntentType) -> None:
    assert classify_tibo_reset_intent(text).type == expected


def test_normalize_tibo_query_handles_mentions_width_and_common_typos() -> None:
    assert normalize_query_text("@zzz\u2005 ＣＯＤＸＥ\u200b 本周几次") == "codex 本周几次"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Codex 那今天呢", TiboResetIntentType.TODAY_STATUS),
        ("ChatGPT 这礼拜呢？", TiboResetIntentType.WEEK_COUNT),
        ("Codex 最近一次呢", TiboResetIntentType.LATEST),
        ("ChatGPT Work 历史呢", TiboResetIntentType.RETENTION),
        ("Codex 那一共几波", TiboResetIntentType.WEEK_COUNT),
        ("那今天呢", TiboResetIntentType.NONE),
        ("这礼拜呢？", TiboResetIntentType.NONE),
        ("今天吃什么", TiboResetIntentType.NONE),
    ],
)
def test_classify_tibo_reset_followup(
    text: str,
    expected: TiboResetIntentType,
) -> None:
    previous = classify_tibo_reset_intent("Codex 本周重置多少次")

    assert classify_tibo_reset_followup(text, previous).type == expected


def test_format_tibo_reset_reply_preserves_scope_and_reset_categories() -> None:
    intent = classify_tibo_reset_intent("Codex 本周重置多少次")

    reply = format_tibo_reset_reply(intent, _stats())

    assert "本周（周一 00:00 起）有 5 次" in reply
    assert "面向所有用户 4 次" in reply
    assert "周额度 3 次" in reply
    assert "banked reset 1 次" in reply
    assert "另有 1 次仅部分用户" in reply
    assert "今天有 1 次" in reply
    assert "不代表每个账号" in reply


class _HookStore:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.stats_calls = 0

    async def is_scope_enabled(self, tenant_id: str, session_id: str) -> bool:
        return self.enabled and tenant_id == "default" and session_id == "00000000000@chatroom"

    async def reset_stats(self):
        self.stats_calls += 1
        return _stats()


def _ctx(
    text: str,
    *,
    session_id: str = "00000000000@chatroom",
    external_session_id: str = "",
    turns: list[Turn] | None = None,
    sender_wxid: str = "wxid-user",
    variables: dict | None = None,
    received_at: datetime | None = None,
) -> PipelineContext:
    event = InboundEvent(
        message_id="m-1",
        tenant_id="default",
        channel=Channel.WECHAT,
        user_id=sender_wxid,
        session_id=session_id,
        message=Message(content=text),
        trace_id="trace-1",
        received_at=received_at or datetime.now(UTC),
        external_conversation_id=external_session_id,
        canonical_conversation_id=session_id,
        metadata={
            "session_kind": "group",
            "sender_wxid": sender_wxid,
            **({"external_conversation_id": external_session_id} if external_session_id else {}),
        },
    )
    session = Session(
        session_id=session_id,
        tenant_id="default",
        user_id=sender_wxid,
        channel=Channel.WECHAT,
        external_conversation_id=external_session_id,
        canonical_conversation_id=session_id,
        turns=list(turns or []),
        variables=dict(variables or {}),
    )
    return PipelineContext(
        event=event,
        trace_id="trace-1",
        session=session,
        pre=PreprocessedMessage(original_text=text, cleaned_text=text),
    )


@pytest.mark.asyncio
async def test_tibo_intent_hook_answers_only_enabled_group_and_forces_reply() -> None:
    store = _HookStore(enabled=True)
    ctx = _ctx("Codex 今天重置了吗？")

    with pytest.raises(HookAbort) as excinfo:
        await TiboResetIntentHook(store).run(ctx)

    assert "今天有 1 次" in excinfo.value.reply_text
    override = get_reply_policy_override(ctx.extras)
    assert override["force_send"] is True
    assert override["mention_sender"] is True
    assert ctx.signals["tibo_reset"]["intent"] == "today_status"
    marker = ctx.session.variables["tibo_reset_followup_context"]["wxid-user"]
    assert marker["intent"] == "today_status"
    assert marker["trace_id"] == "trace-1"


@pytest.mark.asyncio
async def test_tibo_intent_hook_ignores_disabled_or_unrelated_group() -> None:
    disabled_store = _HookStore(enabled=False)
    await TiboResetIntentHook(disabled_store).run(_ctx("Codex 本周重置多少次？"))
    assert disabled_store.stats_calls == 0

    enabled_store = _HookStore(enabled=True)
    await TiboResetIntentHook(enabled_store).run(
        _ctx("Codex 本周重置多少次？", session_id="other@chatroom")
    )
    assert enabled_store.stats_calls == 0


@pytest.mark.asyncio
async def test_tibo_intent_managed_group_uses_external_configuration_scope() -> None:
    store = _HookStore(enabled=True)
    ctx = _ctx(
        "最近 Codex 重置了吗？",
        session_id="cx1:c:d9a9638d@chatroom",
        external_session_id="00000000000@chatroom",
    )

    with pytest.raises(HookAbort) as excinfo:
        await TiboResetIntentHook(store).run(ctx)

    assert "最近一次公告" in excinfo.value.reply_text
    assert store.stats_calls == 1
    assert ctx.signals["tibo_reset"]["intent"] == "latest"


@pytest.mark.asyncio
async def test_tibo_intent_disabled_scope_records_diagnostic_signal() -> None:
    ctx = _ctx("Codex 本周重置多少次？")

    await TiboResetIntentHook(_HookStore(enabled=False)).run(ctx)

    assert ctx.signals["tibo_reset"]["reason"] == "scope_disabled"
    assert ctx.signals["tibo_reset"]["scope_session_id"] == "00000000000@chatroom"


@pytest.mark.asyncio
async def test_tibo_intent_flow_step_answers_before_normal_routing() -> None:
    step = TiboResetIntentStep(_HookStore(enabled=True))
    ctx = _ctx("@zzz\u2005 codex本周重置了几次")

    result = await step.run(ctx)

    assert result.action == "stop"
    assert result.finalize is True
    assert result.route_label == "canned"
    assert result.result is not None
    assert "本周（周一 00:00 起）有 5 次" in result.result.reply_text
    assert ctx.signals["tibo_reset"]["intent"] == "week_count"
    override = get_reply_policy_override(ctx.extras)
    assert override["force_send"] is True
    assert override["mention_sender"] is True


@pytest.mark.asyncio
async def test_tibo_intent_flow_step_continues_for_unrelated_question() -> None:
    step = TiboResetIntentStep(_HookStore(enabled=True))
    ctx = _ctx("Codex CLI 怎么重置配置？")

    result = await step.run(ctx)

    assert result.action == "continue"
    assert result.reason == "no_tibo_reset_intent"


@pytest.mark.asyncio
async def test_tibo_intent_flow_step_ignores_reset_without_product_subject() -> None:
    store = _HookStore(enabled=True)

    result = await TiboResetIntentStep(store).run(_ctx("本周重置了几次？"))

    assert result.action == "continue"
    assert result.reason == "no_tibo_reset_intent"
    assert store.stats_calls == 0


@pytest.mark.asyncio
async def test_tibo_intent_flow_step_resolves_recent_same_sender_followup() -> None:
    now = datetime.now(UTC)
    ctx = _ctx(
        "Codex 那今天呢",
        received_at=now,
        variables={
            "tibo_reset_followup_context": {
                "wxid-user": {
                    "intent": "week_count",
                    "handled_at": (now - timedelta(minutes=2)).isoformat(),
                    "trace_id": "trace-previous",
                }
            }
        },
    )

    result = await TiboResetIntentStep(_HookStore(enabled=True)).run(ctx)

    assert result.action == "stop"
    assert result.result is not None
    assert "今天有 1 次" in result.result.reply_text
    assert ctx.signals["tibo_reset"]["intent"] == "today_status"
    assert ctx.signals["tibo_reset"]["match_source"] == "handled_context"


@pytest.mark.asyncio
async def test_tibo_followup_requires_product_subject_even_with_recent_context() -> None:
    now = datetime.now(UTC)
    context = {
        "tibo_reset_followup_context": {
            "wxid-user": {
                "intent": "week_count",
                "handled_at": (now - timedelta(minutes=2)).isoformat(),
                "trace_id": "trace-previous",
            }
        }
    }

    result = await TiboResetIntentStep(_HookStore(enabled=True)).run(
        _ctx("那今天呢", variables=context, received_at=now)
    )

    assert result.action == "continue"
    assert result.reason == "no_tibo_reset_intent"


@pytest.mark.asyncio
async def test_tibo_followup_does_not_reuse_other_sender_or_stale_context() -> None:
    now = datetime.now(UTC)
    other_context = {
        "tibo_reset_followup_context": {
            "wxid-other": {
                "intent": "week_count",
                "handled_at": (now - timedelta(minutes=2)).isoformat(),
                "trace_id": "trace-other",
            }
        }
    }
    stale_context = {
        "tibo_reset_followup_context": {
            "wxid-user": {
                "intent": "week_count",
                "handled_at": (now - timedelta(minutes=20)).isoformat(),
                "trace_id": "trace-stale",
            }
        }
    }

    other_result = await TiboResetIntentStep(_HookStore(enabled=True)).run(
        _ctx("Codex 那今天呢", variables=other_context, received_at=now)
    )
    stale_result = await TiboResetIntentStep(_HookStore(enabled=True)).run(
        _ctx("Codex 那今天呢", variables=stale_context, received_at=now)
    )

    assert other_result.action == "continue"
    assert stale_result.action == "continue"


@pytest.mark.asyncio
async def test_tibo_followup_does_not_reclassify_unhandled_session_turn() -> None:
    previous = Turn(
        session_id="00000000000@chatroom",
        role=Role.USER,
        content="Codex 本周重置了几次",
        trace_id="trace-previous",
        created_at=datetime.now(UTC) - timedelta(minutes=2),
        metadata={"sender_wxid": "wxid-user"},
    )

    result = await TiboResetIntentStep(_HookStore(enabled=True)).run(
        _ctx("Codex 那今天呢", turns=[previous])
    )

    assert result.action == "continue"


@pytest.mark.asyncio
async def test_reset_stats_uses_shanghai_calendar_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = TiboResetStore(type("Settings", (), {"tibo_reset_timezone": "Asia/Shanghai"})())
    captured: dict = {}

    async def fake_fetch(sql: str, params: dict | None = None):
        captured["sql"] = sql
        captured["params"] = params or {}
        return [
            {
                **_stats(),
                "latest_reset_at": datetime.fromisoformat("2026-07-16T04:14:09+00:00"),
            }
        ]

    monkeypatch.setattr(store, "_fetch", fake_fetch)
    result = await store.reset_stats(now=datetime.fromisoformat("2026-07-16T13:00:00+08:00"))

    assert captured["params"]["today_start_utc"].isoformat() == "2026-07-15T16:00:00+00:00"
    assert captured["params"]["week_start_utc"].isoformat() == "2026-07-12T16:00:00+00:00"
    assert result["today_has_reset"] is True
    assert result["week_count"] == 5
    assert result["timezone"] == "Asia/Shanghai"
    assert result["latest_reset_at"] == "2026-07-16T04:14:09+00:00"
