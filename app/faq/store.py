"""
FAQStore — CRUD wrapper over the FAQ SQLAlchemy model.

Data access goes through a ``FAQRepository`` Protocol so tests can inject an
in-memory implementation. Both FAQEngine (for reads of the answer payload) and
admin endpoints (for CRUD) use the same store.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import delete, select, text

from app.common.logging import get_logger
from app.kb.scope import normalize_scope_session_id, scoped_collection_name
from app.models.faq import FAQ

log = get_logger(__name__)


@dataclass
class FAQRecord:
    id: int
    tenant_id: str
    question: str
    answer: str
    session_id: str = ""
    variants: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    version: int = 1
    status: str = "published"


FAQ_COLLECTION_PREFIX = "faq_"
FAQ_MUTATION_LOCK_KEY = "faq-mutations"


def faq_collection_for(tenant_id: str, session_id: str | None = None) -> str:
    return scoped_collection_name(FAQ_COLLECTION_PREFIX, tenant_id, session_id)


class FAQRepository(Protocol):
    def resource_lock(
        self,
        tenant_id: str,
        session_id: str | None,
        faq_id: int | str,
    ) -> AbstractAsyncContextManager[None]: ...

    async def insert(
        self,
        *,
        tenant_id: str,
        session_id: str | None,
        question: str,
        answer: str,
        variants: list[str] | None,
        tags: list[str] | None,
    ) -> FAQRecord: ...

    async def update(
        self,
        *,
        faq_id: int,
        tenant_id: str,
        session_id: str | None,
        question: str | None = None,
        answer: str | None = None,
        variants: list[str] | None = None,
        tags: list[str] | None = None,
        status: str | None = None,
    ) -> FAQRecord | None: ...

    async def delete(self, tenant_id: str, session_id: str | None, faq_id: int) -> bool: ...

    async def get(
        self, tenant_id: str, session_id: str | None, faq_id: int
    ) -> FAQRecord | None: ...

    async def list(
        self, tenant_id: str, session_id: str | None, limit: int = 100, offset: int = 0
    ) -> list[FAQRecord]: ...


class InMemoryFAQRepository:
    """Unit-test repository. Not thread-safe; not for production use."""

    name = "memory"

    def __init__(self) -> None:
        self._rows: dict[int, FAQRecord] = {}
        self._next_id = 1
        self._resource_locks: dict[str, asyncio.Lock] = {}

    @asynccontextmanager
    async def resource_lock(
        self,
        tenant_id: str,
        session_id: str | None,
        faq_id: int | str,
    ) -> AsyncIterator[None]:
        scope = f"{tenant_id}:{normalize_scope_session_id(session_id)}:{faq_id}"
        lock = self._resource_locks.setdefault(scope, asyncio.Lock())
        async with lock:
            yield

    async def insert(
        self,
        *,
        tenant_id: str,
        session_id: str | None,
        question: str,
        answer: str,
        variants: list[str] | None,
        tags: list[str] | None,
    ) -> FAQRecord:
        faq_id = self._next_id
        self._next_id += 1
        rec = FAQRecord(
            id=faq_id,
            tenant_id=tenant_id,
            session_id=normalize_scope_session_id(session_id),
            question=question,
            answer=answer,
            variants=list(variants or []),
            tags=list(tags or []),
        )
        self._rows[faq_id] = rec
        return rec

    async def update(
        self,
        *,
        faq_id: int,
        tenant_id: str,
        session_id: str | None,
        question: str | None = None,
        answer: str | None = None,
        variants: list[str] | None = None,
        tags: list[str] | None = None,
        status: str | None = None,
    ) -> FAQRecord | None:
        rec = self._rows.get(faq_id)
        if (
            rec is None
            or rec.tenant_id != tenant_id
            or rec.session_id != normalize_scope_session_id(session_id)
        ):
            return None
        if question is not None:
            rec.question = question
        if answer is not None:
            rec.answer = answer
        if variants is not None:
            rec.variants = list(variants)
        if tags is not None:
            rec.tags = list(tags)
        if status is not None:
            rec.status = status
        rec.version += 1
        return rec

    async def delete(self, tenant_id: str, session_id: str | None, faq_id: int) -> bool:
        rec = self._rows.get(faq_id)
        if (
            rec is None
            or rec.tenant_id != tenant_id
            or rec.session_id != normalize_scope_session_id(session_id)
        ):
            return False
        del self._rows[faq_id]
        return True

    async def get(self, tenant_id: str, session_id: str | None, faq_id: int) -> FAQRecord | None:
        rec = self._rows.get(faq_id)
        if (
            rec is None
            or rec.tenant_id != tenant_id
            or rec.session_id != normalize_scope_session_id(session_id)
        ):
            return None
        return rec

    async def list(
        self, tenant_id: str, session_id: str | None, limit: int = 100, offset: int = 0
    ) -> list[FAQRecord]:
        normalized = normalize_scope_session_id(session_id)
        rows = [
            r
            for r in self._rows.values()
            if r.tenant_id == tenant_id and r.session_id == normalized
        ]
        rows.sort(key=lambda r: r.id, reverse=True)
        return rows[offset : offset + limit]


class SQLAlchemyFAQRepository:
    """Production FAQ repository backed by the FAQ SQLAlchemy model."""

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
        faq_id: int | str,
    ) -> AsyncIterator[None]:
        """Hold a per-FAQ mutation lock across SQL and vector-store effects."""

        scope = f"faq:{tenant_id}:{normalize_scope_session_id(session_id)}:{faq_id}"
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

    async def insert(
        self,
        *,
        tenant_id: str,
        session_id: str | None,
        question: str,
        answer: str,
        variants: list[str] | None,
        tags: list[str] | None,
    ) -> FAQRecord:
        normalized = normalize_scope_session_id(session_id)
        async with self._session_factory() as session:
            row = FAQ(
                tenant_id=tenant_id,
                session_id=normalized,
                question=question,
                answer=answer,
                variants=list(variants) if variants else None,
                tags=list(tags) if tags else None,
            )
            session.add(row)
            await session.flush()
            return _to_record(row)

    async def update(
        self,
        *,
        faq_id: int,
        tenant_id: str,
        session_id: str | None,
        question: str | None = None,
        answer: str | None = None,
        variants: list[str] | None = None,
        tags: list[str] | None = None,
        status: str | None = None,
    ) -> FAQRecord | None:
        normalized = normalize_scope_session_id(session_id)
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(FAQ).where(
                        FAQ.id == faq_id,
                        FAQ.tenant_id == tenant_id,
                        FAQ.session_id == normalized,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            if question is not None:
                row.question = question
            if answer is not None:
                row.answer = answer
            if variants is not None:
                row.variants = list(variants)
            if tags is not None:
                row.tags = list(tags)
            if status is not None:
                row.status = status
            row.version = (row.version or 1) + 1
            await session.flush()
            return _to_record(row)

    async def delete(self, tenant_id: str, session_id: str | None, faq_id: int) -> bool:
        normalized = normalize_scope_session_id(session_id)
        async with self._session_factory() as session:
            result = await session.execute(
                delete(FAQ).where(
                    FAQ.id == faq_id,
                    FAQ.tenant_id == tenant_id,
                    FAQ.session_id == normalized,
                )
            )
            return bool(result.rowcount)

    async def get(self, tenant_id: str, session_id: str | None, faq_id: int) -> FAQRecord | None:
        normalized = normalize_scope_session_id(session_id)
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(FAQ).where(
                        FAQ.id == faq_id,
                        FAQ.tenant_id == tenant_id,
                        FAQ.session_id == normalized,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return _to_record(row)

    async def list(
        self, tenant_id: str, session_id: str | None, limit: int = 100, offset: int = 0
    ) -> list[FAQRecord]:
        normalized = normalize_scope_session_id(session_id)
        async with self._session_factory() as session:
            stmt = (
                select(FAQ)
                .where(FAQ.tenant_id == tenant_id, FAQ.session_id == normalized)
                .order_by(FAQ.id.desc())
                .limit(limit)
                .offset(offset)
            )
            rows = (await session.execute(stmt)).scalars().all()
            return [_to_record(r) for r in rows]


def _to_record(row: FAQ) -> FAQRecord:
    return FAQRecord(
        id=int(row.id),
        tenant_id=row.tenant_id,
        session_id=normalize_scope_session_id(getattr(row, "session_id", "")),
        question=row.question,
        answer=row.answer,
        variants=list(row.variants or []),
        tags=list(row.tags or []),
        version=int(row.version or 1),
        status=row.status or "published",
    )


class FAQStore:
    """High-level FAQ store tying the repository to the vector index."""

    def __init__(
        self,
        repository: FAQRepository,
        vector_store: Any,
        llm_provider: Any,
        *,
        embed_model: str = "voyage-3",
    ) -> None:
        self._repo = repository
        self._vector = vector_store
        self._llm = llm_provider
        self._embed_model = embed_model

    async def _embed(self, tenant_id: str, texts: list[str]) -> list[list[float]]:
        from app.common.ids import new_trace_id
        from app.llm.base import EmbedRequest

        resp = await self._llm.embed(
            EmbedRequest(
                tenant_id=tenant_id,
                trace_id=new_trace_id(),
                model=self._embed_model,
                texts=texts,
            )
        )
        return list(resp.vectors)

    async def create(
        self,
        tenant_id: str,
        question: str,
        answer: str,
        variants: list[str] | None = None,
        tags: list[str] | None = None,
        session_id: str | None = None,
    ) -> FAQRecord:
        # A scope-wide lock also covers the short interval between the SQL
        # insert assigning an id and the first vector upsert.
        async with self._repo.resource_lock(
            tenant_id,
            session_id,
            FAQ_MUTATION_LOCK_KEY,
        ):
            rec = await self._repo.insert(
                tenant_id=tenant_id,
                session_id=session_id,
                question=question,
                answer=answer,
                variants=variants,
                tags=tags,
            )
            try:
                await self._index(tenant_id, rec)
            except Exception as exc:
                return await self._mark_index_degraded(tenant_id, rec, exc)
            return rec

    async def update(
        self,
        tenant_id: str,
        faq_id: int,
        *,
        question: str | None = None,
        answer: str | None = None,
        variants: list[str] | None = None,
        tags: list[str] | None = None,
        status: str | None = None,
        session_id: str | None = None,
    ) -> FAQRecord | None:
        async with self._repo.resource_lock(
            tenant_id,
            session_id,
            FAQ_MUTATION_LOCK_KEY,
        ):
            rec = await self._repo.update(
                faq_id=faq_id,
                tenant_id=tenant_id,
                session_id=session_id,
                question=question,
                answer=answer,
                variants=variants,
                tags=tags,
                status=status,
            )
            if rec is None:
                return None
            # Remove stale vectors for this faq, then reindex while the
            # cross-process resource lock prevents delete/update reordering.
            try:
                await self._remove_vectors(
                    tenant_id,
                    rec.session_id,
                    faq_id,
                    max_variants=64,
                )
                await self._index(tenant_id, rec)
            except Exception as exc:
                return await self._mark_index_degraded(tenant_id, rec, exc)
            return rec

    async def delete(self, tenant_id: str, faq_id: int, session_id: str | None = None) -> bool:
        normalized = normalize_scope_session_id(session_id)
        async with self._repo.resource_lock(
            tenant_id,
            normalized,
            FAQ_MUTATION_LOCK_KEY,
        ):
            record = await self._repo.get(tenant_id, normalized, faq_id)

            # Qdrant is not part of the PostgreSQL transaction. Delete vectors
            # first so a transport failure leaves the authoritative FAQ row
            # available for a safe retry. This cleanup also runs for an absent
            # SQL row, allowing retries to remove vectors left by an interrupted
            # legacy mutation.
            await self._remove_vectors(
                tenant_id,
                normalized,
                faq_id,
                max_variants=max(64, len(record.variants) if record else 0),
            )
            if record is None:
                return False
            return await self._repo.delete(tenant_id, normalized, faq_id)

    async def list(
        self, tenant_id: str, session_id: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[FAQRecord]:
        return await self._repo.list(tenant_id, session_id, limit=limit, offset=offset)

    async def _index(self, tenant_id: str, rec: FAQRecord) -> None:
        texts = [rec.question, *(rec.variants or [])]
        vectors = await self._embed(tenant_id, texts)
        if not vectors:
            return
        dim = len(vectors[0])
        collection = faq_collection_for(tenant_id, rec.session_id)
        await self._vector.ensure_collection(collection, dim)
        from app.kb.vector.base import VectorRecord

        records: list[VectorRecord] = []
        for i, vec in enumerate(vectors):
            suffix = "q" if i == 0 else f"v{i}"
            records.append(
                VectorRecord(
                    id=f"{rec.id}:{suffix}",
                    vector=vec,
                    payload={
                        "faq_id": rec.id,
                        "tenant_id": tenant_id,
                        "session_id": rec.session_id,
                        "question": texts[i],
                        "answer": rec.answer,
                        "variant_idx": 0 if i == 0 else i,
                    },
                )
            )
        await self._vector.upsert(collection, records)

    async def _mark_index_degraded(
        self, tenant_id: str, rec: FAQRecord, exc: Exception
    ) -> FAQRecord:
        log.warning(
            "faq.index_degraded",
            tenant_id=tenant_id,
            faq_id=rec.id,
            session_id=normalize_scope_session_id(rec.session_id) or None,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        await self._remove_vectors(tenant_id, rec.session_id, rec.id, max_variants=16)
        try:
            degraded = await self._repo.update(
                faq_id=rec.id,
                tenant_id=tenant_id,
                session_id=rec.session_id,
                status="degraded",
            )
        except Exception as status_exc:
            log.warning(
                "faq.index_degraded_status_update_failed",
                tenant_id=tenant_id,
                faq_id=rec.id,
                error_type=type(status_exc).__name__,
                error=str(status_exc),
            )
            degraded = None
        return degraded or FAQRecord(
            id=rec.id,
            tenant_id=rec.tenant_id,
            session_id=rec.session_id,
            question=rec.question,
            answer=rec.answer,
            variants=list(rec.variants or []),
            tags=list(rec.tags or []),
            version=rec.version,
            status="degraded",
        )

    async def _remove_vectors(
        self, tenant_id: str, session_id: str | None, faq_id: int, *, max_variants: int
    ) -> None:
        collection = faq_collection_for(tenant_id, session_id)
        ids = [f"{faq_id}:q"] + [f"{faq_id}:v{i}" for i in range(1, max_variants + 1)]
        # Each VectorStore implementation owns the idempotent
        # missing-collection behavior. Connectivity/auth/service failures
        # must propagate so callers never delete the SQL row first.
        await self._vector.delete(collection, ids)
