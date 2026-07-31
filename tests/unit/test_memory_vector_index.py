from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import plugins.memory.store as memory_store_module
from app.kb.vector.memory_store import InMemoryVectorStore
from plugins.memory.store import MemoryStore

from ._fake_llm import FakeEmbeddingsProvider


def _settings(**overrides: Any) -> SimpleNamespace:
    values = {
        "memory_vector_index_enabled": True,
        "memory_vector_collection": "test_memory_items",
        "memory_vector_size": 64,
        "memory_vector_embed_model": "fake",
        "memory_vector_timeout_seconds": 1.0,
        "memory_vector_top_k": 8,
        "memory_graph_vector_top_k": 8,
        "llm_embed_model": "fake",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _row(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": 1,
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
        "session_id": "",
        "scope_type": "identity",
        "source_type": "manual",
        "memory_type": "note",
        "content": "用户喜欢 Adidas",
        "value_json": "{}",
        "normalized_key": "preference:brand:adidas",
        "confidence": 1.0,
        "status": "active",
        "pinned": True,
        "priority": 100,
        "sensitivity": "normal",
        "source_event_id": None,
        "source_trace_id": "",
        "original_text": "用户喜欢 Adidas",
        "occurrence_count": 1,
        "first_seen_at": None,
        "last_seen_at": None,
        "created_at": None,
        "updated_at": "2026-05-11T00:00:00",
        "deleted_at": None,
        # SQL retrieval computes this projection; fake rows must preserve the
        # relevance contract instead of bypassing it with an incomplete row.
        "match_count": 1,
    }
    values.update(overrides)
    return values


def _fact(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": 101,
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
        "subject_entity_id": 1,
        "subject_name": "user:wxid_a",
        "subject_normalized_name": "user wxid_a",
        "predicate": "likes",
        "object_entity_id": 2,
        "object_name": "Adidas",
        "object_normalized_name": "adidas",
        "object_value": "",
        "memory_item_id": 7,
        "source_event_id": 88,
        "confidence": 0.86,
        "status": "active",
        "valid_at": None,
        "invalid_at": None,
        "created_at": None,
        "updated_at": "2026-05-11T00:00:00",
    }
    values.update(overrides)
    return values


def _episode(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "id": 201,
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
        "session_id": "group-1",
        "title": "用户询问 Adidas 鞋码",
        "summary": "偏好 Adidas 的尺码建议",
        "event_ids": [88],
        "memory_item_ids": [7],
        "importance": 6,
        "status": "active",
        "created_at": None,
        "updated_at": "2026-05-11T00:00:00",
    }
    values.update(overrides)
    return values


class _FakeQdrantVectorStore(InMemoryVectorStore):
    name = "qdrant"


class _FailingQdrantVectorStore(_FakeQdrantVectorStore):
    async def ensure_collection(self, name: str, dim: int) -> None:
        _ = name, dim
        raise RuntimeError("qdrant unavailable")


class _DimensionMismatchQdrantVectorStore(_FakeQdrantVectorStore):
    async def ensure_collection(self, name: str, dim: int) -> None:
        existing = self._collections.get(name)
        if existing is not None and existing.dim != dim:
            raise ValueError(
                f"collection dimension mismatch: existing={existing.dim}, requested={dim}"
            )
        await super().ensure_collection(name, dim)


class _SearchFailingVectorStore(_FakeQdrantVectorStore):
    async def search(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        _ = args, kwargs
        raise RuntimeError("qdrant search unavailable")


class _RecordingEmbeddingsProvider(FakeEmbeddingsProvider):
    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []

    async def embed(self, request):  # type: ignore[no-untyped-def]
        self.texts.extend(str(text) for text in request.texts)
        return await super().embed(request)


@pytest.mark.asyncio
async def test_memory_vector_upsert_rechecks_scope_after_embedding() -> None:
    vector = InMemoryVectorStore()
    store = MemoryStore(
        _settings(),
        llm_service=FakeEmbeddingsProvider(),
        vector_store=vector,
    )
    checks = 0

    async def gate(tenant_id: str, session_id: str) -> bool:
        nonlocal checks
        assert (tenant_id, session_id) == ("demo", "group-1")
        checks += 1
        return checks == 1

    status = await store.vector_index.upsert_item(
        store._finalize_memory_item(
            _row(id=7, session_id="group-1", scope_type="session")
        ),
        scope_execution_allowed=gate,
    )

    assert status == "scope_disabled"
    assert checks == 2
    assert vector._collections == {}


@pytest.mark.asyncio
async def test_memory_vector_rebuild_gates_each_actual_row_scope() -> None:
    vector = InMemoryVectorStore()
    store = MemoryStore(
        _settings(),
        llm_service=FakeEmbeddingsProvider(),
        vector_store=vector,
    )

    async def gate(tenant_id: str, session_id: str) -> bool:
        return tenant_id == "demo" and session_id != "disabled-room"

    result = await store.vector_index.rebuild_items(
        [
            store._finalize_memory_item(
                _row(id=7, session_id="allowed-room", scope_type="session")
            ),
            store._finalize_memory_item(
                _row(id=8, session_id="disabled-room", scope_type="session")
            ),
        ],
        scope_execution_allowed=gate,
    )

    assert result["indexed"] == 1
    assert result["skipped"] == 1


@pytest.mark.asyncio
async def test_runtime_memory_vector_rebuild_requires_explicit_tenant() -> None:
    store = MemoryStore(
        _settings(),
        llm_service=FakeEmbeddingsProvider(),
        vector_store=InMemoryVectorStore(),
    )
    store.runtime_scope_gates_required = True

    async def gate(_tenant_id: str, _session_id: str) -> bool:
        return True

    store.scope_execution_allowed = gate

    with pytest.raises(RuntimeError, match="tenant_id required"):
        await store.rebuild_memory_item_vector_index(tenant_id=None)


@pytest.mark.asyncio
async def test_memory_vector_upsert_payload_scope() -> None:
    vector = InMemoryVectorStore()
    store = MemoryStore(_settings(), llm_service=FakeEmbeddingsProvider(), vector_store=vector)
    item = store._finalize_memory_item(_row(id=7, session_id="group-1", scope_type="session"))

    await store._sync_memory_vector_for_item_safe(item)

    hits = await vector.search(
        "test_memory_items",
        await store.vector_index._embed(tenant_id="demo", text="Adidas", trace_id="test"),
        top_k=1,
        filter_={
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "wxid_a",
            "session_id": "group-1",
            "status": "active",
            "sensitivity": "normal",
            "scope_type": "session",
        },
    )

    assert hits
    assert hits[0].id == "memory_item:7"
    assert hits[0].payload["tenant"] == "demo"
    assert hits[0].payload["tenant_id"] == "demo"
    assert hits[0].payload["item_id"] == "7"
    assert hits[0].payload["memory_type"] == "note"
    assert hits[0].payload["source_type"] == "manual"
    assert hits[0].payload["updated_at"] == "2026-05-11T00:00:00"


@pytest.mark.asyncio
async def test_memory_vector_enable_smoke_passes_with_fake_qdrant() -> None:
    vector = _FakeQdrantVectorStore()
    store = MemoryStore(
        _settings(memory_vector_index_enabled=False),
        llm_service=FakeEmbeddingsProvider(),
        vector_store=vector,
    )

    result = await store.vector_index.smoke_enable_preflight()
    smoke_vector = await store.vector_index._embed(
        tenant_id="demo",
        text="memory vector enable smoke",
        trace_id="test",
    )
    leftovers = await vector.search(
        "test_memory_items",
        smoke_vector,
        top_k=3,
        filter_={"__smoke": "memory_vector_enable"},
    )

    assert result["safe_to_enable"] is True
    assert result["reasons"] == []
    assert result["checks"]["dimension"]["actual"] == 64
    assert leftovers == []


@pytest.mark.asyncio
async def test_memory_vector_enable_smoke_dimension_mismatch_safe_false() -> None:
    store = MemoryStore(
        _settings(memory_vector_index_enabled=False, memory_vector_size=128),
        llm_service=FakeEmbeddingsProvider(dim=64),
        vector_store=_FakeQdrantVectorStore(),
    )

    result = await store.vector_index.smoke_enable_preflight()

    assert result["safe_to_enable"] is False
    assert result["checks"]["dimension"] == {"ok": False, "configured": 128, "actual": 64}
    assert any(str(reason).startswith("dimension_mismatch") for reason in result["reasons"])




@pytest.mark.asyncio
async def test_memory_vector_enable_smoke_collection_dimension_mismatch_safe_false() -> None:
    vector = _DimensionMismatchQdrantVectorStore()
    await vector.ensure_collection("test_memory_items", 32)
    store = MemoryStore(
        _settings(memory_vector_index_enabled=False, memory_vector_size=64),
        llm_service=FakeEmbeddingsProvider(dim=64),
        vector_store=vector,
    )

    result = await store.vector_index.smoke_enable_preflight()

    assert result["safe_to_enable"] is False
    assert result["checks"]["dimension"] == {"ok": True, "configured": 64, "actual": 64}
    assert any(str(reason).startswith("collection_error") for reason in result["reasons"])


@pytest.mark.asyncio
async def test_memory_vector_enable_smoke_qdrant_error_safe_false_and_fallback_unaffected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(
        _settings(memory_vector_index_enabled=False),
        llm_service=FakeEmbeddingsProvider(),
        vector_store=_FailingQdrantVectorStore(),
    )

    result = await store.vector_index.smoke_enable_preflight()

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        assert "FROM plugin_memory_item" in sql
        assert params is not None
        return [_row(id=12, content="SQL fallback after vector error")]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    items = await store.retrieve_memory_items(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="",
        query="fallback",
    )

    assert result["safe_to_enable"] is False
    assert any(str(reason).startswith("collection_error") for reason in result["reasons"])
    assert [item["id"] for item in items] == [12]


@pytest.mark.asyncio
async def test_memory_vector_smoke_rebuild_dry_run_and_search_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vector = _FakeQdrantVectorStore()
    store = MemoryStore(
        _settings(memory_vector_index_enabled=False),
        llm_service=FakeEmbeddingsProvider(),
        vector_store=vector,
    )

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        assert "status = 'active'" in sql
        assert "sensitivity = 'normal'" in sql
        assert "sensitivity_category = 'normal'" in sql
        assert "deleted_at IS NULL" in sql
        assert params is not None
        if "ORDER BY updated_at DESC, id DESC LIMIT :lim" in sql:
            assert "expires_at IS NULL" in sql
            assert "expires_at > NOW()" in sql
            assert params["lim"] == 1
        return [_row(id=7, content="用户喜欢 Adidas")]

    async def publish_current(
        item_id: int,
        *,
        fallback_item: dict[str, Any] | None = None,
        force: bool = False,
        scope_execution_allowed=None,
    ) -> str:
        assert item_id == 7
        assert fallback_item is not None
        return await store.vector_index.upsert_item(
            fallback_item,
            force=force,
            scope_execution_allowed=scope_execution_allowed,
        )

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_publish_current_memory_vectors", publish_current)

    dry_run = await store.rebuild_memory_item_vector_index(
        tenant_id="demo",
        limit=1,
        dry_run=True,
        force=True,
    )
    real_rebuild = await store.rebuild_memory_item_vector_index(
        tenant_id="demo",
        limit=1,
        dry_run=False,
        force=True,
    )
    search = await store.smoke_memory_item_vector_search(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="",
        query="Adidas",
        limit=1,
        force=True,
    )

    assert dry_run["dry_run"] is True
    assert dry_run["indexed"] == 1
    assert real_rebuild["indexed"] == 1
    assert search["ok"] is True
    assert search["behavior"] == "vector_hit"
    assert search["vector_ids"] == [7]


@pytest.mark.asyncio
async def test_memory_vector_rebuild_session_scope_never_embeds_other_groups_or_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _RecordingEmbeddingsProvider()
    store = MemoryStore(
        _settings(memory_vector_index_enabled=False),
        llm_service=provider,
        vector_store=_FakeQdrantVectorStore(),
    )
    scope_checks: list[tuple[str, str]] = []

    async def scope_gate(tenant_id: str, session_id: str) -> bool:
        scope_checks.append((tenant_id, session_id))
        return (tenant_id, session_id) == ("demo", "group-a@chatroom")

    store.runtime_scope_gates_required = True
    store.scope_execution_allowed = scope_gate
    rows = [
        _row(
            id=71,
            session_id="group-a@chatroom",
            scope_type="session",
            content="GROUP-A-ALLOWED",
        ),
        _row(
            id=72,
            session_id="group-b@chatroom",
            scope_type="session",
            content="GROUP-B-MUST-NOT-EMBED",
        ),
        _row(
            id=73,
            session_id="",
            scope_type="identity",
            content="IDENTITY-MUST-NOT-EMBED",
        ),
    ]

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        assert "FROM plugin_memory_item" in sql
        assert "scope_type = 'session'" in sql
        assert "session_id = :sid" in sql
        assert params is not None
        assert params["sid"] == "group-a@chatroom"
        return [
            row
            for row in rows
            if row["scope_type"] == "session" and row["session_id"] == params["sid"]
        ]

    async def publish_current(
        item_id: int,
        *,
        fallback_item: dict[str, Any] | None = None,
        force: bool = False,
        scope_execution_allowed=None,
    ) -> str:
        assert fallback_item is not None
        assert int(fallback_item["id"]) == item_id
        return await store.vector_index.upsert_item(
            fallback_item,
            force=force,
            scope_execution_allowed=scope_execution_allowed,
        )

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_publish_current_memory_vectors", publish_current)

    result = await store.rebuild_memory_item_vector_index(
        tenant_id="demo",
        session_id="group-a@chatroom",
        limit=20,
        dry_run=False,
        force=True,
    )

    assert result["scanned"] == 1
    assert result["indexed"] == 1
    assert provider.texts == ["GROUP-A-ALLOWED"]
    assert scope_checks
    assert set(scope_checks) == {("demo", "group-a@chatroom")}


@pytest.mark.asyncio
async def test_memory_vector_rebuild_erase_first_never_bypasses_safe_publisher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _RecordingEmbeddingsProvider()
    vector = InMemoryVectorStore()
    store = MemoryStore(
        _settings(),
        llm_service=provider,
        vector_store=vector,
    )
    publication_started = asyncio.Event()
    erase_committed = asyncio.Event()
    publish_calls: list[dict[str, Any]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        assert "FROM plugin_memory_item" in sql
        assert params is not None
        return [_row(id=77, content="MUST-NOT-BE-REPUBLISHED")]

    async def publish_current(
        item_id: int,
        *,
        fallback_item: dict[str, Any] | None = None,
        force: bool = False,
        scope_execution_allowed=None,
    ) -> str:
        publish_calls.append(
            {
                "item_id": item_id,
                "fallback_item": fallback_item,
                "force": force,
                "scope_execution_allowed": scope_execution_allowed,
            }
        )
        publication_started.set()
        await erase_committed.wait()
        # The reusable store publisher re-read the committed DB state under
        # the member fence and observed that forget had already removed it.
        return "deleted"

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_publish_current_memory_vectors", publish_current)

    rebuild = asyncio.create_task(
        store.rebuild_memory_item_vector_index(
            tenant_id="demo",
            limit=1,
            dry_run=False,
        )
    )
    await asyncio.wait_for(publication_started.wait(), timeout=1)
    erase_committed.set()
    result = await rebuild

    assert result["scanned"] == 1
    assert result["indexed"] == 0
    assert result["deleted"] == 1
    assert result["errors"] == 0
    assert [call["item_id"] for call in publish_calls] == [77]
    assert provider.texts == []
    assert vector._collections == {}


@pytest.mark.asyncio
async def test_memory_vector_rebuild_publish_first_is_removed_by_later_forget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vector = InMemoryVectorStore()
    store = MemoryStore(
        _settings(),
        llm_service=FakeEmbeddingsProvider(),
        vector_store=vector,
    )
    member_fence = asyncio.Lock()
    published = asyncio.Event()
    release_publication = asyncio.Event()
    erased = asyncio.Event()

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        assert "FROM plugin_memory_item" in sql
        assert params is not None
        return [_row(id=78, content="publish-before-forget")]

    async def publish_current(
        item_id: int,
        *,
        fallback_item: dict[str, Any] | None = None,
        force: bool = False,
        scope_execution_allowed=None,
    ) -> str:
        assert item_id == 78
        assert fallback_item is not None
        async with member_fence:
            status = await store.vector_index.upsert_item(
                fallback_item,
                force=force,
                scope_execution_allowed=scope_execution_allowed,
            )
            assert status == "indexed"
            published.set()
            await release_publication.wait()
        return "published"

    async def forget_after_publication() -> None:
        async with member_fence:
            await store.vector_index.delete_item(78, force=True)
            erased.set()

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_publish_current_memory_vectors", publish_current)

    rebuild = asyncio.create_task(
        store.rebuild_memory_item_vector_index(
            tenant_id="demo",
            limit=1,
            dry_run=False,
        )
    )
    await asyncio.wait_for(published.wait(), timeout=1)
    forget = asyncio.create_task(forget_after_publication())
    await asyncio.sleep(0)
    assert erased.is_set() is False

    release_publication.set()
    result, _ = await asyncio.gather(rebuild, forget)

    assert result["indexed"] == 1
    assert erased.is_set() is True
    query_vector = await store.vector_index._embed(
        tenant_id="demo",
        text="publish-before-forget",
        trace_id="verify-forget",
    )
    assert (
        await vector.search(
            "test_memory_items",
            query_vector,
            top_k=3,
            filter_={"tenant_id": "demo", "item_id": "78"},
        )
        == []
    )


@pytest.mark.asyncio
async def test_memory_vector_smoke_forwards_only_explicit_session_as_rebuild_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(
        _settings(memory_vector_index_enabled=False),
        llm_service=FakeEmbeddingsProvider(),
        vector_store=_FakeQdrantVectorStore(),
    )
    rebuild_calls: list[dict[str, Any]] = []

    async def fake_preflight(**_kwargs: Any) -> dict[str, Any]:
        return {"safe_to_enable": True, "reasons": []}

    async def fake_rebuild(**kwargs: Any) -> dict[str, Any]:
        rebuild_calls.append(kwargs)
        return {"errors": 0}

    async def fake_search(**_kwargs: Any) -> dict[str, Any]:
        return {"ok": False, "skipped": True, "vector_error": ""}

    monkeypatch.setattr(store.vector_index, "smoke_enable_preflight", fake_preflight)
    monkeypatch.setattr(store, "rebuild_memory_item_vector_index", fake_rebuild)
    monkeypatch.setattr(store, "smoke_memory_item_vector_search", fake_search)

    await store.smoke_memory_vector_enable(
        tenant_id="demo",
        channel="wechat",
        user_id="wxid_a",
        session_id="group-a@chatroom",
        dry_run=False,
    )
    await store.smoke_memory_vector_enable(
        tenant_id="demo",
        channel="wechat",
        user_id="wxid_a",
        session_id="",
        dry_run=False,
    )

    assert rebuild_calls[0]["session_id"] == "group-a@chatroom"
    assert rebuild_calls[1]["session_id"] is None


@pytest.mark.asyncio
async def test_memory_vector_enable_smoke_no_data_scope_remains_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vector = _FakeQdrantVectorStore()
    store = MemoryStore(
        _settings(memory_vector_index_enabled=False),
        llm_service=FakeEmbeddingsProvider(),
        vector_store=vector,
    )

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        assert "FROM plugin_memory_item" in sql
        assert params is not None
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    result = await store.smoke_memory_vector_enable(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_missing",
        query="no matching memory",
        limit=2,
        dry_run=True,
    )

    assert result["safe_to_enable"] is True
    assert result["rebuild"]["scanned"] == 0
    assert result["search"]["ok"] is False
    assert result["search"]["behavior"] == "miss"
    assert result["reasons"] == []


@pytest.mark.asyncio
async def test_memory_hybrid_sql_only_fallback_and_debug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(_settings(memory_hybrid_retrieval_enabled=True), llm_service=None, vector_store=None)

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        assert "status = 'active'" in sql
        assert "sensitivity = 'normal'" in sql
        assert params is not None
        return [_row(id=31, content="SQL only Adidas", source_type="auto", pinned=False, priority=0)]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    result = await store.retrieve_memory_hybrid(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="",
        query="Adidas",
        include_graph=False,
        debug=True,
    )

    assert [item["id"] for item in result["items"]] == [31]
    assert result["items"][0]["retrieval_sources"] == ["sql"]
    assert "hybrid_score_breakdown" in result["items"][0]
    assert result["debug"]["vector_error"] == ""


@pytest.mark.asyncio
async def test_memory_hybrid_vector_and_sql_merge_dedupe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vector = InMemoryVectorStore()
    store = MemoryStore(
        _settings(memory_hybrid_retrieval_enabled=True),
        llm_service=FakeEmbeddingsProvider(),
        vector_store=vector,
    )
    sql_item = store._finalize_memory_item(
        _row(id=41, content="用户喜欢 Adidas", source_type="auto", pinned=False, priority=0)
    )
    vector_item = store._finalize_memory_item(
        _row(id=42, content="用户偏好 Adidas 鞋", source_type="auto", pinned=False, priority=0)
    )
    await store._sync_memory_vector_for_item_safe(sql_item)
    await store._sync_memory_vector_for_item_safe(vector_item)

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "WHERE id = :id" in sql:
            return [_row(id=params["id"], content="用户偏好 Adidas 鞋", source_type="auto", pinned=False, priority=0)]
        return [_row(id=41, content="用户喜欢 Adidas", source_type="auto", pinned=False, priority=0)]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    items = await store.retrieve_memory_items(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="",
        query="Adidas",
        limit=5,
        debug=True,
    )

    ids = [item["id"] for item in items]
    assert len(ids) == len(set(ids))
    assert {41, 42}.issubset(set(ids))
    assert "sql" in next(item for item in items if item["id"] == 41)["retrieval_sources"]
    assert "vector" in next(item for item in items if item["id"] == 42)["retrieval_sources"]


@pytest.mark.asyncio
async def test_memory_hybrid_manual_pinned_outranks_ordinary_vector_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vector = InMemoryVectorStore()
    store = MemoryStore(
        _settings(memory_hybrid_retrieval_enabled=True),
        llm_service=FakeEmbeddingsProvider(),
        vector_store=vector,
    )
    vector_item = store._finalize_memory_item(
        _row(id=51, content="Adidas very close semantic hit", source_type="auto", pinned=False, priority=0)
    )
    await store._sync_memory_vector_for_item_safe(vector_item)

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "WHERE id = :id" in sql:
            return [_row(id=51, content="Adidas very close semantic hit", source_type="auto", pinned=False, priority=0)]
        return [
            _row(id=50, content="核心人工记忆", source_type="manual", pinned=True, priority=100),
        ]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    result = await store.retrieve_memory_hybrid(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="",
        query="Adidas",
        limit=2,
        include_graph=False,
    )

    assert [item["id"] for item in result["items"]][:2] == [50, 51]


@pytest.mark.asyncio
async def test_memory_hybrid_session_scope_boost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(_settings(memory_hybrid_retrieval_enabled=True), llm_service=None, vector_store=None)

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        return [
            _row(id=61, content="Adidas identity", scope_type="identity", session_id="", priority=0, pinned=False),
            _row(id=62, content="Adidas session", scope_type="session", session_id="s1", priority=0, pinned=False),
        ]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    result = await store.retrieve_memory_hybrid(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="s1",
        query="Adidas",
        limit=2,
        include_graph=False,
    )

    assert [item["id"] for item in result["items"]] == [62, 61]


@pytest.mark.asyncio
async def test_memory_hybrid_safety_filters_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(_settings(memory_hybrid_retrieval_enabled=True), llm_service=None, vector_store=None)

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        return [
            _row(id=71, content="active Adidas"),
            _row(id=72, content="pending Adidas", status="pending"),
            _row(id=73, content="sensitive Adidas", sensitivity="pii"),
            _row(id=74, content="cross user Adidas", user_id="wxid_b"),
            _row(id=75, content="other session Adidas", scope_type="session", session_id="s2"),
        ]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    result = await store.retrieve_memory_hybrid(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="s1",
        query="Adidas",
        limit=10,
        include_graph=False,
    )

    assert [item["id"] for item in result["items"]] == [71]


@pytest.mark.asyncio
async def test_memory_hybrid_vector_error_falls_back_to_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(
        _settings(memory_hybrid_retrieval_enabled=True),
        llm_service=FakeEmbeddingsProvider(),
        vector_store=_SearchFailingVectorStore(),
    )

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        return [_row(id=81, content="SQL survives vector error")]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    result = await store.retrieve_memory_hybrid(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="",
        query="Adidas",
        include_graph=False,
        debug=True,
    )

    assert [item["id"] for item in result["items"]] == [81]
    assert result["debug"]["vector_error"].startswith("RuntimeError:")


@pytest.mark.asyncio
async def test_memory_graph_vector_fact_and_episode_upsert_payload_scope() -> None:
    vector = InMemoryVectorStore()
    store = MemoryStore(_settings(), llm_service=FakeEmbeddingsProvider(), vector_store=vector)
    item = store._finalize_memory_item(_row(id=7, session_id="group-1", scope_type="session"))

    assert await store.vector_index.upsert_fact(_fact(), backing_item=item) == "indexed"
    assert await store.vector_index.upsert_episode(_episode(), backing_items=[item]) == "indexed"

    fact_hits = await vector.search(
        "test_memory_items",
        await store.vector_index._embed(tenant_id="demo", text="Adidas", trace_id="test"),
        top_k=5,
        filter_={
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "wxid_a",
            "session_id": "group-1",
            "status": "active",
            "object_type": "fact",
        },
    )
    episode_hits = await vector.search(
        "test_memory_items",
        await store.vector_index._embed(tenant_id="demo", text="Adidas", trace_id="test"),
        top_k=5,
        filter_={
            "tenant_id": "demo",
            "channel": "wechat",
            "source_key": "wxbot",
            "user_id": "wxid_a",
            "session_id": "group-1",
            "status": "active",
            "object_type": "episode",
        },
    )

    assert [hit.id for hit in fact_hits] == ["memory_fact:101"]
    assert [hit.id for hit in episode_hits] == ["memory_episode:201"]
    assert fact_hits[0].payload["fact_id"] == "101"
    assert fact_hits[0].payload["predicate"] == "likes"
    assert fact_hits[0].payload["memory_item_id"] == "7"
    assert fact_hits[0].payload["source_event_id"] == "88"
    assert episode_hits[0].payload["episode_id"] == "201"
    assert episode_hits[0].payload["importance"] == 6
    assert episode_hits[0].payload["event_ids"] == "88"
    assert episode_hits[0].payload["memory_item_ids"] == "7"


@pytest.mark.asyncio
async def test_memory_graph_vector_invalidated_and_hidden_backing_item_delete() -> None:
    vector = InMemoryVectorStore()
    store = MemoryStore(_settings(), llm_service=FakeEmbeddingsProvider(), vector_store=vector)
    active_item = store._finalize_memory_item(
        _row(id=7, session_id="group-1", scope_type="session")
    )

    await store.vector_index.upsert_fact(_fact(), backing_item=active_item)
    await store.vector_index.upsert_episode(_episode(), backing_items=[active_item])

    assert (
        await store.vector_index.upsert_fact(
            _fact(status="invalidated", invalid_at="now"),
            backing_item=active_item,
        )
        == "deleted"
    )
    assert (
        await store.vector_index.upsert_episode(
            _episode(),
            backing_items=[store._finalize_memory_item(_row(id=7, sensitivity="high"))],
        )
        == "deleted"
    )

    query_vector = await store.vector_index._embed(tenant_id="demo", text="Adidas", trace_id="test")
    assert await vector.search("test_memory_items", query_vector, top_k=5) == []


@pytest.mark.asyncio
async def test_memory_graph_vector_sync_deletes_stale_graph_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vector = InMemoryVectorStore()
    store = MemoryStore(_settings(), llm_service=FakeEmbeddingsProvider(), vector_store=vector)
    active_item = store._finalize_memory_item(
        _row(id=7, session_id="group-1", scope_type="session")
    )
    stale_fact = _fact(status="invalidated", invalid_at="now")
    stale_episode = {
        **_episode(status="archived", memory_item_ids=[7, 8]),
        "event_ids_json": "[88]",
        "memory_item_ids_json": "[7, 8]",
    }

    await store.vector_index.upsert_fact(_fact(), backing_item=active_item)
    await store.vector_index.upsert_episode(
        _episode(memory_item_ids=[7, 8]),
        backing_items=[active_item],
    )

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "FROM plugin_memory_fact fact" in sql:
            return [stale_fact]
        if "FROM plugin_memory_episode" in sql:
            return [stale_episode]
        if "FROM plugin_memory_item WHERE id = ANY" in sql:
            return [active_item]
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    await store._sync_graph_vectors_for_memory_item_safe(active_item)

    query_vector = await store.vector_index._embed(tenant_id="demo", text="Adidas", trace_id="test")
    assert await vector.search("test_memory_items", query_vector, top_k=5) == []


@pytest.mark.asyncio
async def test_get_graph_episode_for_memory_item_matches_multi_item_episode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(_settings())
    item = store._finalize_memory_item(
        _row(id=7, session_id="group-1", scope_type="session")
    )

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        assert params is not None
        assert params["sid"] == "group-1"
        assert "memory_item_ids_json = :memory_item_ids_json" not in sql
        return [
            {
                **_episode(id=202, memory_item_ids=[8]),
                "event_ids_json": "[99]",
                "memory_item_ids_json": "[8]",
            },
            {
                **_episode(id=201, memory_item_ids=[7, 8]),
                "event_ids_json": "[88]",
                "memory_item_ids_json": "[7, 8]",
            },
        ]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    episode = await store._get_graph_episode_for_memory_item(7, item=item)

    assert episode is not None
    assert episode["id"] == 201
    assert episode["memory_item_ids"] == [7, 8]


@pytest.mark.asyncio
async def test_memory_graph_vector_payload_filter_prevents_cross_scope_leak() -> None:
    vector = InMemoryVectorStore()
    store = MemoryStore(_settings(), llm_service=FakeEmbeddingsProvider(), vector_store=vector)
    allowed_item = store._finalize_memory_item(
        _row(id=7, session_id="group-1", scope_type="session")
    )
    other_user_item = store._finalize_memory_item(
        _row(id=8, user_id="wxid_b", session_id="group-1", scope_type="session")
    )
    other_source_item = store._finalize_memory_item(
        _row(id=9, source_key="other", session_id="group-1", scope_type="session")
    )
    other_session_item = store._finalize_memory_item(
        _row(id=10, session_id="group-2", scope_type="session")
    )

    await store.vector_index.upsert_fact(_fact(id=101, memory_item_id=7), backing_item=allowed_item)
    await store.vector_index.upsert_fact(
        _fact(id=102, user_id="wxid_b", memory_item_id=8),
        backing_item=other_user_item,
    )
    await store.vector_index.upsert_fact(
        _fact(id=103, source_key="other", memory_item_id=9),
        backing_item=other_source_item,
    )
    await store.vector_index.upsert_fact(
        _fact(id=104, memory_item_id=10),
        backing_item=other_session_item,
    )

    hits = await store.vector_index.search_graph_ids(
        tenant_id="demo",
        channel="wechat",
        source_keys=["wxbot"],
        user_id="wxid_a",
        session_id="group-1",
        query="Adidas",
        object_type="fact",
        top_k=10,
    )

    assert [fact_id for fact_id, _score in hits] == [101]


@pytest.mark.asyncio
async def test_memory_graph_vector_search_includes_identity_and_session_scope() -> None:
    vector = InMemoryVectorStore()
    store = MemoryStore(_settings(), llm_service=FakeEmbeddingsProvider(), vector_store=vector)
    identity_item = store._finalize_memory_item(_row(id=7))
    session_item = store._finalize_memory_item(
        _row(id=8, session_id="group-1", scope_type="session")
    )

    await store.vector_index.upsert_fact(
        _fact(id=101, memory_item_id=7),
        backing_item=identity_item,
    )
    await store.vector_index.upsert_fact(
        _fact(id=102, memory_item_id=8),
        backing_item=session_item,
    )

    hits = await store.vector_index.search_graph_ids(
        tenant_id="demo",
        channel="wechat",
        source_keys=["wxbot"],
        user_id="wxid_a",
        session_id="group-1",
        query="Adidas",
        object_type="fact",
        top_k=10,
    )

    assert {fact_id for fact_id, _score in hits} == {101, 102}


@pytest.mark.asyncio
async def test_memory_graph_vector_search_uses_graph_top_k_default() -> None:
    vector = InMemoryVectorStore()
    store = MemoryStore(
        _settings(memory_vector_top_k=1, memory_graph_vector_top_k=2),
        llm_service=FakeEmbeddingsProvider(),
        vector_store=vector,
    )
    item = store._finalize_memory_item(_row(id=7))
    await store.vector_index.upsert_fact(_fact(id=101, object_name="Adidas Samba"), backing_item=item)
    await store.vector_index.upsert_fact(_fact(id=102, object_name="Adidas Gazelle"), backing_item=item)
    await store.vector_index.upsert_fact(_fact(id=103, object_name="Adidas Spezial"), backing_item=item)

    default_hits = await store.vector_index.search_graph_ids(
        tenant_id="demo",
        channel="wechat",
        source_keys=["wxbot"],
        user_id="wxid_a",
        session_id="",
        query="Adidas",
        object_type="fact",
    )
    explicit_hits = await store.vector_index.search_graph_ids(
        tenant_id="demo",
        channel="wechat",
        source_keys=["wxbot"],
        user_id="wxid_a",
        session_id="",
        query="Adidas",
        object_type="fact",
        top_k=1,
    )

    assert len(default_hits) == 2
    assert len(explicit_hits) == 1
    assert store.vector_index.default_top_k == 1
    assert store.vector_index.graph_top_k == 2


@pytest.mark.asyncio
async def test_memory_graph_vector_disabled_no_hit_and_error_fall_back_to_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "FROM plugin_memory_fact fact" in sql:
            return [
                {
                    **_fact(id=301, memory_item_id=31),
                    "item_source_type": "auto",
                    "item_pinned": False,
                    "item_priority": 0,
                    "item_confidence": 0.86,
                    "item_deleted_at": None,
                    "item_status": "active",
                    "item_sensitivity": "normal",
                }
            ]
        if "FROM plugin_memory_episode" in sql:
            return []
        return []

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    disabled = MemoryStore(_settings(memory_vector_index_enabled=False))
    no_hit = MemoryStore(
        _settings(),
        llm_service=FakeEmbeddingsProvider(),
        vector_store=InMemoryVectorStore(),
    )
    error = MemoryStore(
        _settings(),
        llm_service=FakeEmbeddingsProvider(),
        vector_store=_FailingVectorStore(),
    )

    for store in (disabled, no_hit, error):
        result = await store.retrieve_memory_graph(
            tenant_id="demo",
            channel="wechat",
            source_key="wxbot",
            user_id="wxid_a",
            session_id="",
            query="Adidas",
            fact_top_k=1,
            episode_top_k=0,
        )
        assert [fact["id"] for fact in result["facts"]] == [301]


@pytest.mark.asyncio
async def test_memory_graph_vector_rebuild_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    vector = InMemoryVectorStore()
    store = MemoryStore(_settings(), llm_service=FakeEmbeddingsProvider(), vector_store=vector)
    item = _row(id=7, session_id="group-1", scope_type="session")
    episode_row = {
        "id": 201,
        "tenant_id": "demo",
        "channel": "wechat",
        "source_key": "wxbot",
        "user_id": "wxid_a",
        "session_id": "group-1",
        "title": "用户询问 Adidas 鞋码",
        "summary": "偏好 Adidas 的尺码建议",
        "event_ids_json": "[88]",
        "memory_item_ids_json": "[7]",
        "importance": 6,
        "status": "active",
        "created_at": None,
        "updated_at": "2026-05-11T00:00:00",
    }

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "FROM plugin_memory_fact fact" in sql:
            return [_fact()]
        if "FROM plugin_memory_episode" in sql:
            return [episode_row]
        if "FROM plugin_memory_item" in sql:
            return [item]
        return []

    async def publish_graph(
        object_type: str,
        object_id: int,
        *,
        fallback_row: dict[str, Any] | None = None,
        scope_execution_allowed=None,
    ) -> str:
        assert fallback_row is not None
        assert int(fallback_row["id"]) == object_id
        backing_item = store._finalize_memory_item(item)
        if object_type == "fact":
            return await store.vector_index.upsert_fact(
                fallback_row,
                backing_item=backing_item,
                scope_execution_allowed=scope_execution_allowed,
            )
        assert object_type == "episode"
        return await store.vector_index.upsert_episode(
            fallback_row,
            backing_items=[backing_item],
            scope_execution_allowed=scope_execution_allowed,
        )

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(
        store,
        "_publish_current_memory_graph_vector",
        publish_graph,
    )

    first = await store.rebuild_memory_graph_vector_index(tenant_id="demo")
    second = await store.rebuild_memory_graph_vector_index(tenant_id="demo")

    assert first["indexed"] == 2
    assert second["indexed"] == 2
    query_vector = await store.vector_index._embed(tenant_id="demo", text="Adidas", trace_id="test")
    fact_hits = await vector.search(
        "test_memory_items",
        query_vector,
        top_k=10,
        filter_={"object_type": "fact", "tenant_id": "demo"},
    )
    episode_hits = await vector.search(
        "test_memory_items",
        query_vector,
        top_k=10,
        filter_={"object_type": "episode", "tenant_id": "demo"},
    )

    assert [hit.id for hit in fact_hits] == ["memory_fact:101"]
    assert [hit.id for hit in episode_hits] == ["memory_episode:201"]


@pytest.mark.asyncio
async def test_memory_graph_vector_rebuild_erase_first_uses_exact_safe_publishers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _RecordingEmbeddingsProvider()
    vector = InMemoryVectorStore()
    store = MemoryStore(
        _settings(),
        llm_service=provider,
        vector_store=vector,
    )
    item = _row(id=7, session_id="group-1", scope_type="session")
    episode_row = {
        **_episode(id=201),
        "event_ids_json": "[88]",
        "memory_item_ids_json": "[7]",
    }
    publication_started = asyncio.Event()
    erase_committed = asyncio.Event()
    calls: list[tuple[str, int]] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "FROM plugin_memory_fact fact" in sql:
            return [_fact(id=101)]
        if "FROM plugin_memory_episode" in sql:
            return [episode_row]
        if "FROM plugin_memory_item" in sql:
            return [item]
        return []

    async def publish_graph(
        object_type: str,
        object_id: int,
        *,
        fallback_row: dict[str, Any] | None = None,
        scope_execution_allowed=None,
    ) -> str:
        assert fallback_row is not None
        assert scope_execution_allowed is None
        calls.append((object_type, object_id))
        publication_started.set()
        await erase_committed.wait()
        # The exact graph publisher re-read the fact/episode and backing
        # evidence under the member fence after forget committed.
        return "deleted"

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(
        store,
        "_publish_current_memory_graph_vector",
        publish_graph,
    )

    rebuild = asyncio.create_task(
        store.rebuild_memory_graph_vector_index(
            tenant_id="demo",
            limit=1,
        )
    )
    await asyncio.wait_for(publication_started.wait(), timeout=1)
    erase_committed.set()
    result = await rebuild

    assert result["scanned"] == 2
    assert result["indexed"] == 0
    assert result["deleted"] == 2
    assert calls == [("fact", 101), ("episode", 201)]
    assert provider.texts == []
    assert vector._collections == {}


@pytest.mark.asyncio
async def test_memory_graph_vector_rebuild_filters_stale_rows_before_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vector = InMemoryVectorStore()
    store = MemoryStore(_settings(), llm_service=FakeEmbeddingsProvider(), vector_store=vector)
    item = _row(id=7, session_id="group-1", scope_type="session")
    episode_row = {
        **_episode(id=201),
        "event_ids_json": "[88]",
        "memory_item_ids_json": "[7]",
    }

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        if "FROM plugin_memory_fact fact" in sql:
            assert "fact.status = 'active'" in sql
            assert "fact.invalid_at IS NULL" in sql
            assert params is not None
            assert params["lim"] == 1
            return [_fact(id=101)]
        if "FROM plugin_memory_episode" in sql:
            assert "status = 'active'" in sql
            assert params is not None
            assert params["lim"] == 1
            return [episode_row]
        if "FROM plugin_memory_item" in sql:
            return [item]
        return []

    async def publish_graph(
        object_type: str,
        object_id: int,
        *,
        fallback_row: dict[str, Any] | None = None,
        scope_execution_allowed=None,
    ) -> str:
        assert fallback_row is not None
        assert int(fallback_row["id"]) == object_id
        backing_item = store._finalize_memory_item(item)
        if object_type == "fact":
            return await store.vector_index.upsert_fact(
                fallback_row,
                backing_item=backing_item,
                scope_execution_allowed=scope_execution_allowed,
            )
        assert object_type == "episode"
        return await store.vector_index.upsert_episode(
            fallback_row,
            backing_items=[backing_item],
            scope_execution_allowed=scope_execution_allowed,
        )

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(
        store,
        "_publish_current_memory_graph_vector",
        publish_graph,
    )

    result = await store.rebuild_memory_graph_vector_index(tenant_id="demo", limit=1)

    assert result["scanned"] == 2
    assert result["indexed"] == 2


@pytest.mark.asyncio
async def test_memory_vector_delete_on_soft_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    vector = InMemoryVectorStore()
    store = MemoryStore(_settings(), llm_service=FakeEmbeddingsProvider(), vector_store=vector)
    active = store._finalize_memory_item(_row(id=8))
    deleted = store._finalize_memory_item(_row(id=8, status="deleted", deleted_at="now"))
    row_deleted = False

    await store._sync_memory_vector_for_item_safe(active)

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        nonlocal row_deleted
        if sql.startswith("SELECT id, tenant_id"):
            return [deleted if row_deleted else active]
        if sql.startswith("UPDATE plugin_memory_item SET status = 'deleted'"):
            row_deleted = True
            return [{"id": 8}]
        return []

    async def noop(_item: dict) -> None:
        return None

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)
    monkeypatch.setattr(store, "_refresh_legacy_cache_for_item_scope", noop)
    monkeypatch.setattr(store, "_sync_memory_graph_for_item_safe", noop)

    token = memory_store_module._ACTIVE_MUTATION_CONNECTION.set(
        SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    )
    try:
        result = await store.soft_delete_memory_item(8, allow_pinned=True)
    finally:
        memory_store_module._ACTIVE_MUTATION_CONNECTION.reset(token)

    assert result is not None
    hits = await vector.search(
        "test_memory_items",
        await store.vector_index._embed(tenant_id="demo", text="Adidas", trace_id="test"),
        top_k=5,
    )
    assert hits == []


@pytest.mark.asyncio
async def test_memory_vector_filter_prevents_cross_user_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vector = InMemoryVectorStore()
    store = MemoryStore(_settings(), llm_service=FakeEmbeddingsProvider(), vector_store=vector)
    allowed = store._finalize_memory_item(_row(id=1, user_id="wxid_a", content="用户偏好 Adidas 品牌"))
    other_user = store._finalize_memory_item(_row(id=2, user_id="wxid_b", content="用户偏好 Adidas 品牌"))
    other_session = store._finalize_memory_item(
        _row(id=3, scope_type="session", session_id="other-group", content="当前群聊提到 Adidas 鞋")
    )
    current_session = store._finalize_memory_item(
        _row(id=4, scope_type="session", session_id="group-1", content="当前群聊提到 Adidas 鞋")
    )
    all_items = {item["id"]: item for item in [allowed, other_user, other_session, current_session]}
    for item in all_items.values():
        await store._sync_memory_vector_for_item_safe(item)

    async def fake_get(item_id: int) -> dict[str, Any] | None:
        return all_items.get(item_id)

    monkeypatch.setattr(store, "get_memory_item", fake_get)

    rows = await store.retrieve_memory_items(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="group-1",
        query="Adidas",
        limit=10,
    )

    assert {row["id"] for row in rows} == {1, 4}


@pytest.mark.asyncio
async def test_memory_vector_index_deletes_non_accepted_acceptance_items() -> None:
    vector = InMemoryVectorStore()
    store = MemoryStore(_settings(), llm_service=FakeEmbeddingsProvider(), vector_store=vector)
    accepted = store._finalize_memory_item(
        _row(id=11, content="accepted Adidas", value_json='{"acceptance":{"status":"accepted","score":0.9}}')
    )
    rejected = store._finalize_memory_item(
        _row(id=12, content="rejected Adidas", value_json='{"acceptance":{"status":"rejected","score":0.1}}')
    )
    needs_review = store._finalize_memory_item(
        _row(id=13, content="review Adidas", value_json='{"acceptance":{"status":"needs_review","score":0.5}}')
    )
    expired = store._finalize_memory_item(
        _row(id=14, content="expired Adidas", value_json='{"acceptance":{"status":"expired","score":0.8}}')
    )
    superseded = store._finalize_memory_item(
        _row(id=15, content="superseded Adidas", value_json='{"acceptance":{"status":"superseded","score":0.8}}')
    )

    for item in [accepted, rejected, needs_review, expired, superseded]:
        await store._sync_memory_vector_for_item_safe(item)

    query_vector = await store.vector_index._embed(tenant_id="demo", text="Adidas", trace_id="test")
    hits = await vector.search("test_memory_items", query_vector, top_k=10)

    assert [hit.id for hit in hits] == ["memory_item:11"]


@pytest.mark.asyncio
async def test_memory_vector_dereferenced_items_must_match_tenant_and_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore(
        _settings(),
        llm_service=FakeEmbeddingsProvider(),
        vector_store=InMemoryVectorStore(),
    )
    allowed = store._finalize_memory_item(_row(id=1, content="用户偏好 Adidas 品牌"))
    other_tenant = store._finalize_memory_item(
        _row(id=2, tenant_id="other", content="用户偏好 Adidas 品牌")
    )
    other_channel = store._finalize_memory_item(
        _row(id=3, channel="web", content="用户偏好 Adidas 品牌")
    )

    async def fake_search_item_ids(**_kwargs: Any) -> list[tuple[int, float]]:
        return [(1, 0.9), (2, 0.95), (3, 0.96)]

    async def fake_get(item_id: int) -> dict[str, Any] | None:
        return {
            1: allowed,
            2: other_tenant,
            3: other_channel,
        }.get(item_id)

    monkeypatch.setattr(store.vector_index, "search_item_ids", fake_search_item_ids)
    monkeypatch.setattr(store, "get_memory_item", fake_get)

    rows = await store._retrieve_memory_items_vector(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="",
        query="Adidas",
        limit=10,
    )

    assert [row["id"] for row in rows] == [1]


@pytest.mark.asyncio
async def test_memory_vector_disabled_falls_back_to_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryStore(_settings(memory_vector_index_enabled=False))
    calls: list[str] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append(sql)
        return [_row(id=11, content="SQL fallback")]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    rows = await store.retrieve_memory_items(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="",
        query="fallback",
        limit=5,
    )

    assert rows[0]["id"] == 11
    assert calls and "FROM plugin_memory_item" in calls[0]


class _FailingVectorStore(InMemoryVectorStore):
    async def search(  # type: ignore[no-untyped-def]
        self,
        collection,
        vector,
        top_k=10,
        filter_=None,
    ):
        raise RuntimeError("qdrant unavailable")


@pytest.mark.asyncio
async def test_memory_vector_error_falls_back_to_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryStore(
        _settings(),
        llm_service=FakeEmbeddingsProvider(),
        vector_store=_FailingVectorStore(),
    )
    calls: list[str] = []

    async def fake_exec(sql: str, params: dict | None = None) -> list[dict]:
        calls.append(sql)
        return [_row(id=12, content="SQL fallback after vector error")]

    monkeypatch.setattr(memory_store_module, "_exec", fake_exec)

    rows = await store.retrieve_memory_items(
        tenant_id="demo",
        channel="wechat",
        source_key="wxbot",
        user_id="wxid_a",
        session_id="",
        query="Adidas",
        limit=5,
    )

    assert rows[0]["id"] == 12
    assert calls and "FROM plugin_memory_item" in calls[0]
