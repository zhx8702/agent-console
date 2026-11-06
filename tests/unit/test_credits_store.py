from __future__ import annotations

from datetime import date

import pytest

from app.channel.identity import preferred_external_user_id
from plugins.credits.store import CreditStore, draw_quality_cost_for_config


def test_preferred_external_user_id_skips_hashed_channel_identity() -> None:
    hashed = "cx1:p:5591a89ab37df0027016fefee052ae7503b0cd937df6170b"

    assert preferred_external_user_id(hashed, "zhx870255124") == "zhx870255124"
    assert preferred_external_user_id(hashed, hashed, hashed) == hashed
    assert preferred_external_user_id("", None, "wxid_real") == "wxid_real"
    assert preferred_external_user_id() == ""


def test_draw_quality_cost_for_config_defaults_by_quality() -> None:
    cfg = {}

    assert draw_quality_cost_for_config(cfg, "low") == 5
    assert draw_quality_cost_for_config(cfg, "medium") == 10
    assert draw_quality_cost_for_config(cfg, "high") == 20


def test_draw_quality_cost_for_config_uses_configured_mapping() -> None:
    cfg = {"draw_quality_costs": {"low": 3, "medium": 7, "high": 15}}

    assert draw_quality_cost_for_config(cfg, "medium") == 7


@pytest.mark.asyncio
async def test_get_balance_rejects_empty_user_id() -> None:
    store = CreditStore(settings=None)

    with pytest.raises(ValueError, match="user_id is required"):
        await store.get_balance("demo", "group@chatroom", "")


@pytest.mark.asyncio
async def test_list_members_filters_blank_user_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    executed_sql: list[str] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        executed_sql.append(sql)
        if "WITH ranked AS" in sql:
            return []
        if "COUNT(*) AS member_count" in sql:
            return [{"member_count": 0, "total_credits": 0}]
        if "COUNT(DISTINCT user_id) AS checked_in_today_count" in sql:
            return [{"checked_in_today_count": 0}]
        if "COUNT(*) AS filtered_count" in sql:
            return [{"filtered_count": 0}]
        return []

    monkeypatch.setattr("plugins.credits.store._today_cn", lambda: date(2026, 4, 23))
    monkeypatch.setattr("plugins.credits.store._exec", fake_exec)

    store = CreditStore(settings=None)
    await store.list_members("demo", "group@chatroom")

    assert executed_sql
    assert "COALESCE(BTRIM(b.user_id), '') <> ''" in executed_sql[0]
    assert any("COALESCE(BTRIM(user_id), '') <> ''" in sql for sql in executed_sql[1:])
