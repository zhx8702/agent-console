from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.channel.identity import canonical_conversation_id
from plugins.group_activity.store import (
    GroupActivityStore,
    normalize_group_activity_config_values,
    normalize_group_activity_identity,
)


def test_normalize_group_activity_identity_accepts_only_coherent_owners() -> None:
    settings = SimpleNamespace(wxbot_default_tenant_id="demo")
    external = "managed-room@chatroom"
    managed_session = canonical_conversation_id("managed-a", external)

    assert normalize_group_activity_identity(
        settings,
        tenant_id="demo",
        session_id="legacy-room@chatroom",
    ) == {
        "adapter_id": "wechat-sdk",
        "connection_id": "legacy-wechat-default",
        "external_session_id": "legacy-room@chatroom",
    }
    assert normalize_group_activity_identity(
        settings,
        tenant_id="demo",
        session_id=managed_session,
        connection_id="managed-a",
        adapter_id="wechat-sdk",
        external_session_id=external,
    ) == {
        "adapter_id": "wechat-sdk",
        "connection_id": "managed-a",
        "external_session_id": external,
    }


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        (
            {
                "tenant_id": "other",
                "session_id": "room@chatroom",
                "connection_id": "legacy-wechat-default",
                "external_session_id": "room@chatroom",
            },
            "legacy_wxbot_history_tenant_unavailable",
        ),
        (
            {
                "tenant_id": "demo",
                "session_id": "cx1:c:bad@chatroom",
                "connection_id": "legacy-wechat-default",
                "external_session_id": "cx1:c:bad@chatroom",
            },
            "channel_identity_mismatch",
        ),
        (
            {
                "tenant_id": "demo",
                "session_id": "cx1:c:bad@chatroom",
                "connection_id": "managed-a",
                "adapter_id": "wechat-sdk",
                "external_session_id": "",
            },
            "external_session_identity_unavailable",
        ),
    ],
)
def test_normalize_group_activity_identity_rejects_ambiguous_scope(
    kwargs: dict[str, str],
    reason: str,
) -> None:
    settings = SimpleNamespace(wxbot_default_tenant_id="demo")

    with pytest.raises(ValueError, match=reason):
        normalize_group_activity_identity(settings, **kwargs)


def test_normalize_group_activity_config_values_repairs_legacy_bounds() -> None:
    legacy = {
        "tenant_id": "demo",
        "session_id": "room@chatroom",
        "idle_minutes": 60,
        "lookback_minutes": 30,
        "min_send_interval_minutes": 15,
        "max_per_day": 24,
        "topic_repeat_window_minutes": 30,
        "temperature": float("nan"),
        "version": 7,
    }

    normalized = normalize_group_activity_config_values(legacy)

    assert normalized == {
        **legacy,
        "idle_minutes": 180,
        "lookback_minutes": 60,
        "min_send_interval_minutes": 60,
        "max_per_day": 3,
        "topic_repeat_window_minutes": 60,
        "temperature": 0.9,
    }
    assert legacy["idle_minutes"] == 60
    assert normalized["version"] == 7


def test_normalize_group_activity_config_values_preserves_valid_boundaries() -> None:
    valid = {
        "idle_minutes": 180,
        "lookback_minutes": 60,
        "min_send_interval_minutes": 60,
        "max_per_day": 1,
        "topic_repeat_window_minutes": 10080,
        "temperature": 0.0,
    }

    assert normalize_group_activity_config_values(valid) == valid


def test_config_params_persist_normalized_legacy_values() -> None:
    import plugins.group_activity.store as module

    params = module._config_params(
        {
            "tenant_id": "demo",
            "session_id": "room@chatroom",
            "idle_minutes": 60,
            "lookback_minutes": 30,
            "min_send_interval_minutes": 15,
            "max_per_day": 24,
            "topic_repeat_window_minutes": 20000,
            "temperature": float("inf"),
        },
        expected_version=7,
    )

    assert params["idle_minutes"] == 180
    assert params["lookback_minutes"] == 60
    assert params["min_interval"] == 60
    assert params["max_per_day"] == 3
    assert params["topic_repeat_window_minutes"] == 10080
    assert params["temperature"] == 0.9
    assert params["expected_version"] == 7


@pytest.mark.asyncio
async def test_config_lists_normalize_legacy_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plugins.group_activity.store as module

    async def execute_rows(sql: str, params: dict | None = None):
        _ = (sql, params)
        return [
            {
                "tenant_id": "demo",
                "session_id": "room@chatroom",
                "idle_minutes": 60,
                "lookback_minutes": 120,
                "min_send_interval_minutes": 180,
                "max_per_day": 1,
                "topic_repeat_window_minutes": 1440,
                "temperature": 0.9,
            }
        ]

    monkeypatch.setattr(module, "_exec", execute_rows)
    store = GroupActivityStore(SimpleNamespace(wxbot_default_tenant_id="demo"))

    listed = await store.list_configs("demo")
    enabled = await store.list_enabled_configs()

    assert listed[0]["idle_minutes"] == 180
    assert enabled[0]["idle_minutes"] == 180


@pytest.mark.asyncio
async def test_get_config_hydrates_durable_identity_from_session_metadata_join(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import plugins.group_activity.store as module

    settings = SimpleNamespace(wxbot_default_tenant_id="demo")
    store = GroupActivityStore(settings)
    external = "managed-room@chatroom"
    session_id = canonical_conversation_id("managed-a", external)
    row = {
        **store.default_config("demo", session_id),
        "idle_minutes": 60,
        "version": 1,
        "channel_id": "wechat",
        "adapter_id": "wechat-sdk",
        "connection_id": "managed-a",
        "external_session_id": external,
    }
    statements: list[str] = []

    class _Mappings:
        def first(self):
            return row

    class _Result:
        def mappings(self):
            return _Mappings()

    class _Connection:
        async def execute(self, statement, params):
            assert params == {"tid": "demo", "sid": session_id}
            statements.append(str(statement))
            return _Result()

    @asynccontextmanager
    async def read_connection():
        yield _Connection()

    monkeypatch.setattr(module, "_read_connection", read_connection)

    hydrated = await store.get_config("demo", session_id)

    assert hydrated["adapter_id"] == "wechat-sdk"
    assert hydrated["connection_id"] == "managed-a"
    assert hydrated["external_session_id"] == external
    assert hydrated["idle_minutes"] == 180
    assert len(statements) == 1
    assert "LEFT JOIN sessions AS s" in statements[0]
    assert "s.metadata ->> 'adapter_id'" in statements[0]
    assert "s.metadata ->> 'connection_id'" in statements[0]
    assert "s.metadata ->> 'external_conversation_id'" in statements[0]
