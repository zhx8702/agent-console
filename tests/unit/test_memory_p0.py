from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import plugins.memory.store as memory_store_module
from plugins.memory.store import (
    MemoryStore,
    _extract_long_term_candidates,
    _merge_long_term_items,
    _render_legacy_identity_from_items,
    _render_session_manual_from_items,
    _semantic_key,
    extract_structured_memory_actions,
)


@pytest.mark.asyncio
async def test_ensure_tables_only_verifies_migrated_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = object()
    calls: list[tuple[object, str]] = []

    async def verify(target, *, component: str) -> None:
        calls.append((target, component))

    monkeypatch.setattr(memory_store_module, "get_engine", lambda: engine)
    monkeypatch.setattr(memory_store_module, "verify_runtime_schema", verify)

    await MemoryStore(SimpleNamespace()).ensure_tables()

    assert calls == [(engine, "memory store")]


def test_long_term_merge_no_longer_drops_early_memories() -> None:
    existing = [f"早期事实{i}" for i in range(25)]

    merged, rendered = _merge_long_term_items(existing, ["新事实"], "")

    assert "早期事实0" in merged
    assert "新事实" in merged
    assert "早期事实0" in rendered


@pytest.mark.parametrize(
    "text",
    [
        "需要售后",
        "我想要下单",
        "帮我开一下发票",
        "好的收到",
    ],
)
def test_auto_extraction_rejects_short_and_one_off_service_text(text: str) -> None:
    assert _extract_long_term_candidates(text) == []


@pytest.mark.parametrize(
    "text,source_type,memory_type",
    [
        ("请记住以后默认发顺丰", "explicit_user", "preference"),
        ("我喜欢黑色包装", "auto", "preference"),
        ("我不喜欢电话沟通", "auto", "preference"),
        ("以后不要给我发长文", "explicit_user", "constraint"),
    ],
)
def test_auto_extraction_accepts_explicit_long_term_memory(
    text: str,
    source_type: str,
    memory_type: str,
) -> None:
    candidates = _extract_long_term_candidates(text)

    assert len(candidates) == 1
    assert candidates[0]["source_type"] == source_type
    assert candidates[0]["op"] in {"add", "update"}
    assert candidates[0]["memory_type"] == memory_type
    assert candidates[0]["status"] == "active"


def test_auto_extraction_marks_pii_auto_memory_pending() -> None:
    candidates = _extract_long_term_candidates("我叫张三，手机号是13800138000")

    assert len(candidates) == 1
    assert candidates[0]["sensitivity"] == "pii"
    assert candidates[0]["status"] == "pending"


def test_auto_extraction_allows_explicit_service_preference() -> None:
    candidates = _extract_long_term_candidates("以后退款默认原路退回")

    assert len(candidates) == 1
    assert candidates[0]["source_type"] == "explicit_user"
    assert candidates[0]["status"] == "active"


def test_structured_extractor_adds_preference_with_stable_key() -> None:
    actions = extract_structured_memory_actions("记住我喜欢 Adidas")

    assert len(actions) == 1
    assert actions[0]["op"] == "add"
    assert actions[0]["memory_type"] == "preference"
    assert actions[0]["content"] == "用户喜欢 Adidas"
    assert actions[0]["normalized_key"] == _semantic_key("preference", "brand", "Adidas")
    assert actions[0]["status"] == "active"


def test_structured_extractor_invalidates_old_and_adds_replacement() -> None:
    actions = extract_structured_memory_actions("我现在不喜欢 Adidas 了，换成 Puma")

    assert [action["op"] for action in actions] == ["invalidate", "add"]
    assert actions[0]["invalidates_normalized_key"] == _semantic_key(
        "preference", "brand", "Adidas"
    )
    assert actions[1]["content"] == "用户喜欢 Puma"
    assert actions[1]["normalized_key"] == _semantic_key("preference", "brand", "Puma")


def test_structured_extractor_updates_response_default() -> None:
    actions = extract_structured_memory_actions("以后默认中文简洁回答")

    assert len(actions) == 1
    assert actions[0]["op"] == "update"
    assert actions[0]["memory_type"] == "constraint"
    assert actions[0]["normalized_key"] == _semantic_key(
        "constraint", "response_defaults", "language_style"
    )
    assert "中文" in actions[0]["content"]
    assert "简洁" in actions[0]["content"]


