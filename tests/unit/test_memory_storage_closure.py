from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

import plugins.memory.store as memory_store_module
from app.social.contracts import MemberPrivacyValues
from plugins.memory.store import (
    GROUP_HISTORY_USER_ID_SCOPE,
    MemoryErasureIncompleteError,
    MemoryMutationError,
    MemoryStore,
    _interaction_event_key,
    _redact_memory_storage_text,
)


@pytest.fixture(autouse=True)
def _bind_unit_memory_transaction():
    token = memory_store_module._ACTIVE_MUTATION_CONNECTION.set(
        SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    )
    try:
        yield
    finally:
        memory_store_module._ACTIVE_MUTATION_CONNECTION.reset(token)


def _event_key(**overrides: str) -> str:
    values = {
        "tenant_id": "tenant-a",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid-a",
        "session_id": "room@chatroom",
        "source_message_id": "message-1",
        "trace_id": "trace-1",
        "user_text": "same",
        "assistant_text": "reply",
    }
    values.update(overrides)
    return _interaction_event_key(**values)


def _policy_snapshot(policy: MemberPrivacyValues) -> dict[str, Any]:
    return {"policy": policy.model_dump(mode="json")}


def test_interaction_key_prefers_message_id_and_unkeyed_turns_do_not_false_merge() -> None:
    assert _event_key(trace_id="trace-a") == _event_key(trace_id="trace-b")
    assert _event_key(source_message_id="message-2") != _event_key()
    assert _event_key(source_message_id="", trace_id="trace-a") == _event_key(
        source_message_id="",
        trace_id="trace-a",
    )
    assert _event_key(source_message_id="", trace_id="") != _event_key(
        source_message_id="",
        trace_id="",
    )


def test_storage_redaction_avoids_address_false_positives() -> None:
    for text in ("会议室", "这条路很难走", "我喜欢上海市"):
        assert _redact_memory_storage_text(text) == text
    assert "[redacted-address]" in _redact_memory_storage_text(
        "地址：北京市朝阳区建国路88号"
    )
    assert "[redacted-address]" in _redact_memory_storage_text(
        "北京市朝阳区建国路88号"
    )


@pytest.mark.asyncio
async def test_remember_replay_uses_partial_unique_conflict_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace(memory_auto_expire_days=180))
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_exec(
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        calls.append((sql, dict(params or {})))
        if sql.startswith("INSERT INTO plugin_memory_event"):
            return []
        raise AssertionError(f"projection ran after replay gate: {sql}")

    async def fake_runtime_profile(**kwargs: Any) -> dict[str, Any]:
        return {"replayed": True, **kwargs}

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "get_runtime_profile", fake_runtime_profile)

    result = await store.remember_interaction(
        tenant_id="tenant-a",
        channel="web",
        source_key="web",
        user_id="user-a",
        session_id="session-a",
        user_text="email me at person@example.com",
        assistant_text="ok",
        trace_id="trace-a",
        source_message_id="message-a",
    )

    assert result["replayed"] is True
    sql, params = calls[0]
    assert (
        "ON CONFLICT (event_key) WHERE event_key IS NOT NULL DO NOTHING"
        in sql
    )
    assert params["source_message_id"] == "message-a"
    assert "[redacted-email]" in params["user_text"]


@pytest.mark.asyncio
async def test_group_backfill_consent_is_fail_closed_and_session_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    now = datetime.now(UTC).replace(tzinfo=None)
    enabled = MemberPrivacyValues(
        memory_enabled=True,
        audience_scope="session",
    )
    explicit = MemberPrivacyValues(
        memory_enabled=True,
        audience_scope="explicit",
        allowed_session_ids=["room-a@chatroom"],
    )
    history_rows = [
        {
            "snapshot_json": _policy_snapshot(enabled),
            "created_at": now - timedelta(days=3),
        }
    ]

    async def fake_exec(
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if "social_member_policy_history" in sql:
            return history_rows
        assert "FROM audit_events" in sql
        return []

    async def load_enabled(**kwargs: Any) -> MemberPrivacyValues:
        return enabled

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "get_group_member_privacy_policy", load_enabled)
    assert (
        await store._group_backfill_member_policy(
            tenant_id="tenant-a",
            session_id="room-a@chatroom",
            user_id="wxid-a",
            user_text="normal",
            event_created_at=now - timedelta(days=1),
        )
        == enabled
    )

    async def load_explicit(**kwargs: Any) -> MemberPrivacyValues:
        return explicit

    monkeypatch.setattr(store, "get_group_member_privacy_policy", load_explicit)
    history_rows[0]["snapshot_json"] = _policy_snapshot(explicit)
    assert (
        await store._group_backfill_member_policy(
            tenant_id="tenant-a",
            session_id="room-a@chatroom",
            user_id="wxid-a",
            event_created_at=now,
        )
        == explicit
    )
    assert (
        await store._group_backfill_member_policy(
            tenant_id="tenant-a",
            session_id="room-b@chatroom",
            user_id="wxid-a",
            event_created_at=now,
        )
        is None
    )

    async def load_default(**kwargs: Any) -> MemberPrivacyValues:
        return MemberPrivacyValues()

    monkeypatch.setattr(store, "get_group_member_privacy_policy", load_default)
    assert (
        await store._group_backfill_member_policy(
            tenant_id="tenant-a",
            session_id="room-a@chatroom",
            user_id="wxid-a",
            event_created_at=now,
        )
        is None
    )

    async def load_failed(**kwargs: Any) -> MemberPrivacyValues:
        raise RuntimeError("policy unavailable")

    monkeypatch.setattr(store, "get_group_member_privacy_policy", load_failed)
    assert (
        await store._group_backfill_member_policy(
            tenant_id="tenant-a",
            session_id="room-a@chatroom",
            user_id="wxid-a",
            event_created_at=now,
        )
        is None
    )


