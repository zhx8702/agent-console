from __future__ import annotations

import time

import pytest

from app.common.config import Settings
from plugins.wxbot.message_reader import (
    WxbotMessageReader,
    decode_message_hex,
    message_table_name,
    parse_group_body,
)


@pytest.mark.asyncio
async def test_message_reader_returns_empty_when_table_missing() -> None:
    async def query_rows(**kwargs):
        assert kwargs["database"] == "message"
        return []

    reader = WxbotMessageReader(Settings(customer_service_prompt_enabled=False), query_rows=query_rows)

    assert await reader.load_group_text_messages("room@chatroom") == []


@pytest.mark.asyncio
async def test_message_reader_loads_group_text_messages() -> None:
    now = int(time.time())
    calls: list[dict] = []

    async def query_rows(**kwargs):
        calls.append(kwargs)
        if "sqlite_master" in kwargs["sql"]:
            return [{"ok": 1}]
        return [
            {
                "server_id": 101,
                "create_time": now - 30,
                "message_content_hex": "wxid_a:\n今天聊到部署".encode().hex(),
                "compression_type": 0,
            },
            {
                "server_id": 102,
                "create_time": now - 10,
                "message_content_hex": "wxid_b:\n我来看看日志".encode().hex(),
                "compression_type": 0,
            },
            {
                "server_id": 103,
                "create_time": now - 5,
                "message_content_hex": "bad hex",
                "compression_type": 0,
            },
        ]

    reader = WxbotMessageReader(Settings(customer_service_prompt_enabled=False), query_rows=query_rows)
    messages = await reader.load_group_text_messages(
        "room@chatroom",
        member_name_map={"wxid_a": "张三", "wxid_b": "李四"},
        hours=1,
        limit=10,
    )

    assert calls[0]["params"] == [message_table_name("room@chatroom")]
    assert "SELECT server_id" in calls[1]["sql"]
    assert len(messages) == 2
    assert messages[0]["message_id"] == "101"
    assert messages[0]["sender_name"] == "张三"
    assert messages[0]["text"] == "今天聊到部署"
    assert messages[1]["sender_wxid"] == "wxid_b"


def test_message_reader_decodes_and_parses_group_body() -> None:
    assert decode_message_hex("测试".encode().hex(), 0) == "测试"
    assert parse_group_body("wxid_a:\nhello") == ("wxid_a", "hello")
    assert parse_group_body("hello") == (None, "hello")