@pytest.mark.parametrize("text", ["我想要退款", "我需要下单", "查一下"])
def test_structured_extractor_ignores_one_off_requests(text: str) -> None:
    actions = extract_structured_memory_actions(text)

    assert len(actions) == 1
    assert actions[0]["op"] == "ignore"
    assert _extract_long_term_candidates(text) == []


def test_structured_extractor_pii_is_pending_even_when_explicit() -> None:
    actions = extract_structured_memory_actions("记住我的手机号是13800138000")

    assert len(actions) == 1
    assert actions[0]["sensitivity"] == "pii"
    assert actions[0]["status"] == "pending"


def test_render_legacy_identity_excludes_pending_sensitive_and_low_confidence_auto() -> None:
    long_items, rendered, manual = _render_legacy_identity_from_items(
        [
            {
                "source_type": "manual",
                "status": "active",
                "confidence": 1.0,
                "sensitivity": "pii",
                "content": "人工手机号 13800138000",
            },
            {
                "source_type": "manual",
                "status": "pending",
                "confidence": 1.0,
                "sensitivity": "sensitive",
                "content": "pending 人工敏感不注入",
            },
            {
                "source_type": "explicit_user",
                "status": "pending",
                "confidence": 0.95,
                "sensitivity": "sensitive",
                "content": "pending 显式敏感不注入",
            },
            {
                "source_type": "auto",
                "status": "pending",
                "confidence": 0.9,
                "sensitivity": "normal",
                "content": "pending 不注入",
            },
            {
                "source_type": "auto",
                "status": "active",
                "confidence": 0.9,
                "sensitivity": "pii",
                "content": "自动手机号 13800138000",
            },
            {
                "source_type": "auto",
                "status": "active",
                "confidence": 0.5,
                "sensitivity": "normal",
                "content": "低置信不注入",
            },
            {
                "source_type": "auto",
                "status": "active",
                "confidence": 0.8,
                "sensitivity": "normal",
                "content": "用户喜欢黑色包装",
            },
        ]
    )

    assert long_items == ["用户喜欢黑色包装"]
    assert "用户喜欢黑色包装" in rendered
    assert "人工手机号 13800138000" in rendered
    assert manual == "人工手机号 13800138000"
    assert "pending 不注入" not in rendered
    assert "pending 人工敏感不注入" not in rendered
    assert "pending 显式敏感不注入" not in rendered
    assert "自动手机号 13800138000" not in rendered
    assert "低置信不注入" not in rendered


def test_render_manual_items_excludes_explicit_non_accepted_acceptance_metadata() -> None:
    items = [
        {
            "source_type": "manual",
            "status": "active",
            "confidence": 1.0,
            "sensitivity": "normal",
            "content": "legacy manual included",
        },
        {
            "source_type": "manual",
            "status": "active",
            "confidence": 1.0,
            "sensitivity": "normal",
            "content": "accepted manual included",
            "value_json": '{"acceptance":{"status":"accepted","score":0.9}}',
        },
        {
            "source_type": "manual",
            "status": "active",
            "confidence": 1.0,
            "sensitivity": "normal",
            "content": "rejected manual excluded",
            "value_json": '{"acceptance":{"status":"rejected","score":0.1}}',
        },
        {
            "source_type": "manual",
            "status": "active",
            "confidence": 1.0,
            "sensitivity": "normal",
            "content": "needs_review manual excluded",
            "value_json": '{"acceptance":{"status":"needs_review","score":0.6}}',
        },
        {
            "source_type": "manual",
            "status": "active",
            "confidence": 1.0,
            "sensitivity": "normal",
            "content": "expired manual excluded",
            "value_json": '{"acceptance":{"status":"expired","score":0.8}}',
        },
        {
            "source_type": "manual",
            "status": "active",
            "confidence": 1.0,
            "sensitivity": "normal",
            "content": "candidate manual excluded",
            "value_json": '{"acceptance":{"status":"candidate","score":0.7}}',
        },
        {
            "source_type": "manual",
            "status": "active",
            "confidence": 1.0,
            "sensitivity": "normal",
            "content": "superseded manual excluded",
            "value_json": '{"acceptance":{"status":"superseded","score":0.7}}',
        },
    ]

    _long_items, rendered, identity_manual = _render_legacy_identity_from_items(items)
    session_manual = _render_session_manual_from_items(items)

    for prompt_text in (rendered, identity_manual, session_manual):
        assert "legacy manual included" in prompt_text
        assert "accepted manual included" in prompt_text
        assert "rejected manual excluded" not in prompt_text
        assert "needs_review manual excluded" not in prompt_text
        assert "expired manual excluded" not in prompt_text
        assert "candidate manual excluded" not in prompt_text
        assert "superseded manual excluded" not in prompt_text


