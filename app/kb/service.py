"""
KnowledgeBaseService — facade over ingestion + document CRUD.

Data access goes through a ``KBStore`` Protocol so tests can inject an in-memory
implementation (avoiding a database dependency for unit tests). A default
SQLAlchemy-backed store is provided (``SQLAlchemyKBStore``) and uses the shared
session_scope() factory for DB access in production.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import delete, select, text

from app.common.config import Settings, get_settings
from app.common.ids import new_trace_id
from app.kb.scope import normalize_scope_session_id, scope_payload, scoped_collection_name
from app.kb.vector.base import VectorSearchHit
from app.llm.base import EmbedRequest
from app.models.kb import KBChunk, KBDocument

if TYPE_CHECKING:
    from app.kb.ingest import IngestionService
    from app.kb.vector.base import VectorStore


@dataclass
class DocumentRecord:
    id: int
    tenant_id: str
    title: str | None
    source: str | None
    url: str | None
    content_hash: str | None
    session_id: str = ""
    content: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChunkRecord:
    id: int
    tenant_id: str
    doc_id: int
    chunk_idx: int
    content: str
    token_count: int
    session_id: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class DocumentSearchHit:
    chunk_id: int
    doc_id: int
    title: str | None
    content: str
    score: float
    session_id: str | None = None
    source: str | None = None
    url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class KBStore(Protocol):
    def resource_lock(
        self,
        tenant_id: str,
        session_id: str | None,
        resource_key: str,
    ) -> AbstractAsyncContextManager[None]: ...

    async def insert_document(
        self,
        *,
        tenant_id: str,
        session_id: str | None,
        title: str | None,
        content: str,
        source: str | None,
        url: str | None,
        content_hash: str,
        meta: dict[str, Any],
    ) -> int: ...

    async def find_document_by_hash(
        self, tenant_id: str, session_id: str | None, content_hash: str
    ) -> DocumentRecord | None: ...

    async def get_document(
        self, tenant_id: str, session_id: str | None, doc_id: int
    ) -> DocumentRecord | None: ...

    async def replace_document(
        self,
        *,
        tenant_id: str,
        session_id: str | None,
        doc_id: int,
        title: str | None,
        content: str,
        source: str | None,
        url: str | None,
        content_hash: str,
        meta: dict[str, Any],
    ) -> list[int] | None:
        """Update document metadata/content, delete old chunks, return old chunk ids."""
        ...

    async def insert_chunks(
        self,
        tenant_id: str,
        session_id: str | None,
        doc_id: int,
        chunks: list[tuple[int, str, int, dict[str, Any]]],
    ) -> list[int]:
        """Return a list of chunk ids in the same order as input chunks."""
        ...

    async def replace_document_indexed(
        self,
        *,
        tenant_id: str,
        session_id: str | None,
        doc_id: int,
        title: str | None,
        content: str,
        source: str | None,
        url: str | None,
        content_hash: str,
        meta: dict[str, Any],
        chunks: list[tuple[int, str, int, dict[str, Any]]],
        before_commit: Callable[[list[int]], Awaitable[None]],
        on_rollback: Callable[[list[int]], Awaitable[None]],
    ) -> tuple[list[int], list[int]] | None:
        """Replace a document and stage its external index before DB commit.

        Implementations must roll back the database mutation when
        ``before_commit`` fails, and call ``on_rollback`` for external-index
        compensation when any error (including commit failure) occurs.
        """
        ...

    async def list_documents(
        self, tenant_id: str, session_id: str | None, limit: int = 100, offset: int = 0
    ) -> list[DocumentRecord]: ...

    async def list_document_chunk_ids(
        self,
        tenant_id: str,
        session_id: str | None,
        doc_id: int,
    ) -> list[int]: ...

    async def delete_document(
        self, tenant_id: str, session_id: str | None, doc_id: int
    ) -> list[int]:
        """Delete the document and its chunks. Return the deleted chunk ids."""
        ...

    async def list_chunks(self, tenant_id: str, session_id: str | None) -> list[ChunkRecord]: ...


# ---------- In-memory store -------------------------------------------------


class InMemoryKBStore:
    """Unit-test backend. Not thread-safe; not for production use."""

    name = "memory"

    def __init__(self) -> None:
        self._docs: dict[int, DocumentRecord] = {}
        self._chunks: dict[int, ChunkRecord] = {}
        self._next_doc_id = 1
        self._next_chunk_id = 1
        self._resource_locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def resource_lock(
        self,
        tenant_id: str,
        session_id: str | None,
        resource_key: str,
    ) -> AsyncIterator[None]:
        scope = f"{tenant_id}:{normalize_scope_session_id(session_id)}:{resource_key}"
        lock = self._resource_locks.setdefault(scope, asyncio.Lock())
        async with lock:
            yield

    async def insert_document(
        self,
        *,
        tenant_id: str,
        session_id: str | None,
        title: str | None,
        content: str,
        source: str | None,
        url: str | None,
        content_hash: str,
        meta: dict[str, Any],
    ) -> int:
        doc_id = self._next_doc_id
        self._next_doc_id += 1
        self._docs[doc_id] = DocumentRecord(
            id=doc_id,
            tenant_id=tenant_id,
            session_id=normalize_scope_session_id(session_id),
            title=title,
            source=source,
            url=url,
            content_hash=content_hash,
            content=content,
            meta=dict(meta),
        )
        return doc_id

    async def find_document_by_hash(
        self, tenant_id: str, session_id: str | None, content_hash: str
    ) -> DocumentRecord | None:
        normalized = normalize_scope_session_id(session_id)
        for d in self._docs.values():
            if (
                d.tenant_id == tenant_id
                and d.session_id == normalized
                and d.content_hash == content_hash
            ):
                return d
        return None

    async def get_document(
        self, tenant_id: str, session_id: str | None, doc_id: int
    ) -> DocumentRecord | None:
        normalized = normalize_scope_session_id(session_id)
        doc = self._docs.get(doc_id)
        if doc is None or doc.tenant_id != tenant_id or doc.session_id != normalized:
            return None
        return doc

    async def replace_document(
        self,
        *,
        tenant_id: str,
        session_id: str | None,
        doc_id: int,
        title: str | None,
        content: str,
        source: str | None,
        url: str | None,
        content_hash: str,
        meta: dict[str, Any],
    ) -> list[int] | None:
        normalized = normalize_scope_session_id(session_id)
        doc = self._docs.get(doc_id)
        if doc is None or doc.tenant_id != tenant_id or doc.session_id != normalized:
            return None
        old_chunk_ids = [c.id for c in self._chunks.values() if c.doc_id == doc_id]
        for cid in old_chunk_ids:
            self._chunks.pop(cid, None)
        self._docs[doc_id] = DocumentRecord(
            id=doc_id,
            tenant_id=tenant_id,
            session_id=normalized,
            title=title,
            source=source,
            url=url,
            content_hash=content_hash,
            content=content,
            meta=dict(meta),
        )
        return old_chunk_ids

    async def insert_chunks(
        self,
        tenant_id: str,
        session_id: str | None,
        doc_id: int,
        chunks: list[tuple[int, str, int, dict[str, Any]]],
    ) -> list[int]:
        normalized = normalize_scope_session_id(session_id)
        ids: list[int] = []
        for idx, content, tok, meta in chunks:
            cid = self._next_chunk_id
            self._next_chunk_id += 1
            self._chunks[cid] = ChunkRecord(
                id=cid,
                tenant_id=tenant_id,
                session_id=normalized,
                doc_id=doc_id,
                chunk_idx=idx,
                content=content,
                token_count=tok,
                meta=dict(meta),
            )
            ids.append(cid)
        return ids

    async def replace_document_indexed(
        self,
        *,
        tenant_id: str,
        session_id: str | None,
        doc_id: int,
        title: str | None,
        content: str,
        source: str | None,
        url: str | None,
        content_hash: str,
        meta: dict[str, Any],
        chunks: list[tuple[int, str, int, dict[str, Any]]],
        before_commit: Callable[[list[int]], Awaitable[None]],
        on_rollback: Callable[[list[int]], Awaitable[None]],
    ) -> tuple[list[int], list[int]] | None:
        snapshot = (
            deepcopy(self._docs),
            deepcopy(self._chunks),
            self._next_doc_id,
            self._next_chunk_id,
        )
        new_chunk_ids: list[int] = []
        try:
            old_chunk_ids = await self.replace_document(
                tenant_id=tenant_id,
                session_id=session_id,
                doc_id=doc_id,
                title=title,
                content=content,
                source=source,
                url=url,
                content_hash=content_hash,
                meta=meta,
            )
            if old_chunk_ids is None:
                return None
            new_chunk_ids = await self.insert_chunks(
                tenant_id, session_id, doc_id, chunks
            )
            await before_commit(new_chunk_ids)
            return old_chunk_ids, new_chunk_ids
        except Exception:
            self._docs, self._chunks, self._next_doc_id, self._next_chunk_id = snapshot
            if new_chunk_ids:
                await on_rollback(new_chunk_ids)
            raise

    async def list_documents(
        self, tenant_id: str, session_id: str | None, limit: int = 100, offset: int = 0
    ) -> list[DocumentRecord]:
        normalized = normalize_scope_session_id(session_id)
        rows = [
            d
            for d in self._docs.values()
            if d.tenant_id == tenant_id and d.session_id == normalized
        ]
        rows.sort(key=lambda d: d.id, reverse=True)
        return rows[offset : offset + limit]

    async def list_document_chunk_ids(
        self,
        tenant_id: str,
        session_id: str | None,
        doc_id: int,
    ) -> list[int]:
        normalized = normalize_scope_session_id(session_id)
        return sorted(
            chunk.id
            for chunk in self._chunks.values()
            if chunk.tenant_id == tenant_id
            and chunk.session_id == normalized
            and chunk.doc_id == doc_id
        )

    async def delete_document(
        self, tenant_id: str, session_id: str | None, doc_id: int
    ) -> list[int]:
        doc = self._docs.get(doc_id)
        if (
            not doc
            or doc.tenant_id != tenant_id
            or doc.session_id != normalize_scope_session_id(session_id)
        ):
            return []
        del self._docs[doc_id]
        chunk_ids = [c.id for c in self._chunks.values() if c.doc_id == doc_id]
        for cid in chunk_ids:
            self._chunks.pop(cid, None)
        return chunk_ids

    async def list_chunks(self, tenant_id: str, session_id: str | None) -> list[ChunkRecord]:
        normalized = normalize_scope_session_id(session_id)
        rows = [
            c
            for c in self._chunks.values()
            if c.tenant_id == tenant_id and c.session_id == normalized
        ]
        rows.sort(key=lambda c: (c.doc_id, c.chunk_idx, c.id))
        return rows


# ---------- SQLAlchemy store ------------------------------------------------


class SQLAlchemyKBStore:
    """Production KB store using session_scope()."""

    name = "sqlalchemy"

    def __init__(self, session_factory: Any | None = None) -> None:
        if session_factory is None:
            from app.infra.db import session_scope

            session_factory = session_scope
        self._session_factory = session_factory
        self._resource_locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def resource_lock(
        self,
        tenant_id: str,
        session_id: str | None,
        resource_key: str,
    ) -> AsyncIterator[None]:
        """Serialize a document mutation across workers and processes.

        PostgreSQL session advisory locks intentionally span the vector-store
        calls made by the service. Other database dialects use a process-local
        lock, which is sufficient for local tests but is not a production
        multi-replica guarantee.
        """

        scope = f"kb:{tenant_id}:{normalize_scope_session_id(session_id)}:{resource_key}"
        async with self._session_factory() as session:
            bind = session.get_bind()
            if bind.dialect.name != "postgresql":
                lock = self._resource_locks.setdefault(scope, asyncio.Lock())
                async with lock:
                    yield
                return

            await session.execute(
                text("SELECT pg_advisory_lock(hashtextextended(:scope, 0))"),
                {"scope": scope},
            )
            try:
                yield
            finally:
                await session.execute(
                    text("SELECT pg_advisory_unlock(hashtextextended(:scope, 0))"),
                    {"scope": scope},
                )

    def _document_record(self, row: KBDocument) -> DocumentRecord:
        return DocumentRecord(
            id=int(row.id),
            tenant_id=row.tenant_id,
            session_id=normalize_scope_session_id(getattr(row, "session_id", "")),
            title=row.title,
            source=row.source,
            url=row.url,
            content_hash=row.content_hash,
            content=str(getattr(row, "content", "") or ""),
            meta=dict(row.meta or {}),
        )

    async def insert_document(
        self,
        *,
        tenant_id: str,
        session_id: str | None,
        title: str | None,
        content: str,
        source: str | None,
        url: str | None,
        content_hash: str,
        meta: dict[str, Any],
    ) -> int:
        normalized = normalize_scope_session_id(session_id)
        async with self._session_factory() as session:
            doc = KBDocument(
                tenant_id=tenant_id,
                session_id=normalized,
                title=title,
                content=content,
                source=source,
                url=url,
                content_hash=content_hash,
                meta=meta,
            )
            session.add(doc)
            await session.flush()
            return int(doc.id)

    async def find_document_by_hash(
        self, tenant_id: str, session_id: str | None, content_hash: str
    ) -> DocumentRecord | None:
        normalized = normalize_scope_session_id(session_id)
        async with self._session_factory() as session:
            stmt = select(KBDocument).where(
                KBDocument.tenant_id == tenant_id,
                KBDocument.session_id == normalized,
                KBDocument.content_hash == content_hash,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            return self._document_record(row)

    async def get_document(
        self, tenant_id: str, session_id: str | None, doc_id: int
    ) -> DocumentRecord | None:
        normalized = normalize_scope_session_id(session_id)
        async with self._session_factory() as session:
            stmt = select(KBDocument).where(
                KBDocument.tenant_id == tenant_id,
                KBDocument.session_id == normalized,
                KBDocument.id == doc_id,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            return self._document_record(row)

    async def replace_document(
        self,
        *,
        tenant_id: str,
        session_id: str | None,
        doc_id: int,
        title: str | None,
        content: str,
        source: str | None,
        url: str | None,
        content_hash: str,
        meta: dict[str, Any],
    ) -> list[int] | None:
        normalized = normalize_scope_session_id(session_id)
        async with self._session_factory() as session:
            doc_stmt = select(KBDocument).where(
                KBDocument.tenant_id == tenant_id,
                KBDocument.session_id == normalized,
                KBDocument.id == doc_id,
            )
            doc = (await session.execute(doc_stmt)).scalar_one_or_none()
            if doc is None:
                return None

            chunk_stmt = select(KBChunk.id).where(
                KBChunk.tenant_id == tenant_id,
                KBChunk.session_id == normalized,
                KBChunk.doc_id == doc_id,
            )
            chunk_ids = [int(x) for x in (await session.execute(chunk_stmt)).scalars().all()]
            await session.execute(
                delete(KBChunk).where(
                    KBChunk.tenant_id == tenant_id,
                    KBChunk.session_id == normalized,
                    KBChunk.doc_id == doc_id,
                )
            )
            doc.title = title
            doc.content = content
            doc.source = source
            doc.url = url
            doc.content_hash = content_hash
            doc.meta = meta
            doc.version = int(doc.version or 1) + 1
            await session.flush()
            return chunk_ids

    async def insert_chunks(
        self,
        tenant_id: str,
        session_id: str | None,
        doc_id: int,
        chunks: list[tuple[int, str, int, dict[str, Any]]],
    ) -> list[int]:
        normalized = normalize_scope_session_id(session_id)
        async with self._session_factory() as session:
            objs = [
                KBChunk(
                    tenant_id=tenant_id,
                    session_id=normalized,
                    doc_id=doc_id,
                    chunk_idx=idx,
                    content=content,
                    token_count=tok,
                    meta=meta,
                )
                for idx, content, tok, meta in chunks
            ]
            session.add_all(objs)
            await session.flush()
            return [int(o.id) for o in objs]

    async def replace_document_indexed(
        self,
        *,
        tenant_id: str,
        session_id: str | None,
        doc_id: int,
        title: str | None,
        content: str,
        source: str | None,
        url: str | None,
        content_hash: str,
        meta: dict[str, Any],
        chunks: list[tuple[int, str, int, dict[str, Any]]],
        before_commit: Callable[[list[int]], Awaitable[None]],
        on_rollback: Callable[[list[int]], Awaitable[None]],
    ) -> tuple[list[int], list[int]] | None:
        normalized = normalize_scope_session_id(session_id)
        new_chunk_ids: list[int] = []
        try:
            async with self._session_factory() as session:
                doc_stmt = (
                    select(KBDocument)
                    .where(
                        KBDocument.tenant_id == tenant_id,
                        KBDocument.session_id == normalized,
                        KBDocument.id == doc_id,
                    )
                    .with_for_update()
                )
                doc = (await session.execute(doc_stmt)).scalar_one_or_none()
                if doc is None:
                    return None

                chunk_stmt = select(KBChunk.id).where(
                    KBChunk.tenant_id == tenant_id,
                    KBChunk.session_id == normalized,
                    KBChunk.doc_id == doc_id,
                )
                old_chunk_ids = [
                    int(value) for value in (await session.execute(chunk_stmt)).scalars().all()
                ]
                await session.execute(
                    delete(KBChunk).where(
                        KBChunk.tenant_id == tenant_id,
                        KBChunk.session_id == normalized,
                        KBChunk.doc_id == doc_id,
                    )
                )
                doc.title = title
                doc.content = content
                doc.source = source
                doc.url = url
                doc.content_hash = content_hash
                doc.meta = meta
                doc.version = int(doc.version or 1) + 1

                objects = [
                    KBChunk(
                        tenant_id=tenant_id,
                        session_id=normalized,
                        doc_id=doc_id,
                        chunk_idx=idx,
                        content=chunk_content,
                        token_count=token_count,
                        meta=chunk_meta,
                    )
                    for idx, chunk_content, token_count, chunk_meta in chunks
                ]
                session.add_all(objects)
                await session.flush()
                new_chunk_ids = [int(obj.id) for obj in objects]
                await before_commit(new_chunk_ids)
                # session_scope commits after leaving the context. Keeping the
                # external write inside this boundary means a failed upsert
                # rolls the DB version back; commit failures are compensated
                # below.
                return old_chunk_ids, new_chunk_ids
        except Exception:
            if new_chunk_ids:
                await on_rollback(new_chunk_ids)
            raise

    async def list_documents(
        self, tenant_id: str, session_id: str | None, limit: int = 100, offset: int = 0
    ) -> list[DocumentRecord]:
        normalized = normalize_scope_session_id(session_id)
        async with self._session_factory() as session:
            stmt = (
                select(KBDocument)
                .where(KBDocument.tenant_id == tenant_id, KBDocument.session_id == normalized)
                .order_by(KBDocument.id.desc())
                .limit(limit)
                .offset(offset)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [self._document_record(r) for r in rows]

    async def list_document_chunk_ids(
        self,
        tenant_id: str,
        session_id: str | None,
        doc_id: int,
    ) -> list[int]:
        normalized = normalize_scope_session_id(session_id)
        async with self._session_factory() as session:
            stmt = (
                select(KBChunk.id)
                .where(
                    KBChunk.tenant_id == tenant_id,
                    KBChunk.session_id == normalized,
                    KBChunk.doc_id == doc_id,
                )
                .order_by(KBChunk.id.asc())
            )
            return [int(value) for value in (await session.execute(stmt)).scalars().all()]

    async def delete_document(
        self, tenant_id: str, session_id: str | None, doc_id: int
    ) -> list[int]:
        normalized = normalize_scope_session_id(session_id)
        async with self._session_factory() as session:
            chunk_stmt = select(KBChunk.id).where(
                KBChunk.tenant_id == tenant_id,
                KBChunk.session_id == normalized,
                KBChunk.doc_id == doc_id,
            )
            chunk_ids = [int(x) for x in (await session.execute(chunk_stmt)).scalars().all()]
            await session.execute(
                delete(KBChunk).where(
                    KBChunk.tenant_id == tenant_id,
                    KBChunk.session_id == normalized,
                    KBChunk.doc_id == doc_id,
                )
            )
            await session.execute(
                delete(KBDocument).where(
                    KBDocument.tenant_id == tenant_id,
                    KBDocument.session_id == normalized,
                    KBDocument.id == doc_id,
                )
            )
            return chunk_ids

    async def list_chunks(self, tenant_id: str, session_id: str | None) -> list[ChunkRecord]:
        normalized = normalize_scope_session_id(session_id)
        async with self._session_factory() as session:
            stmt = (
                select(KBChunk)
                .where(KBChunk.tenant_id == tenant_id, KBChunk.session_id == normalized)
                .order_by(KBChunk.doc_id.asc(), KBChunk.chunk_idx.asc(), KBChunk.id.asc())
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [
                ChunkRecord(
                    id=int(r.id),
                    tenant_id=r.tenant_id,
                    session_id=normalize_scope_session_id(getattr(r, "session_id", "")),
                    doc_id=int(r.doc_id),
                    chunk_idx=r.chunk_idx,
                    content=r.content,
                    token_count=r.token_count,
                    meta=dict(r.meta or {}),
                )
                for r in rows
            ]


# ---------- KnowledgeBaseService facade -------------------------------------


def kb_collection_name(tenant_id: str, session_id: str | None = None) -> str:
    return scoped_collection_name("kb_", tenant_id, session_id)


class KnowledgeBaseService:
    """Facade: delegates to IngestionService for adds, KBStore for reads/deletes."""

    def __init__(
        self,
        kb_store: KBStore,
        vector_store: VectorStore,
        ingestion: IngestionService,
        settings: Settings | None = None,
    ) -> None:
        self._store = kb_store
        self._vector = vector_store
        self._ingest = ingestion
        self._settings = settings or get_settings()

    async def add_text(
        self,
        tenant_id: str,
        title: str,
        content: str,
        source: str = "manual",
        url: str | None = None,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> int:
        return await self._ingest.add_text(
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
        title: str,
        content: str,
        source: str = "manual",
        url: str | None = None,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> int:
        return await self._ingest.add_document(
            tenant_id=tenant_id,
            session_id=session_id,
            title=title,
            content=content,
            source=source,
            url=url,
            metadata=metadata,
        )

    async def update_document(
        self,
        tenant_id: str,
        doc_id: int,
        title: str,
        content: str,
        source: str = "manual",
        url: str | None = None,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> int | None:
        return await self._ingest.update_document(
            tenant_id=tenant_id,
            session_id=session_id,
            doc_id=doc_id,
            title=title,
            content=content,
            source=source,
            url=url,
            metadata=metadata,
        )

    async def delete_document(
        self, tenant_id: str, doc_id: int, session_id: str | None = None
    ) -> None:
        from app.kb.ingest import KB_MUTATION_LOCK_KEY

        normalized = normalize_scope_session_id(session_id)
        async with self._store.resource_lock(
            tenant_id,
            normalized,
            KB_MUTATION_LOCK_KEY,
        ):
            chunk_ids = await self._store.list_document_chunk_ids(
                tenant_id,
                normalized,
                doc_id,
            )
            if chunk_ids:
                # Qdrant and PostgreSQL cannot share a transaction. Clean the
                # idempotent vector side first; if it fails, the authoritative DB
                # rows remain intact. A crash after this call is safe to retry.
                await self._vector.delete(
                    scoped_collection_name("kb_", tenant_id, normalized),
                    [str(c) for c in chunk_ids],
                )
            await self._store.delete_document(tenant_id, normalized, doc_id)
            self._ingest.invalidate_cache(tenant_id, normalized)

    async def get_document(
        self, tenant_id: str, doc_id: int, session_id: str | None = None
    ) -> DocumentRecord | None:
        doc = await self._store.get_document(tenant_id, session_id, doc_id)
        if doc is None:
            return None
        if doc.content:
            return doc
        chunks = [
            c for c in await self._store.list_chunks(tenant_id, session_id) if c.doc_id == doc_id
        ]
        chunks.sort(key=lambda c: (c.chunk_idx, c.id))
        return DocumentRecord(
            id=doc.id,
            tenant_id=doc.tenant_id,
            session_id=doc.session_id,
            title=doc.title,
            source=doc.source,
            url=doc.url,
            content_hash=doc.content_hash,
            content="\n\n".join(c.content for c in chunks),
            meta=dict(doc.meta),
        )

    async def list_documents(
        self, tenant_id: str, session_id: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[DocumentRecord]:
        return await self._store.list_documents(tenant_id, session_id, limit=limit, offset=offset)

    async def reindex_documents(
        self,
        tenant_id: str,
        *,
        session_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Re-chunk and re-index historical documents in bounded batches."""
        docs = await self._store.list_documents(
            tenant_id, session_id, limit=max(1, limit), offset=max(0, offset)
        )
        result: dict[str, Any] = {
            **scope_payload(tenant_id, session_id),
            "dry_run": dry_run,
            "selected": len(docs),
            "reindexed": 0,
            "failed": [],
            "document_ids": [doc.id for doc in docs],
        }
        if dry_run:
            return result
        for doc in docs:
            try:
                await self.update_document(
                    tenant_id,
                    doc.id,
                    doc.title or "",
                    doc.content,
                    source=doc.source or "manual",
                    url=doc.url,
                    metadata=doc.meta,
                    session_id=session_id,
                )
                result["reindexed"] += 1
            except Exception as exc:
                result["failed"].append({"doc_id": doc.id, "error": str(exc)[:300]})
        return result

    async def search_documents(
        self, tenant_id: str, query: str, session_id: str | None = None, top_k: int = 5
    ) -> list[DocumentSearchHit]:
        query = (query or "").strip()
        if not query:
            return []
        try:
            resp = await self._ingest.llm_provider.embed(
                EmbedRequest(
                    tenant_id=tenant_id,
                    trace_id=new_trace_id(),
                    model=self._settings.llm_embed_model,
                    texts=[query],
                )
            )
        except Exception:
            return []
        if not resp.vectors:
            return []

        normalized = normalize_scope_session_id(session_id)
        valid_chunk_ids = {
            chunk.id for chunk in await self._store.list_chunks(tenant_id, normalized)
        }
        hits = await self._vector.search(
            scoped_collection_name("kb_", tenant_id, normalized),
            resp.vectors[0],
            top_k=max(top_k * 4, top_k),
        )
        authoritative_hits = [
            self._search_hit_from_vector_hit(hit)
            for hit in hits
            if int((hit.payload or {}).get("chunk_id") or hit.id or 0) in valid_chunk_ids
        ]
        return authoritative_hits[:top_k]

    def _search_hit_from_vector_hit(self, hit: VectorSearchHit) -> DocumentSearchHit:
        payload = dict(hit.payload or {})
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        return DocumentSearchHit(
            chunk_id=int(payload.get("chunk_id") or hit.id or 0),
            doc_id=int(payload.get("doc_id") or 0),
            title=payload.get("title"),
            content=str(payload.get("content") or ""),
            score=float(hit.score),
            session_id=normalize_scope_session_id(payload.get("session_id")) or None,
            source=payload.get("source"),
            url=payload.get("url"),
            metadata=dict(metadata),
        )
