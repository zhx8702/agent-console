"""
In-process VectorStore implementation.

Cosine similarity via numpy; supports simple equality filter on payload keys.
Useful for unit tests and local development without Qdrant.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.kb.vector.base import VectorRecord, VectorSearchHit


@dataclass
class _Collection:
    dim: int
    records: dict[str, VectorRecord] = field(default_factory=dict)


class InMemoryVectorStore:
    """Thread-unsafe, in-memory VectorStore implementation."""

    name = "memory"

    def __init__(self) -> None:
        self._collections: dict[str, _Collection] = {}

    async def ensure_collection(self, name: str, dim: int) -> None:
        existing = self._collections.get(name)
        if existing is None:
            self._collections[name] = _Collection(dim=dim)
            return
        if existing.dim != dim:
            raise ValueError(
                f"collection {name} already exists with dim={existing.dim}, requested dim={dim}"
            )

    async def upsert(self, collection: str, records: list[VectorRecord]) -> None:
        col = self._collections.get(collection)
        if col is None:
            # Auto-create using dim of first record.
            if not records:
                return
            await self.ensure_collection(collection, len(records[0].vector))
            col = self._collections[collection]
        for r in records:
            if len(r.vector) != col.dim:
                raise ValueError(
                    f"vector dim mismatch for id={r.id}: expected {col.dim}, got {len(r.vector)}"
                )
            col.records[str(r.id)] = VectorRecord(
                id=str(r.id),
                vector=list(r.vector),
                payload=dict(r.payload),
            )

    async def delete(self, collection: str, ids: list[str]) -> None:
        col = self._collections.get(collection)
        if col is None:
            return
        for _id in ids:
            col.records.pop(str(_id), None)

    async def search(
        self,
        collection: str,
        vector: list[float],
        top_k: int = 10,
        filter_: dict[str, Any] | None = None,
    ) -> list[VectorSearchHit]:
        col = self._collections.get(collection)
        if col is None or not col.records:
            return []
        query = np.asarray(vector, dtype=np.float32)
        qn = np.linalg.norm(query)
        if qn == 0:
            return []
        results: list[VectorSearchHit] = []
        for rec in col.records.values():
            if filter_:
                ok = True
                for k, v in filter_.items():
                    if rec.payload.get(k) != v:
                        ok = False
                        break
                if not ok:
                    continue
            rv = np.asarray(rec.vector, dtype=np.float32)
            rn = np.linalg.norm(rv)
            if rn == 0:
                continue
            score = float(np.dot(query, rv) / (qn * rn))
            results.append(VectorSearchHit(id=rec.id, score=score, payload=dict(rec.payload)))
        results.sort(key=lambda h: h.score, reverse=True)
        return results[:top_k]

    async def close(self) -> None:
        self._collections.clear()
