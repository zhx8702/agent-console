from __future__ import annotations

import asyncio

import pytest

from app.kb.chunker import chunk_text, count_tokens
from app.kb.ingest import IngestionService, kb_collection_for
from app.kb.service import InMemoryKBStore, KnowledgeBaseService
from app.kb.vector.memory_store import InMemoryVectorStore
from app.llm.base import EmbedResponse
from app.rag.retriever import HybridRetriever

from ._fake_llm import FakeEmbeddingsProvider, hash_embed


class _FailOnceDeleteVectorStore(InMemoryVectorStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next_delete = False
        self.delete_calls = 0

    async def delete(self, collection: str, ids: list[str]) -> None:
        self.delete_calls += 1
        if self.fail_next_delete:
            self.fail_next_delete = False
            raise RuntimeError("qdrant delete unavailable")
        await super().delete(collection, ids)


class _FailOnceDeleteKBStore(InMemoryKBStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next_delete = False

    async def delete_document(
        self,
        tenant_id: str,
        session_id: str | None,
        doc_id: int,
    ) -> list[int]:
        if self.fail_next_delete:
            self.fail_next_delete = False
            raise RuntimeError("simulated crash before kb db delete")
        return await super().delete_document(tenant_id, session_id, doc_id)


class _BlockingUpsertVectorStore(InMemoryVectorStore):
    def __init__(self) -> None:
        super().__init__()
        self.block_next_upsert = False
        self.upsert_entered = asyncio.Event()
        self.release_upsert = asyncio.Event()

    async def upsert(self, collection, records):  # type: ignore[no-untyped-def]
        if self.block_next_upsert:
            self.block_next_upsert = False
            self.upsert_entered.set()
            await self.release_upsert.wait()
        await super().upsert(collection, records)


def test_chunker_short_text_returns_single_chunk() -> None:
    text = "This is a short paragraph with only a few tokens."
    chunks = chunk_text(text, max_tokens=400)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_chunker_empty_input_returns_empty_list() -> None:
    assert chunk_text("") == []
    assert chunk_text("   \n\n  \n  ") == []


def test_chunker_long_text_is_split() -> None:
    # Build ~1500 tokens of repeated paragraphs.
    para = ("word " * 50).strip()
    text = "\n\n".join([para] * 30)
    assert count_tokens(text) > 600
    chunks = chunk_text(text, max_tokens=200, overlap_tokens=40)
    assert len(chunks) > 1
    for c in chunks:
        # Within a reasonable bound — 200 is an aim not a hard ceiling
        # for the greedy packer, but no chunk should be enormous.
        assert count_tokens(c) <= 260


def test_chunker_applies_overlap_between_regular_paragraph_chunks() -> None:
    paragraphs = [f"section {index} " + ("detail " * 24) for index in range(5)]
    chunks = chunk_text("\n\n".join(paragraphs), max_tokens=45, overlap_tokens=8)
    assert len(chunks) >= 2
    previous_tail = set(chunks[0].split()[-5:])
    assert previous_tail.intersection(chunks[1].split()[:10])


def test_chunker_splits_oversized_single_paragraph() -> None:
    giant_para = "alpha " * 1000
    chunks = chunk_text(giant_para, max_tokens=150, overlap_tokens=30)
    assert len(chunks) > 1
    for c in chunks:
        assert count_tokens(c) <= 160


@pytest.mark.asyncio
async def test_ingest_creates_document_and_chunks_with_vectors() -> None:
    kb = InMemoryKBStore()
    vec = InMemoryVectorStore()
    llm = FakeEmbeddingsProvider()
    ingest = IngestionService(kb, vec, llm, max_tokens_per_chunk=80, chunk_overlap_tokens=10)
    svc = KnowledgeBaseService(kb, vec, ingest)

    long = (
        "Paragraph one. " * 30 + "\n\n" + "Paragraph two. " * 30 + "\n\n" + "Paragraph three. " * 30
    )
    doc_id = await svc.add_text("demo", "My Title", long)
    assert doc_id == 1

    chunks = await kb.list_chunks("demo", None)
    assert len(chunks) >= 2

    # Vectors should exist in the kb_demo collection.
    hits = await vec.search(
        kb_collection_for("demo"), [0.0] * 64 if False else chunks_probe_vec(), top_k=5
    )
    assert hits


def chunks_probe_vec():
    # Use the fake hash embed to generate a matching query vector.
    from tests.unit._fake_llm import hash_embed

    return hash_embed("Paragraph one")


@pytest.mark.asyncio
async def test_ingest_dedupes_by_content_hash() -> None:
    kb = InMemoryKBStore()
    vec = InMemoryVectorStore()
    llm = FakeEmbeddingsProvider()
    ingest = IngestionService(kb, vec, llm)
    svc = KnowledgeBaseService(kb, vec, ingest)

    id1 = await svc.add_text("demo", "T", "Hello world.")
    id2 = await svc.add_text("demo", "T", "Hello world.")
    assert id1 == id2
    docs = await kb.list_documents("demo", None)
    assert len(docs) == 1


@pytest.mark.asyncio
async def test_ingest_rejects_empty_documents() -> None:
    kb = InMemoryKBStore()
    vec = InMemoryVectorStore()
    ingest = IngestionService(kb, vec, FakeEmbeddingsProvider())
    with pytest.raises(ValueError, match="must not be empty"):
        await ingest.add_document("demo", None, "Empty", "   ")
    assert await kb.list_documents("demo", None) == []


@pytest.mark.asyncio
async def test_ingest_rolls_back_document_on_embedding_count_mismatch() -> None:
    class BrokenEmbeddings(FakeEmbeddingsProvider):
        async def embed(self, request):  # type: ignore[override]
            return EmbedResponse(vectors=[], model="broken")

    kb = InMemoryKBStore()
    vec = InMemoryVectorStore()
    ingest = IngestionService(kb, vec, BrokenEmbeddings())
    with pytest.raises(RuntimeError, match="embedding count mismatch"):
        await ingest.add_document("demo", None, "Broken", "Index this content")
    assert await kb.list_documents("demo", None) == []


@pytest.mark.asyncio
async def test_ingest_cleans_partial_vectors_when_upsert_fails() -> None:
    class PartialFailVectorStore(InMemoryVectorStore):
        async def upsert(self, collection, records):  # type: ignore[override]
            await super().upsert(collection, records)
            raise RuntimeError("partial vector failure")

    kb = InMemoryKBStore()
    vec = PartialFailVectorStore()
    ingest = IngestionService(kb, vec, FakeEmbeddingsProvider())

    with pytest.raises(RuntimeError, match="partial vector failure"):
        await ingest.add_document("demo", None, "Broken", "Index this content")

    assert await kb.list_documents("demo", None) == []
    assert await kb.list_chunks("demo", None) == []
    assert await vec.search(kb_collection_for("demo"), chunks_probe_vec(), top_k=5) == []


@pytest.mark.asyncio
async def test_delete_document_clears_vectors() -> None:
    kb = InMemoryKBStore()
    vec = InMemoryVectorStore()
    llm = FakeEmbeddingsProvider()
    ingest = IngestionService(kb, vec, llm)
    svc = KnowledgeBaseService(kb, vec, ingest)

    doc_id = await svc.add_text("demo", "T", "Alpha beta gamma")
    await svc.delete_document("demo", doc_id)
    assert not await kb.list_documents("demo", None)
    hits = await vec.search(kb_collection_for("demo"), [1.0] + [0.0] * 63, top_k=5)
    assert hits == []


@pytest.mark.asyncio
async def test_delete_document_vector_failure_preserves_db_and_retry_converges() -> None:
    kb = InMemoryKBStore()
    vec = _FailOnceDeleteVectorStore()
    ingest = IngestionService(kb, vec, FakeEmbeddingsProvider())
    svc = KnowledgeBaseService(kb, vec, ingest)
    doc_id = await svc.add_text("demo", "T", "Alpha beta gamma")
    original_chunks = await kb.list_chunks("demo", None)
    vec.fail_next_delete = True

    with pytest.raises(RuntimeError, match="qdrant delete unavailable"):
        await svc.delete_document("demo", doc_id)

    assert await kb.get_document("demo", None, doc_id) is not None
    assert await kb.list_chunks("demo", None) == original_chunks
    assert await vec.search(
        kb_collection_for("demo"),
        hash_embed("Alpha beta gamma"),
        top_k=5,
    )

    await svc.delete_document("demo", doc_id)
    assert await kb.get_document("demo", None, doc_id) is None
    assert await kb.list_chunks("demo", None) == []
    assert vec.delete_calls == 2
    assert (
        await vec.search(
            kb_collection_for("demo"),
            hash_embed("Alpha beta gamma"),
            top_k=5,
        )
        == []
    )


@pytest.mark.asyncio
async def test_delete_document_crash_after_vector_cleanup_is_retryable() -> None:
    kb = _FailOnceDeleteKBStore()
    vec = _FailOnceDeleteVectorStore()
    ingest = IngestionService(kb, vec, FakeEmbeddingsProvider())
    svc = KnowledgeBaseService(kb, vec, ingest)
    doc_id = await svc.add_text("demo", "T", "Alpha beta gamma")
    original_chunks = await kb.list_chunks("demo", None)
    kb.fail_next_delete = True

    with pytest.raises(RuntimeError, match="simulated crash"):
        await svc.delete_document("demo", doc_id)

    assert await kb.get_document("demo", None, doc_id) is not None
    assert await kb.list_chunks("demo", None) == original_chunks
    assert (
        await vec.search(
            kb_collection_for("demo"),
            hash_embed("Alpha beta gamma"),
            top_k=5,
        )
        == []
    )

    await svc.delete_document("demo", doc_id)
    assert await kb.get_document("demo", None, doc_id) is None
    assert await kb.list_chunks("demo", None) == []
    assert vec.delete_calls == 2


@pytest.mark.asyncio
async def test_kb_mutations_invalidate_retrieval_cache() -> None:
    invalidated: list[tuple[str, str | None]] = []
    kb = InMemoryKBStore()
    vec = InMemoryVectorStore()
    ingest = IngestionService(
        kb,
        vec,
        FakeEmbeddingsProvider(),
        cache_invalidator=lambda tenant_id, session_id: invalidated.append((tenant_id, session_id)),
    )
    svc = KnowledgeBaseService(kb, vec, ingest)

    doc_id = await svc.add_text("demo", "T", "Alpha beta gamma")
    await svc.delete_document("demo", doc_id)

    assert invalidated == [("demo", ""), ("demo", "")]


@pytest.mark.asyncio
async def test_ingest_separates_global_and_session_scopes() -> None:
    kb = InMemoryKBStore()
    vec = InMemoryVectorStore()
    llm = FakeEmbeddingsProvider()
    ingest = IngestionService(kb, vec, llm)
    svc = KnowledgeBaseService(kb, vec, ingest)

    global_id = await svc.add_text("demo", "退款", "全局说明")
    session_id = await svc.add_text("demo", "退款", "群专属说明", session_id="group-1@chatroom")

    assert global_id != session_id
    assert len(await kb.list_documents("demo", None)) == 1
    assert len(await kb.list_documents("demo", "group-1@chatroom")) == 1

    global_hits = await vec.search(kb_collection_for("demo"), chunks_probe_vec(), top_k=5)
    scoped_hits = await vec.search(
        kb_collection_for("demo", "group-1@chatroom"), chunks_probe_vec(), top_k=5
    )
    assert global_hits
    assert scoped_hits


@pytest.mark.asyncio
async def test_update_document_replaces_content_chunks_and_vectors() -> None:
    kb = InMemoryKBStore()
    vec = InMemoryVectorStore()
    llm = FakeEmbeddingsProvider()
    ingest = IngestionService(kb, vec, llm)
    svc = KnowledgeBaseService(kb, vec, ingest)

    doc_id = await svc.add_text("demo", "Policy", "Old alpha content")
    old_chunks = await kb.list_chunks("demo", None)
    assert old_chunks

    updated_id = await svc.update_document("demo", doc_id, "Policy", "New beta content")
    assert updated_id == doc_id

    doc = await svc.get_document("demo", doc_id)
    assert doc is not None
    assert doc.content == "New beta content"
    chunks = await kb.list_chunks("demo", None)
    assert len(chunks) == 1
    assert chunks[0].content == "New beta content"
    assert chunks[0].id != old_chunks[0].id

    old_hits = await vec.search(kb_collection_for("demo"), chunks_probe_vec(), top_k=10)
    assert all(int(hit.payload["chunk_id"]) != old_chunks[0].id for hit in old_hits)


@pytest.mark.asyncio
async def test_delete_waits_for_inflight_update_and_leaves_no_orphan_vectors() -> None:
    kb = InMemoryKBStore()
    vec = _BlockingUpsertVectorStore()
    ingest = IngestionService(kb, vec, FakeEmbeddingsProvider())
    svc = KnowledgeBaseService(kb, vec, ingest)
    doc_id = await svc.add_text("demo", "Policy", "Old content")

    vec.block_next_upsert = True
    update_task = asyncio.create_task(
        svc.update_document("demo", doc_id, "Policy", "Replacement content")
    )
    await asyncio.wait_for(vec.upsert_entered.wait(), timeout=1)
    delete_task = asyncio.create_task(svc.delete_document("demo", doc_id))
    await asyncio.sleep(0)
    assert delete_task.done() is False

    vec.release_upsert.set()
    updated, _ = await asyncio.gather(update_task, delete_task)

    assert updated == doc_id
    assert await kb.get_document("demo", None, doc_id) is None
    assert (
        await vec.search(
            kb_collection_for("demo"),
            hash_embed("Replacement content"),
            top_k=5,
        )
        == []
    )


@pytest.mark.asyncio
async def test_update_embedding_failure_preserves_current_document() -> None:
    class ToggleEmbeddings(FakeEmbeddingsProvider):
        fail = False

        async def embed(self, request):  # type: ignore[override]
            if self.fail:
                raise RuntimeError("embedding unavailable")
            return await super().embed(request)

    kb = InMemoryKBStore()
    vec = InMemoryVectorStore()
    llm = ToggleEmbeddings()
    ingest = IngestionService(kb, vec, llm)
    svc = KnowledgeBaseService(kb, vec, ingest)
    doc_id = await svc.add_text("demo", "Policy", "Old searchable content")
    old_chunks = await kb.list_chunks("demo", None)

    llm.fail = True
    with pytest.raises(RuntimeError, match="embedding unavailable"):
        await svc.update_document("demo", doc_id, "Policy", "New content")

    doc = await svc.get_document("demo", doc_id)
    chunks = await kb.list_chunks("demo", None)
    assert doc is not None and doc.content == "Old searchable content"
    assert [chunk.id for chunk in chunks] == [chunk.id for chunk in old_chunks]


@pytest.mark.asyncio
async def test_update_vector_failure_rolls_back_document_and_new_chunks() -> None:
    class ToggleVectorStore(InMemoryVectorStore):
        fail_upsert = False

        async def upsert(self, collection, records):  # type: ignore[override]
            await super().upsert(collection, records)
            if self.fail_upsert:
                raise RuntimeError("qdrant partial upsert")

    kb = InMemoryKBStore()
    vec = ToggleVectorStore()
    llm = FakeEmbeddingsProvider()
    svc = KnowledgeBaseService(kb, vec, IngestionService(kb, vec, llm))
    doc_id = await svc.add_text("demo", "Policy", "Old searchable content")
    old_chunks = await kb.list_chunks("demo", None)

    vec.fail_upsert = True
    with pytest.raises(RuntimeError, match="qdrant partial upsert"):
        await svc.update_document("demo", doc_id, "Policy", "New uncommitted content")

    doc = await svc.get_document("demo", doc_id)
    chunks = await kb.list_chunks("demo", None)
    assert doc is not None and doc.content == "Old searchable content"
    assert [chunk.id for chunk in chunks] == [chunk.id for chunk in old_chunks]


@pytest.mark.asyncio
async def test_retriever_filters_stale_vectors_when_cleanup_is_delayed() -> None:
    class CleanupFailVectorStore(InMemoryVectorStore):
        fail_delete = False

        async def delete(self, collection, ids):  # type: ignore[override]
            if self.fail_delete:
                raise RuntimeError("qdrant cleanup unavailable")
            await super().delete(collection, ids)

    kb = InMemoryKBStore()
    vec = CleanupFailVectorStore()
    llm = FakeEmbeddingsProvider()
    svc = KnowledgeBaseService(kb, vec, IngestionService(kb, vec, llm))
    doc_id = await svc.add_text("demo", "Policy", "Old alpha policy")
    old_chunk_id = (await kb.list_chunks("demo", None))[0].id
    vec.fail_delete = True
    await svc.update_document("demo", doc_id, "Policy", "New alpha policy")

    async def chunk_source(tenant_id: str, session_id: str | None):
        return await kb.list_chunks(tenant_id, session_id)

    hits = await HybridRetriever(vec, chunk_source, llm).retrieve(
        "demo", "alpha policy", top_k=5
    )
    assert hits
    assert all(hit.chunk_id != old_chunk_id for hit in hits)
    assert hits[0].content == "New alpha policy"
    admin_hits = await svc.search_documents("demo", "alpha policy", top_k=5)
    assert admin_hits
    assert all(hit.chunk_id != old_chunk_id for hit in admin_hits)


@pytest.mark.asyncio
async def test_search_documents_returns_vector_hits() -> None:
    kb = InMemoryKBStore()
    vec = InMemoryVectorStore()
    llm = FakeEmbeddingsProvider()
    ingest = IngestionService(kb, vec, llm)
    svc = KnowledgeBaseService(kb, vec, ingest)

    doc_id = await svc.add_text("demo", "Refund Policy", "Refunds are available within seven days.")
    hits = await svc.search_documents("demo", "Refunds", top_k=3)

    assert hits
    assert hits[0].doc_id == doc_id
    assert hits[0].title == "Refund Policy"
    assert "Refunds" in hits[0].content