@pytest.mark.asyncio
async def test_insert_or_touch_preserves_pending_sensitive_manual_and_explicit_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inserted: list[dict] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "SELECT id FROM plugin_memory_item" in sql:
            return []
        if "INSERT INTO plugin_memory_item" in sql and params:
            inserted.append(params)
            return [
                {
                    "id": len(inserted),
                    "tenant_id": params["tid"],
                    "channel": params["channel"],
                    "source_key": params["source_key"],
                    "user_id": params["uid"],
                    "session_id": params["sid"],
                    "scope_type": params["scope_type"],
                    "source_type": params["source_type"],
                    "memory_type": params["memory_type"],
                    "content": params["content"],
                    "value_json": params["value_json"],
                    "normalized_key": params["normalized_key"],
                    "confidence": params["confidence"],
                    "status": params["status"],
                    "pinned": params["pinned"],
                    "priority": params["priority"],
                    "sensitivity": params["sensitivity"],
                    "source_event_id": params["source_event_id"],
                    "source_trace_id": params["source_trace_id"],
                    "original_text": params["original_text"],
                    "occurrence_count": 1,
                    "first_seen_at": None,
                    "last_seen_at": None,
                    "created_at": None,
                    "updated_at": None,
                    "deleted_at": None,
                }
            ]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    store = MemoryStore(SimpleNamespace())

    for source_type in ("manual", "explicit_user"):
        item = await store._insert_or_touch_memory_item(
            tenant_id="demo",
            channel="wechat",
            source_key="wxbot",
            user_id=f"wxid_{source_type}",
            scope_type="identity",
            source_type=source_type,
            memory_type="note",
            content="手机号 13800138000",
            confidence=1.0,
            status="pending",
            sensitivity="sensitive",
        )
        assert item is not None
        assert item["status"] == "pending"

    manual_normal = await store._insert_or_touch_memory_item(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_manual_normal",
        scope_type="identity",
        source_type="manual",
        memory_type="note",
        content="重点客户",
        confidence=1.0,
        status="active",
        sensitivity="normal",
    )
    assert manual_normal is not None
    assert manual_normal["status"] == "active"

    manual_sensitive_active = await store._insert_or_touch_memory_item(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_manual_sensitive",
        scope_type="identity",
        source_type="manual",
        memory_type="note",
        content="手机号 13800138000",
        confidence=1.0,
        status="active",
        sensitivity="sensitive",
    )
    assert manual_sensitive_active is not None
    assert manual_sensitive_active["status"] == "pending"

    explicit_sensitive_active = await store._insert_or_touch_memory_item(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_explicit_sensitive",
        scope_type="identity",
        source_type="explicit_user",
        memory_type="note",
        content="手机号 13800138000",
        confidence=1.0,
        status="active",
        sensitivity="sensitive",
    )
    assert explicit_sensitive_active is not None
    assert explicit_sensitive_active["status"] == "pending"

    explicit_detected_sensitive = await store._insert_or_touch_memory_item(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_explicit_detected_sensitive",
        scope_type="identity",
        source_type="explicit_user",
        memory_type="note",
        content="手机号 13800138000",
        confidence=1.0,
        status="active",
        sensitivity="normal",
    )
    assert explicit_detected_sensitive is not None
    assert explicit_detected_sensitive["status"] == "pending"
    assert explicit_detected_sensitive["sensitivity"] == "pii"

    assert [item["status"] for item in inserted] == [
        "pending",
        "pending",
        "active",
        "pending",
        "pending",
        "pending",
    ]


