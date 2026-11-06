from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.ops import memory_vector_reconcile as repair


@pytest.mark.asyncio
async def test_repair_reports_missing_vector_when_write_hook_swallowed_failure(monkeypatch):
    item = {"id": 1, "tenant_id": "t", "user_id": "u", "channel": "wechat"}
    provider = SimpleNamespace(close=AsyncMock())
    vector = SimpleNamespace(_client=SimpleNamespace(retrieve=AsyncMock(return_value=[])),
                             close=AsyncMock())
    store = SimpleNamespace(
        _publish_current_memory_vectors=AsyncMock(return_value="published"),
        get_memory_item=AsyncMock(return_value=item),
        member_memory_write_blocked=AsyncMock(return_value=False),
        vector_index=SimpleNamespace(is_indexable=lambda _: True,
                                     payload_for_item=lambda _: {"updated_at": "now"}),
    )
    pages = iter([[{"id": 1}], []])

    async def execute(*_args):
        rows = next(pages)
        return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: rows))

    @asynccontextmanager
    async def connect():
        yield SimpleNamespace(execute=execute)

    monkeypatch.setattr(repair, "get_engine", lambda: SimpleNamespace(connect=connect))
    monkeypatch.setattr(repair, "_build_embed_provider", lambda _: provider)
    monkeypatch.setattr(repair, "QdrantVectorStore", lambda *_: vector)
    monkeypatch.setattr(repair, "MemoryStore", lambda *_a, **_kw: store)
    monkeypatch.setattr(repair, "PluginStateStore", lambda: SimpleNamespace(
        execution_snapshot_allowed=AsyncMock(return_value=True)))
    result = await repair.reconcile(SimpleNamespace(
        llm_embed_provider="openai", memory_vector_index_enabled=True,
        qdrant_url="unused", qdrant_api_key="unused", memory_vector_collection="new"))
    assert result["scanned"] == 1
    assert result["errors"] == 1
    assert result["published"] == 0
    provider.close.assert_awaited_once()
    vector.close.assert_awaited_once()
