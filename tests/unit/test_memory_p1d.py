from __future__ import annotations

from types import SimpleNamespace

import pytest

import plugins.memory.store as memory_store_module
from app.common.prompting import augment_prompt_with_persona_and_memory
from app.common.types import Channel, Session
from plugins.memory.store import MemoryStore


def _row(**kwargs):
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
        "normalized_key": "preference:brand:adidas",
        "confidence": 0.9,
        "status": "active",
        "pinned": False,
        "priority": 0,
        "sensitivity": "normal",
        "source_event_id": None,
        "source_trace_id": "",
        "original_text": "",
        "occurrence_count": 1,
        "first_seen_at": "2026-04-01T00:00:00",
        "last_seen_at": "2026-04-01T00:00:00",
        "created_at": "2026-04-01T00:00:00",
        "updated_at": "2026-04-01T00:00:00",
        "deleted_at": None,
        "match_count": 0,
    }
    data.update(kwargs)
    return data


@pytest.mark.asyncio
async def test_retrieve_filters_non_injectable_items(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        assert "status = 'active'" in sql
        assert "sensitivity = 'normal'" in sql
        assert "deleted_at IS NULL" in sql
        return [
            _row(id=1, content="用户喜欢 Adidas", match_count=1),
            _row(id=2, content="pending", status="pending", match_count=10),
            _row(id=3, content="deleted", status="deleted", deleted_at="2026-04-02", match_count=10),
            _row(id=4, content="invalidated", status="invalidated", match_count=10),
            _row(id=5, content="sensitive", sensitivity="pii", match_count=10),
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

    assert [row["content"] for row in rows] == ["用户喜欢 Adidas"]


@pytest.mark.asyncio
async def test_retrieve_query_hits_rank_above_unrelated_recent(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        assert params and params["token_0"] == "%adidas%"
        return [
            _row(id=1, content="昨天问了发票", updated_at="2026-05-01T00:00:00", match_count=0),
            _row(id=2, content="用户喜欢 Adidas", updated_at="2026-04-01T00:00:00", match_count=1),
        ]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    rows = await MemoryStore(SimpleNamespace()).retrieve_memory_items(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="s1",
        query="Adidas 尺码",
        limit=2,
    )

    assert rows[0]["content"] == "用户喜欢 Adidas"


@pytest.mark.asyncio
async def test_retrieve_cjk_query_uses_ngrams_to_rank_relevant_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        assert "LOWER(content) LIKE" in sql
        assert params
        token_values = [
            params[f"token_{index}"]
            for index in range(12)
            if f"token_{index}" in params
        ]
        assert len(token_values) <= 12
        assert "%物流%" in token_values

        def match_count(row: dict) -> int:
            searchable = f"{row['content']} {row['normalized_key']}".lower()
            return sum(1 for token in token_values if token.strip("%") in searchable)

        unrelated_recent = _row(
            id=1,
            content="用户最近在问发票",
            normalized_key="note:invoice",
            updated_at="2026-05-01T00:00:00",
        )
        relevant_older = _row(
            id=2,
            content="用户正在查物流",
            normalized_key="note:shipping:物流",
            updated_at="2026-04-01T00:00:00",
        )
        unrelated_recent["match_count"] = match_count(unrelated_recent)
        relevant_older["match_count"] = match_count(relevant_older)
        assert relevant_older["match_count"] > 0
        assert unrelated_recent["match_count"] == 0
        return [unrelated_recent, relevant_older]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    rows = await MemoryStore(SimpleNamespace()).retrieve_memory_items(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="s1",
        query="查一下物流",
        limit=2,
    )

    assert rows[0]["content"] == "用户正在查物流"


@pytest.mark.asyncio
async def test_retrieve_manual_pinned_boost_works_without_query_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        return [
            _row(id=1, content="普通近期备注", updated_at="2026-05-01T00:00:00", match_count=0),
            _row(
                id=2,
                content="人工标记为 VIP",
                source_type="manual",
                pinned=True,
                priority=100,
                updated_at="2026-04-01T00:00:00",
                match_count=0,
            ),
        ]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    rows = await MemoryStore(SimpleNamespace()).retrieve_memory_items(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="s1",
        query="",
        limit=2,
    )

    assert rows[0]["content"] == "人工标记为 VIP"


@pytest.mark.asyncio
async def test_retrieve_session_scope_boost_works(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        assert "(scope_type = 'identity' OR (scope_type = 'session' AND session_id = :sid))" in sql
        return [
            _row(id=1, content="身份备注 Adidas", scope_type="identity", match_count=1),
            _row(
                id=2,
                content="本会话刚问 Adidas 物流",
                scope_type="session",
                session_id="s1",
                match_count=1,
            ),
        ]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    rows = await MemoryStore(SimpleNamespace()).retrieve_memory_items(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="s1",
        query="Adidas",
        limit=2,
    )

    assert rows[0]["scope_type"] == "session"


@pytest.mark.asyncio
async def test_retrieve_source_fallback_does_not_cross_user_or_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        assert "source_key IN (:source_key, '*')" in sql
        assert "user_id = :uid" in sql
        assert params
        assert params["source_key"] == "wxbot"
        assert params["uid"] == "wxid_a"
        return [
            _row(id=1, source_key="*", user_id="wxid_a", content="全局源记忆", match_count=1),
            _row(id=2, source_key="wxbot", user_id="other", content="别人的记忆", match_count=20),
            _row(id=3, source_key="discord", user_id="wxid_a", content="其他来源记忆", match_count=20),
        ]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    rows = await MemoryStore(SimpleNamespace()).retrieve_memory_items(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="s1",
        query="记忆",
        limit=10,
    )

    assert [row["content"] for row in rows] == ["全局源记忆"]


def test_prompt_injection_uses_topk_relevant_memory_not_all_auto_noise() -> None:
    session = Session(
        session_id="s1",
        tenant_id="demo",
        user_id="wxid_a",
        channel=Channel.WECHAT,
    )
    session.variables["user_memory"] = {
        "short_term": "用户最近说：要买鞋",
        "memory_items": {
            "identity": [
                {
                    "source_type": "manual",
                    "status": "active",
                    "pinned": True,
                    "confidence": 1.0,
                    "sensitivity": "normal",
                    "content": "人工标记为 VIP",
                },
                {
                    "source_type": "auto",
                    "status": "active",
                    "confidence": 0.99,
                    "sensitivity": "normal",
                    "content": "无关长期噪音",
                },
            ],
            "session": [],
        },
        "relevant_memory_items": [
            {
                "source_type": "auto",
                "status": "active",
                "confidence": 0.9,
                "sensitivity": "normal",
                "content": "用户喜欢 Adidas",
            }
        ],
    }

    prompt = augment_prompt_with_persona_and_memory("base", session, memory_intro="memory")

    assert "短期记忆：" in prompt
    assert "人工/置顶核心记忆：" in prompt
    assert "与当前消息相关的记忆" in prompt
    assert "可能不完整或已过时" in prompt
    assert "用户喜欢 Adidas" in prompt
    assert "无关长期噪音" not in prompt


def test_prompt_injection_uses_structured_session_state_before_legacy_short_term() -> None:
    session = Session(
        session_id="s1",
        tenant_id="demo",
        user_id="wxid_a",
        channel=Channel.WECHAT,
    )
    session.variables["user_memory"] = {
        "session_summary": "Open items: next step check invoice",
        "open_items": [{"text": "next step check invoice"}],
        "decisions": [{"text": "decided use short replies"}],
        "recent_turns": [{"user_text": "need continue invoice", "assistant_text": "ok"}],
        "short_term": "用户最近说：旧缓存",
    }

    prompt = augment_prompt_with_persona_and_memory("base", session, memory_intro="memory")

    assert "当前会话摘要" in prompt
    assert "当前未完成事项" in prompt
    assert "当前会话已确认决定" in prompt
    assert "近期会话轮次摘要" in prompt
    assert prompt.index("当前会话摘要") < prompt.index("短期记忆")