@pytest.mark.asyncio
async def test_insert_memory_item_sql_has_single_returning(monkeypatch: pytest.MonkeyPatch) -> None:
    insert_sql: list[str] = []
    inserted = {
        "id": 1,
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
        "session_id": "",
        "scope_type": "identity",
        "source_type": "manual",
        "memory_type": "note",
        "content": "重点客户",
        "value_json": "{}",
        "normalized_key": "n",
        "confidence": 1.0,
        "status": "active",
        "pinned": True,
        "priority": 100,
        "sensitivity": "normal",
        "source_event_id": None,
        "source_trace_id": "",
        "original_text": "重点客户",
        "occurrence_count": 1,
        "first_seen_at": None,
        "last_seen_at": None,
        "created_at": None,
        "updated_at": None,
        "deleted_at": None,
    }

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "SELECT id FROM plugin_memory_item" in sql:
            return []
        if "INSERT INTO plugin_memory_item" in sql:
            insert_sql.append(sql)
            return [inserted]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    item = await MemoryStore(SimpleNamespace())._insert_or_touch_memory_item(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        source_type="manual",
        content="重点客户",
    )

    assert item is not None
    assert len(insert_sql) == 1
    assert insert_sql[0].count("RETURNING") == 1


@pytest.mark.asyncio
async def test_create_memory_item_manual_is_pinned_and_refreshes_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict | None]] = []
    inserted = {
        "id": 1,
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
        "session_id": "",
        "scope_type": "identity",
        "source_type": "manual",
        "memory_type": "note",
        "content": "重点客户",
        "value_json": "{}",
        "normalized_key": "n",
        "confidence": 1.0,
        "status": "active",
        "pinned": True,
        "priority": 100,
        "sensitivity": "normal",
        "source_event_id": None,
        "source_trace_id": "",
        "original_text": "重点客户",
        "occurrence_count": 1,
        "first_seen_at": None,
        "last_seen_at": None,
        "created_at": None,
        "updated_at": None,
        "deleted_at": None,
    }

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params))
        if "SELECT id FROM plugin_memory_item" in sql:
            return []
        if "INSERT INTO plugin_memory_item" in sql:
            assert params is not None
            assert params["source_type"] == "manual"
            assert params["pinned"] is True
            assert params["confidence"] == 1.0
            return [inserted]
        return []

    async def fake_refresh(item: dict) -> None:
        calls.append(("REFRESH", item))

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    store = MemoryStore(SimpleNamespace())
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", fake_refresh)

    item = await store.create_memory_item(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        source_type="manual",
        content="重点客户",
    )

    assert item is not None
    assert item["content"] == "重点客户"
    assert any(call[0] == "REFRESH" for call in calls)


@pytest.mark.asyncio
async def test_apply_action_updates_existing_active_auto_without_manual_overwrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    key = _semantic_key("constraint", "response_defaults", "language_style")
    existing = {
        "id": 9,
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
        "session_id": "",
        "scope_type": "identity",
        "source_type": "auto",
        "memory_type": "constraint",
        "content": "默认中文回答",
        "value_json": "{}",
        "normalized_key": key,
        "confidence": 0.8,
        "status": "active",
        "pinned": False,
        "priority": 0,
        "sensitivity": "normal",
        "source_event_id": 1,
        "source_trace_id": "",
        "original_text": "以后默认中文回答",
        "occurrence_count": 1,
        "first_seen_at": None,
        "last_seen_at": None,
        "created_at": None,
        "updated_at": None,
        "deleted_at": None,
    }
    calls: list[tuple[str, dict | None]] = []
    refreshed: list[dict] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params))
        if "FROM plugin_memory_item" in sql and "normalized_key = :normalized_key" in sql:
            return [existing]
        if sql.startswith("SELECT id, tenant_id"):
            return [{**existing, "content": "默认中文简洁回答", "occurrence_count": 2}]
        return []

    async def fake_refresh(item: dict) -> None:
        refreshed.append(item)

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", fake_refresh)

    item = await store._apply_structured_memory_action(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        action=extract_structured_memory_actions("以后默认中文简洁回答")[0],
        source_event_id=2,
        source_trace_id="trace",
        original_text="以后默认中文简洁回答",
    )

    assert item is not None
    assert item["content"] == "默认中文简洁回答"
    assert any(
        "UPDATE plugin_memory_item SET content = :content" in sql and params and params["id"] == 9
        for sql, params in calls
    )
    assert [item["id"] for item in refreshed] == [9]
    assert not any("source_type = 'manual'" in sql and sql.startswith("UPDATE") for sql, _ in calls)


