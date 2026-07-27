from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.channel import ChannelSendOptions, ChannelTarget
from app.social.speech_ledger import (
    GroupSpeechBudgetExceeded,
    GroupSpeechLedger,
    InMemoryGroupSpeechLedger,
    SpeechBudgetPolicy,
    SpeechBudgetSnapshot,
    evaluate_speech_budget,
)
from plugins.wxbot import store as wxbot_store_module
from plugins.wxbot.channel import WxbotChannelOutbound
from plugins.wxbot.reports import WxbotReportService
from plugins.wxbot.store import WxbotStore

_NOW = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)


class _Rows:
    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self.rows = rows or []

    def mappings(self) -> _Rows:
        return self

    def first(self) -> dict[str, object] | None:
        return self.rows[0] if self.rows else None

    def all(self) -> list[dict[str, object]]:
        return list(self.rows)


class _VersionedPolicyConnection:
    def __init__(
        self,
        *,
        policy_json: dict[str, object] | None = None,
        bot_10m: int = 1,
        bot_hour: int = 1,
        recent_author_kinds: tuple[str, ...] = ("human",) * 39,
    ) -> None:
        self.statements: list[str] = []
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.policy_json = policy_json or {
            "max_soft_replies_10m": 1,
            "max_soft_replies_hour": 99,
            "max_bot_ratio_last_40": 1.0,
            "max_consecutive_bot_messages": 2,
        }
        self.bot_10m = bot_10m
        self.bot_hour = bot_hour
        self.recent_author_kinds = recent_author_kinds

    async def execute(self, statement, _params=None) -> _Rows:
        sql = str(statement)
        self.statements.append(sql)
        self.calls.append((sql, dict(_params or {})))
        if "SELECT version, policy_json FROM social_group_policy" in sql:
            return _Rows(
                [
                    {
                        "version": 12,
                        "policy_json": self.policy_json,
                    }
                ]
            )
        if "COUNT(*) FILTER" in sql:
            return _Rows([{"bot_10m": self.bot_10m, "bot_hour": self.bot_hour}])
        if "SELECT author_kind FROM social_group_speech_ledger" in sql:
            return _Rows([{"author_kind": author} for author in self.recent_author_kinds])
        return _Rows()


class _VersionedPolicyEngine:
    def __init__(self, **kwargs: object) -> None:
        self.connection = _VersionedPolicyConnection(**kwargs)  # type: ignore[arg-type]

    def begin(self) -> _VersionedPolicyEngine:
        return self

    async def __aenter__(self) -> _VersionedPolicyConnection:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        return None


async def _seed_humans(
    ledger: InMemoryGroupSpeechLedger,
    session_id: str,
    *,
    count: int = 7,
) -> None:
    for index in range(count):
        await ledger.observe_message(
            tenant_id="demo",
            session_id=session_id,
            message_id=f"human-{session_id}-{index}",
            is_bot=False,
            text=f"成员消息 {index}",
            occurred_at=_NOW - timedelta(minutes=count - index),
        )


def test_budget_allows_exact_fifteen_percent_boundary() -> None:
    snapshot = SpeechBudgetSnapshot(
        recent_author_kinds=(
            "human",
            "bot",
            "human",
            "bot",
            "human",
            "bot",
            "human",
            "bot",
            "human",
            "bot",
        )
        + ("human",) * 29,
        bot_messages_10m=1,
        bot_messages_hour=5,
    )

    decision = evaluate_speech_budget(snapshot)

    assert decision.allowed is True
    assert decision.prospective_bot_ratio == pytest.approx(0.15)


