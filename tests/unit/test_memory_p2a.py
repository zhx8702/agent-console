from __future__ import annotations

from types import SimpleNamespace

import pytest

import plugins.memory.store as memory_store_module
from plugins.memory.store import MemoryItemProtectedError, MemoryStore


def _item(**kwargs):
    data = {
        "id": 1,
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
        "session_id": "",
        "scope_type": "identity",
        "source_type": "auto",
        "memory_type": "note",
        "content": "用户喜欢 Adidas",
        "value_json": "{}",
        "normalized_key": "n",
        "confidence": 0.9,
        "status": "active",
        "pinned": False,
        "priority": 0,
        "sensitivity": "normal",
        "source_event_id": None,
        "source_trace_id": "",
        "original_text": "",
        "occurrence_count": 1,
        "first_seen_at": None,
        "last_seen_at": None,
        "created_at": None,
        "updated_at": None,
        "deleted_at": None,
    }
    data.update(kwargs)
    return data


@pytest.mark.asyncio
async def test_forget_by_id_is_scoped_and_does_not_cross_user_or_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict | None]] = []
    row = _item(id=7, user_id="other", source_key="wxbot")

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params))
        if sql.startswith("SELECT id, tenant_id"):
            return [row]
        if sql.startswith("UPDATE plugin_memory_item SET status = 'deleted'"):
            pytest.fail("cross-user item should not be deleted")
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    result = await MemoryStore(SimpleNamespace()).forget_memory_items(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        item_id=7,
        allow_pinned=True,
    )

    assert result == {"ids": [], "count": 0}


@pytest.mark.asyncio
async def test_forget_by_query_filters_scope_before_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict | None]] = []
    refreshes: list[dict] = []
    store = MemoryStore(SimpleNamespace())

    async def fake_retrieve_memory_items(**kwargs):
        return [
            _item(id=1, user_id="wxid_a", source_key="wxbot", content="匹配本人"),
            _item(id=2, user_id="other", source_key="wxbot", content="别人的记忆"),
            _item(id=3, user_id="wxid_a", source_key="other-source", content="其他来源"),
        ]

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params))
        if sql.startswith("UPDATE plugin_memory_item SET status = 'deleted'"):
            assert params is not None
            assert params["uid"] == "wxid_a"
            assert params["source_key"] == "wxbot"
            return [{"id": params["id"]}]
        return []

    async def fake_refresh(item: dict) -> None:
        refreshes.append(item)

    monkeypatch.setattr(store, "retrieve_memory_items", fake_retrieve_memory_items)
    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", fake_refresh)

    result = await store.forget_memory_items(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        query="匹配",
        allow_pinned=True,
    )

    assert result == {"ids": [1], "count": 1}
    update_ids = [params["id"] for sql, params in calls if sql.startswith("UPDATE plugin_memory_item")]
    assert update_ids == [1]
    assert [item["id"] for item in refreshes] == [1]


@pytest.mark.asyncio
async def test_forget_pinned_or_manual_requires_allow_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _item(id=5, source_type="manual", pinned=True)

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if sql.startswith("SELECT id, tenant_id"):
            return [row]
        if sql.startswith("UPDATE plugin_memory_item SET status = 'deleted'"):
            pytest.fail("protected item should not be deleted without confirmation")
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    with pytest.raises(MemoryItemProtectedError) as exc:
        await MemoryStore(SimpleNamespace()).forget_memory_items(
            tenant_id="demo",
            channel="wechat",
            source_key="wxbot",
            user_id="wxid_a",
            item_id=5,
        )

    assert exc.value.protected_ids == [5]


@pytest.mark.asyncio
async def test_soft_delete_pinned_or_manual_requires_allow_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _item(id=5, source_type="manual", pinned=True)

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if sql.startswith("SELECT id, tenant_id"):
            return [row]
        if sql.startswith("UPDATE plugin_memory_item SET status = 'deleted'"):
            pytest.fail("protected item should not be deleted without confirmation")
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    with pytest.raises(MemoryItemProtectedError) as exc:
        await MemoryStore(SimpleNamespace()).soft_delete_memory_item(5)

    assert exc.value.protected_ids == [5]


@pytest.mark.asyncio
async def test_forget_allow_pinned_deletes_protected_item(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict | None]] = []
    row = _item(id=5, source_type="manual", pinned=True)
    store = MemoryStore(SimpleNamespace())

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params))
        if sql.startswith("SELECT id, tenant_id"):
            return [row]
        if sql.startswith("UPDATE plugin_memory_item SET status = 'deleted'"):
            return [{"id": params["id"]}]
        return []

    async def fake_refresh(item: dict) -> None:
        calls.append(("REFRESH", item))

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", fake_refresh)

    result = await store.forget_memory_items(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        item_id=5,
        allow_pinned=True,
    )

    assert result == {"ids": [5], "count": 1}
    assert any(call[0] == "REFRESH" for call in calls)


