"""Load precomputed KB vectors into Postgres + Qdrant inside the API container."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.common.config import get_settings
from app.common.hashing import stable_hash
from app.kb.ingest import kb_collection_for
from app.kb.scope import normalize_scope_session_id
from app.kb.service import SQLAlchemyKBStore
from app.kb.vector.base import VectorRecord
from app.kb.vector.qdrant_store import QdrantVectorStore


def _vector_records(
    *,
    tenant_id: str,
    session_id: str,
    doc_id: int,
    title: str,
    source: str,
    metadata: dict,
    chunks: list[str],
    vectors: list[list[float]],
    chunk_ids: list[int],
) -> list[VectorRecord]:
    return [
        VectorRecord(
            id=str(chunk_ids[i]),
            vector=vectors[i],
            payload={
                "chunk_id": chunk_ids[i],
                "doc_id": doc_id,
                "tenant_id": tenant_id,
                "session_id": session_id,
                "title": title,
                "source": source,
                "url": None,
                "content": chunks[i],
                "chunk_idx": i,
                "metadata": metadata,
            },
        )
        for i in range(len(chunks))
    ]


async def load_one(
    *,
    store: SQLAlchemyKBStore,
    vector: QdrantVectorStore,
    tenant_id: str,
    session_id: str,
    row: dict,
) -> str:
    title = str(row["title"])
    content = str(row["content"])
    source = str(row.get("source") or "maoxuan-official")
    metadata = dict(row.get("metadata") or {})
    article_id = str(row.get("article_id") or metadata.get("article_id") or "")
    if article_id:
        metadata["article_id"] = article_id
    digest = stable_hash(
        f"{normalize_scope_session_id(session_id)}\n{(title or '')}\n{(content or '')}"
    )
    existing = await store.find_document_by_hash(tenant_id, session_id, digest)
    if existing is not None:
        return f"skip {article_id} doc={existing.id}"

    chunks = [str(item["content"]) for item in row["chunks"]]
    vectors = [list(item["vector"]) for item in row["chunks"]]
    if not chunks:
        return f"empty {article_id}"
    collection = kb_collection_for(tenant_id, session_id)
    await vector.ensure_collection(collection, len(vectors[0]))
    doc_id = await store.insert_document(
        tenant_id=tenant_id,
        session_id=session_id,
        title=title,
        content=content,
        source=source,
        url=None,
        content_hash=digest,
        meta=metadata,
    )
    rows = [
        (idx, chunk, int(item.get("token_count") or 0), {"title": title})
        for idx, (chunk, item) in enumerate(zip(chunks, row["chunks"], strict=True))
    ]
    chunk_ids = await store.insert_chunks(tenant_id, session_id, doc_id, rows)
    records = _vector_records(
        tenant_id=tenant_id,
        session_id=session_id,
        doc_id=doc_id,
        title=title,
        source=source,
        metadata=metadata,
        chunks=chunks,
        vectors=vectors,
        chunk_ids=chunk_ids,
    )
    await vector.upsert(collection, records)
    return f"ok {article_id} doc={doc_id} chunks={len(chunks)}"


async def async_main(args: argparse.Namespace) -> int:
    settings = get_settings()
    store = SQLAlchemyKBStore()
    vector = QdrantVectorStore(settings.qdrant_url, settings.qdrant_api_key or None)
    created = 0
    skipped = 0
    failed: list[str] = []
    for line in Path(args.input).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        try:
            result = await load_one(
                store=store,
                vector=vector,
                tenant_id=args.tenant_id,
                session_id=args.session_id,
                row=row,
            )
        except Exception as exc:
            failed.append(f"{row.get('article_id')}: {exc}")
            print(f"fail {row.get('article_id')}: {exc}", flush=True)
            continue
        print(result, flush=True)
        if result.startswith("skip"):
            skipped += 1
        elif result.startswith("ok"):
            created += 1
    print(
        json.dumps(
            {"created": created, "skipped": skipped, "failed": len(failed), "failures": failed[:20]},
            ensure_ascii=False,
        )
    )
    await vector.close()
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--tenant-id", default="default")
    parser.add_argument("--session-id", required=True)
    return asyncio.run(async_main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
