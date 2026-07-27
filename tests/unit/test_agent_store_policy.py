from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import app.agent.store as store_module
from app.agent.store import AgentStore


@pytest.mark.asyncio
async def test_missing_agent_tool_policy_inherits_enabled_default_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        _ = sql, params
        return []

    monkeypatch.setattr(store_module, "_exec", fake_exec)
    policy = await AgentStore(SimpleNamespace()).get_session_policy(
        "tenant-a",
        "room@chatroom",
        available_tools=["list_group_members", "search_group_messages"],
    )

    assert policy["enabled"] is True
    assert policy["policy_configured"] is False
    assert policy["allowed_tools"] == []
    assert policy["effective_tools"] == ["list_group_members", "search_group_messages"]
    assert policy["inherits_default_tools"] is True
    assert policy["denial_reason"] == ""


@pytest.mark.asyncio
async def test_empty_agent_tool_allowlist_inherits_enabled_default_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        _ = sql, params
        return [
            {
                "tenant_id": "tenant-a",
                "session_id": "room@chatroom",
                "scope": "group_info",
                "enabled": True,
                "allowed_tools_json": "[]",
                "updated_at": None,
            }
        ]

    monkeypatch.setattr(store_module, "_exec", fake_exec)
    policy = await AgentStore(SimpleNamespace()).get_session_policy(
        "tenant-a",
        "room@chatroom",
        available_tools=["list_group_members", "search_group_messages"],
    )

    assert policy["enabled"] is True
    assert policy["policy_configured"] is True
    assert policy["effective_tools"] == ["list_group_members", "search_group_messages"]
    assert policy["inherits_default_tools"] is True
    assert policy["denial_reason"] == ""


@pytest.mark.asyncio
async def test_explicitly_disabled_agent_tool_policy_stays_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        _ = sql, params
        return [
            {
                "tenant_id": "tenant-a",
                "session_id": "room@chatroom",
                "scope": "group_info",
                "enabled": False,
                "allowed_tools_json": "[]",
                "updated_at": None,
            }
        ]

    monkeypatch.setattr(store_module, "_exec", fake_exec)
    policy = await AgentStore(SimpleNamespace()).get_session_policy(
        "tenant-a",
        "room@chatroom",
        available_tools=["list_group_members", "search_group_messages"],
    )

    assert policy["enabled"] is False
    assert policy["policy_configured"] is True
    assert policy["effective_tools"] == []
    assert policy["inherits_default_tools"] is True
    assert policy["denial_reason"] == "policy_disabled"


@pytest.mark.asyncio
async def test_setting_empty_allowlist_round_trips_as_inherited_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored_row: dict[str, Any] | None = None

    async def fake_exec(
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        nonlocal stored_row
        if sql.startswith("SELECT"):
            return [dict(stored_row)] if stored_row else []
        if sql.startswith("INSERT"):
            values = params or {}
            stored_row = {
                "tenant_id": values["tid"],
                "session_id": values["sid"],
                "scope": values["scope"],
                "enabled": values["enabled"],
                "allowed_tools_json": values["allowed_tools_json"],
                "updated_at": None,
            }
        return []

    monkeypatch.setattr(store_module, "_exec", fake_exec)
    policy = await AgentStore(SimpleNamespace()).set_session_policy(
        "tenant-a",
        "room@chatroom",
        enabled=True,
        allowed_tools=[],
        available_tools=["list_group_members", "search_group_messages"],
    )

    assert policy["enabled"] is True
    assert policy["policy_configured"] is True
    assert policy["allowed_tools"] == []
    assert policy["inherits_default_tools"] is True
    assert policy["effective_tools"] == ["list_group_members", "search_group_messages"]


@pytest.mark.asyncio
async def test_agent_tool_policy_only_allows_explicit_catalog_intersection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(
        sql: str,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        _ = sql, params
        return [
            {
                "tenant_id": "tenant-a",
                "session_id": "room@chatroom",
                "scope": "group_info",
                "enabled": True,
                "allowed_tools_json": '["get_group_info", "unknown_tool"]',
                "updated_at": None,
            }
        ]

    monkeypatch.setattr(store_module, "_exec", fake_exec)
    policy = await AgentStore(SimpleNamespace()).get_session_policy(
        "tenant-a",
        "room@chatroom",
        available_tools=["get_group_info", "search_group_messages"],
    )

    assert policy["effective_tools"] == ["get_group_info"]
    assert policy["inherits_default_tools"] is False
    assert policy["denial_reason"] == ""
