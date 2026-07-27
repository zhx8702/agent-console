from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.kb.vector.qdrant_store import QdrantVectorStore


class _MissingCollectionError(Exception):
    status_code = 404
    content = b"Not found: Collection faq_default does not exist"


class _MissingCollectionClient:
    def __init__(self) -> None:
        self.search_calls: list[dict] = []
        self.delete_calls: list[dict] = []

    async def search(self, **kwargs):
        self.search_calls.append(kwargs)
        raise _MissingCollectionError("Not found: Collection faq_default does not exist")

    async def delete(self, **kwargs):
        self.delete_calls.append(kwargs)
        raise _MissingCollectionError("Not found: Collection faq_default does not exist")

    async def close(self) -> None:
        return None


class _MissingCollectionListClient:
    def __init__(self) -> None:
        self.get_collections_calls = 0
        self.search_calls: list[dict] = []

    async def get_collections(self):
        self.get_collections_calls += 1
        return SimpleNamespace(collections=[SimpleNamespace(name="kb_default")])

    async def search(self, **kwargs):
        self.search_calls.append(kwargs)
        raise AssertionError("search should not be called for known-missing collection")

    async def close(self) -> None:
        return None


class _UnavailableClient:
    async def search(self, **kwargs):
        _ = kwargs
        raise RuntimeError("qdrant unavailable")

    async def close(self) -> None:
        return None


class _ExistingCollectionClient:
    def __init__(self, dim: int) -> None:
        self.dim = dim
        self.create_calls: list[dict] = []

    async def get_collections(self):
        return SimpleNamespace(collections=[SimpleNamespace(name="memory")])

    async def get_collection(self, collection_name: str):
        assert collection_name == "memory"
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(vectors=SimpleNamespace(size=self.dim))
            )
        )

    async def create_collection(self, **kwargs):
        self.create_calls.append(kwargs)

    async def close(self) -> None:
        return None


class _QueryOnlyClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def query_points(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            points=[
                SimpleNamespace(
                    id="point-1",
                    score=0.91,
                    payload={"_raw_id": "doc-1", "title": "FAQ"},
                )
            ]
        )

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_qdrant_store_search_supports_query_points_only_client() -> None:
    client = _QueryOnlyClient()
    store = QdrantVectorStore(url="http://localhost:6333", client=client)  # type: ignore[arg-type]

    hits = await store.search(
        "kb_demo",
        [0.1, 0.2, 0.3],
        top_k=5,
        filter_={"tenant_id": "demo"},
    )

    assert len(client.calls) == 1
    assert client.calls[0]["collection_name"] == "kb_demo"
    assert client.calls[0]["query"] == [0.1, 0.2, 0.3]
    assert client.calls[0]["limit"] == 5
    assert hits[0].id == "doc-1"
    assert hits[0].payload == {"title": "FAQ"}
    assert hits[0].score == pytest.approx(0.91)


@pytest.mark.asyncio
async def test_qdrant_store_search_treats_missing_collection_as_empty() -> None:
    client = _MissingCollectionClient()
    store = QdrantVectorStore(url="http://localhost:6333", client=client)  # type: ignore[arg-type]

    hits = await store.search("faq_default", [0.1, 0.2, 0.3], top_k=5)

    assert hits == []
    assert len(client.search_calls) == 1


@pytest.mark.asyncio
async def test_qdrant_store_search_skips_known_missing_collection_before_query() -> None:
    client = _MissingCollectionListClient()
    store = QdrantVectorStore(url="http://localhost:6333", client=client)  # type: ignore[arg-type]

    hits = await store.search("faq_default", [0.1, 0.2, 0.3], top_k=5)
    hits_again = await store.search("faq_default", [0.1, 0.2, 0.3], top_k=5)

    assert hits == []
    assert hits_again == []
    assert client.get_collections_calls == 1
    assert client.search_calls == []


@pytest.mark.asyncio
async def test_qdrant_store_delete_ignores_missing_collection() -> None:
    client = _MissingCollectionClient()
    store = QdrantVectorStore(url="http://localhost:6333", client=client)  # type: ignore[arg-type]

    await store.delete("faq_default", ["1:q"])

    assert len(client.delete_calls) == 1


@pytest.mark.asyncio
async def test_qdrant_store_ensure_collection_rejects_existing_dimension_mismatch() -> None:
    client = _ExistingCollectionClient(dim=32)
    store = QdrantVectorStore(url="http://localhost:6333", client=client)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="dim=32, requested dim=64"):
        await store.ensure_collection("memory", 64)

    assert client.create_calls == []


@pytest.mark.asyncio
async def test_qdrant_store_ensure_collection_accepts_existing_matching_dimension() -> None:
    client = _ExistingCollectionClient(dim=64)
    store = QdrantVectorStore(url="http://localhost:6333", client=client)  # type: ignore[arg-type]

    await store.ensure_collection("memory", 64)

    assert client.create_calls == []


@pytest.mark.asyncio
async def test_qdrant_store_search_reraises_runtime_unavailable() -> None:
    store = QdrantVectorStore(url="http://localhost:6333", client=_UnavailableClient())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="qdrant unavailable"):
        await store.search("faq_default", [0.1, 0.2, 0.3], top_k=5)