@pytest.mark.parametrize(
    ("snapshot", "reason"),
    [
        (
            SpeechBudgetSnapshot(
                recent_author_kinds=("human",) * 39,
                bot_messages_10m=2,
            ),
            "budget_10m",
        ),
        (
            SpeechBudgetSnapshot(
                recent_author_kinds=("human",) * 39,
                bot_messages_hour=6,
            ),
            "budget_hour",
        ),
        (
            SpeechBudgetSnapshot(
                recent_author_kinds=("bot", "bot") + ("human",) * 37,
            ),
            "third_consecutive_bot_message",
        ),
        (
            SpeechBudgetSnapshot(
                recent_author_kinds=(
                    "human",
                    "bot",
                    "human",
                    "bot",
                    "human",
                    "bot",
                    "human",
                    "bot",
                    "human",
                    "bot",
                    "human",
                    "bot",
                )
                + ("human",) * 27,
            ),
            "bot_ratio_last_40",
        ),
    ],
)
def test_budget_denies_each_hard_limit(
    snapshot: SpeechBudgetSnapshot,
    reason: str,
) -> None:
    decision = evaluate_speech_budget(snapshot)

    assert decision.allowed is False
    assert decision.reason == reason


@pytest.mark.asyncio
async def test_database_ledger_loads_versioned_group_policy_inside_group_lock() -> None:
    engine = _VersionedPolicyEngine()
    ledger = GroupSpeechLedger(engine)  # type: ignore[arg-type]

    reservation = await ledger.reserve(
        tenant_id="demo",
        session_id="room@chatroom",
        idempotency_key="current-policy",
        output_kind="ordinary",
        text="这条受当前群策略限制",
    )

    assert reservation.allowed is False
    assert reservation.reason == "budget_10m"
    assert reservation.policy_version == 12
    lock_index = next(
        index
        for index, sql in enumerate(engine.connection.statements)
        if "pg_advisory_xact_lock" in sql
    )
    policy_index = next(
        index
        for index, sql in enumerate(engine.connection.statements)
        if "SELECT version, policy_json FROM social_group_policy" in sql
    )
    assert lock_index < policy_index
    lock_params = engine.connection.calls[lock_index][1]
    assert lock_params["scope"] == "social-speech-v1:4:demoroom@chatroom"
    assert "\0" not in str(lock_params["scope"])