@pytest.mark.asyncio
async def test_group_backfill_only_imports_current_continuous_consent_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    enabled = MemberPrivacyValues(
        memory_enabled=True,
        audience_scope="session",
    )
    disabled = MemberPrivacyValues()
    history_rows = [
        {
            "snapshot_json": _policy_snapshot(enabled),
            "created_at": datetime(2026, 7, 20),
        },
        {
            "snapshot_json": _policy_snapshot(disabled),
            "created_at": datetime(2026, 7, 15),
        },
        {
            "snapshot_json": _policy_snapshot(enabled),
            "created_at": datetime(2026, 7, 1),
        },
    ]

    async def fake_exec(
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if "social_member_policy_history" in sql:
            return history_rows
        assert "FROM audit_events" in sql
        return []

    async def load_policy(**kwargs: Any) -> MemberPrivacyValues:
        return enabled

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "get_group_member_privacy_policy", load_policy)

    assert (
        await store._group_backfill_member_policy(
            tenant_id="tenant-a",
            session_id="room-a@chatroom",
            user_id="wxid-a",
            event_created_at=datetime(2026, 7, 10),
        )
        is None
    )
    assert (
        await store._group_backfill_member_policy(
            tenant_id="tenant-a",
            session_id="room-a@chatroom",
            user_id="wxid-a",
            event_created_at=datetime(2026, 7, 21),
        )
        == enabled
    )

    history_rows.clear()
    assert (
        await store._group_backfill_member_policy(
            tenant_id="tenant-a",
            session_id="room-a@chatroom",
            user_id="wxid-a",
            event_created_at=datetime(2026, 7, 21),
        )
        is None
    )


@pytest.mark.asyncio
async def test_group_backfill_does_not_revive_pre_erasure_history_after_reopt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    enabled = MemberPrivacyValues(
        memory_enabled=True,
        audience_scope="session",
    )
    audits = [
        {
            "action": "tenant_member_control.update",
            "before_state_json": {
                "control": {"memory_opt_out": False},
                "deletion_state": "none",
            },
            "after_state_json": {
                "control": {"memory_opt_out": True},
                "deletion_state": "requested",
            },
            "created_at": datetime(2026, 7, 10),
        },
        {
            "action": "tenant_member_memory_deletion.completed",
            "before_state_json": {"deletion_state": "requested"},
            "after_state_json": {"deletion_state": "completed"},
            "created_at": datetime(2026, 7, 11),
        },
        {
            "action": "tenant_member_control.update",
            "before_state_json": {
                "control": {"memory_opt_out": True},
                "deletion_state": "completed",
            },
            "after_state_json": {
                "control": {"memory_opt_out": False},
                "deletion_state": "completed",
            },
            "created_at": datetime(2026, 7, 20),
        },
    ]

    async def fake_exec(
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if "social_member_policy_history" in sql:
            return [
                {
                    "snapshot_json": _policy_snapshot(enabled),
                    "created_at": datetime(2026, 7, 1),
                }
            ]
        if "FROM audit_events" in sql:
            return audits
        return []

    async def load_policy(**kwargs: Any) -> MemberPrivacyValues:
        return enabled

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "get_group_member_privacy_policy", load_policy)

    assert (
        await store._group_backfill_member_policy(
            tenant_id="tenant-a",
            session_id="room-a@chatroom",
            user_id="wxid-a",
            event_created_at=datetime(2026, 7, 19),
        )
        is None
    )
    assert (
        await store._group_backfill_member_policy(
            tenant_id="tenant-a",
            session_id="room-a@chatroom",
            user_id="wxid-a",
            event_created_at=datetime(2026, 7, 21),
        )
        == enabled
    )