@pytest.mark.asyncio
async def test_search_reuses_retrieval_filters_for_non_injectable_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        assert "status = 'active'" in sql
        assert "sensitivity = 'normal'" in sql
        assert "deleted_at IS NULL" in sql
        return [
            _item(id=1, content="用户喜欢 Adidas", match_count=1),
            _item(id=2, content="pending", status="pending", match_count=10),
            _item(id=3, content="deleted", status="deleted", deleted_at="2026-04-02", match_count=10),
            _item(id=4, content="invalidated", status="invalidated", match_count=10),
            _item(id=5, content="sensitive", sensitivity="pii", match_count=10),
        ]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    rows = await MemoryStore(SimpleNamespace()).retrieve_memory_items(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="s1",
        query="Adidas",
        limit=10,
    )

    assert [row["id"] for row in rows] == [1]


@pytest.mark.asyncio
async def test_update_scoped_does_not_cross_user(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _item(id=9, user_id="other")

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if sql.startswith("SELECT id, tenant_id"):
            return [row]
        if sql.startswith("UPDATE plugin_memory_item SET"):
            pytest.fail("cross-user item should not be updated")
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    result = await MemoryStore(SimpleNamespace()).update_memory_item_scoped(
        9,
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        content="不该改",
    )

    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_session_id", ["", "session-b"])
async def test_forget_by_id_requires_exact_session_for_session_item(
    monkeypatch: pytest.MonkeyPatch,
    requested_session_id: str,
) -> None:
    row = _item(id=8, session_id="session-a", scope_type="session")

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if sql.startswith("SELECT id, tenant_id"):
            return [row]
        if sql.startswith("UPDATE plugin_memory_item SET status = 'deleted'"):
            pytest.fail("session item should not be deleted without matching session_id")
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    result = await MemoryStore(SimpleNamespace()).forget_memory_items(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        item_id=8,
        session_id=requested_session_id,
        allow_pinned=True,
    )

    assert result == {"ids": [], "count": 0}


@pytest.mark.asyncio
async def test_forget_by_id_allows_matching_session_for_session_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict | None]] = []
    row = _item(id=8, session_id="session-a", scope_type="session")
    store = MemoryStore(SimpleNamespace())

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params))
        if sql.startswith("SELECT id, tenant_id"):
            return [row]
        if sql.startswith("UPDATE plugin_memory_item SET status = 'deleted'"):
            assert params is not None
            assert params["sid"] == "session-a"
            return [{"id": params["id"]}]
        return []

    async def fake_refresh(item: dict) -> None:
        calls.append(("REFRESH", item))

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", fake_refresh)

    result = await store.forget_memory_items(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        item_id=8,
        session_id="session-a",
        allow_pinned=True,
    )

    assert result == {"ids": [8], "count": 1}
    assert any(call[0] == "REFRESH" for call in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_session_id", ["", "session-b"])
async def test_update_by_id_requires_exact_session_for_session_item(
    monkeypatch: pytest.MonkeyPatch,
    requested_session_id: str,
) -> None:
    row = _item(id=9, session_id="session-a", scope_type="session")
    store = MemoryStore(SimpleNamespace())

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if sql.startswith("SELECT id, tenant_id"):
            return [row]
        if sql.startswith("UPDATE plugin_memory_item SET"):
            pytest.fail("session item should not be updated without matching session_id")
        return []

    async def fake_update_memory_item(item_id: int, **updates):
        pytest.fail("session item should not reach update without matching session_id")

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "update_memory_item", fake_update_memory_item)

    result = await store.update_memory_item_scoped(
        9,
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id=requested_session_id,
        content="不该改",
    )

    assert result is None


@pytest.mark.asyncio
async def test_update_by_id_allows_matching_session_for_session_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _item(id=9, session_id="session-a", scope_type="session")
    store = MemoryStore(SimpleNamespace())
    updates_seen: list[tuple[int, dict]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if sql.startswith("SELECT id, tenant_id"):
            return [row]
        return []

    async def fake_update_memory_item(item_id: int, **updates):
        updates_seen.append((item_id, updates))
        return {**row, **updates}

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "update_memory_item", fake_update_memory_item)

    result = await store.update_memory_item_scoped(
        9,
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="session-a",
        content="可以改",
    )

    assert result is not None
    assert result["content"] == "可以改"
    assert updates_seen == [(9, {"content": "可以改"})]
