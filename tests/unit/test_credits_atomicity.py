from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from plugins.credits.router import build_credits_router
from plugins.credits.store import CreditIdempotencyConflict, CreditStore


@pytest_asyncio.fixture
async def credits_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncEngine]:
    database_path = (tmp_path / "credits-atomicity.sqlite3").resolve().as_posix()
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.execute(
            text(
                """
                CREATE TABLE plugin_credits_config (
                    tenant_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    enabled BOOLEAN DEFAULT FALSE,
                    credit_name TEXT DEFAULT '积分',
                    cost_per_chat INTEGER DEFAULT 0,
                    initial_credits INTEGER DEFAULT 100,
                    daily_checkin INTEGER DEFAULT 10,
                    streak_bonus INTEGER DEFAULT 5,
                    streak_cap INTEGER DEFAULT 50,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (tenant_id, session_id)
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE TABLE plugin_credits_balance (
                    tenant_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    display_name TEXT DEFAULT '',
                    credits INTEGER NOT NULL DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (tenant_id, session_id, user_id)
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE TABLE plugin_credits_checkin (
                    tenant_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    checkin_date DATE NOT NULL,
                    streak INTEGER NOT NULL DEFAULT 1,
                    reward INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (tenant_id, session_id, user_id, checkin_date)
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE TABLE plugin_credits_reservation (
                    reservation_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    reference TEXT DEFAULT '',
                    idempotency_key TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'reserved',
                    captured_amount INTEGER,
                    metadata_json TEXT DEFAULT '{}',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    captured_at TIMESTAMP,
                    released_at TIMESTAMP
                )
                """
            )
        )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX ux_plugin_credits_reservation_idempotency "
                "ON plugin_credits_reservation "
                "(tenant_id, session_id, user_id, idempotency_key) "
                "WHERE idempotency_key <> ''"
            )
        )
        await conn.execute(
            text(
                """
                CREATE TABLE plugin_credits_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    delta INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    actor TEXT DEFAULT '',
                    reference TEXT DEFAULT '',
                    idempotency_key TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX ux_plugin_credits_ledger_idempotency "
                "ON plugin_credits_ledger (idempotency_key) WHERE idempotency_key <> ''"
            )
        )
    monkeypatch.setattr("plugins.credits.store.get_engine", lambda: engine)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _seed_balance(engine: AsyncEngine, user_id: str, amount: int = 100) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO plugin_credits_balance "
                "(tenant_id, session_id, user_id, credits) VALUES ('tenant', 'room', :uid, :amount)"
            ),
            {"uid": user_id, "amount": amount},
        )


async def _subject_state(engine: AsyncEngine, user_id: str) -> dict[str, object]:
    async with engine.connect() as conn:
        balance = (
            (
                await conn.execute(
                    text(
                        "SELECT credits FROM plugin_credits_balance "
                        "WHERE tenant_id = 'tenant' AND session_id = 'room' AND user_id = :uid"
                    ),
                    {"uid": user_id},
                )
            )
            .mappings()
            .first()
        )
        reservation = (
            (
                await conn.execute(
                    text(
                        "SELECT reservation_id, amount, captured_amount, status "
                        "FROM plugin_credits_reservation WHERE user_id = :uid"
                    ),
                    {"uid": user_id},
                )
            )
            .mappings()
            .first()
        )
        ledger_rows = (
            (
                await conn.execute(
                    text(
                        "SELECT delta, idempotency_key FROM plugin_credits_ledger "
                        "WHERE user_id = :uid ORDER BY id"
                    ),
                    {"uid": user_id},
                )
            )
            .mappings()
            .all()
        )
    return {
        "balance": int(balance["credits"]) if balance is not None else None,
        "reservation": dict(reservation) if reservation is not None else None,
        "ledger": [dict(row) for row in ledger_rows],
    }


async def _balance(engine: AsyncEngine, user_id: str) -> int:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT credits FROM plugin_credits_balance "
                "WHERE tenant_id = 'tenant' AND session_id = 'room' AND user_id = :uid"
            ),
            {"uid": user_id},
        )
        row = result.mappings().first()
    assert row is not None
    return int(row["credits"])


async def _ledger_count(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(text("SELECT COUNT(*) FROM plugin_credits_ledger"))).scalar())


async def _checkin_state(engine: AsyncEngine, user_id: str) -> dict[str, object]:
    async with engine.connect() as conn:
        checkins = (
            await conn.execute(
                text(
                    "SELECT checkin_date, streak, reward FROM plugin_credits_checkin "
                    "WHERE tenant_id = 'tenant' AND session_id = 'room' AND user_id = :uid "
                    "ORDER BY checkin_date"
                ),
                {"uid": user_id},
            )
        ).mappings().all()
        ledger = (
            await conn.execute(
                text(
                    "SELECT delta, reason, actor, reference, idempotency_key "
                    "FROM plugin_credits_ledger WHERE user_id = :uid ORDER BY id"
                ),
                {"uid": user_id},
            )
        ).mappings().all()
    return {
        "balance": await _balance(engine, user_id),
        "checkins": [dict(row) for row in checkins],
        "ledger": [dict(row) for row in ledger],
    }


def _inject_failure(store: CreditStore, target: str):
    async def checkpoint(step: str) -> None:
        if step == target:
            raise RuntimeError(f"injected:{target}")

    return checkpoint


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "checkpoint",
    ["reserve_reservation", "reserve_balance_initialized", "reserve_balance_debit"],
)
async def test_reserve_rolls_back_after_every_intermediate_statement(
    credits_engine: AsyncEngine,
    checkpoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = f"reserve-{checkpoint}"
    await _seed_balance(credits_engine, user_id)
    store = CreditStore(settings=None)
    monkeypatch.setattr(store, "_financial_checkpoint", _inject_failure(store, checkpoint))

    with pytest.raises(RuntimeError, match=f"injected:{checkpoint}"):
        await store.reserve_charge(
            "tenant",
            "room",
            user_id,
            10,
            reason="chat_cost",
            idempotency_key=f"request:{checkpoint}",
        )

    assert await _subject_state(credits_engine, user_id) == {
        "balance": 100,
        "reservation": None,
        "ledger": [],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint", ["capture_status", "capture_refund", "capture_ledger"])
async def test_capture_rolls_back_after_every_intermediate_statement(
    credits_engine: AsyncEngine,
    checkpoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = f"capture-{checkpoint}"
    store = CreditStore(settings=None)
    reservation = await store.reserve_charge(
        "tenant",
        "room",
        user_id,
        10,
        reason="chat_cost",
        idempotency_key=f"request:{checkpoint}",
    )
    monkeypatch.setattr(store, "_financial_checkpoint", _inject_failure(store, checkpoint))

    with pytest.raises(RuntimeError, match=f"injected:{checkpoint}"):
        await store.capture_reservation(str(reservation["reservation_id"]), amount=4)

    state = await _subject_state(credits_engine, user_id)
    assert state["balance"] == 90
    assert state["reservation"] == {
        "reservation_id": reservation["reservation_id"],
        "amount": 10,
        "captured_amount": None,
        "status": "reserved",
    }
    assert state["ledger"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint", ["release_status", "release_refund"])
async def test_release_rolls_back_after_every_intermediate_statement(
    credits_engine: AsyncEngine,
    checkpoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = f"release-{checkpoint}"
    store = CreditStore(settings=None)
    reservation = await store.reserve_charge(
        "tenant",
        "room",
        user_id,
        10,
        reason="chat_cost",
        idempotency_key=f"request:{checkpoint}",
    )
    monkeypatch.setattr(store, "_financial_checkpoint", _inject_failure(store, checkpoint))

    with pytest.raises(RuntimeError, match=f"injected:{checkpoint}"):
        await store.release_reservation(str(reservation["reservation_id"]))

    state = await _subject_state(credits_engine, user_id)
    assert state["balance"] == 90
    assert state["reservation"] == {
        "reservation_id": reservation["reservation_id"],
        "amount": 10,
        "captured_amount": None,
        "status": "reserved",
    }
    assert state["ledger"] == []


@pytest.mark.asyncio
async def test_concurrent_reserve_with_one_idempotency_key_debits_once(
    credits_engine: AsyncEngine,
) -> None:
    store = CreditStore(settings=None)

    reservations = await asyncio.gather(
        *(
            store.reserve_charge(
                "tenant",
                "room",
                "reserve-race",
                10,
                reason="chat_cost",
                reference="trace-race",
                idempotency_key="chat:llm:trace-race",
            )
            for _ in range(12)
        )
    )

    assert len({str(item["reservation_id"]) for item in reservations}) == 1
    state = await _subject_state(credits_engine, "reserve-race")
    assert state["balance"] == 90
    assert state["ledger"] == []


@pytest.mark.asyncio
async def test_reserve_rejects_conflicting_payload_for_idempotency_key_without_second_debit(
    credits_engine: AsyncEngine,
) -> None:
    store = CreditStore(settings=None)
    await store.reserve_charge(
        "tenant",
        "room",
        "reserve-conflict",
        10,
        reason="chat_cost",
        idempotency_key="same-request",
    )

    with pytest.raises(ValueError, match="different reservation"):
        await store.reserve_charge(
            "tenant",
            "room",
            "reserve-conflict",
            11,
            reason="chat_cost",
            idempotency_key="same-request",
        )

    state = await _subject_state(credits_engine, "reserve-conflict")
    assert state["balance"] == 90
    assert state["ledger"] == []


@pytest.mark.asyncio
async def test_insufficient_reserve_does_not_leave_a_reservation(
    credits_engine: AsyncEngine,
) -> None:
    store = CreditStore(settings=None)
    await _seed_balance(credits_engine, "reserve-insufficient", amount=3)

    with pytest.raises(ValueError, match="余额不足"):
        await store.reserve_charge(
            "tenant",
            "room",
            "reserve-insufficient",
            4,
            reason="chat_cost",
            idempotency_key="insufficient-request",
        )

    assert await _subject_state(credits_engine, "reserve-insufficient") == {
        "balance": 3,
        "reservation": None,
        "ledger": [],
    }


@pytest.mark.asyncio
async def test_concurrent_capture_refunds_and_records_ledger_once(
    credits_engine: AsyncEngine,
) -> None:
    store = CreditStore(settings=None)
    reservation = await store.reserve_charge(
        "tenant",
        "room",
        "capture-race",
        10,
        reason="chat_cost",
        idempotency_key="capture-race",
    )

    results = await asyncio.gather(
        *(
            store.capture_reservation(str(reservation["reservation_id"]), amount=4)
            for _ in range(12)
        )
    )

    assert all(result is not None and result["amount"] == 4 for result in results)
    state = await _subject_state(credits_engine, "capture-race")
    assert state["balance"] == 96
    assert state["reservation"] == {
        "reservation_id": reservation["reservation_id"],
        "amount": 10,
        "captured_amount": 4,
        "status": "captured",
    }
    assert state["ledger"] == [
        {
            "delta": -4,
            "idempotency_key": f"credits:capture:{reservation['reservation_id']}",
        }
    ]


@pytest.mark.asyncio
async def test_concurrent_release_refunds_once(
    credits_engine: AsyncEngine,
) -> None:
    store = CreditStore(settings=None)
    reservation = await store.reserve_charge(
        "tenant",
        "room",
        "release-race",
        10,
        reason="chat_cost",
        idempotency_key="release-race",
    )

    await asyncio.gather(
        *(store.release_reservation(str(reservation["reservation_id"])) for _ in range(12))
    )

    state = await _subject_state(credits_engine, "release-race")
    assert state["balance"] == 100
    assert state["reservation"] == {
        "reservation_id": reservation["reservation_id"],
        "amount": 10,
        "captured_amount": None,
        "status": "released",
    }
    assert state["ledger"] == []


@pytest.mark.asyncio
async def test_capture_release_race_has_exactly_one_net_settlement(
    credits_engine: AsyncEngine,
) -> None:
    store = CreditStore(settings=None)
    reservation = await store.reserve_charge(
        "tenant",
        "room",
        "settlement-race",
        10,
        reason="chat_cost",
        idempotency_key="settlement-race",
    )
    reservation_id = str(reservation["reservation_id"])

    await asyncio.gather(
        *(store.capture_reservation(reservation_id, amount=5) for _ in range(8)),
        *(store.release_reservation(reservation_id) for _ in range(8)),
    )

    state = await _subject_state(credits_engine, "settlement-race")
    reservation_state = state["reservation"]
    assert isinstance(reservation_state, dict)
    if reservation_state["status"] == "captured":
        assert state["balance"] == 95
        assert reservation_state["captured_amount"] == 5
        assert state["ledger"] == [
            {
                "delta": -5,
                "idempotency_key": f"credits:capture:{reservation_id}",
            }
        ]
    else:
        assert reservation_state["status"] == "released"
        assert state["balance"] == 100
        assert state["ledger"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "checkpoint",
    [
        "checkin_balance_initialized",
        "checkin_record",
        "checkin_balance_reward",
        "checkin_ledger",
    ],
)
async def test_checkin_record_balance_and_ledger_roll_back_together(
    credits_engine: AsyncEngine,
    checkpoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("plugins.credits.store._today_cn", lambda: date(2026, 7, 18))
    await _seed_balance(credits_engine, "checkin-failure", amount=100)
    store = CreditStore(settings=None)
    monkeypatch.setattr(store, "_financial_checkpoint", _inject_failure(store, checkpoint))

    with pytest.raises(RuntimeError, match=f"injected:{checkpoint}"):
        await store.checkin("tenant", "room", "checkin-failure")

    assert await _checkin_state(credits_engine, "checkin-failure") == {
        "balance": 100,
        "checkins": [],
        "ledger": [],
    }


@pytest.mark.asyncio
async def test_checkin_retry_after_failure_awards_once(
    credits_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("plugins.credits.store._today_cn", lambda: date(2026, 7, 18))
    await _seed_balance(credits_engine, "checkin-retry", amount=100)
    store = CreditStore(settings=None)
    failed = False

    async def fail_once(step: str) -> None:
        nonlocal failed
        if step == "checkin_balance_reward" and not failed:
            failed = True
            raise RuntimeError("injected-checkin-crash")

    monkeypatch.setattr(store, "_financial_checkpoint", fail_once)
    with pytest.raises(RuntimeError, match="injected-checkin-crash"):
        await store.checkin("tenant", "room", "checkin-retry")

    retried = await store.checkin("tenant", "room", "checkin-retry")
    replayed = await store.checkin("tenant", "room", "checkin-retry")
    state = await _checkin_state(credits_engine, "checkin-retry")

    assert retried["checked_in"] is True
    assert retried["reward"] == 10
    assert retried["balance"] == 110
    assert replayed["checked_in"] is False
    assert replayed["already_checked_in"] is True
    assert replayed["reward"] == 10
    assert replayed["streak"] == 1
    assert replayed["balance"] == 110
    assert state["balance"] == 110
    assert len(state["checkins"]) == 1
    assert state["checkins"][0]["streak"] == 1
    assert state["checkins"][0]["reward"] == 10
    assert len(state["ledger"]) == 1
    assert state["ledger"][0]["delta"] == 10
    assert state["ledger"][0]["reason"] == "checkin"
    assert state["ledger"][0]["actor"] == "system"


@pytest.mark.asyncio
async def test_checkin_retry_after_post_commit_read_failure_returns_existing_award(
    credits_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("plugins.credits.store._today_cn", lambda: date(2026, 7, 18))
    await _seed_balance(credits_engine, "checkin-post-commit", amount=100)
    store = CreditStore(settings=None)
    original_status = store.get_checkin_status
    failed = False

    async def fail_once(tenant_id: str, session_id: str, user_id: str) -> dict[str, object]:
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("injected-status-read-failure")
        return await original_status(tenant_id, session_id, user_id)

    monkeypatch.setattr(store, "get_checkin_status", fail_once)
    with pytest.raises(RuntimeError, match="injected-status-read-failure"):
        await store.checkin("tenant", "room", "checkin-post-commit")

    replayed = await store.checkin("tenant", "room", "checkin-post-commit")
    state = await _checkin_state(credits_engine, "checkin-post-commit")

    assert replayed["already_checked_in"] is True
    assert replayed["reward"] == 10
    assert replayed["streak"] == 1
    assert replayed["balance"] == 110
    assert state["balance"] == 110
    assert len(state["checkins"]) == 1
    assert len(state["ledger"]) == 1
    assert state["ledger"][0]["delta"] == 10


@pytest.mark.asyncio
async def test_concurrent_same_day_checkins_award_once(
    credits_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("plugins.credits.store._today_cn", lambda: date(2026, 7, 18))
    await _seed_balance(credits_engine, "checkin-race", amount=100)
    store = CreditStore(settings=None)

    results = await asyncio.gather(
        *(store.checkin("tenant", "room", "checkin-race") for _ in range(12))
    )
    state = await _checkin_state(credits_engine, "checkin-race")

    assert sum(item["checked_in"] is True for item in results) == 1
    assert sum(item["already_checked_in"] is True for item in results) == 11
    assert {int(item["balance"]) for item in results} == {110}
    assert state["balance"] == 110
    assert len(state["checkins"]) == 1
    assert len(state["ledger"]) == 1
    assert state["ledger"][0]["delta"] == 10


@pytest.mark.asyncio
async def test_checkin_merges_weekly_bonus_into_one_atomic_ledger_entry(
    credits_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("plugins.credits.store._today_cn", lambda: date(2026, 7, 18))
    await _seed_balance(credits_engine, "checkin-streak", amount=100)
    async with credits_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO plugin_credits_config "
                "(tenant_id, session_id, initial_credits, daily_checkin, streak_bonus, "
                "streak_cap) VALUES ('tenant', 'room', 100, 10, 5, 50)"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO plugin_credits_checkin "
                "(tenant_id, session_id, user_id, checkin_date, streak, reward) "
                "VALUES ('tenant', 'room', 'checkin-streak', '2026-07-17', 6, 10)"
            )
        )

    result = await CreditStore(settings=None).checkin(
        "tenant",
        "room",
        "checkin-streak",
    )
    state = await _checkin_state(credits_engine, "checkin-streak")

    assert result["streak"] == 7
    assert result["bonus"] == 5
    assert result["reward"] == 15
    assert result["balance"] == 115
    assert len(state["ledger"]) == 1
    assert state["ledger"][0]["delta"] == 15
    assert "base:10;bonus:5" in str(state["ledger"][0]["reference"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "checkpoint",
    [
        "adjust_claim",
        "adjust_balance_initialized",
        "adjust_balance_mutated",
        "adjust_ledger",
    ],
)
async def test_adjust_balance_and_ledger_roll_back_together(
    credits_engine: AsyncEngine,
    checkpoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_balance(credits_engine, "adjust-failure")
    store = CreditStore(settings=None)
    monkeypatch.setattr(store, "_financial_checkpoint", _inject_failure(store, checkpoint))

    with pytest.raises(RuntimeError, match=f"injected:{checkpoint}"):
        await store.adjust(
            "tenant",
            "room",
            "adjust-failure",
            -25,
            "admin_adjust",
            actor="admin",
            idempotency_key=f"adjust:{checkpoint}",
        )

    assert await _balance(credits_engine, "adjust-failure") == 100
    assert await _ledger_count(credits_engine) == 0


@pytest.mark.asyncio
async def test_set_balance_is_atomic_and_replay_returns_original_result(
    credits_engine: AsyncEngine,
) -> None:
    await _seed_balance(credits_engine, "set-member", amount=40)
    store = CreditStore(settings=None)

    first = await store.set_balance(
        "tenant",
        "room",
        "set-member",
        75,
        "admin_set",
        actor="admin",
        idempotency_key="set-request-1",
    )
    await store.adjust("tenant", "room", "set-member", 5, "other_change")
    replay = await store.set_balance(
        "tenant",
        "room",
        "set-member",
        75,
        "admin_set",
        actor="admin",
        idempotency_key="set-request-1",
    )

    assert first == replay == 75
    assert await _balance(credits_engine, "set-member") == 80
    assert await _ledger_count(credits_engine) == 2


@pytest.mark.asyncio
async def test_adjust_idempotency_conflict_does_not_mutate_balance(
    credits_engine: AsyncEngine,
) -> None:
    await _seed_balance(credits_engine, "adjust-conflict")
    store = CreditStore(settings=None)
    await store.adjust(
        "tenant",
        "room",
        "adjust-conflict",
        10,
        "admin_adjust",
        actor="admin",
        idempotency_key="adjust-same-key",
    )

    with pytest.raises(CreditIdempotencyConflict):
        await store.adjust(
            "tenant",
            "room",
            "adjust-conflict",
            11,
            "admin_adjust",
            actor="admin",
            idempotency_key="adjust-same-key",
        )

    assert await _balance(credits_engine, "adjust-conflict") == 110
    assert await _ledger_count(credits_engine) == 1


@pytest.mark.asyncio
async def test_concurrent_adjustments_keep_balance_and_ledger_in_sync(
    credits_engine: AsyncEngine,
) -> None:
    await _seed_balance(credits_engine, "adjust-race", amount=100)
    store = CreditStore(settings=None)

    await asyncio.gather(
        *(
            store.adjust(
                "tenant",
                "room",
                "adjust-race",
                1,
                "admin_adjust",
                actor="admin",
                idempotency_key=f"adjust-race-{index}",
            )
            for index in range(20)
        )
    )

    async with credits_engine.connect() as conn:
        ledger = (
            await conn.execute(
                text(
                    "SELECT COUNT(*) AS entries, COALESCE(SUM(delta), 0) AS total_delta "
                    "FROM plugin_credits_ledger WHERE user_id = 'adjust-race'"
                )
            )
        ).mappings().one()
    assert await _balance(credits_engine, "adjust-race") == 120
    assert dict(ledger) == {"entries": 20, "total_delta": 20}


@pytest.mark.asyncio
async def test_adjust_retry_after_transaction_failure_charges_once(
    credits_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_balance(credits_engine, "adjust-retry", amount=100)
    store = CreditStore(settings=None)
    failed = False

    async def fail_once(step: str) -> None:
        nonlocal failed
        if step == "adjust_balance_mutated" and not failed:
            failed = True
            raise RuntimeError("injected-adjust-crash")

    monkeypatch.setattr(store, "_financial_checkpoint", fail_once)
    with pytest.raises(RuntimeError, match="injected-adjust-crash"):
        await store.adjust(
            "tenant",
            "room",
            "adjust-retry",
            -10,
            "admin_adjust",
            actor="admin",
            idempotency_key="adjust-retry-key",
        )

    retried = await store.adjust(
        "tenant",
        "room",
        "adjust-retry",
        -10,
        "admin_adjust",
        actor="admin",
        idempotency_key="adjust-retry-key",
    )
    assert retried == 90
    assert await _balance(credits_engine, "adjust-retry") == 90
    assert await _ledger_count(credits_engine) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "checkpoint",
    [
        "transfer_claim",
        "transfer_balances_initialized",
        "transfer_sender_debit",
        "transfer_recipient_credit",
        "transfer_ledger_out",
        "transfer_ledger_in",
    ],
)
async def test_transfer_balances_and_both_ledgers_roll_back_together(
    credits_engine: AsyncEngine,
    checkpoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_balance(credits_engine, "transfer-source", amount=100)
    await _seed_balance(credits_engine, "transfer-target", amount=20)
    store = CreditStore(settings=None)
    monkeypatch.setattr(store, "_financial_checkpoint", _inject_failure(store, checkpoint))

    with pytest.raises(RuntimeError, match=f"injected:{checkpoint}"):
        await store.transfer(
            "tenant",
            "room",
            "transfer-source",
            "transfer-target",
            25,
            actor="admin",
            idempotency_key=f"transfer:{checkpoint}",
        )

    assert await _balance(credits_engine, "transfer-source") == 100
    assert await _balance(credits_engine, "transfer-target") == 20
    assert await _ledger_count(credits_engine) == 0


@pytest.mark.asyncio
async def test_concurrent_transfers_cannot_overdraw_sender(
    credits_engine: AsyncEngine,
) -> None:
    await _seed_balance(credits_engine, "race-source", amount=100)
    await _seed_balance(credits_engine, "race-target", amount=0)
    store = CreditStore(settings=None)

    results = await asyncio.gather(
        *(
            store.transfer(
                "tenant",
                "room",
                "race-source",
                "race-target",
                20,
                actor="admin",
                idempotency_key=f"race-transfer-{index}",
            )
            for index in range(10)
        ),
        return_exceptions=True,
    )

    successes = [item for item in results if isinstance(item, dict)]
    failures = [item for item in results if isinstance(item, ValueError)]
    assert len(successes) == 5
    assert len(failures) == 5
    assert await _balance(credits_engine, "race-source") == 0
    assert await _balance(credits_engine, "race-target") == 100
    assert await _ledger_count(credits_engine) == 10


@pytest.mark.asyncio
async def test_concurrent_transfer_replay_debits_and_credits_once(
    credits_engine: AsyncEngine,
) -> None:
    await _seed_balance(credits_engine, "idem-source", amount=100)
    await _seed_balance(credits_engine, "idem-target", amount=0)
    store = CreditStore(settings=None)

    results = await asyncio.gather(
        *(
            store.transfer(
                "tenant",
                "room",
                "idem-source",
                "idem-target",
                30,
                actor="admin",
                reference="gift",
                idempotency_key="transfer-one-request",
            )
            for _ in range(12)
        )
    )

    assert results == [{"from_balance": 70, "to_balance": 30}] * 12
    assert await _balance(credits_engine, "idem-source") == 70
    assert await _balance(credits_engine, "idem-target") == 30
    assert await _ledger_count(credits_engine) == 2


@pytest.mark.asyncio
async def test_transfer_retry_after_transaction_failure_moves_credits_once(
    credits_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_balance(credits_engine, "transfer-retry-source", amount=100)
    await _seed_balance(credits_engine, "transfer-retry-target", amount=0)
    store = CreditStore(settings=None)
    failed = False

    async def fail_once(step: str) -> None:
        nonlocal failed
        if step == "transfer_recipient_credit" and not failed:
            failed = True
            raise RuntimeError("injected-transfer-crash")

    monkeypatch.setattr(store, "_financial_checkpoint", fail_once)
    with pytest.raises(RuntimeError, match="injected-transfer-crash"):
        await store.transfer(
            "tenant",
            "room",
            "transfer-retry-source",
            "transfer-retry-target",
            30,
            actor="admin",
            idempotency_key="transfer-retry-key",
        )

    retried = await store.transfer(
        "tenant",
        "room",
        "transfer-retry-source",
        "transfer-retry-target",
        30,
        actor="admin",
        idempotency_key="transfer-retry-key",
    )
    assert retried == {"from_balance": 70, "to_balance": 30}
    assert await _balance(credits_engine, "transfer-retry-source") == 70
    assert await _balance(credits_engine, "transfer-retry-target") == 30
    assert await _ledger_count(credits_engine) == 2


@pytest.mark.asyncio
async def test_transfer_replay_returns_original_result_and_payload_conflict_is_rejected(
    credits_engine: AsyncEngine,
) -> None:
    await _seed_balance(credits_engine, "replay-source", amount=100)
    await _seed_balance(credits_engine, "replay-target", amount=0)
    store = CreditStore(settings=None)
    first = await store.transfer(
        "tenant",
        "room",
        "replay-source",
        "replay-target",
        25,
        actor="admin",
        reference="gift",
        idempotency_key="transfer-replay",
    )
    await store.adjust("tenant", "room", "replay-source", 10, "other_change")
    replay = await store.transfer(
        "tenant",
        "room",
        "replay-source",
        "replay-target",
        25,
        actor="admin",
        reference="gift",
        idempotency_key="transfer-replay",
    )
    with pytest.raises(CreditIdempotencyConflict):
        await store.transfer(
            "tenant",
            "room",
            "replay-source",
            "replay-target",
            26,
            actor="admin",
            reference="gift",
            idempotency_key="transfer-replay",
        )

    assert first == replay == {"from_balance": 75, "to_balance": 25}
    assert await _balance(credits_engine, "replay-source") == 85
    assert await _balance(credits_engine, "replay-target") == 25
    assert await _ledger_count(credits_engine) == 3


@pytest.mark.asyncio
async def test_admin_routes_require_idempotency_and_replay_exact_response(
    credits_engine: AsyncEngine,
) -> None:
    await _seed_balance(credits_engine, "route-member", amount=100)
    app = FastAPI()
    app.include_router(build_credits_router(CreditStore(settings=None)))
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        payload = {
            "tenant_id": "tenant",
            "session_id": "room",
            "user_id": "route-member",
            "mode": "delta",
            "delta": 10,
            "reason": "admin_adjust",
        }
        missing = await client.post("/adjust", json=payload)
        first = await client.post(
            "/adjust",
            json=payload,
            headers={"Idempotency-Key": "route-adjust-one"},
        )
        await client.post(
            "/adjust",
            json={**payload, "delta": 5},
            headers={"Idempotency-Key": "route-adjust-two"},
        )
        replay = await client.post(
            "/adjust",
            json=payload,
            headers={"Idempotency-Key": "route-adjust-one"},
        )
        conflict = await client.post(
            "/adjust",
            json={**payload, "delta": 11},
            headers={"Idempotency-Key": "route-adjust-one"},
        )

    assert missing.status_code == 428
    assert missing.json()["detail"] == "idempotency_key_required"
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json() == {
        "user_id": "route-member",
        "credits": 110,
        "mode": "delta",
    }
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == {"code": "idempotency_conflict"}
    assert await _balance(credits_engine, "route-member") == 115


@pytest.mark.asyncio
async def test_admin_transfer_route_replays_and_maps_payload_conflict(
    credits_engine: AsyncEngine,
) -> None:
    await _seed_balance(credits_engine, "route-source", amount=100)
    await _seed_balance(credits_engine, "route-target", amount=0)
    app = FastAPI()
    app.include_router(build_credits_router(CreditStore(settings=None)))
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        payload = {
            "tenant_id": "tenant",
            "session_id": "room",
            "from_user_id": "route-source",
            "to_user_id": "route-target",
            "amount": 20,
            "reason": "admin_transfer",
        }
        missing = await client.post("/transfer", json=payload)
        invalid = await client.post(
            "/transfer",
            json=payload,
            headers={"Idempotency-Key": " "},
        )
        first = await client.post(
            "/transfer",
            json=payload,
            headers={"Idempotency-Key": "route-transfer-one"},
        )
        await client.post(
            "/adjust",
            json={
                "tenant_id": "tenant",
                "session_id": "room",
                "user_id": "route-source",
                "mode": "delta",
                "delta": 5,
            },
            headers={"Idempotency-Key": "route-adjust-after-transfer"},
        )
        replay = await client.post(
            "/transfer",
            json=payload,
            headers={"Idempotency-Key": "route-transfer-one"},
        )
        conflict = await client.post(
            "/transfer",
            json={**payload, "amount": 21},
            headers={"Idempotency-Key": "route-transfer-one"},
        )

    assert missing.status_code == 428
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "invalid_idempotency_key"
    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json() == {
        "from_user_id": "route-source",
        "to_user_id": "route-target",
        "amount": 20,
        "from_balance": 80,
        "to_balance": 20,
    }
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == {"code": "idempotency_conflict"}
    assert await _balance(credits_engine, "route-source") == 85
    assert await _balance(credits_engine, "route-target") == 20


@pytest.mark.asyncio
async def test_config_route_requires_if_match_and_rejects_stale_writes(
    credits_engine: AsyncEngine,
) -> None:
    app = FastAPI()
    app.include_router(build_credits_router(CreditStore(settings=None)))
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        initial = await client.get("/config/tenant/room")
        initial_etag = initial.headers["etag"]
        missing = await client.post(
            "/config/tenant/room",
            json={"enabled": True},
        )
        invalid = await client.post(
            "/config/tenant/room",
            json={"enabled": True},
            headers={"If-Match": "not-an-etag"},
        )
        created = await client.post(
            "/config/tenant/room",
            json={"enabled": True, "credit_name": "群积分"},
            headers={"If-Match": initial_etag},
        )
        created_etag = created.headers["etag"]
        stale = await client.post(
            "/config/tenant/room",
            json={"enabled": False},
            headers={"If-Match": initial_etag},
        )
        current = await client.get("/config/tenant/room")
        negative_initial_balance = await client.post(
            "/config/tenant/room",
            json={"initial_credits": -1},
            headers={"If-Match": created_etag},
        )

    assert initial.status_code == 200
    assert initial.headers["cache-control"] == "no-store"
    assert initial_etag.startswith('"credits-config-')
    assert missing.status_code == 428
    assert missing.json()["detail"] == "if_match_required"
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "invalid_if_match"
    assert created.status_code == 200
    assert created.json()["enabled"] is True
    assert created.json()["credit_name"] == "群积分"
    assert created_etag != initial_etag
    assert stale.status_code == 409
    assert stale.headers["etag"] == created_etag
    assert stale.json()["detail"] == {
        "code": "version_conflict",
        "expected_etag": initial_etag,
        "current_etag": created_etag,
    }
    assert current.headers["etag"] == created_etag
    assert current.json()["enabled"] is True
    assert current.json()["credit_name"] == "群积分"
    assert negative_initial_balance.status_code == 400
    assert "初始积分 不能小于 0" in negative_initial_balance.json()["detail"]


@pytest.mark.asyncio
async def test_concurrent_config_writes_with_one_etag_have_one_winner(
    credits_engine: AsyncEngine,
) -> None:
    app = FastAPI()
    app.include_router(build_credits_router(CreditStore(settings=None)))
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        initial = await client.get("/config/tenant/room")
        etag = initial.headers["etag"]
        responses = await asyncio.gather(
            *(
                client.post(
                    "/config/tenant/room",
                    json={"credit_name": name},
                    headers={"If-Match": etag},
                )
                for name in ("积分甲", "积分乙")
            )
        )
        current = await client.get("/config/tenant/room")

    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response for response in responses if response.status_code == 200)
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.headers["etag"] == winner.headers["etag"]
    assert current.headers["etag"] == winner.headers["etag"]
    assert current.json()["credit_name"] == winner.json()["credit_name"]
