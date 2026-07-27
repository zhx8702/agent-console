from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class VectorRecord:
    id: str
    vector: list[float]
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class VectorSearchHit:
    id: str
    score: float
    payload: dict[str, Any]


class VectorStore(Protocol):
    async def ensure_collection(self, name: str, dim: int) -> None: ...
    async def upsert(self, collection: str, records: list[VectorRecord]) -> None: ...
    async def delete(self, collection: str, ids: list[str]) -> None: ...
    async def search(
        self,
        collection: str,
        vector: list[float],
        top_k: int = 10,
        filter_: dict[str, Any] | None = None,
    ) -> list[VectorSearchHit]: ...
    async def close(self) -> None: ...
