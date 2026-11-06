"""Embed official 毛选 articles locally and write a precomputed JSONL."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.kb.chunker import chunk_text, count_tokens
from scripts.ingest_maoxuan_kb import export_official_documents, split_document

OLLAMA = "http://192.168.3.94:11434"
MODEL = "bge-m3:latest"


def embed_texts(texts: list[str]) -> list[list[float]]:
    payload = json.dumps(
        {"model": MODEL, "input": texts, "keep_alive": -1, "truncate": False},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        OLLAMA + "/api/embed",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        body = json.load(response)
    vectors = body["embeddings"]
    if len(vectors) != len(texts):
        raise RuntimeError("embed count mismatch")
    return vectors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--batch", type=int, default=4)
    args = parser.parse_args()

    documents = [
        part
        for document in export_official_documents(args.corpus)
        for part in split_document(document)
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with args.output.open("w", encoding="utf-8") as handle:
        for index, document in enumerate(documents, start=1):
            chunks = chunk_text(document["content"], max_tokens=400, overlap_tokens=60)
            vectors: list[list[float]] = []
            for offset in range(0, len(chunks), args.batch):
                batch = chunks[offset : offset + args.batch]
                for attempt in range(1, 4):
                    try:
                        vectors.extend(embed_texts(batch))
                        break
                    except Exception:
                        if attempt == 3:
                            raise
                        time.sleep(2 * attempt)
            row = {
                "article_id": document["article_id"],
                "title": document["title"],
                "content": document["content"],
                "source": document["source"],
                "metadata": document["metadata"],
                "chunks": [
                    {
                        "content": chunk,
                        "token_count": count_tokens(chunk),
                        "vector": vector,
                    }
                    for chunk, vector in zip(chunks, vectors, strict=True)
                ],
            }
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += 1
            print(f"[{index}/{len(documents)}] {document['article_id']} chunks={len(chunks)}", flush=True)
    print(json.dumps({"documents": written, "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
