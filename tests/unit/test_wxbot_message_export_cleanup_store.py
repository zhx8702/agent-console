from __future__ import annotations

import pytest

from app.common.config import Settings
from plugins.wxbot import store as store_module
from plugins.wxbot.store import WxbotStore


@pytest.mark.asyncio
async def test_active_outbound_file_paths_are_global_and_only_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append((sql, dict(params or {})))
        return [
            {"file_path": "/data/wxbot-outbound/a.txt"},
            {"file_path": ""},
            {"file_path": "/data/wxbot-outbound/b.txt"},
        ]

    monkeypatch.setattr(store_module, "_exec", fake_exec)
    store = WxbotStore(Settings(customer_service_prompt_enabled=False))

    paths = await store.list_active_outbound_file_paths(limit=20000)

    assert paths == [
        "/data/wxbot-outbound/a.txt",
        "/data/wxbot-outbound/b.txt",
    ]
    sql, params = calls[0]
    assert "msg_type = 'file'" in sql
    assert "status IN ('pending', 'sending', 'queued')" in sql
    assert "tenant_id" not in sql
    assert params == {"lim": 20000}
