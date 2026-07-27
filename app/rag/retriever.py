"""
Hybrid retriever = vector ANN + BM25 keyword match.

For MVP the BM25 index is built from all chunks for the given tenant (pulled
via a ``chunk_source`` callable). In production this should be replaced by an
inverted index (e.g. OpenSearch) or by BM25-lite via pgvector.

A simple in-memory LRU cache keyed by tenant_id keeps the BM25 index warm for
60 seconds so that high-frequency retrievals don't rebuild the tokenized corpus
on every call.
"""
from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from rank_bm25 import BM25Okapi

from app.common.config import Settings, get_settings
from app.common.ids import new_trace_id
from app.kb.ingest import kb_collection_for
from app.kb.scope import normalize_scope_session_id, scope_kind
from app.kb.vector.base import VectorStore
from app.llm.base import EmbedRequest, LLMProvider


@dataclass
class RetrievalHit:
    chunk_id: int
    doc_id: int
    content: str
    score: float
    title: str | None = None
    scope: str = "global"
    session_id: str | None = None
    source: str | None = None
    url: str | None = None
    metadata: dict[str, Any] | None = None


ChunkSource = Callable[[str, str | None], Awaitable[list[Any]]]


_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_-]*|[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+", re.I)


def _tokenize(text: str) -> list[str]:
    """Tokenize ASCII words and CJK character n-grams.

    Per-character CJK BM25 makes common characters look relevant.  Bigrams and
    trigrams retain enough phrase semantics without adding a mandatory native
    segmentation dependency to the service image.
    """
    if not text:
        return []
    tokens: list[str] = []
    for match in _WORD_RE.finditer(text.lower()):
        value = match.group(0)
        if value.isascii():
            tokens.append(value)
            continue
        if len(value) == 1:
            tokens.append(value)
            continue
        tokens.extend(value[i : i + 2] for i in range(len(value) - 1))
        if len(value) >= 3:
            tokens.extend(value[i : i + 3] for i in range(len(value) - 2))
    return tokens


def _min_max_normalize(scores: list[float]) -> list[float]:
    if not scores:
        return []
    lo = min(scores)
    hi = max(scores)
    if hi - lo < 1e-9:
        return [0.0 for _ in scores]
    return [(s - lo) / (hi - lo) for s in scores]


def _overlap_ratio(query_tokens: list[str], document_tokens: list[str]) -> float:
    query_set = set(query_tokens)
    if not query_set:
        return 0.0
    return len(query_set.intersection(document_tokens)) / len(query_set)


@dataclass
class _BM25Cache:
    built_at: float
    bm25: BM25Okapi
    chunks: list[Any]
    tokens: list[list[str]]


class HybridRetriever:
    """Vector + BM25 hybrid retrieval with pluggable reranking."""

    def __init__(
        self,
        vector_store: VectorStore,
        chunk_source: ChunkSource,
        llm_provider: LLMProvider,
        settings: Settings | None = None,
        *,
        bm25_ttl_seconds: float = 60.0,
        top_k_vector: int = 20,
        top_k_bm25: int = 20,
        vector_weight: float = 0.6,
        bm25_weight: float = 0.4,
        vector_relevance_threshold: float | None = None,
        keyword_overlap_threshold: float | None = None,
    ) -> None:
        self._vector = vector_store
        self._chunk_source = chunk_source
        self._llm = llm_provider
        self._settings = settings or get_settings()
        self._ttl = bm25_ttl_seconds
        self._top_k_vector = top_k_vector
        self._top_k_bm25 = top_k_bm25
        self._vector_weight = vector_weight
        self._bm25_weight = bm25_weight
        self._bm25_cache: dict[str, _BM25Cache] = {}
        self._vector_relevance_threshold = (
            float(self._settings.rag_vector_relevance_threshold)
            if vector_relevance_threshold is None
            else float(vector_relevance_threshold)
        )
        self._keyword_overlap_threshold = (
            float(self._settings.rag_keyword_overlap_threshold)
            if keyword_overlap_threshold is None
            else float(keyword_overlap_threshold)
        )

    def rerank(self, query: str, hits: list[RetrievalHit]) -> list[RetrievalHit]:
        """Second-stage lexical/phrase reranker over the fused candidates.

        The first stage already combines vector and BM25 ranks. This stage
        rewards actual query coverage, title matches and exact phrases. It is
        deterministic, cheap enough for every message, and can later be
        replaced by a learned reranker behind the same hook.
        """
        if not hits or not bool(self._settings.rag_rerank_enabled):
            return hits
        query_tokens = _tokenize(query)
        normalized_query = "".join(str(query or "").lower().split())
        base_order = sorted(hits, key=lambda hit: hit.score, reverse=True)
        for rank, hit in enumerate(base_order, start=1):
            content_tokens = _tokenize(hit.content)
            title_tokens = _tokenize(hit.title or "")
            coverage = _overlap_ratio(query_tokens, content_tokens)
            title_coverage = _overlap_ratio(query_tokens, title_tokens)
            normalized_content = "".join(str(hit.content or "").lower().split())
            phrase = 1.0 if normalized_query and normalized_query in normalized_content else 0.0
            reciprocal_rank = 1.0 / rank
            scope_bonus = 0.03 if hit.scope == "session" else 0.0
            hit.score = (
                reciprocal_rank * 0.45
                + coverage * 0.32
                + title_coverage * 0.15
                + phrase * 0.05
                + scope_bonus
            )
        return sorted(base_order, key=lambda hit: hit.score, reverse=True)

    def _cache_key(self, tenant_id: str, session_id: str | None) -> str:
        return f"{tenant_id}::{normalize_scope_session_id(session_id)}"

    async def _get_bm25(self, tenant_id: str, session_id: str | None) -> _BM25Cache | None:
        cache_key = self._cache_key(tenant_id, session_id)
        cache = self._bm25_cache.get(cache_key)
        now = time.monotonic()
        if cache and now - cache.built_at < self._ttl:
            return cache
        chunks = await self._chunk_source(tenant_id, session_id)
        if not chunks:
            self._bm25_cache.pop(cache_key, None)
            return None
        tokens = [_tokenize(getattr(c, "content", "") or "") for c in chunks]
        # rank_bm25 requires at least one non-empty doc.
        if not any(tokens):
            return None
        cache = _BM25Cache(built_at=now, bm25=BM25Okapi(tokens), chunks=list(chunks), tokens=tokens)
        self._bm25_cache[cache_key] = cache
        return cache

    async def _embed_query(self, tenant_id: str, query: str) -> list[float]:
        resp = await self._llm.embed(
            EmbedRequest(
                tenant_id=tenant_id,
                trace_id=new_trace_id(),
                model=self._settings.llm_embed_model,
                texts=[query],
            )
        )
        if not resp.vectors:
            return []
        return resp.vectors[0]

    async def _retrieve_scope(
        self,
        tenant_id: str,
        session_id: str | None,
        query: str,
        top_k: int,
        *,
        query_vector: list[float] | None = None,
    ) -> tuple[list[RetrievalHit], bool]:
        normalized_session_id = normalize_scope_session_id(session_id)
        scope = scope_kind(normalized_session_id)

        # Qdrant and PostgreSQL cannot share a transaction. The committed DB
        # chunk ids are therefore the authoritative cutover set: stale vectors
        # and vectors from a failed DB commit are ignored even when Qdrant
        # cleanup is delayed.
        try:
            authoritative_chunks = await self._chunk_source(tenant_id, normalized_session_id)
        except Exception:
            authoritative_chunks = []
        valid_chunk_ids = {str(getattr(chunk, "id", "")) for chunk in authoritative_chunks}

        vec_scores: dict[str, float] = {}
        vec_payloads: dict[str, dict[str, Any]] = {}
        if query_vector is None:
            try:
                qvec = await self._embed_query(tenant_id, query)
            except Exception:
                qvec = []
        else:
            qvec = query_vector
        if qvec:
            try:
                vec_hits = await self._vector.search(
                    kb_collection_for(tenant_id, normalized_session_id),
                    qvec,
                    top_k=self._top_k_vector,
                )
            except Exception:
                vec_hits = []
            for h in vec_hits:
                key = str(h.payload.get("chunk_id") or h.id)
                if key not in valid_chunk_ids:
                    continue
                score = float(h.score)
                vec_scores[key] = score
                vec_payloads[key] = h.payload

        bm25_scores: dict[str, float] = {}
        bm25_payloads: dict[str, dict[str, Any]] = {}
        cache = await self._get_bm25(tenant_id, normalized_session_id)
        if cache is not None:
            tok_q = _tokenize(query)
            if tok_q:
                scores = cache.bm25.get_scores(tok_q)
                ranked = sorted(enumerate(scores), key=lambda p: p[1], reverse=True)
                ranked = ranked[: self._top_k_bm25]
                for idx, s in ranked:
                    overlap = _overlap_ratio(tok_q, cache.tokens[idx])
                    # BM25 IDF can be zero/negative in very small corpora even
                    # for an exact term. Preserve explicit lexical overlap as a
                    # bounded fallback score.
                    if s <= 0 and overlap <= 0:
                        continue
                    chunk = cache.chunks[idx]
                    cid = str(chunk.id)
                    bm25_scores[cid] = max(float(s), overlap)
                    bm25_payloads[cid] = {
                        "chunk_id": chunk.id,
                        "doc_id": chunk.doc_id,
                        "content": getattr(chunk, "content", ""),
                        "title": (getattr(chunk, "meta", None) or {}).get("title"),
                        "metadata": getattr(chunk, "meta", None) or {},
                    }

        keys = (set(vec_scores) | set(bm25_scores)).intersection(valid_chunk_ids)
        if not keys:
            return [], False

        # Reciprocal-rank fusion is robust when the vector and BM25 backends
        # expose scores on unrelated scales (and for one-candidate result sets).
        vector_rank = {
            key: rank
            for rank, (key, _score) in enumerate(
                sorted(vec_scores.items(), key=lambda item: item[1], reverse=True),
                start=1,
            )
        }
        bm25_rank = {
            key: rank
            for rank, (key, _score) in enumerate(
                sorted(bm25_scores.items(), key=lambda item: item[1], reverse=True),
                start=1,
            )
        }

        query_tokens = _tokenize(query)
        chunk_tokens_by_id: dict[str, list[str]] = {}
        if cache is not None:
            chunk_tokens_by_id = {
                str(chunk.id): tokens
                for chunk, tokens in zip(cache.chunks, cache.tokens, strict=False)
            }
        vector_relevance_enabled = (
            str(getattr(self._settings, "llm_embed_provider", "") or "")
            .strip()
            .lower()
            != "fake"
        )

        merged: list[RetrievalHit] = []
        for key in keys:
            score = 0.0
            if key in vector_rank:
                score += self._vector_weight / (60.0 + vector_rank[key])
            if key in bm25_rank:
                score += self._bm25_weight / (60.0 + bm25_rank[key])
            payload = vec_payloads.get(key) or bm25_payloads.get(key) or {}
            content = str(payload.get("content") or "")
            vector_score = float(vec_scores.get(key, 0.0) or 0.0)
            keyword_overlap = _overlap_ratio(
                query_tokens,
                chunk_tokens_by_id.get(key) or _tokenize(content),
            )
            relevant = (
                (
                    vector_relevance_enabled
                    and vector_score >= self._vector_relevance_threshold
                )
                or keyword_overlap >= self._keyword_overlap_threshold
            )
            if not relevant:
                continue
            doc_id = int(payload.get("doc_id") or 0)
            chunk_id = int(payload.get("chunk_id") or key or 0)
            title = payload.get("title")
            metadata = (
                dict(payload.get("metadata"))
                if isinstance(payload.get("metadata"), dict)
                else {}
            )
            metadata["retrieval"] = {
                "vector_score": vector_score,
                "keyword_overlap": keyword_overlap,
                "vector_relevance_enabled": vector_relevance_enabled,
            }
            merged.append(
                RetrievalHit(
                    chunk_id=chunk_id,
                    doc_id=doc_id,
                    content=content,
                    score=score,
                    title=title,
                    scope=scope,
                    session_id=normalized_session_id or None,
                    source=payload.get("source"),
                    url=payload.get("url"),
                    metadata=metadata,
                )
            )
        if not merged:
            return [], False
        merged.sort(key=lambda h: h.score, reverse=True)

        seen: set[int] = set()
        deduped: list[RetrievalHit] = []
        for h in merged:
            if h.chunk_id in seen:
                continue
            seen.add(h.chunk_id)
            deduped.append(h)
            if len(deduped) >= top_k:
                break

        return self.rerank(query, deduped), bool(deduped)

    async def retrieve(
        self, tenant_id: str, query: str, top_k: int = 5, session_id: str | None = None
    ) -> list[RetrievalHit]:
        query = (query or "").strip()
        if not query:
            return []

        normalized_session_id = normalize_scope_session_id(session_id)
        try:
            query_vector = await self._embed_query(tenant_id, query)
        except Exception:
            query_vector = []
        scoped_hits: list[RetrievalHit] = []
        if normalized_session_id:
            scoped_hits, scoped_ok = await self._retrieve_scope(
                tenant_id,
                normalized_session_id,
                query,
                max(top_k * 2, top_k),
                query_vector=query_vector,
            )
            if not scoped_ok:
                scoped_hits = []

        global_hits, global_ok = await self._retrieve_scope(
            tenant_id,
            None,
            query,
            max(top_k * 2, top_k),
            query_vector=query_vector,
        )
        if not global_ok:
            global_hits = []

        merged = [*scoped_hits, *global_hits]
        merged = self.rerank(query, merged)
        merged.sort(
            key=lambda hit: (hit.score, hit.scope == "session"),
            reverse=True,
        )
        seen: set[tuple[int, int]] = set()
        output: list[RetrievalHit] = []
        for hit in merged:
            key = (hit.doc_id, hit.chunk_id)
            if key in seen:
                continue
            seen.add(key)
            output.append(hit)
            if len(output) >= top_k:
                break
        return output

    def invalidate(self, tenant_id: str | None = None, session_id: str | None = None) -> None:
        if tenant_id is None:
            self._bm25_cache.clear()
            return
        if session_id is not None:
            self._bm25_cache.pop(self._cache_key(tenant_id, session_id), None)
            return
        prefix = f"{tenant_id}::"
        for key in list(self._bm25_cache):
            if key.startswith(prefix):
                self._bm25_cache.pop(key, None)
