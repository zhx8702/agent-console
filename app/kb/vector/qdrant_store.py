"""
Qdrant-backed VectorStore implementation.

Uses qdrant_client.AsyncQdrantClient. Collection is created with COSINE distance.
Point ids may be strings; we coerce non-numeric ids to UUIDv5 to satisfy Qdrant's
id requirements (int or UUID) while remaining deterministic.
"""
from __future__ import annotations

import uuid
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qm

from app.kb.vector.base import VectorRecord, VectorSearchHit

_UPSERT_NAMESPACE = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


def is_qdrant_collection_missing_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status_code is None and response is not None:
        status_code = getattr(response, "status_code", None)
    try:
        normalized_status = int(status_code or 0)
    except (TypeError, ValueError):
        normalized_status = 0
    if normalized_status != 404:
        return False

    content = getattr(exc, "content", b"")
    if isinstance(content, bytes):
        content_text = content.decode("utf-8", errors="ignore")
    else:
        content_text = str(content or "")
    message = f"{exc} {content_text}".lower()
    return (
        "collection" in message
        or "not found" in message
        or "doesn't exist" in message
        or "does not exist" in message
    )


def _coerce_id(raw: str) -> str | int:
    s = str(raw)
    # Accept ints as ints.
    if s.isdigit():
        try:
            return int(s)
        except ValueError:
            pass
    # Accept valid UUID strings as-is.
    try:
        return str(uuid.UUID(s))
    except (ValueError, AttributeError):
        pass
    return str(uuid.uuid5(_UPSERT_NAMESPACE, s))


def _filter_from_dict(filter_: dict[str, Any] | None) -> qm.Filter | None:
    if not filter_:
        return None
    musts: list[qm.FieldCondition] = []
    for k, v in filter_.items():
        musts.append(qm.FieldCondition(key=k, match=qm.MatchValue(value=v)))
    return qm.Filter(must=musts)


def _vectors_config_size(vectors_config: Any) -> int | None:
    if vectors_config is None:
        return None
    if isinstance(vectors_config, dict):
        if not vectors_config:
            return None
        values = list(vectors_config.values())
        first = values[0] if values else None
        return _vectors_config_size(first)
    size = getattr(vectors_config, "size", None)
    try:
        return int(size) if size is not None else None
    except (TypeError, ValueError):
        return None


class QdrantVectorStore:
    name = "qdrant"

    def __init__(
        self,
        url: str,
        api_key: str | None = None,
        *,
        client: AsyncQdrantClient | None = None,
    ) -> None:
        self._client = client or AsyncQdrantClient(
            url=url,
            api_key=api_key,
            check_compatibility=False,
            trust_env=False,
        )
        # Cache original-id -> qdrant-id mapping so payload/delete work consistently.
        self._id_map: dict[str, dict[str, str | int]] = {}
        self._known_collections: set[str] = set()
        self._missing_collections: set[str] = set()

    async def _collection_exists(self, name: str) -> bool | None:
        if name in self._known_collections:
            return True
        if name in self._missing_collections:
            return False
        get_collections = getattr(self._client, "get_collections", None)
        if get_collections is None:
            return None
        collections = await get_collections()
        existing = {str(c.name) for c in collections.collections}
        self._known_collections.update(existing)
        if name in existing:
            self._missing_collections.discard(name)
            return True
        self._missing_collections.add(name)
        return False

    async def ensure_collection(self, name: str, dim: int) -> None:
        collections = await self._client.get_collections()
        existing = {c.name for c in collections.collections}
        if name in existing:
            get_collection = getattr(self._client, "get_collection", None)
            if get_collection is not None:
                info = await get_collection(collection_name=name)
                config = getattr(info, "config", None)
                params = getattr(config, "params", None)
                vectors = getattr(params, "vectors", None)
                existing_dim = _vectors_config_size(vectors)
                if existing_dim is not None and existing_dim != dim:
                    raise ValueError(
                        f"collection {name} already exists with dim={existing_dim}, requested dim={dim}"
                    )
            self._known_collections.add(name)
            self._missing_collections.discard(name)
            return
        await self._client.create_collection(
            collection_name=name,
            vectors_config=qm.VectorParams(size=dim, distance=qm.Distance.COSINE),
        )
        self._known_collections.add(name)
        self._missing_collections.discard(name)

    async def upsert(self, collection: str, records: list[VectorRecord]) -> None:
        if not records:
            return
        self._known_collections.add(collection)
        self._missing_collections.discard(collection)
        id_map = self._id_map.setdefault(collection, {})
        points: list[qm.PointStruct] = []
        for r in records:
            pid = _coerce_id(r.id)
            id_map[str(r.id)] = pid
            payload = dict(r.payload)
            payload.setdefault("_raw_id", str(r.id))
            points.append(qm.PointStruct(id=pid, vector=list(r.vector), payload=payload))
        # Batch upserts; qdrant-client handles reasonable batch sizes internally.
        for i in range(0, len(points), 256):
            batch = points[i : i + 256]
            await self._client.upsert(collection_name=collection, points=batch, wait=True)

    async def delete(self, collection: str, ids: list[str]) -> None:
        if not ids:
            return
        id_map = self._id_map.get(collection, {})
        qids = [id_map.get(str(_id), _coerce_id(_id)) for _id in ids]
        try:
            await self._client.delete(
                collection_name=collection,
                points_selector=qm.PointIdsList(points=qids),
                wait=True,
            )
        except Exception as exc:
            if is_qdrant_collection_missing_error(exc):
                return
            raise
        for _id in ids:
            id_map.pop(str(_id), None)

    async def search(
        self,
        collection: str,
        vector: list[float],
        top_k: int = 10,
        filter_: dict[str, Any] | None = None,
    ) -> list[VectorSearchHit]:
        exists = await self._collection_exists(collection)
        if exists is False:
            return []
        flt = _filter_from_dict(filter_)
        try:
            if hasattr(self._client, "search"):
                res = await self._client.search(
                    collection_name=collection,
                    query_vector=list(vector),
                    query_filter=flt,
                    limit=top_k,
                    with_payload=True,
                )
            else:
                query_res = await self._client.query_points(
                    collection_name=collection,
                    query=list(vector),
                    query_filter=flt,
                    limit=top_k,
                    with_payload=True,
                )
                res = list(getattr(query_res, "points", []) or [])
        except Exception as exc:
            if is_qdrant_collection_missing_error(exc):
                self._missing_collections.add(collection)
                self._known_collections.discard(collection)
                return []
            raise
        hits: list[VectorSearchHit] = []
        for p in res:
            payload = dict(p.payload or {})
            raw_id = payload.pop("_raw_id", str(p.id))
            hits.append(VectorSearchHit(id=str(raw_id), score=float(p.score), payload=payload))
        return hits

    async def close(self) -> None:
        await self._client.close()
