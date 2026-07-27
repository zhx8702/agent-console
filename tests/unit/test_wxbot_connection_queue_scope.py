from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.channel.identity import LEGACY_WXBOT_CONNECTION_ID
from plugins.wxbot.store import WxbotStore


def _queue_row(reply_id: int, connection_id: str, claim_token: str) -> dict[str, object]:
    return {
        "id": reply_id,
        "tenant_id": "demo",
        "connection_id": connection_id,
        "source_message_json": "{}",
        "delivery_json": "{}",
        "claim_token": claim_token,
    }


@pytest.mark.asyncio
async def test_enqueue_persists_scope_from_source_event_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        values: dict[str, object] = dict(params or {})
        calls.append((sql, values))
        if sql.startswith("INSERT INTO plugin_wxbot_reply_queue"):
            return [{"id": 20}]
        return []

    monkeypatch.setattr("plugins.wxbot.store._exec", fake_exec)

    reply_id = await WxbotStore(SimpleNamespace()).enqueue_reply(
        "demo",
        "room@chatroom",
        "群",
        "成员",
        "处理中",
        source_message={"connection_id": "wechat-primary"},
        delivery={"response_kind": "tool_progress"},
    )

    assert reply_id == 20
    insert_sql, insert_params = calls[0]
    assert "(tenant_id, connection_id, session_id" in insert_sql
    assert insert_params["connection_id"] == "wechat-primary"
    assert '"connection_id": "wechat-primary"' in str(insert_params["delivery_json"])


@pytest.mark.asyncio
async def test_enqueue_canonicalizes_both_legacy_scope_forms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies: dict[tuple[str, str, str], int] = {}
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        values: dict[str, object] = dict(params or {})
        calls.append((sql, values))
        key = (
            str(values.get("tid") or ""),
            str(values.get("connection_id") or ""),
            str(values.get("command_id") or ""),
        )
        if sql.startswith("INSERT INTO plugin_wxbot_reply_queue"):
            if key in replies:
                return []
            reply_id = len(replies) + 1
            replies[key] = reply_id
            return [{"id": reply_id}]
        if sql.startswith("SELECT id FROM plugin_wxbot_reply_queue"):
            reply_id = replies.get(key)
            return [{"id": reply_id}] if reply_id is not None else []
        return []

    monkeypatch.setattr("plugins.wxbot.store._exec", fake_exec)
    store = WxbotStore(SimpleNamespace())

    empty_legacy = await store.enqueue_reply(
        "demo",
        "room",
        "群",
        "成员",
        "A",
        command_id="legacy-command",
    )
    explicit_legacy = await store.enqueue_reply(
        "demo",
        "room",
        "群",
        "成员",
        "B",
        delivery={"connection_id": LEGACY_WXBOT_CONNECTION_ID},
        command_id="legacy-command",
    )

    assert (empty_legacy, explicit_legacy) == (1, 1)
    assert list(replies) == [
        ("demo", LEGACY_WXBOT_CONNECTION_ID, "legacy-command")
    ]
    scoped_calls = [
        params
        for sql, params in calls
        if sql.startswith(
            ("INSERT INTO plugin_wxbot_reply_queue", "SELECT id FROM plugin_wxbot_reply_queue")
        )
    ]
    assert scoped_calls
    assert all(
        params["connection_id"] == LEGACY_WXBOT_CONNECTION_ID
        for params in scoped_calls
    )


@pytest.mark.asyncio
async def test_claim_fenced_transition_checks_connection_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        values: dict[str, object] = dict(params or {})
        calls.append((sql, values))
        return [{"id": 30}]

    monkeypatch.setattr("plugins.wxbot.store._exec", fake_exec)

    updated = await WxbotStore(SimpleNamespace()).mark_reply_queued(
        30,
        tenant_id="demo",
        connection_id="wechat-primary",
        claim_token="claim-30",
    )

    assert updated is True
    sql, params = calls[0]
    assert "connection_id = :connection_id" in sql
    assert params["connection_id"] == "wechat-primary"
    assert params["claim_token"] == "claim-30"