@pytest.mark.asyncio
async def test_apply_action_invalidates_old_auto_and_adds_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    adidas_key = _semantic_key("preference", "brand", "Adidas")
    puma_key = _semantic_key("preference", "brand", "Puma")
    old_item = {
        "id": 7,
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
        "session_id": "",
        "scope_type": "identity",
        "source_type": "auto",
        "memory_type": "preference",
        "content": "用户喜欢 Adidas",
        "value_json": "{}",
        "normalized_key": adidas_key,
        "confidence": 0.82,
        "status": "active",
        "pinned": False,
        "priority": 0,
        "sensitivity": "normal",
        "source_event_id": 1,
        "source_trace_id": "",
        "original_text": "我喜欢 Adidas",
        "occurrence_count": 1,
        "first_seen_at": None,
        "last_seen_at": None,
        "created_at": None,
        "updated_at": None,
        "deleted_at": None,
    }
    inserted = {**old_item, "id": 8, "content": "用户喜欢 Puma", "normalized_key": puma_key}
    calls: list[tuple[str, dict | None]] = []
    refreshed: list[dict] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params))
        if "FROM plugin_memory_item" in sql and "normalized_key = :normalized_key" in sql:
            if params and params.get("normalized_key") == adidas_key:
                return [old_item]
            return []
        if sql.startswith("SELECT id, tenant_id"):
            if params and params.get("id") == 7:
                return [old_item]
            return [inserted]
        if "UPDATE plugin_memory_item SET status = 'invalidated'" in sql:
            if not params or params.get("id") != 7:
                return []
            if (
                old_item["tenant_id"] != params.get("tenant_id")
                or old_item["channel"] != params.get("channel")
                or old_item["user_id"] != params.get("user_id")
                or old_item["status"] != params.get("status")
                or old_item["source_type"] == "manual"
                or old_item["pinned"]
                or old_item["deleted_at"] is not None
            ):
                return []
            old_item["status"] = "invalidated"
            old_item["value_json"] = params["value_json"]
            return [{"id": 7}]
        if "SELECT id FROM plugin_memory_item" in sql:
            return []
        if "INSERT INTO plugin_memory_item" in sql:
            return [inserted]
        return []

    async def fake_refresh(item: dict) -> None:
        refreshed.append(item)

    mutation_connection = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    @asynccontextmanager
    async def fake_mutation_transaction():
        token = memory_store_module._ACTIVE_MUTATION_CONNECTION.set(mutation_connection)
        try:
            yield mutation_connection
        finally:
            memory_store_module._ACTIVE_MUTATION_CONNECTION.reset(token)

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", fake_refresh)
    monkeypatch.setattr(store, "_mutation_transaction", fake_mutation_transaction)

    for action in extract_structured_memory_actions("我现在不喜欢 Adidas 了，换成 Puma"):
        await store._apply_structured_memory_action(
            tenant_id="demo",
            channel="wechat",
            source_key="wxbot",
            user_id="wxid_a",
            action=action,
            source_event_id=2,
            source_trace_id="trace",
            original_text="我现在不喜欢 Adidas 了，换成 Puma",
        )

    assert any(
        "status = 'invalidated'" in sql and params and params["id"] == 7 for sql, params in calls
    )
    assert [item["id"] for item in refreshed] == [7]
    invalidation_updates = [
        sql
        for sql, params in calls
        if "status = 'invalidated'" in sql and params and params["id"] == 7
    ]
    assert invalidation_updates
    assert any("FOR UPDATE" in sql and params and params["id"] == 7 for sql, params in calls)
    assert not any("deleted_at" in sql.split("WHERE", 1)[0] for sql in invalidation_updates)
    assert any(
        "INSERT INTO plugin_memory_item" in sql and params and params["normalized_key"] == puma_key
        for sql, params in calls
    )


