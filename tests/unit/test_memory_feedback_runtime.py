from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.common.types import Channel, InboundEvent, Message
from app.orchestrator.pipeline import PipelineContext
from plugins.memory import store as memory_store_module
from plugins.memory.hooks import MemorySaveStep
from plugins.memory.store import MemoryStore


@contextmanager
def _fake_mutation_connection() -> Iterator[None]:
    token = memory_store_module._ACTIVE_MUTATION_CONNECTION.set(
        SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    )
    try:
        yield
    finally:
        memory_store_module._ACTIVE_MUTATION_CONNECTION.reset(token)


def _memory_row(item_id: int = 7) -> dict[str, object]:
    return {
        "id": item_id,
        "tenant_id": "tenant-a",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "member-a",
        "session_id": "room@chatroom",
        "scope_type": "session",
        "source_type": "auto",
        "memory_type": "fact",
        "content": "喜欢香菜",
        "value_json": "{}",
        "normalized_key": "preference:coriander",
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


@pytest.mark.asyncio
async def test_forget_member_repeats_tenant_channel_and_member_scope_on_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    listed = False

    async def fake_exec(
        sql: str,
        params: dict | None = None,
    ) -> list[dict[str, object]]:
        nonlocal listed
        payload = dict(params or {})
        calls.append((sql, payload))
        if sql.startswith("SELECT id FROM plugin_memory_item"):
            listed = True
            return [{"id": 7}]
        return []

    monkeypatch.setattr("plugins.memory.store._exec", fake_exec)
    store = MemoryStore(SimpleNamespace())

    token = memory_store_module._ACTIVE_MUTATION_CONNECTION.set(
        SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    )
    try:
        changed = await store.forget_member(
            tenant_id="tenant-a",
            session_id="room@chatroom",
            user_id="member-a",
            idempotency_key="natural-feedback:message-1:forget_member",
        )
    finally:
        memory_store_module._ACTIVE_MUTATION_CONNECTION.reset(token)

    assert changed == 1
    item_delete_sql, item_delete_params = next(
        item
        for item in calls
        if item[0].startswith("DELETE FROM plugin_memory_item ")
    )
    assert "tenant_id = :tid" in item_delete_sql
    assert "channel = :memory_channel" in item_delete_sql
    assert "user_id = :uid" in item_delete_sql
    assert item_delete_params == {
        "tid": "tenant-a",
        "uid": "member-a",
        "memory_channel": "wechat",
    }
    profile_deletes = [sql for sql, _ in calls if sql.startswith("DELETE FROM")]
    assert all("tenant_id = :tid" in sql and "user_id = :uid" in sql for sql in profile_deletes)
    assert not any(sql.startswith("UPDATE plugin_memory_item") for sql, _ in calls)


@pytest.mark.asyncio
async def test_forget_member_does_not_delete_database_when_vector_erasure_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        _ = params
        calls.append(sql)
        if sql.startswith("SELECT id FROM plugin_memory_item"):
            return [{"id": 7}]
        return []

    async def fail_vector_delete(item_id: object, *, force: bool = False) -> str:
        assert item_id == 7
        assert force is True
        raise RuntimeError("vector backend unavailable")

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    store = MemoryStore(SimpleNamespace())
    store.vector_index.delete_item = fail_vector_delete  # type: ignore[method-assign]
    token = memory_store_module._ACTIVE_MUTATION_CONNECTION.set(
        SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    )
    try:
        with pytest.raises(RuntimeError, match="vector backend unavailable"):
            await store.forget_member(
                tenant_id="tenant-a",
                session_id="room@chatroom",
                user_id="member-a",
                idempotency_key="member-memory-delete:test",
            )
    finally:
        memory_store_module._ACTIVE_MUTATION_CONNECTION.reset(token)

    assert not any(sql.startswith("DELETE FROM") for sql in calls)


@pytest.mark.asyncio
async def test_remember_interaction_rechecks_tenant_erasure_control_under_member_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        _ = params
        calls.append(sql)
        if "FROM social_tenant_member_control" in sql:
            return [{"memory_opt_out": True, "deletion_state": "completed"}]
        if sql.lstrip().startswith(("INSERT", "UPDATE", "DELETE")):
            raise AssertionError("blocked member memory must not be written")
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    store = MemoryStore(SimpleNamespace())
    token = memory_store_module._ACTIVE_MUTATION_CONNECTION.set(
        SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    )
    try:
        profile = await store.remember_interaction(
            tenant_id="tenant-a",
            channel="wechat",
            source_key="wxbot",
            user_id="member-a",
            session_id="room@chatroom",
            user_text="do not retain this",
            assistant_text="",
        )
    finally:
        memory_store_module._ACTIVE_MUTATION_CONNECTION.reset(token)

    assert profile["memory_items"] == []
    assert profile["message_count"] == 0
    assert not any(sql.lstrip().startswith(("INSERT", "UPDATE", "DELETE")) for sql in calls)


@pytest.mark.asyncio
async def test_correction_selects_scoped_fact_and_passes_idempotency_to_invalidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_exec(
        sql: str,
        params: dict | None = None,
    ) -> list[dict[str, object]]:
        payload = dict(params or {})
        calls.append((sql, payload))
        if "status = 'invalidated'" in sql:
            return []
        if "status = 'active'" in sql:
            return [
                {
                    "id": 8,
                    "memory_type": "preference",
                    "content": "喜欢香菜",
                    "normalized_key": "preference:coriander",
                },
                {
                    "id": 9,
                    "memory_type": "residence",
                    "content": "住在上海",
                    "normalized_key": "residence:shanghai",
                },
            ]
        return []

    monkeypatch.setattr("plugins.memory.store._exec", fake_exec)
    store = MemoryStore(SimpleNamespace())
    invalidations: list[dict[str, object]] = []

    async def invalidate(item_id: int, **kwargs: object) -> dict[str, object]:
        invalidations.append({"item_id": item_id, **kwargs})
        return {
            "id": item_id,
            "tenant_id": "tenant-a",
            "channel": "wechat",
            "user_id": "member-a",
        }

    store._mark_memory_item_invalidated = invalidate  # type: ignore[method-assign]
    with _fake_mutation_connection():
        resolution = await store.resolve_member_fact_correction(
            tenant_id="tenant-a",
            session_id="room@chatroom",
            user_id="member-a",
            correction_text="你记错了，我不住在上海",
            idempotency_key="natural-feedback:message-2:correct_memory",
        )

    assert resolution.status == "applied"
    assert resolution.changed == 1
    select_sql, select_params = next(
        item for item in calls if "status = 'active'" in item[0]
    )
    assert "tenant_id = :tid" in select_sql
    assert "channel = 'wechat'" in select_sql
    assert "user_id = :uid" in select_sql
    assert select_params == {
        "tid": "tenant-a",
        "uid": "member-a",
        "sid": "room@chatroom",
    }
    replay_sql, replay_params = next(
        item for item in calls if "status = 'invalidated'" in item[0]
    )
    assert "CAST(value_json AS TEXT)" in replay_sql
    assert replay_params["operation_marker"] == (
        "%natural-feedback:message-2:correct\\_memory%"
    )
    assert invalidations[0]["source_trace_id"] == (
        "natural-feedback:message-2:correct_memory"
    )
    assert invalidations[0]["item_id"] == 9
    assert invalidations[0]["original_text"] == ""


@pytest.mark.asyncio
async def test_correction_replay_uses_content_free_invalidation_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, object]]:
        _ = params
        calls.append(sql)
        if "status = 'invalidated'" in sql:
            return [{"id": 9}]
        raise AssertionError("a replay must not scan or mutate active member facts")

    monkeypatch.setattr("plugins.memory.store._exec", fake_exec)
    store = MemoryStore(SimpleNamespace())

    with _fake_mutation_connection():
        resolution = await store.resolve_member_fact_correction(
            tenant_id="tenant-a",
            session_id="room@chatroom",
            user_id="member-a",
            correction_text="你记错了，我不住在上海",
            idempotency_key="natural-feedback:message-2:correct_memory",
        )

    assert resolution.status == "applied"
    assert resolution.changed == 1
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_correction_invalidation_refuses_a_row_changed_after_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    changed = _memory_row()
    changed["normalized_key"] = "residence:beijing"

    async def fake_exec(
        sql: str,
        params: dict | None = None,
    ) -> list[dict[str, object]]:
        _ = params
        calls.append(sql)
        if "FROM plugin_memory_item WHERE id = :id" in sql:
            return [changed]
        if sql.lstrip().startswith("UPDATE"):
            raise AssertionError("a changed correction candidate must not be invalidated")
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    store = MemoryStore(SimpleNamespace())

    with _fake_mutation_connection():
        result = await store._mark_memory_item_invalidated(
            7,
            reason="natural_feedback_correction",
            source_event_id=None,
            source_trace_id="correction-1",
            original_text="",
            include_original_text_metadata=False,
            expected_tenant_id="tenant-a",
            expected_channel="wechat",
            expected_user_id="member-a",
            expected_normalized_key="residence:shanghai",
        )

    assert result is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_correction_without_property_never_invalidates_arbitrary_recent_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(sql: str, params: dict | None = None) -> list[dict[str, object]]:
        _ = params
        if "status = 'invalidated'" in sql:
            return []
        if "status = 'active'" in sql:
            return [
                {
                    "id": 1,
                    "memory_type": "residence",
                    "content": "住在上海",
                    "normalized_key": "residence:shanghai",
                },
                {
                    "id": 2,
                    "memory_type": "preference",
                    "content": "喜欢香菜",
                    "normalized_key": "preference:coriander",
                },
            ]
        return []

    monkeypatch.setattr("plugins.memory.store._exec", fake_exec)
    store = MemoryStore(SimpleNamespace())

    async def should_not_invalidate(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError(f"ambiguous correction must not mutate: {args}, {kwargs}")

    store._mark_memory_item_invalidated = should_not_invalidate  # type: ignore[method-assign]
    with _fake_mutation_connection():
        resolution = await store.resolve_member_fact_correction(
            tenant_id="tenant-a",
            session_id="room@chatroom",
            user_id="member-a",
            correction_text="你记错了",
            idempotency_key="natural-feedback:message-3:correct_memory",
        )

    assert resolution.status == "confirmation_required"
    assert resolution.changed == 0
    assert resolution.candidate_count == 2


@pytest.mark.asyncio
async def test_memory_save_fails_closed_when_member_policy_cannot_be_loaded() -> None:
    class _UnavailablePolicyStore:
        async def get_group_member_privacy_policy(self, **kwargs: object) -> object:
            _ = kwargs
            raise RuntimeError("database unavailable")

        async def remember_interaction(self, **kwargs: object) -> dict[str, object]:
            raise AssertionError(f"memory must not be written: {kwargs}")

    event = InboundEvent(
        message_id="message-3",
        tenant_id="tenant-a",
        channel=Channel.WECHAT,
        user_id="member-a",
        session_id="room@chatroom",
        message=Message(content="不要记录这条消息"),
        metadata={"session_kind": "group"},
    )
    ctx = PipelineContext(event=event, trace_id="trace-3")

    result = await MemorySaveStep(  # type: ignore[arg-type]
        _UnavailablePolicyStore()
    ).run(ctx)

    assert result.reason == "member_privacy_blocked"
    assert ctx.signals["memory"]["privacy_fail_closed"] is True
    assert ctx.signals["memory"]["member_capture_blocked"] is True