@pytest.mark.asyncio
async def test_delivery_event_transition_checks_supplied_connection_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        values: dict[str, object] = dict(params or {})
        calls.append((sql, values))
        return []

    monkeypatch.setattr("plugins.wxbot.store._exec", fake_exec)

    await WxbotStore(SimpleNamespace()).mark_reply_delivery_succeeded(
        "command-40",
        tenant_id="demo",
        connection_id="wechat-primary",
    )

    sql, params = calls[0]
    assert "connection_id = :connection_id" in sql
    assert params["connection_id"] == "wechat-primary"


@pytest.mark.asyncio
async def test_managed_connections_only_claim_their_own_reply_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = [
        {"id": 1, "connection_id": "wechat-primary"},
        {"id": 2, "connection_id": "wechat-backup"},
    ]
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        values: dict[str, object] = dict(params or {})
        calls.append((sql, values))
        if "WITH picked AS" not in sql:
            return []
        for index, row in enumerate(pending):
            if row["connection_id"] == values.get("connection_id"):
                picked = pending.pop(index)
                return [
                    _queue_row(
                        int(picked["id"]),
                        str(picked["connection_id"]),
                        str(values["claim_token"]),
                    )
                ]
        return []

    monkeypatch.setattr("plugins.wxbot.store._exec", fake_exec)
    store = WxbotStore(SimpleNamespace())

    primary = await store.claim_pending_reply(
        "demo",
        connection_id="wechat-primary",
        claim_owner="primary-worker",
    )
    backup = await store.claim_pending_reply(
        "demo",
        connection_id="wechat-backup",
        claim_owner="backup-worker",
    )

    assert primary is not None and primary["id"] == 1
    assert backup is not None and backup["id"] == 2
    assert primary["connection_id"] == "wechat-primary"
    assert backup["connection_id"] == "wechat-backup"
    assert pending == []
    for sql, params in calls[:3]:
        assert "connection_id = :connection_id" in sql
        assert params["connection_id"] == "wechat-primary"
    for sql, params in calls[3:]:
        assert "connection_id = :connection_id" in sql
        assert params["connection_id"] == "wechat-backup"


@pytest.mark.asyncio
async def test_legacy_connection_claims_explicit_and_pre_scope_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = [
        {"id": 10, "connection_id": ""},
        {"id": 11, "connection_id": LEGACY_WXBOT_CONNECTION_ID},
        {"id": 12, "connection_id": "wechat-primary"},
    ]
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        values: dict[str, object] = dict(params or {})
        calls.append((sql, values))
        if "WITH picked AS" not in sql:
            return []
        accepted = {"", str(values["legacy_connection_id"])}
        for index, row in enumerate(pending):
            if row["connection_id"] in accepted:
                picked = pending.pop(index)
                return [
                    _queue_row(
                        int(picked["id"]),
                        str(picked["connection_id"]),
                        str(values["claim_token"]),
                    )
                ]
        return []

    monkeypatch.setattr("plugins.wxbot.store._exec", fake_exec)
    store = WxbotStore(SimpleNamespace())

    old_unscoped = await store.claim_pending_reply(
        "demo",
        connection_id=LEGACY_WXBOT_CONNECTION_ID,
        claim_owner="legacy-worker",
    )
    explicit_legacy = await store.claim_pending_reply(
        "demo",
        connection_id=LEGACY_WXBOT_CONNECTION_ID,
        claim_owner="legacy-worker",
    )

    assert old_unscoped is not None and old_unscoped["id"] == 10
    assert explicit_legacy is not None and explicit_legacy["id"] == 11
    assert pending == [{"id": 12, "connection_id": "wechat-primary"}]
    assert all(
        "COALESCE(" in sql and "IN ('', :legacy_connection_id)" in sql
        for sql, _ in calls
    )
    assert all(
        params["legacy_connection_id"] == LEGACY_WXBOT_CONNECTION_ID
        for _, params in calls
    )