@pytest.mark.asyncio
async def test_apply_action_manual_same_key_creates_pending_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    key = _semantic_key("preference", "brand", "Adidas")
    manual = {
        "id": 3,
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
        "session_id": "",
        "scope_type": "identity",
        "source_type": "manual",
        "memory_type": "preference",
        "content": "手动：用户喜欢 Adidas",
        "value_json": "{}",
        "normalized_key": key,
        "confidence": 1.0,
        "status": "active",
        "pinned": True,
        "priority": 100,
        "sensitivity": "normal",
        "source_event_id": None,
        "source_trace_id": "",
        "original_text": "手动：用户喜欢 Adidas",
        "occurrence_count": 1,
        "first_seen_at": None,
        "last_seen_at": None,
        "created_at": None,
        "updated_at": None,
        "deleted_at": None,
    }
    inserted = {
        **manual,
        "id": 4,
        "source_type": "auto",
        "content": "用户喜欢 Adidas",
        "status": "pending",
        "pinned": False,
    }
    calls: list[tuple[str, dict | None]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params))
        if "SELECT id FROM plugin_memory_item" in sql:
            return []
        if sql.startswith("SELECT id, audience_scope"):
            return []
        if "FROM plugin_memory_item" in sql and "normalized_key = :normalized_key" in sql:
            return [manual]
        if "INSERT INTO plugin_memory_item" in sql:
            return [inserted]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    item = await store._apply_structured_memory_action(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        action=extract_structured_memory_actions("记住我喜欢 Adidas")[0],
        source_event_id=2,
        source_trace_id="trace",
        original_text="记住我喜欢 Adidas",
    )

    assert item is not None
    assert item["status"] == "pending"
    assert any(
        "INSERT INTO plugin_memory_item" in sql
        and params
        and params["status"] == "pending"
        and "manual_or_pinned_conflict" in params["value_json"]
        for sql, params in calls
    )
    assert not any("UPDATE plugin_memory_item SET content = :content" in sql for sql, _ in calls)


@pytest.mark.asyncio
async def test_list_memory_items_filters_non_item_rows_from_legacy_monkeypatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        assert "source_key IN (:source_key, '*')" in sql
        return [
            {
                "tenant_id": "demo",
                "channel": "wechat",
                "source_key": "wxbot",
                "user_id": "wxid_a",
                "long_term_memory": "legacy profile row",
                "manual_notes": "VIP",
            }
        ]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    rows = await MemoryStore(SimpleNamespace()).list_memory_items(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        scope_type="identity",
    )

    assert rows == []


@pytest.mark.asyncio
async def test_import_legacy_identity_marks_sensitive_auto_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inserted: list[dict] = []

    async def fake_insert(**kwargs):
        inserted.append(kwargs)
        return {"ok": True, **kwargs}

    async def fake_refresh(item: dict) -> None:
        return None

    store = MemoryStore(SimpleNamespace())
    monkeypatch.setattr(store, "_insert_or_touch_memory_item", fake_insert)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", fake_refresh)

    await store._import_legacy_identity_items(
        {
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "wxid_a",
            "manual_notes": "",
            "long_term_memory": "已知用户事实与偏好：\n- 手机号 13800138000\n- 喜欢黑色包装",
            "long_term_items_json": "[]",
        }
    )

    by_content = {item["content"]: item for item in inserted}
    assert by_content["手机号 13800138000"]["sensitivity"] == "pii"
    assert by_content["手机号 13800138000"]["status"] == "pending"
    assert by_content["喜欢黑色包装"]["sensitivity"] == "normal"
    assert by_content["喜欢黑色包装"]["status"] == "active"


