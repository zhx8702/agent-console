"""
Document ingestion pipeline.

Pipeline:
1. Compute content_hash. If an existing document has the same hash, return its id
   (dedupe).
2. Insert a KBDocument row.
3. Chunk the content (paragraph-first, then sentence fallback) using chunk_text.
4. Insert KBChunk rows.
5. Embed chunks via llm_provider.embed() in batches (batch_size=16).
6. Upsert chunk vectors into the vector store at collection kb_<tenant_id>.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from app.common.config import Settings, get_settings
from app.common.hashing import stable_hash
from app.common.ids import new_trace_id
from app.common.logging import get_logger
from app.infra.metrics import KB_INDEX_OPERATIONS
from app.kb.chunker import chunk_text, count_tokens
from app.kb.scope import normalize_scope_session_id, scoped_collection_name
from app.kb.vector.base import VectorRecord
from app.llm.base import EmbedRequest

if TYPE_CHECKING:
    from app.kb.service import KBStore
    from app.kb.vector.base import VectorStore
    from app.llm.base import LLMProvider


KB_COLLECTION_PREFIX = "kb_"
KB_MUTATION_LOCK_KEY = "kb-mutations"
logger = get_logger(__name__)
PreparedIndex = tuple[list[str], list[list[float]], str]


def kb_collection_for(tenant_id: str, session_id: str | None = None) -> str:
    return scoped_collection_name(KB_COLLECTION_PREFIX, tenant_id, session_id)


class IngestionService:
    """Ingests text documents into KB store + vector store."""

    def __init__(
        self,
        kb_store: KBStore,
        vector_store: VectorStore,
        llm_provider: LLMProvider,
        settings: Settings | None = None,
        *,
        embed_batch_size: int = 16,
        max_tokens_per_chunk: int = 400,
        chunk_overlap_tokens: int = 60,
        cache_invalidator: Callable[[str, str | None], None] | None = None,
    ) -> None:
        self._store = kb_store
        self._vector = vector_store
        self._llm = llm_provider
        self._settings = settings or get_settings()
        self._batch_size = embed_batch_size
        self._max_tokens = max_tokens_per_chunk
        self._overlap = chunk_overlap_tokens
        self._cache_invalidator = cache_invalidator

    def set_cache_invalidator(self, callback: Callable[[str, str | None], None] | None) -> None:
        self._cache_invalidator = callback

    def invalidate_cache(self, tenant_id: str, session_id: str | None) -> None:
        if self._cache_invalidator is not None:
            self._cache_invalidator(tenant_id, normalize_scope_session_id(session_id))

    @property
    def llm_provider(self) -> LLMProvider:
        return self._llm

    async def add_text(
        self,
        tenant_id: str,
        session_id: str | None,
        title: str,
        content: str,
        source: str = "manual",
        url: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        return await self.add_document(
            tenant_id=tenant_id,
            session_id=session_id,
            title=title,
            content=content,
            source=source,
            url=url,
            metadata=metadata,
        )

    async def add_document(
        self,
        tenant_id: str,
        session_id: str | None,
        title: str,
        content: str,
        source: str = "manual",
        url: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        meta = dict(metadata or {})
        if not str(content or "").strip():
            raise ValueError("knowledge document content must not be empty")
        normalized_session_id = normalize_scope_session_id(session_id)
        digest = self._content_hash(normalized_session_id, title, content)
        async with self._store.resource_lock(
            tenant_id,
            normalized_session_id,
            KB_MUTATION_LOCK_KEY,
        ):
            existing = await self._store.find_document_by_hash(
                tenant_id,
                normalized_session_id,
                digest,
            )
            if existing is not None:
                return existing.id

            doc_id = await self._store.insert_document(
                tenant_id=tenant_id,
                session_id=session_id,
                title=title,
                content=content,
                source=source,
                url=url,
                content_hash=digest,
                meta=meta,
            )
            try:
                await self._index_document(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    doc_id=doc_id,
                    title=title,
                    content=content,
                    source=source,
                    url=url,
                    metadata=meta,
                )
            except Exception:
                # Do not leave a document advertised as available when indexing
                # never completed.
                try:
                    orphan_chunk_ids = await self._store.delete_document(
                        tenant_id, normalized_session_id, doc_id
                    )
                    if orphan_chunk_ids:
                        await self._vector.delete(
                            kb_collection_for(tenant_id, normalized_session_id),
                            [str(chunk_id) for chunk_id in orphan_chunk_ids],
                        )
                except Exception as cleanup_error:
                    logger.error(
                        "kb.ingest_rollback_failed",
                        tenant_id=tenant_id,
                        session_id=normalized_session_id,
                        doc_id=doc_id,
                        error=str(cleanup_error),
                    )
                raise
            self.invalidate_cache(tenant_id, normalized_session_id)
            return doc_id

    async def update_document(
        self,
        tenant_id: str,
        session_id: str | None,
        doc_id: int,
        title: str,
        content: str,
        source: str = "manual",
        url: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int | None:
        meta = dict(metadata or {})
        if not str(content or "").strip():
            raise ValueError("knowledge document content must not be empty")
        normalized_session_id = normalize_scope_session_id(session_id)
        async with self._store.resource_lock(
            tenant_id,
            normalized_session_id,
            KB_MUTATION_LOCK_KEY,
        ):
            return await self._update_document_locked(
                tenant_id=tenant_id,
                session_id=normalized_session_id,
                doc_id=doc_id,
                title=title,
                content=content,
                source=source,
                url=url,
                meta=meta,
            )

    async def _update_document_locked(
        self,
        *,
        tenant_id: str,
        session_id: str,
        doc_id: int,
        title: str,
        content: str,
        source: str,
        url: str | None,
        meta: dict[str, Any],
    ) -> int | None:
        existing = await self._store.get_document(
            tenant_id, session_id, doc_id
        )
        if existing is None:
            return None
        # Embedding is the least reliable and most expensive part. Complete it
        # before mutating the current document so provider failures leave the
        # old searchable version untouched.
        prepared = await self._prepare_document_index(
            tenant_id=tenant_id,
            session_id=session_id,
            content=content,
        )
        digest = self._content_hash(session_id, title, content)
        rows = [
            (idx, text, count_tokens(text), {"title": title})
            for idx, text in enumerate(prepared[0])
        ]
        collection = prepared[2]

        async def write_new_index(chunk_ids: list[int]) -> None:
            records = self._vector_records(
                tenant_id=tenant_id,
                session_id=session_id,
                doc_id=doc_id,
                title=title,
                source=source,
                url=url,
                metadata=meta,
                chunks=prepared[0],
                vectors=prepared[1],
                chunk_ids=chunk_ids,
            )
            await self._vector.upsert(collection, records)

        async def rollback_new_index(chunk_ids: list[int]) -> None:
            try:
                await self._vector.delete(collection, [str(chunk_id) for chunk_id in chunk_ids])
                KB_INDEX_OPERATIONS.labels(operation="update", result="rolled_back").inc()
            except Exception as cleanup_error:
                # Retrieval validates vector ids against the authoritative DB
                # chunk set, so even failed compensation cannot expose this
                # staged version.
                KB_INDEX_OPERATIONS.labels(operation="update", result="rollback_failed").inc()
                logger.error(
                    "kb.update_vector_rollback_failed",
                    tenant_id=tenant_id,
                    session_id=session_id,
                    doc_id=doc_id,
                    error=str(cleanup_error),
                )

        replaced = await self._store.replace_document_indexed(
            tenant_id=tenant_id,
            session_id=session_id,
            doc_id=doc_id,
            title=title,
            content=content,
            source=source,
            url=url,
            content_hash=digest,
            meta=meta,
            chunks=rows,
            before_commit=write_new_index,
            on_rollback=rollback_new_index,
        )
        if replaced is None:
            return None
        old_chunk_ids, _new_chunk_ids = replaced
        if old_chunk_ids:
            try:
                await self._vector.delete(
                    collection, [str(chunk_id) for chunk_id in old_chunk_ids]
                )
            except Exception as cleanup_error:
                # Stale vector ids are filtered by HybridRetriever against the
                # committed DB chunk ids. Cleanup can therefore be retried
                # asynchronously without serving a mixed version.
                KB_INDEX_OPERATIONS.labels(operation="update", result="stale_cleanup_failed").inc()
                logger.warning(
                    "kb.update_stale_vector_cleanup_failed",
                    tenant_id=tenant_id,
                    session_id=session_id,
                    doc_id=doc_id,
                    error=str(cleanup_error),
                )
        KB_INDEX_OPERATIONS.labels(operation="update", result="success").inc()
        self.invalidate_cache(tenant_id, session_id)
        return doc_id

    def _content_hash(self, session_id: str | None, title: str, content: str) -> str:
        normalized_content = (title or "") + "\n" + (content or "")
        return stable_hash(f"{normalize_scope_session_id(session_id)}\n{normalized_content}")

    async def _index_document(
        self,
        *,
        tenant_id: str,
        session_id: str | None,
        doc_id: int,
        title: str,
        content: str,
        source: str | None,
        url: str | None,
        metadata: dict[str, Any],
        prepared: PreparedIndex | None = None,
    ) -> None:
        normalized_session_id = normalize_scope_session_id(session_id)
        chunks, vectors, collection = prepared or await self._prepare_document_index(
            tenant_id=tenant_id,
            session_id=normalized_session_id,
            content=content,
        )
        if not chunks:
            return

        rows: list[tuple[int, str, int, dict[str, Any]]] = []
        for idx, text in enumerate(chunks):
            rows.append((idx, text, count_tokens(text), {"title": title}))
        chunk_ids = await self._store.insert_chunks(tenant_id, normalized_session_id, doc_id, rows)
        if len(chunk_ids) != len(chunks):
            raise RuntimeError(
                f"chunk id count mismatch: expected {len(chunks)}, got {len(chunk_ids)}"
            )

        records = self._vector_records(
            tenant_id=tenant_id,
            session_id=normalized_session_id,
            doc_id=doc_id,
            title=title,
            source=source,
            url=url,
            metadata=metadata,
            chunks=chunks,
            vectors=vectors,
            chunk_ids=chunk_ids,
        )
        await self._vector.upsert(collection, records)

    def _vector_records(
        self,
        *,
        tenant_id: str,
        session_id: str | None,
        doc_id: int,
        title: str,
        source: str | None,
        url: str | None,
        metadata: dict[str, Any],
        chunks: list[str],
        vectors: list[list[float]],
        chunk_ids: list[int],
    ) -> list[VectorRecord]:
        normalized_session_id = normalize_scope_session_id(session_id)
        return [
            VectorRecord(
                id=str(chunk_ids[i]),
                vector=vectors[i],
                payload={
                    "chunk_id": chunk_ids[i],
                    "doc_id": doc_id,
                    "tenant_id": tenant_id,
                    "session_id": normalized_session_id,
                    "title": title,
                    "source": source,
                    "url": url,
                    "content": chunks[i],
                    "chunk_idx": i,
                    "metadata": metadata,
                },
            )
            for i in range(len(chunks))
        ]

    async def _prepare_document_index(
        self,
        *,
        tenant_id: str,
        session_id: str | None,
        content: str,
    ) -> PreparedIndex:
        normalized_session_id = normalize_scope_session_id(session_id)
        chunks = chunk_text(
            content,
            max_tokens=self._max_tokens,
            overlap_tokens=self._overlap,
        )
        if not chunks:
            return [], [], kb_collection_for(tenant_id, normalized_session_id)

        trace_id = new_trace_id()
        vectors: list[list[float]] = []
        for i in range(0, len(chunks), self._batch_size):
            batch = chunks[i : i + self._batch_size]
            resp = await self._llm.embed(
                EmbedRequest(
                    tenant_id=tenant_id,
                    trace_id=trace_id,
                    model=self._settings.llm_embed_model,
                    texts=batch,
                )
            )
            vectors.extend(resp.vectors)

        if len(vectors) != len(chunks):
            raise RuntimeError(
                f"embedding count mismatch: expected {len(chunks)}, got {len(vectors)}"
            )
        if not vectors or not vectors[0]:
            raise RuntimeError("embedding provider returned empty vectors")
        dim = len(vectors[0])
        if any(len(vector) != dim for vector in vectors):
            raise RuntimeError("embedding provider returned inconsistent vector dimensions")

        collection = kb_collection_for(tenant_id, normalized_session_id)
        await self._vector.ensure_collection(collection, dim)
        return chunks, vectors, collection
