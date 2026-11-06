"""Connection/tenant isolation against real PostgreSQL temporary tables only."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.channel.identity import canonical_conversation_id
from plugins.memory.runtime_legacy import retrieve_legacy_group_memory
from plugins.memory.store import GROUP_HISTORY_USER_ID_SCOPE
from plugins.wxbot.store import WxbotStore

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.getenv("MEMORY_TEST_DSN"), reason="set MEMORY_TEST_DSN for isolated temporary-table tests",
)]


@pytest_asyncio.fixture
async def connection(monkeypatch):
    import plugins.memory.store as memory_store
    import plugins.wxbot.store as wxbot_store

    engine = create_async_engine(os.environ["MEMORY_TEST_DSN"])
    async with engine.connect() as conn:
        transaction = await conn.begin()
        await conn.execute(text("""
            CREATE TEMP TABLE plugin_wxbot_group_observations (
                id bigint PRIMARY KEY, tenant_id text, session_id text,
                message_id text, sender_name text, sender_wxid text,
                content text, is_self_sent boolean DEFAULT false,
                msg_type text DEFAULT 'text', occurred_ts bigint,
                received_at timestamp DEFAULT '2026-02-04 00:00:00'
            ) ON COMMIT DROP
        """))
        await conn.execute(text("""
            CREATE TEMP TABLE plugin_memory_item (
                id bigint PRIMARY KEY, tenant_id text, channel text DEFAULT 'wechat',
                source_key text DEFAULT 'wxbot', user_id text,
                session_id text, value_json text, status text DEFAULT 'active',
                deleted_at timestamp, scope_type text DEFAULT 'session',
                audience_scope text DEFAULT 'session', origin_session_kind text DEFAULT 'group',
                sensitivity text DEFAULT 'normal', sensitivity_category text DEFAULT 'normal',
                expires_at timestamp, source_type text DEFAULT 'llm_group_window'
            ) ON COMMIT DROP
        """))
        async def execute(sql, params=None):
            return [dict(row) for row in (await conn.execute(text(sql), params or {})).mappings()]
        monkeypatch.setattr(memory_store, "_exec", execute)
        monkeypatch.setattr(wxbot_store, "_exec", execute)
        try:
            yield conn
        finally:
            await transaction.rollback()
    await engine.dispose()


async def test_history_filters_sender_tenant_and_connection_before_limit(connection):
    canonical = canonical_conversation_id("connection-a", "room@chatroom")
    other = canonical_conversation_id("connection-b", "room@chatroom")
    rows = [dict(id=i, tid="demo", sid=canonical, mid=f"m{i}", sender="self" if i <= 26 else "other")
            for i in range(1, 60)]
    rows += [dict(id=100, tid="other-tenant", sid=canonical, mid="x", sender="self"),
             dict(id=101, tid="demo", sid=other, mid="y", sender="self")]
    await connection.execute(text(
        "INSERT INTO plugin_wxbot_group_observations "
        "(id,tenant_id,session_id,message_id,sender_name,sender_wxid,content,occurred_ts) "
        "VALUES (:id,:tid,:sid,:mid,'Name',:sender,'部署问题',1000)"
    ), rows)
    store = WxbotStore(SimpleNamespace())
    args = dict(tenant_id="demo", session_id=canonical, start_ts=900, end_ts=1100,
                sender_wxid="self", exclude_message_id="m26", limit=20)
    result = await store.search_recorded_group_messages(**args)
    assert result["total"] == 25
    assert result["truncated"] is True
    assert len(result["messages"]) == 20
    assert {row["observation_id"] for row in result["messages"]} == set(range(6, 26))
    assert (await store.search_recorded_group_messages(**args, query="' OR 1=1 --"))["total"] == 0
    assert (await store.search_recorded_group_messages(**(args | {"start_ts": 1100, "end_ts": 1200})))["total"] == 0
    await connection.execute(text(
        "UPDATE plugin_wxbot_group_observations SET content = :body WHERE id = 25"
    ), {"body": "长消息" * 500})
    partial = await store.search_recorded_group_messages(**args)
    assert partial["messages"][0]["text_truncated"] is True
    assert len(partial["messages"][0]["text"]) == 1200


async def test_legacy_compatibility_rejects_mixed_missing_and_other_tenant_evidence(connection):
    canonical = canonical_conversation_id("connection-a", "room@chatroom")
    other = canonical_conversation_id("connection-b", "room@chatroom")
    await connection.execute(text(
        "INSERT INTO plugin_wxbot_group_observations (id,tenant_id,session_id) VALUES (:id,:tid,:sid)"
    ), [{"id": 1, "tid": "demo", "sid": canonical},
        {"id": 2, "tid": "demo", "sid": other},
        {"id": 3, "tid": "other-tenant", "sid": canonical}])
    rows = [{"id": item, "tid": "demo", "uid": GROUP_HISTORY_USER_ID_SCOPE,
             "value": json.dumps({"source_observation_ids": ids})}
            for item, ids in [(10, [1]), (11, [1, 2]), (12, [3]), (13, [1, 999]), (14, [])]]
    rows.append({"id": 15, "tid": "other-tenant", "uid": GROUP_HISTORY_USER_ID_SCOPE,
                 "value": json.dumps({"source_observation_ids": [1]})})
    await connection.execute(text(
        "INSERT INTO plugin_memory_item (id,tenant_id,user_id,session_id,value_json) "
        "VALUES (:id,:tid,:uid,'room@chatroom',:value)"
    ), rows)
    store = SimpleNamespace(
        settings=SimpleNamespace(memory_graph_retrieval_enabled=True),
        combined_history_scope_execution_allowed=AsyncMock(return_value=True),
        retrieve_memory_hybrid=AsyncMock(return_value={
            "items": [{"id": i} for i in range(10, 16)],
            "facts": [{"id": 110, "memory_item_id": 10}, {"id": 111, "memory_item_id": 11}],
        }),
    )
    result = await retrieve_legacy_group_memory(
        store, tenant_id="demo", connection_id="connection-a", session_id=canonical,
        external_session_id="room@chatroom", source_key="wxbot", query="部署",
    )
    assert result == {"items": [{"id": 10}], "facts": [{"id": 110, "memory_item_id": 10}]}