@pytest.mark.asyncio
async def test_soft_delete_marks_deleted_and_refreshes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict | None]] = []
    row = {
        "id": 1,
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
        "session_id": "",
        "scope_type": "identity",
        "source_type": "manual",
        "memory_type": "note",
        "content": "重点客户",
        "value_json": "{}",
        "normalized_key": "n",
        "confidence": 1.0,
        "status": "active",
        "pinned": True,
        "priority": 100,
        "sensitivity": "normal",
        "source_event_id": None,
        "source_trace_id": "",
        "original_text": "重点客户",
        "occurrence_count": 1,
        "first_seen_at": None,
        "last_seen_at": None,
        "created_at": None,
        "updated_at": None,
        "deleted_at": None,
    }

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, params))
        if sql.startswith("SELECT id, tenant_id"):
            return [row]
        return []

    async def fake_refresh(item: dict) -> None:
        calls.append(("REFRESH", item))

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    store = MemoryStore(SimpleNamespace())
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", fake_refresh)

    deleted = await store.soft_delete_memory_item(1, allow_pinned=True)

    assert deleted is not None
    assert any("status = 'deleted'" in call[0] for call in calls)
    assert any(call[0] == "REFRESH" for call in calls)


@pytest.mark.asyncio
async def test_runtime_profile_imports_legacy_once_and_prefers_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    imports = {"identity": 0, "session": 0}

    async def fake_get_identity_profile(**kwargs):
        return {
            **kwargs,
            "long_term_memory": "已知用户事实与偏好：\n- 旧偏好",
            "manual_notes": "人工备注",
            "long_term_items_json": '["旧偏好"]',
            "message_count": 1,
            "imported_message_count": 0,
            "last_session_id": "s1",
        }

    async def fake_get_session_profile(**kwargs):
        return {
            **kwargs,
            "short_term_memory": "用户最近说：你好",
            "manual_notes": "会话备注",
            "short_term_items_json": "[]",
            "message_count": 1,
            "imported_message_count": 0,
        }

    item_calls = {"identity": 0, "session": 0}

    async def fake_list_memory_items(**kwargs):
        scope_type = kwargs.get("scope_type")
        item_calls[scope_type] += 1
        if item_calls[scope_type] == 1:
            return []
        if scope_type == "identity":
            return [
                {
                    "user_id": "u1",
                    "session_id": "",
                    "audience_scope": "private",
                    "origin_session_kind": "unknown",
                    "allowed_session_ids": [],
                    "sensitivity": "normal",
                    "source_type": "manual",
                    "status": "active",
                    "confidence": 1.0,
                    "content": "人工备注",
                    "priority": 100,
                },
                {
                    "user_id": "u1",
                    "session_id": "",
                    "audience_scope": "private",
                    "origin_session_kind": "unknown",
                    "allowed_session_ids": [],
                    "sensitivity": "normal",
                    "source_type": "auto",
                    "status": "active",
                    "confidence": 0.8,
                    "content": "旧偏好",
                    "priority": 0,
                },
            ]
        return [
            {
                "user_id": "u1",
                "session_id": "s1",
                "audience_scope": "private",
                "origin_session_kind": "unknown",
                "allowed_session_ids": [],
                "sensitivity": "normal",
                "source_type": "manual",
                "status": "active",
                "confidence": 1.0,
                "content": "会话备注",
                "priority": 100,
            }
        ]

    async def fake_import_identity(profile):
        imports["identity"] += 1

    async def fake_import_session(profile):
        imports["session"] += 1

    monkeypatch.setattr(store, "get_identity_profile", fake_get_identity_profile)
    monkeypatch.setattr(store, "get_session_profile", fake_get_session_profile)
    monkeypatch.setattr(store, "list_memory_items", fake_list_memory_items)
    monkeypatch.setattr(store, "_import_legacy_identity_items", fake_import_identity)
    monkeypatch.setattr(store, "_import_legacy_session_items", fake_import_session)

    profile = await store.get_runtime_profile(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        session_id="s1",
        user_id="u1",
    )

    assert imports == {"identity": 1, "session": 1}
    assert "人工备注" in profile["identity_manual_notes"]
    assert "旧偏好" in profile["long_term_memory"]
    assert profile["session_manual_notes"] == "会话备注"
