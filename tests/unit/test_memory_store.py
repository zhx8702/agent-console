from __future__ import annotations

import pytest

from app.kb.vector.base import VectorRecord
from app.kb.vector.memory_store import InMemoryVectorStore


@pytest.mark.asyncio
async def test_upsert_and_search_orders_by_cosine() -> None:
    store = InMemoryVectorStore()
    await store.ensure_collection("c", dim=3)
    await store.upsert(
        "c",
        [
            VectorRecord(id="a", vector=[1.0, 0.0, 0.0], payload={"kind": "x"}),
            VectorRecord(id="b", vector=[0.9, 0.1, 0.0], payload={"kind": "x"}),
            VectorRecord(id="c", vector=[0.0, 1.0, 0.0], payload={"kind": "y"}),
        ],
    )

    hits = await store.search("c", [1.0, 0.0, 0.0], top_k=3)
    ids = [h.id for h in hits]
    assert ids[0] == "a"
    assert ids[1] == "b"  # close to [1,0,0]
    assert ids[2] == "c"  # orthogonal
    assert hits[0].score > hits[1].score > hits[2].score


@pytest.mark.asyncio
async def test_search_with_equality_filter() -> None:
    store = InMemoryVectorStore()
    await store.ensure_collection("c", dim=3)
    await store.upsert(
        "c",
        [
            VectorRecord(id="a", vector=[1.0, 0.0, 0.0], payload={"kind": "x"}),
            VectorRecord(id="b", vector=[0.9, 0.1, 0.0], payload={"kind": "y"}),
        ],
    )
    hits = await store.search("c", [1.0, 0.0, 0.0], top_k=5, filter_={"kind": "y"})
    assert [h.id for h in hits] == ["b"]


@pytest.mark.asyncio
async def test_delete_removes_records() -> None:
    store = InMemoryVectorStore()
    await store.ensure_collection("c", dim=2)
    await store.upsert(
        "c",
        [
            VectorRecord(id="a", vector=[1.0, 0.0]),
            VectorRecord(id="b", vector=[0.0, 1.0]),
        ],
    )
    await store.delete("c", ["a"])
    hits = await store.search("c", [1.0, 0.0], top_k=5)
    assert [h.id for h in hits] == ["b"]


@pytest.mark.asyncio
async def test_dim_mismatch_raises() -> None:
    store = InMemoryVectorStore()
    await store.ensure_collection("c", dim=3)
    with pytest.raises(ValueError):
        await store.upsert("c", [VectorRecord(id="a", vector=[1.0, 0.0])])