@pytest.mark.asyncio
async def test_group_backfill_expiry_uses_member_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace(memory_auto_expire_days=180))
    policy = MemberPrivacyValues(
        memory_enabled=True,
        audience_scope="session",
        retention_days=7,
    )
    created_at = (
        datetime.now(UTC).replace(tzinfo=None, microsecond=0)
        - timedelta(days=1)
    )
    inserted_params: dict[str, Any] = {}

    async def fake_exec(
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        nonlocal inserted_params
        if "social_member_policy_history" in sql:
            return [
                {
                    "snapshot_json": _policy_snapshot(policy),
                    "created_at": created_at - timedelta(days=1),
                }
            ]
        if "FROM audit_events" in sql:
            return []
        if sql.startswith("INSERT INTO plugin_memory_event"):
            inserted_params = dict(params or {})
            return [{"id": 91, **inserted_params}]
        return []

    async def load_policy(**kwargs: Any) -> MemberPrivacyValues:
        return policy

    async def allow_scope(**kwargs: Any) -> None:
        return None

    async def not_blocked(**kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "get_group_member_privacy_policy", load_policy)
    monkeypatch.setattr(store, "_require_history_runtime_scope", allow_scope)
    monkeypatch.setattr(store, "_member_memory_write_blocked", not_blocked)

    event, inserted = await store._insert_backfill_event(
        tenant_id="tenant-a",
        channel="wechat",
        source_key="wxbot",
        user_id=GROUP_HISTORY_USER_ID_SCOPE,
        message={
            "session_id": "room-a@chatroom",
            "source_member_id": "wxid-a",
            "source_message_id": "msg-1",
            "user_text": "normal",
            "created_at": created_at.strftime("%Y-%m-%dT%H:%M:%S"),
        },
    )

    assert inserted is True
    assert event is not None
    assert inserted_params["expires_at"] == created_at + timedelta(days=7)


@pytest.mark.asyncio
async def test_expiry_archives_shared_episode_instead_of_deleting_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    calls: list[tuple[str, dict[str, Any]]] = []
    member_locks: list[str] = []

    async def fake_exec(
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        calls.append((sql, dict(params or {})))
        if "FROM plugin_memory_item" in sql and "expires_at <= NOW()" in sql:
            return [
                {
                    "id": 1,
                    "tenant_id": "tenant-a",
                    "channel": "wechat",
                    "user_id": GROUP_HISTORY_USER_ID_SCOPE,
                    "source_evidence_json": [
                        {
                            "source_event_id": 10,
                            "source_member_id": "wxid-b",
                        },
                        {
                            "source_event_id": 11,
                            "source_member_id": "wxid-a",
                        },
                    ],
                }
            ]
        if "FROM plugin_memory_event" in sql and "expires_at <= NOW()" in sql:
            return []
        if (
            "FROM plugin_memory_extraction_job" in sql
            and sql.startswith("SELECT id")
        ):
            return []
        if "FROM plugin_memory_fact" in sql:
            return []
        if "FROM plugin_memory_episode ORDER BY" in sql:
            return [
                {
                    "id": 10,
                    "memory_item_ids_json": "[1, 2]",
                    "event_ids_json": "[]",
                }
            ]
        return []

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    async def lock_member(**kwargs: str) -> None:
        member_locks.append(kwargs["user_id"])

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store.vector_index, "delete_item", noop)
    monkeypatch.setattr(store.vector_index, "delete_fact", noop)
    monkeypatch.setattr(store.vector_index, "delete_episode", noop)
    monkeypatch.setattr(store, "_lock_member_memory_mutation", lock_member)

    result = await store._run_physical_expiry_sweep(dry_run=False, batch=50)

    assert result["items_purged"] == 1
    assert result["episodes_archived"] == 1
    assert result["rebuild_required_episode_ids"] == [10]
    assert member_locks == ["wxid-a", "wxid-b"]
    archive_calls = [
        (sql, params)
        for sql, params in calls
        if sql.startswith("UPDATE plugin_memory_episode SET status = 'archived'")
    ]
    assert len(archive_calls) == 1
    assert json.loads(archive_calls[0][1]["item_ids_json"]) == [2]
    assert not any(
        sql.startswith("DELETE FROM plugin_memory_episode") for sql, _ in calls
    )


@pytest.mark.asyncio
async def test_group_profile_rebuild_keeps_only_consented_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    now = datetime.now(UTC).replace(tzinfo=None)
    enabled = MemberPrivacyValues(
        memory_enabled=True,
        audience_scope="session",
    )
    disabled = MemberPrivacyValues()
    writes: list[dict[str, Any]] = []

    async def fake_exec(
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        params = dict(params or {})
        if "social_member_policy_history" in sql:
            policy = enabled if params["uid"] == "wxid-b" else disabled
            return [
                {
                    "snapshot_json": _policy_snapshot(policy),
                    "created_at": now - timedelta(days=2),
                }
            ]
        if "FROM audit_events" in sql:
            return []
        if sql.startswith("INSERT INTO plugin_memory_session_profile"):
            writes.append(params)
        return []

    async def load_policy(**kwargs: Any) -> MemberPrivacyValues:
        return enabled if kwargs["user_id"] == "wxid-b" else disabled

    async def not_blocked(**kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "get_group_member_privacy_policy", load_policy)
    monkeypatch.setattr(store, "_member_memory_write_blocked", not_blocked)
    rows = [
        {
            "id": 1,
            "source_key": "wxbot",
            "session_id": "room-a@chatroom",
            "source_member_id": "wxid-a",
            "user_text": "wxid-a: secret A",
            "assistant_text": "",
            "created_at": now - timedelta(hours=2),
            "expires_at": now + timedelta(days=1),
        },
        {
            "id": 2,
            "source_key": "wxbot",
            "session_id": "room-a@chatroom",
            "source_member_id": "wxid-b",
            "user_text": "wxid-b: retained B",
            "assistant_text": "",
            "created_at": now - timedelta(hours=1),
            "expires_at": now + timedelta(days=1),
        },
    ]

    allowed = await store._consented_group_event_rows(
        tenant_id="tenant-a",
        rows=rows,
    )
    assert [row["id"] for row in allowed] == [2]
    assert (
        await store._rebuild_group_session_profiles(
            tenant_id="tenant-a",
            session_ids=["room-a@chatroom"],
            event_rows=allowed,
        )
        == 1
    )
    serialized = json.dumps(writes, ensure_ascii=False, default=str)
    assert "retained B" in serialized
    assert "secret A" not in serialized


@pytest.mark.asyncio
async def test_non_wechat_forget_deletes_selected_channel_and_scans_residuals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    calls: list[tuple[str, dict[str, Any]]] = []

    async def fake_exec(
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        params = dict(params or {})
        calls.append((sql, params))
        assert params.get("memory_channel", "discord") == "discord"
        if sql.startswith("SELECT id FROM plugin_memory_item"):
            return [{"id": 1}]
        if sql.startswith("SELECT id FROM plugin_memory_fact"):
            return [{"id": 2}]
        if sql.startswith("SELECT id FROM plugin_memory_episode"):
            return [{"id": 3}]
        if sql.startswith("DELETE FROM plugin_memory_"):
            return [{"deleted": 1}]
        if sql.startswith("SELECT table_name, row_count"):
            return []
        return []

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store.vector_index, "delete_item", noop)
    monkeypatch.setattr(store.vector_index, "delete_fact", noop)
    monkeypatch.setattr(store.vector_index, "delete_episode", noop)

    result = await store.forget_member_detailed(
        tenant_id="tenant-a",
        session_id="discord-room",
        user_id="discord-user",
        idempotency_key="forget-1",
        channel="discord",
    )

    assert result["channel"] == "discord"
    assert result["complete"] is True
    assert result["count"] >= 3
    direct_deletes = [
        (sql, params)
        for sql, params in calls
        if sql.startswith("DELETE FROM plugin_memory_")
    ]
    assert direct_deletes
    assert all(params["memory_channel"] == "discord" for _, params in direct_deletes)
    assert not any("source_member_id" in sql for sql, _ in calls)


@pytest.mark.asyncio
async def test_forget_deletes_prepared_intents_and_retries_running_residual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())

    async def fake_exec(
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if "SELECT to_regclass('plugin_memory_profile')" in sql:
            return [{"table_name": None}]
        if sql.startswith("DELETE FROM message_effect_intent"):
            return [
                {"status": "prepared"},
                {"status": "completed"},
                {"status": "failed"},
            ]
        if (
            sql.startswith("SELECT status, COUNT(*)")
            and "message_effect_intent" in sql
        ):
            return [{"status": "running", "count": 1}]
        if sql.startswith("SELECT table_name, row_count"):
            return []
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    postgres_token = memory_store_module._ACTIVE_MUTATION_CONNECTION.set(
        SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    )
    try:
        detailed = await store.forget_member_detailed(
            tenant_id="tenant-a",
            session_id="web-session",
            user_id="user-a",
            idempotency_key="forget-running",
            channel="web",
        )
        assert detailed["complete"] is False
        assert detailed["effect_intents_deleted_by_status"] == {
            "prepared": 1,
            "completed": 1,
            "failed": 1,
        }
        assert detailed["residual_by_table"]["message_effect_intent"] == {
            "running": 1
        }
        with pytest.raises(MemoryErasureIncompleteError) as exc_info:
            await store.forget_member(
                tenant_id="tenant-a",
                session_id="web-session",
                user_id="user-a",
                idempotency_key="forget-running",
                channel="web",
            )
        assert exc_info.value.code == "memory_erasure_incomplete"
        assert exc_info.value.residual_tables == ("message_effect_intent",)
    finally:
        memory_store_module._ACTIVE_MUTATION_CONNECTION.reset(postgres_token)


@pytest.mark.asyncio
async def test_non_wechat_item_mutators_share_member_erasure_fence_without_social_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    locks: list[tuple[str, str]] = []
    inserted = {
        "id": 31,
        "tenant_id": "tenant-a",
        "channel": "discord",
        "source_key": "discord-bot",
        "user_id": "discord-user",
        "session_id": "",
        "scope_type": "identity",
        "source_type": "manual",
        "memory_type": "note",
        "content": "remember this",
        "value_json": "{}",
        "normalized_key": "remember-this",
        "confidence": 1.0,
        "status": "active",
        "pinned": True,
        "priority": 100,
        "sensitivity": "normal",
        "occurrence_count": 1,
        "deleted_at": None,
    }

    async def lock_member(**kwargs: str) -> None:
        locks.append((kwargs["tenant_id"], kwargs["user_id"]))

    async def social_gate(**kwargs: str) -> bool:
        raise AssertionError("WeChat social control must not gate Discord")

    async def fake_insert(**kwargs: Any) -> dict[str, Any]:
        return dict(inserted)

    async def fake_get(
        item_id: int,
        *,
        for_update: bool = False,
    ) -> dict[str, Any]:
        assert item_id == 31
        if for_update:
            assert locks[-1] == ("tenant-a", "discord-user")
        return dict(inserted)

    async def fake_exec(
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        assert sql.startswith("UPDATE plugin_memory_item SET")
        assert (params or {})["id"] == 31
        return []

    async def noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(store, "_lock_member_memory_mutation", lock_member)
    monkeypatch.setattr(store, "_member_memory_write_blocked", social_gate)
    monkeypatch.setattr(store, "_insert_or_touch_memory_item", fake_insert)
    monkeypatch.setattr(store, "get_memory_item", fake_get)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", noop)
    monkeypatch.setattr(store, "_sync_memory_graph_for_item_safe", noop)
    monkeypatch.setattr(store, "_sync_memory_vector_for_item_safe", noop)
    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    created = await store.create_memory_item(
        tenant_id="tenant-a",
        channel="discord",
        source_key="discord-bot",
        user_id="discord-user",
        content="remember this",
    )
    updated = await store.update_memory_item(31, content="updated")

    assert created is not None
    assert updated is not None
    assert locks == [
        ("tenant-a", "discord-user"),
        ("tenant-a", "discord-user"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_kind", ["identity", "session"])
async def test_wechat_profile_upsert_is_blocked_under_member_lock(
    monkeypatch: pytest.MonkeyPatch,
    profile_kind: str,
) -> None:
    store = MemoryStore(SimpleNamespace())
    locks: list[tuple[str, str]] = []

    async def lock_member(**kwargs: str) -> None:
        locks.append((kwargs["tenant_id"], kwargs["user_id"]))

    async def blocked(**kwargs: str) -> bool:
        assert locks == [("tenant-a", "wxid-a")]
        return True

    async def unexpected_exec(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("blocked profile write reached durable storage")

    monkeypatch.setattr(store, "_lock_member_memory_mutation", lock_member)
    monkeypatch.setattr(store, "_member_memory_write_blocked", blocked)
    monkeypatch.setattr(memory_store_module, "_exec", unexpected_exec)

    with pytest.raises(MemoryMutationError) as exc_info:
        if profile_kind == "identity":
            await store.upsert_identity_profile(
                tenant_id="tenant-a",
                channel="wechat",
                source_key="wxbot",
                user_id="wxid-a",
                manual_notes="must not return",
            )
        else:
            await store.upsert_session_profile(
                tenant_id="tenant-a",
                channel="wechat",
                source_key="wxbot",
                session_id="room-a",
                user_id="wxid-a",
                manual_notes="must not return",
            )

    assert exc_info.value.detail == "member_memory_write_blocked"
    assert exc_info.value.status_code == 409
    assert locks == [("tenant-a", "wxid-a")]


@pytest.mark.asyncio
async def test_profile_enrichment_candidate_uses_authoritative_member_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    locks: list[str] = []

    async def lock_member(**kwargs: str) -> None:
        locks.append(kwargs["user_id"])

    async def blocked(**kwargs: str) -> bool:
        return True

    async def unexpected_exec(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("blocked candidate reached durable storage")

    monkeypatch.setattr(store, "_lock_member_memory_mutation", lock_member)
    monkeypatch.setattr(store, "_member_memory_write_blocked", blocked)
    monkeypatch.setattr(memory_store_module, "_exec", unexpected_exec)

    with pytest.raises(MemoryMutationError) as exc_info:
        await store.create_profile_enrichment_candidate(
            tenant_id="tenant-a",
            channel="wechat",
            source_key="wxbot",
            session_id="room-a@chatroom",
            user_id="wxid-a",
            report_payload={"profile": {"summary": "candidate"}},
        )

    assert exc_info.value.detail == "member_memory_write_blocked"
    assert locks == ["wxid-a"]


@pytest.mark.asyncio
@pytest.mark.parametrize("scope_type", ["identity", "session"])
async def test_legacy_lazy_import_is_fenced_and_suppressed_after_wechat_opt_out(
    monkeypatch: pytest.MonkeyPatch,
    scope_type: str,
) -> None:
    store = MemoryStore(SimpleNamespace())
    locks: list[str] = []

    async def lock_member(**kwargs: str) -> None:
        locks.append(kwargs["user_id"])

    async def blocked(**kwargs: str) -> bool:
        assert locks == ["wxid-a"]
        return True

    async def unexpected_insert(**kwargs: Any) -> dict[str, Any]:
        raise AssertionError("legacy cache content was re-materialized after opt-out")

    monkeypatch.setattr(store, "_lock_member_memory_mutation", lock_member)
    monkeypatch.setattr(store, "_member_memory_write_blocked", blocked)
    monkeypatch.setattr(store, "_insert_or_touch_memory_item", unexpected_insert)

    profile = {
        "tenant_id": "tenant-a",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid-a",
        "session_id": "room-a",
        "manual_notes": "legacy note",
        "long_term_memory": "legacy memory",
        "long_term_items_json": '["legacy memory"]',
    }
    if scope_type == "identity":
        await store._import_legacy_identity_items(profile)
    else:
        await store._import_legacy_session_items(profile)

    assert locks == ["wxid-a"]


@pytest.mark.asyncio
async def test_deleted_source_projection_tombstone_blocks_only_same_event_and_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    inserted_keys: list[str] = []

    async def fake_exec(
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        params = dict(params or {})
        if "FROM plugin_memory_event WHERE id = ANY(:event_ids)" in sql:
            return [
                {
                    "id": 9,
                    "source_member_id": "",
                    "source_message_id": "message-9",
                }
            ]
        if "deleted_at IS NOT NULL" in sql:
            return (
                [{"id": 41}]
                if params["normalized_key"] == "preference:value:deleted"
                else []
            )
        if (
            "FROM plugin_memory_item" in sql
            and "normalized_key = :normalized_key" in sql
        ):
            return []
        if sql.startswith("INSERT INTO plugin_memory_item"):
            inserted_keys.append(str(params["normalized_key"]))
            return [
                {
                    "id": 42,
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
                    "source_evidence_json": params["source_evidence"],
                    "source_event_id": params["source_event_id"],
                    "occurrence_count": 1,
                    "deleted_at": None,
                }
            ]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    deleted_projection = await store._insert_or_touch_memory_item(
        tenant_id="tenant-a",
        channel="discord",
        source_key="discord-bot",
        user_id="discord-user",
        source_type="auto",
        memory_type="preference",
        content="deleted preference",
        normalized_key="preference:value:deleted",
        source_event_id=9,
    )
    unrelated_projection = await store._insert_or_touch_memory_item(
        tenant_id="tenant-a",
        channel="discord",
        source_key="discord-bot",
        user_id="discord-user",
        source_type="auto",
        memory_type="preference",
        content="different preference",
        normalized_key="preference:value:other",
        source_event_id=9,
    )

    assert deleted_projection is None
    assert unrelated_projection is not None
    assert inserted_keys == ["preference:value:other"]


@pytest.mark.asyncio
async def test_exact_forget_holds_member_fence_and_invalidates_graph_vectors_and_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    current = {
        "id": 51,
        "tenant_id": "tenant-a",
        "channel": "discord",
        "source_key": "discord-bot",
        "user_id": "discord-user",
        "session_id": "",
        "scope_type": "identity",
        "source_type": "auto",
        "memory_type": "preference",
        "content": "old preference",
        "normalized_key": "preference:value:old",
        "status": "active",
        "pinned": False,
        "sensitivity": "normal",
        "source_event_id": 9,
        "source_evidence": [
            {
                "source_event_id": 9,
                "source_member_id": "",
                "source_message_id": "message-9",
            }
        ],
        "deleted_at": None,
    }
    locks: list[str] = []
    sql_calls: list[tuple[str, dict[str, Any]]] = []
    vector_items: list[int] = []
    vector_episodes: list[int] = []
    refreshed: list[int] = []

    async def get_item(
        item_id: int,
        *,
        for_update: bool = False,
    ) -> dict[str, Any]:
        assert item_id == 51
        if for_update:
            assert locks == ["discord-user"]
        return dict(current)

    async def lock_member(**kwargs: str) -> None:
        locks.append(kwargs["user_id"])

    async def fake_exec(
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        params = dict(params or {})
        sql_calls.append((sql, params))
        if sql.startswith("SELECT id, memory_item_ids_json"):
            return [{"id": 61, "memory_item_ids_json": "[51, 52]"}]
        if sql.startswith("UPDATE plugin_memory_item SET status = 'deleted'"):
            return [{"id": 51}]
        return []

    async def delete_item_vectors(item_id: int) -> None:
        vector_items.append(item_id)

    async def delete_episode(
        episode_id: int,
        *,
        force: bool = False,
    ) -> str:
        assert force is True
        vector_episodes.append(episode_id)
        return "deleted"

    async def refresh(item: dict[str, Any]) -> None:
        refreshed.append(int(item["id"]))

    monkeypatch.setattr(store, "get_memory_item", get_item)
    monkeypatch.setattr(store, "_lock_member_memory_mutation", lock_member)
    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(
        store,
        "_delete_memory_and_graph_vectors_for_item",
        delete_item_vectors,
    )
    monkeypatch.setattr(store.vector_index, "delete_episode", delete_episode)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", refresh)

    result = await store.forget_memory_items(
        tenant_id="tenant-a",
        channel="discord",
        source_key="discord-bot",
        user_id="discord-user",
        item_id=51,
    )

    assert result == {"ids": [51], "count": 1}
    assert locks == ["discord-user"]
    assert vector_items == [51]
    assert vector_episodes == [61]
    assert refreshed == [51]
    assert any(
        sql.startswith("UPDATE plugin_memory_fact SET status = 'invalidated'")
        for sql, _ in sql_calls
    )
    archive_calls = [
        params
        for sql, params in sql_calls
        if sql.startswith("UPDATE plugin_memory_episode SET status = 'archived'")
    ]
    assert len(archive_calls) == 1
    assert json.loads(archive_calls[0]["memory_item_ids_json"]) == [52]


@pytest.mark.asyncio
async def test_group_member_exact_delete_uses_same_fence_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(SimpleNamespace())
    updated_at = datetime(2026, 7, 30, 9, 0)
    minimal = {
        "id": 71,
        "content": "member preference",
        "memory_type": "preference",
        "scope_type": "session",
        "audience_scope": "session",
        "status": "active",
        "sensitivity_category": "normal",
        "pinned": False,
        "expires_at": None,
        "updated_at": updated_at,
        "source_key": "wxbot",
        "session_id": "room-a@chatroom",
    }
    full_item = {
        **minimal,
        "tenant_id": "tenant-a",
        "channel": "wechat",
        "user_id": "wxid-a",
        "source_type": "auto",
        "normalized_key": "preference:value:member",
        "source_event_id": 17,
        "deleted_at": None,
    }
    locks: list[str] = []
    vector_items: list[int] = []
    refreshed: list[int] = []
    sql_calls: list[str] = []

    async def get_member_item(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return dict(minimal)

    async def get_full_item(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return dict(full_item)

    async def lock_member(**kwargs: str) -> None:
        locks.append(kwargs["user_id"])

    async def fake_exec(
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        sql_calls.append(sql)
        if sql.startswith("SELECT id, memory_item_ids_json"):
            return []
        if sql.startswith("UPDATE plugin_memory_item SET status = 'deleted'"):
            return [{"id": 71}]
        return []

    async def delete_vectors(item_id: int) -> None:
        vector_items.append(item_id)

    async def refresh(item: dict[str, Any]) -> None:
        refreshed.append(int(item["id"]))

    monkeypatch.setattr(store, "get_group_member_memory_item", get_member_item)
    monkeypatch.setattr(store, "get_memory_item", get_full_item)
    monkeypatch.setattr(store, "_lock_member_memory_mutation", lock_member)
    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(
        store,
        "_delete_memory_and_graph_vectors_for_item",
        delete_vectors,
    )
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", refresh)

    deleted = await store.delete_group_member_memory_item(
        71,
        tenant_id="tenant-a",
        session_id="room-a@chatroom",
        user_id="wxid-a",
        policy=MemberPrivacyValues(
            memory_enabled=True,
            allow_group_recall=True,
            audience_scope="session",
        ),
        expected_etag=memory_store_module._member_memory_etag(minimal),
    )

    assert deleted is True
    assert locks == ["wxid-a"]
    assert vector_items == [71]
    assert refreshed == [71]
    assert any(
        sql.startswith("UPDATE plugin_memory_fact SET status = 'invalidated'")
        for sql in sql_calls
    )


@pytest.mark.asyncio
async def test_vector_publish_is_discarded_when_database_transaction_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(
        SimpleNamespace(memory_vector_index_enabled=True),
        llm_service=object(),
        vector_store=object(),
    )
    published: list[int] = []

    class _Transaction:
        async def __aenter__(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class _Engine:
        def begin(self) -> _Transaction:
            return _Transaction()

    async def record_current(
        item_id: int,
        *,
        fallback_item: dict[str, Any] | None = None,
    ) -> str:
        assert fallback_item is not None
        published.append(int(item_id))
        return "published"

    monkeypatch.setattr(memory_store_module, "get_engine", lambda: _Engine())
    monkeypatch.setattr(
        store,
        "_publish_current_memory_vectors",
        record_current,
    )

    with pytest.raises(RuntimeError, match="rollback"):
        async with store._mutation_transaction():
            await store._sync_memory_vector_for_item_safe({"id": 44})
            raise RuntimeError("rollback")

    assert published == []

    async with store._mutation_transaction():
        await store._sync_memory_vector_for_item_safe({"id": 45})
    assert published == [45]


@pytest.mark.asyncio
async def test_post_commit_vector_publication_rechecks_after_member_erase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(
        SimpleNamespace(memory_vector_index_enabled=True),
        llm_service=object(),
        vector_store=object(),
    )
    state_calls = 0
    locks: list[tuple[str, str]] = []
    deleted: list[int] = []

    class _Transaction:
        async def __aenter__(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class _Engine:
        def begin(self) -> _Transaction:
            return _Transaction()

    async def publication_state(
        item_id: int,
        *,
        for_update: bool = False,
    ) -> tuple[dict[str, Any] | None, set[str]]:
        nonlocal state_calls
        state_calls += 1
        if not for_update:
            return (
                {
                    "id": item_id,
                    "tenant_id": "tenant-a",
                    "channel": "discord",
                    "user_id": "discord-user",
                },
                {"discord-user"},
            )
        # The member erase committed after the save but before deferred
        # publication obtained the member fence.
        return None, set()

    async def lock_member(**kwargs: str) -> None:
        locks.append((kwargs["tenant_id"], kwargs["user_id"]))

    async def missing_item(*args: Any, **kwargs: Any) -> None:
        return None

    async def delete_vectors(item_id: int) -> None:
        deleted.append(item_id)

    async def stale_upsert(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("erased item must never be re-published")

    monkeypatch.setattr(memory_store_module, "get_engine", lambda: _Engine())
    monkeypatch.setattr(
        store,
        "_memory_vector_publication_state",
        publication_state,
    )
    monkeypatch.setattr(store, "_lock_member_memory_mutation", lock_member)
    monkeypatch.setattr(store, "get_memory_item", missing_item)
    monkeypatch.setattr(
        store,
        "_delete_memory_and_graph_vectors_for_item",
        delete_vectors,
    )
    monkeypatch.setattr(store, "_sync_memory_vector_for_item_safe", stale_upsert)
    monkeypatch.setattr(
        store,
        "_sync_graph_vectors_for_memory_item_safe",
        stale_upsert,
    )

    active_token = memory_store_module._ACTIVE_MUTATION_CONNECTION.set(None)
    try:
        result = await store._publish_current_memory_vectors(
            77,
            fallback_item={
                "id": 77,
                "tenant_id": "tenant-a",
                "channel": "discord",
                "user_id": "discord-user",
            },
        )
    finally:
        memory_store_module._ACTIVE_MUTATION_CONNECTION.reset(active_token)

    assert result == "deleted"
    assert state_calls == 2
    assert locks == [("tenant-a", "discord-user")]
    assert deleted == [77]