@pytest.mark.asyncio
async def test_database_ledger_observation_uses_postgres_safe_group_lock_key() -> None:
    engine = _VersionedPolicyEngine()
    ledger = GroupSpeechLedger(engine)  # type: ignore[arg-type]

    await ledger.observe_message(
        tenant_id="demo",
        session_id="room@chatroom",
        message_id="message-1",
        is_bot=False,
        text="hello",
        occurred_at=_NOW,
    )

    lock_call = next(
        (sql, params) for sql, params in engine.connection.calls if "pg_advisory_xact_lock" in sql
    )
    assert lock_call[1]["scope"] == "social-speech-v1:4:demoroom@chatroom"
    assert "\0" not in str(lock_call[1]["scope"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy_json", "bot_10m", "bot_hour", "recent", "reason"),
    [
        (
            {
                "max_soft_replies_10m": 1,
                "max_soft_replies_hour": 99,
                "max_bot_ratio_last_40": 1.0,
                "max_consecutive_bot_messages": 9,
            },
            1,
            1,
            ("human",) * 39,
            "budget_10m",
        ),
        (
            {
                "max_soft_replies_10m": 99,
                "max_soft_replies_hour": 3,
                "max_bot_ratio_last_40": 1.0,
                "max_consecutive_bot_messages": 9,
            },
            1,
            3,
            ("human",) * 39,
            "budget_hour",
        ),
        (
            {
                "max_soft_replies_10m": 99,
                "max_soft_replies_hour": 99,
                "max_bot_ratio_last_40": 0.01,
                "max_consecutive_bot_messages": 9,
            },
            0,
            0,
            ("human",) * 39,
            "bot_ratio_last_40",
        ),
        (
            {
                "max_soft_replies_10m": 99,
                "max_soft_replies_hour": 99,
                "max_bot_ratio_last_40": 1.0,
                "max_consecutive_bot_messages": 2,
            },
            0,
            0,
            ("bot", "bot") + ("human",) * 37,
            "third_consecutive_bot_message",
        ),
    ],
)
async def test_database_ledger_applies_every_current_group_budget_field(
    policy_json: dict[str, object],
    bot_10m: int,
    bot_hour: int,
    recent: tuple[str, ...],
    reason: str,
) -> None:
    engine = _VersionedPolicyEngine(
        policy_json=policy_json,
        bot_10m=bot_10m,
        bot_hour=bot_hour,
        recent_author_kinds=recent,
    )
    ledger = GroupSpeechLedger(engine)  # type: ignore[arg-type]

    reservation = await ledger.reserve(
        tenant_id="demo",
        session_id="room@chatroom",
        idempotency_key=f"current-policy-{reason}",
        output_kind="ordinary",
        text="按当前群策略判断",
    )

    assert reservation.allowed is False
    assert reservation.reason == reason
    assert reservation.policy_version == 12


@pytest.mark.asyncio
async def test_in_memory_ledger_is_idempotent_and_merges_bot_observation() -> None:
    ledger = InMemoryGroupSpeechLedger(now=lambda: _NOW)
    await _seed_humans(ledger, "room@chatroom")

    first = await ledger.reserve(
        tenant_id="demo",
        session_id="room@chatroom",
        idempotency_key="reply-1",
        output_kind="ordinary",
        text="同一条回复",
    )
    assert first.allowed is True
    await ledger.commit(first)
    event_count = len(ledger.events)

    replay = await ledger.reserve(
        tenant_id="demo",
        session_id="room@chatroom",
        idempotency_key="reply-1",
        output_kind="ordinary",
        text="同一条回复",
    )
    assert replay.allowed is True
    assert replay.replayed is True
    assert replay.reservation_id == first.reservation_id
    assert len(ledger.events) == event_count

    await ledger.observe_message(
        tenant_id="demo",
        session_id="room@chatroom",
        message_id="sdk-message-1",
        is_bot=True,
        text="同一条回复",
        occurred_at=_NOW,
    )
    assert len(ledger.events) == event_count
    merged = next(event for event in ledger.events if event.reservation_id == first.reservation_id)
    assert merged.observed_message_id == "sdk-message-1"


@pytest.mark.asyncio
async def test_released_reservation_can_be_retried_without_consuming_budget() -> None:
    ledger = InMemoryGroupSpeechLedger(now=lambda: _NOW)
    await _seed_humans(ledger, "room@chatroom")
    first = await ledger.reserve(
        tenant_id="demo",
        session_id="room@chatroom",
        idempotency_key="retryable",
        output_kind="ordinary",
        text="重试",
    )

    await ledger.release(first, reason="queue_failed")
    retried = await ledger.reserve(
        tenant_id="demo",
        session_id="room@chatroom",
        idempotency_key="retryable",
        output_kind="ordinary",
        text="重试",
    )

    assert retried.allowed is True
    assert retried.replayed is False
    assert retried.reservation_id == first.reservation_id


@pytest.mark.asyncio
async def test_committed_reservation_cannot_be_rolled_back_by_late_release() -> None:
    ledger = InMemoryGroupSpeechLedger(now=lambda: _NOW)
    await _seed_humans(ledger, "room@chatroom")
    reservation = await ledger.reserve(
        tenant_id="demo",
        session_id="room@chatroom",
        idempotency_key="committed-reply",
        output_kind="ordinary",
        text="已经送达",
    )

    await ledger.commit(reservation)
    await ledger.release(reservation, reason="late_failure_callback")

    event = next(item for item in ledger.events if item.idempotency_key == "committed-reply")
    assert event.status == "committed"


@pytest.mark.asyncio
async def test_idempotency_replay_rejects_a_different_output_payload() -> None:
    ledger = InMemoryGroupSpeechLedger(now=lambda: _NOW)
    await _seed_humans(ledger, "room@chatroom")
    await ledger.reserve(
        tenant_id="demo",
        session_id="room@chatroom",
        idempotency_key="same-key",
        output_kind="ordinary",
        speech_class="soft",
        text="原始回复",
    )

    with pytest.raises(ValueError, match="idempotency key reused"):
        await ledger.reserve(
            tenant_id="demo",
            session_id="room@chatroom",
            idempotency_key="same-key",
            output_kind="ordinary",
            speech_class="soft",
            text="被替换的回复",
        )


def test_obligation_bypasses_volume_budgets_but_not_consecutive_limit() -> None:
    saturated = SpeechBudgetSnapshot(
        recent_author_kinds=("human", "bot", "bot") + ("human",) * 36,
        bot_messages_10m=2,
        bot_messages_hour=6,
    )

    assert evaluate_speech_budget(saturated, speech_class="obligation").allowed is True
    assert evaluate_speech_budget(saturated, speech_class="soft").allowed is False
    assert evaluate_speech_budget(saturated, speech_class="scheduled").allowed is False

    consecutive = SpeechBudgetSnapshot(
        recent_author_kinds=("bot", "bot") + ("human",) * 37,
        bot_messages_10m=2,
        bot_messages_hour=6,
    )
    obligation = evaluate_speech_budget(consecutive, speech_class="obligation")
    assert obligation.allowed is False
    assert obligation.reason == "third_consecutive_bot_message"

    required = evaluate_speech_budget(
        consecutive,
        speech_class="required_delivery",
    )
    assert required.allowed is True
    assert required.reason == "required_delivery_bypass"


@pytest.mark.asyncio
async def test_no_third_consecutive_bot_even_when_time_budgets_are_relaxed() -> None:
    ledger = InMemoryGroupSpeechLedger(
        now=lambda: _NOW,
        policy=SpeechBudgetPolicy(
            max_bot_messages_10m=100,
            max_bot_messages_hour=100,
            max_bot_ratio_last_40=1.0,
        ),
    )
    await _seed_humans(ledger, "room@chatroom")
    for index in range(2):
        reservation = await ledger.reserve(
            tenant_id="demo",
            session_id="room@chatroom",
            idempotency_key=f"reply-{index}",
            output_kind="ordinary",
            text=f"回复 {index}",
        )
        assert reservation.allowed is True
        await ledger.commit(reservation)

    denied = await ledger.reserve(
        tenant_id="demo",
        session_id="room@chatroom",
        idempotency_key="reply-3",
        output_kind="ordinary",
        text="第三条",
    )

    assert denied.allowed is False
    assert denied.reason == "third_consecutive_bot_message"


@pytest.mark.asyncio
async def test_budgeted_queue_output_kinds_reserve_before_enqueue_and_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = InMemoryGroupSpeechLedger(now=lambda: _NOW)
    queued: dict[tuple[str, str], dict[str, object]] = {}
    next_id = 1

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        nonlocal next_id
        values = dict(params or {})
        if sql.startswith("INSERT INTO plugin_wxbot_reply_queue"):
            key = (str(values["tid"]), str(values["command_id"]))
            active = next(
                event
                for event in ledger.events
                if event.tenant_id == values["tid"]
                and event.session_id == values["sid"]
                and event.idempotency_key == values["command_id"]
            )
            if key in queued:
                assert active.status == "reserved"
                return []
            assert active.status == "reserved"
            queued[key] = {**values, "id": next_id}
            next_id += 1
            return [{"id": queued[key]["id"]}]
        if sql.startswith("SELECT id FROM plugin_wxbot_reply_queue"):
            row = queued.get((str(values["tid"]), str(values["command_id"])))
            return [{"id": row["id"]}] if row else []
        return []

    monkeypatch.setattr(wxbot_store_module, "_exec", fake_exec)
    store = WxbotStore(SimpleNamespace(), speech_ledger=ledger)
    original = "这是一段用于验证自然回复长度约束的群聊内容。" * 6 + "🙂🙂"

    for index, output_kind in enumerate(
        ("ordinary", "proactive", "report"),
        start=1,
    ):
        session_id = f"room-{index}@chatroom"
        command_id = f"command-{output_kind}"
        await _seed_humans(ledger, session_id)
        reply_id = await store.enqueue_reply(
            tenant_id="demo",
            session_id=session_id,
            session_name="测试群",
            sender_name="",
            reply_text=original,
            trace_id=f"trace-{index}",
            session_kind="group",
            source_message={"message": {"content": "随便聊聊"}},
            delivery={
                "speech_output_kind": output_kind,
                "style_eligible": True,
            },
            command_id=command_id,
        )

        row = queued[("demo", command_id)]
        event = next(item for item in ledger.events if item.idempotency_key == command_id)
        assert reply_id == row["id"]
        assert event.output_kind == output_kind
        # Queue admission only reserves the slot.  The SDK delivery callback
        # commits it, while terminal failure/cancellation releases it.
        assert event.status == "reserved"
        if output_kind in {"ordinary", "proactive"}:
            assert str(row["reply"]).count("🙂") <= 1
        else:
            assert row["reply"] == original

    before = len(ledger.events)
    replayed_id = await store.enqueue_reply(
        tenant_id="demo",
        session_id="room-1@chatroom",
        session_name="测试群",
        sender_name="",
        reply_text=original,
        session_kind="group",
        delivery={"speech_output_kind": "ordinary", "style_eligible": True},
        command_id="command-ordinary",
    )
    assert replayed_id == queued[("demo", "command-ordinary")]["id"]
    assert len(ledger.events) == before


@pytest.mark.asyncio
async def test_store_duplicate_guard_cancels_soft_but_preserves_obligation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = InMemoryGroupSpeechLedger(now=lambda: _NOW)
    await _seed_humans(ledger, "duplicate-room@chatroom")
    previous = await ledger.reserve(
        tenant_id="demo",
        session_id="duplicate-room@chatroom",
        idempotency_key="previous",
        output_kind="ordinary",
        speech_class="soft",
        text="好的，结论还是这一条。",
    )
    await ledger.commit(previous)
    inserts: list[dict] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if sql.startswith("INSERT INTO plugin_wxbot_reply_queue"):
            inserts.append(dict(params or {}))
            return [{"id": len(inserts)}]
        if sql.startswith("UPDATE plugin_wxbot_reply_queue"):
            return []
        return []

    monkeypatch.setattr(wxbot_store_module, "_exec", fake_exec)
    store = WxbotStore(SimpleNamespace(), speech_ledger=ledger)

    with pytest.raises(GroupSpeechBudgetExceeded) as caught:
        await store.enqueue_reply(
            tenant_id="demo",
            session_id="duplicate-room@chatroom",
            session_name="测试群",
            sender_name="",
            reply_text="好的，结论还是这一条。",
            session_kind="group",
            delivery={
                "speech_class": "soft",
                "speech_budget_enabled": True,
                "duplicate_guard_enabled": True,
            },
            command_id="soft-duplicate",
        )
    assert caught.value.reason == "near_duplicate_24h"
    assert inserts == []

    reply_id = await store.enqueue_reply(
        tenant_id="demo",
        session_id="duplicate-room@chatroom",
        session_name="测试群",
        sender_name="",
        reply_text="好的，结论还是这一条。",
        session_kind="group",
        delivery={
            "speech_class": "obligation",
            "speech_budget_enabled": True,
            "duplicate_guard_enabled": True,
        },
        command_id="obligation-duplicate",
    )

    assert reply_id == 1
    assert inserts[0]["reply"] == "结论还是这一条。"
    delivery = wxbot_store_module.json.loads(inserts[0]["delivery_json"])
    assert delivery["near_duplicate_guard"]["action"] == "rewritten"
    assert delivery["near_duplicate_guard"]["complete_answer_preserved"] is True


@pytest.mark.asyncio
async def test_deferred_queue_candidate_reserves_only_when_claimed_for_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = InMemoryGroupSpeechLedger(now=lambda: _NOW)
    await _seed_humans(ledger, "deferred-room@chatroom")

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if sql.startswith("UPDATE plugin_wxbot_reply_queue SET reply_text"):
            assert params is not None
            assert params["id"] == 41
            assert params["claim_token"] == "claim-41"
            return [{"id": 41}]
        return []

    monkeypatch.setattr(wxbot_store_module, "_exec", fake_exec)
    store = WxbotStore(SimpleNamespace(), speech_ledger=ledger)
    reply = {
        "id": 41,
        "claim_token": "claim-41",
        "session_id": "deferred-room@chatroom",
        "session_kind": "group",
        "reply_text": "现在补充这一点。",
        "msg_type": "text",
        "source_message_id": "source-41",
        "command_id": "deferred-41",
        "delivery": {
            "speech_output_kind": "ordinary",
            "speech_class": "scheduled",
            "speech_budget_enabled": True,
            "duplicate_guard_enabled": True,
            "deferred_candidate": True,
        },
    }

    prepared = await store.prepare_claimed_reply_speech(
        reply,
        tenant_id="demo",
        claim_token="claim-41",
    )

    assert prepared is True
    event = next(item for item in ledger.events if item.idempotency_key == "deferred-41")
    assert event.status == "reserved"
    assert event.speech_class == "scheduled"
    assert reply["delivery"]["speech_ledger"]["reservation_id"] == event.reservation_id


@pytest.mark.asyncio
async def test_repeater_bypasses_conversational_speech_budget_at_enqueue_and_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = InMemoryGroupSpeechLedger(now=lambda: _NOW)
    inserts: list[dict] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if sql.startswith("INSERT INTO plugin_wxbot_reply_queue"):
            inserts.append(dict(params or {}))
            return [{"id": 51}]
        return []

    monkeypatch.setattr(wxbot_store_module, "_exec", fake_exec)
    store = WxbotStore(SimpleNamespace(), speech_ledger=ledger)

    reply_id = await store.enqueue_reply(
        tenant_id="demo",
        session_id="repeater-room@chatroom",
        session_name="测试群",
        sender_name="",
        reply_text="复读这一句",
        session_kind="group",
        delivery={
            "speech_output_kind": "repeater",
            "speech_class": "scheduled",
            "speech_budget_enabled": True,
        },
        command_id="repeater-51",
    )

    assert reply_id == 51
    delivery = wxbot_store_module.json.loads(inserts[0]["delivery_json"])
    assert delivery["speech_budget_enabled"] is False
    assert ledger.events == []
    assert await store.prepare_claimed_reply_speech(
        {
            "id": 51,
            "session_id": "repeater-room@chatroom",
            "session_kind": "group",
            "reply_text": "复读这一句",
            "delivery": {
                "speech_output_kind": "repeater",
                "speech_budget_enabled": True,
            },
        },
        tenant_id="demo",
        claim_token="claim-51",
    )
    assert ledger.events == []


class _DenyingQueueStore:
    async def enqueue_reply(self, **_kwargs) -> int:
        raise GroupSpeechBudgetExceeded(
            "budget_10m",
            output_kind="ordinary",
            idempotency_key="denied",
        )


@pytest.mark.asyncio
async def test_channel_returns_explicit_suppression_when_budget_denies() -> None:
    channel = WxbotChannelOutbound(_DenyingQueueStore())

    result = await channel.send_text(
        ChannelTarget(
            tenant_id="demo",
            channel="wechat",
            session_id="room@chatroom",
            session_kind="group",
        ),
        "这条不会入队",
        ChannelSendOptions(idempotency_key="denied"),
    )

    assert result.message_id == ""
    assert result.metadata == {
        "suppressed": True,
        "reason": "budget_10m",
        "output_kind": "ordinary",
    }


class _ReportStore:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(wxbot_default_tenant_id="demo")
        self.job = {
            "id": 9,
            "tenant_id": "demo",
            "session_id": "report-room@chatroom",
            "session_name": "报告群",
            "report_type": "daily",
            "period_key": "2026-07-16",
            "status": "completed",
            "result_text": "群日报正文",
            "delivery_status": "pending",
            "delivery_attempt": 0,
            "sdk_outbound_id": None,
        }

    async def get_report_job(self, job_id: int) -> dict[str, object] | None:
        return dict(self.job) if job_id == 9 else None

    async def try_start_report_delivery(self, job_id: int) -> int | None:
        assert job_id == 9
        self.job["delivery_status"] = "sending"
        self.job["delivery_attempt"] = int(self.job["delivery_attempt"]) + 1
        return int(self.job["delivery_attempt"])

    async def mark_report_delivery_sent(
        self,
        job_id: int,
        *,
        delivery_attempt: int,
    ) -> bool:
        assert job_id == 9
        assert delivery_attempt == self.job["delivery_attempt"]
        self.job["delivery_status"] = "sent"
        return True

    async def mark_report_delivery_queued(
        self,
        job_id: int,
        *,
        delivery_attempt: int,
        sdk_outbound_id: int,
    ) -> bool:
        assert job_id == 9
        assert delivery_attempt == self.job["delivery_attempt"]
        self.job["delivery_status"] = "queued"
        self.job["sdk_outbound_id"] = sdk_outbound_id
        return True

    async def touch_report_delivery_check(
        self,
        job_id: int,
        *,
        delivery_attempt: int,
        sdk_outbound_id: int,
        error: str = "",
    ) -> bool:
        assert job_id == 9
        assert delivery_attempt == self.job["delivery_attempt"]
        assert sdk_outbound_id == self.job["sdk_outbound_id"]
        self.job["delivery_error"] = error
        return True

    async def mark_report_delivery_terminal(
        self,
        job_id: int,
        *,
        delivery_attempt: int,
        sdk_outbound_id: int,
        status: str,
        error: str = "",
    ) -> bool:
        assert job_id == 9
        assert delivery_attempt == self.job["delivery_attempt"]
        assert sdk_outbound_id == self.job["sdk_outbound_id"]
        self.job["delivery_status"] = status
        self.job["delivery_error"] = error
        return True

    async def mark_report_delivery_failed(
        self,
        job_id: int,
        error: str,
        *,
        delivery_attempt: int,
    ) -> bool:
        assert job_id == 9
        assert delivery_attempt == self.job["delivery_attempt"]
        self.job["delivery_status"] = "failed"
        self.job["delivery_error"] = error
        return True

    async def release_report_delivery(
        self,
        job_id: int,
        *,
        delivery_attempt: int,
        reason: str,
    ) -> bool:
        assert job_id == 9
        assert delivery_attempt == self.job["delivery_attempt"]
        self.job["delivery_status"] = "pending"
        self.job["delivery_error"] = reason
        return True


class _ReportBridge:
    def __init__(self, ledger: InMemoryGroupSpeechLedger) -> None:
        self.ledger = ledger
        self.calls: list[dict[str, object]] = []

    async def sdk_request(self, method: str, path: str, **kwargs) -> dict[str, object]:
        self.calls.append({"method": method, "path": path, **kwargs})
        if path == "/ext/roster/groups":
            return {
                "sessions": [
                    {
                        "session_id": "report-room@chatroom",
                        "session_name": "日报群",
                    }
                ]
            }
        if path == "/send":
            event = next(
                item
                for item in self.ledger.events
                if item.idempotency_key == "wxbot-report:9"
            )
            assert event.output_kind == "report"
            assert event.status == "reserved"
            assert event.speech_class == "required_delivery"
            return {"queued": True, "id": 19}
        if path == "/queue/messages/19":
            return {
                "id": 19,
                "session_id": "report-room@chatroom",
                "session_name": "日报群",
                "command_id": "wxbot-report:9",
                "status": "sent",
                "error": "",
            }
        return {"ok": True}


async def _allow_report_scope(_tenant_id: str, _session_id: str) -> bool:
    return True


@pytest.mark.asyncio
async def test_report_reserves_required_delivery_before_direct_sdk_send() -> None:
    ledger = InMemoryGroupSpeechLedger(now=lambda: _NOW)
    await _seed_humans(ledger, "report-room@chatroom")
    store = _ReportStore()
    bridge = _ReportBridge(ledger)
    service = WxbotReportService(
        store,
        SimpleNamespace(),
        bridge=bridge,
        speech_ledger=ledger,
        scope_execution_allowed=_allow_report_scope,
    )

    sent = await service.send_report_job(9)

    assert sent is True
    assert store.job["delivery_status"] == "sent"
    assert [call["path"] for call in bridge.calls] == [
        "/ext/roster/groups",
        "/send",
        "/queue/messages/19",
    ]
    event = next(
        item for item in ledger.events if item.idempotency_key == "wxbot-report:9"
    )
    assert event.status == "committed"
    assert event.speech_class == "required_delivery"


@pytest.mark.asyncio
async def test_report_subscription_bypasses_optional_speech_ratio_budget() -> None:
    ledger = InMemoryGroupSpeechLedger(now=lambda: _NOW)
    store = _ReportStore()
    bridge = _ReportBridge(ledger)
    service = WxbotReportService(
        store,
        SimpleNamespace(),
        bridge=bridge,
        speech_ledger=ledger,
        scope_execution_allowed=_allow_report_scope,
    )

    sent = await service.send_report_job(9)

    assert sent is True
    assert store.job["delivery_status"] == "sent"
    assert [call["path"] for call in bridge.calls] == [
        "/ext/roster/groups",
        "/send",
        "/queue/messages/19",
    ]


@pytest.mark.asyncio
async def test_report_subscription_bypasses_consecutive_bot_guard() -> None:
    ledger = InMemoryGroupSpeechLedger(now=lambda: _NOW)
    for index in range(2):
        reservation = await ledger.reserve(
            tenant_id="demo",
            session_id="report-room@chatroom",
            idempotency_key=f"prior-obligation-{index}",
            output_kind="ordinary",
            speech_class="obligation",
            text=f"前置机器人消息 {index}",
        )
        assert reservation.allowed is True
        await ledger.commit(reservation)
    store = _ReportStore()
    bridge = _ReportBridge(ledger)
    service = WxbotReportService(
        store,
        SimpleNamespace(),
        bridge=bridge,
        speech_ledger=ledger,
        scope_execution_allowed=_allow_report_scope,
    )

    sent = await service.send_report_job(9)

    assert sent is True
    assert store.job["delivery_status"] == "sent"
    assert [call["path"] for call in bridge.calls] == [
        "/ext/roster/groups",
        "/send",
        "/queue/messages/19",
    ]
    assert len(ledger.events) == 3
    report_event = next(
        item for item in ledger.events if item.idempotency_key == "wxbot-report:9"
    )
    assert report_event.status == "committed"
    assert report_event.speech_class == "required_delivery"


def test_0019_migration_owns_ledger_indexes_and_warmup_defaults() -> None:
    source = Path("migrations/versions/20260718_0019_group_speech_ledger.py").read_text(
        encoding="utf-8"
    )

    assert 'down_revision = "0018_social_policy_contract"' in source
    assert '"social_group_speech_ledger"' in source
    assert '"ix_social_group_speech_scope_occurred"' in source
    assert '"ix_social_group_speech_active_budget"' in source
    assert '"uq_social_group_speech_observed_message"' in source
    assert 'server_default="23:00"' in source
    assert 'server_default="180"' in source
