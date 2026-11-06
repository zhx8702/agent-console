"""Ingest official 毛泽东选集 articles into a session-scoped knowledge base."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

KB_SOURCE = "maoxuan-official"
DEFAULT_TENANT_ID = "default"
MAX_PART_CHARS = 5000


def split_document(document: dict) -> list[dict]:
    content = str(document.get("content") or "").strip()
    if len(content) <= MAX_PART_CHARS:
        return [document]
    parts: list[dict] = []
    start = 0
    index = 1
    while start < len(content):
        end = min(len(content), start + MAX_PART_CHARS)
        if end < len(content):
            cut = content.rfind("\n", start + MAX_PART_CHARS // 2, end)
            if cut > start:
                end = cut
        part = dict(document)
        metadata = dict(document.get("metadata") or {})
        metadata["part"] = index
        article_id = str(document.get("article_id") or metadata.get("article_id") or "")
        part["title"] = f"{document['title']}（{index}）"
        part["content"] = content[start:end].strip()
        part["metadata"] = metadata
        part["article_id"] = f"{article_id}#p{index}" if article_id else f"part-{index}"
        parts.append(part)
        start = end
        index += 1
    return parts


def _request(
    url: str,
    *,
    method: str,
    token: str,
    payload: dict | None = None,
    timeout: float = 180,
) -> dict:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} -> {exc.code}: {detail[:500]}") from exc


def _list_existing(api_base: str, token: str, tenant_id: str, session_id: str) -> dict[str, int]:
    existing: dict[str, int] = {}
    offset = 0
    while True:
        query = (
            f"{api_base}/v1/admin/kb/documents"
            f"?tenant_id={urllib.parse.quote(tenant_id)}"
            f"&session_id={urllib.parse.quote(session_id)}"
            f"&limit=100&offset={offset}"
        )
        payload = _request(query, method="GET", token=token, timeout=30)
        items = list(payload.get("items") or [])
        if not items:
            break
        for item in items:
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            article_id = str(metadata.get("article_id") or "").strip()
            source = str(item.get("source") or "").strip()
            if article_id and source == KB_SOURCE:
                existing[article_id] = int(item["id"])
        if len(items) < 100:
            break
        offset += 100
    return existing


def export_official_documents(corpus: Path) -> list[dict]:
    from scripts.maoxuan.corpus import load_corpus
    from scripts.maoxuan.kb import build_kb_document

    documents = []
    for article in load_corpus(corpus):
        if article.source != "official":
            continue
        payload = build_kb_document(article)
        payload["article_id"] = article.article_id
        documents.append(payload)
    return documents


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--from-jsonl", type=Path)
    parser.add_argument("--export-jsonl", type=Path)
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default="")
    parser.add_argument("--tenant-id", default=DEFAULT_TENANT_ID)
    parser.add_argument("--session-id", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--sleep", type=float, default=0.05)
    args = parser.parse_args()

    if args.from_jsonl:
        documents = [
            json.loads(line)
            for line in args.from_jsonl.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    elif args.corpus:
        documents = export_official_documents(args.corpus)
    else:
        raise SystemExit("pass --corpus or --from-jsonl")
    if args.limit > 0:
        documents = documents[: args.limit]
    if not documents:
        raise SystemExit("no official articles found")
    documents = [part for document in documents for part in split_document(document)]
    if args.export_jsonl:
        args.export_jsonl.parent.mkdir(parents=True, exist_ok=True)
        args.export_jsonl.write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in documents) + "\n",
            encoding="utf-8",
        )
        print(f"exported {len(documents)} official documents to {args.export_jsonl}")
        if not args.token:
            return 0

    if not args.token:
        raise SystemExit("--token is required unless only exporting")
    if not args.session_id:
        raise SystemExit("--session-id is required unless only exporting")

    existing = _list_existing(args.api_base, args.token, args.tenant_id, args.session_id)
    created = 0
    skipped = 0
    failed: list[str] = []
    for index, document in enumerate(documents, start=1):
        article_id = str(document.get("article_id") or document.get("metadata", {}).get("article_id") or "")
        if article_id in existing:
            skipped += 1
            print(f"[{index}/{len(documents)}] skip {article_id} doc={existing[article_id]}")
            continue
        payload = {
            "tenant_id": args.tenant_id,
            "session_id": args.session_id,
            "title": document["title"],
            "content": document["content"],
            "source": document.get("source") or KB_SOURCE,
            "url": document.get("url"),
            "metadata": document.get("metadata") or {},
        }
        result = None
        last_error = ""
        for attempt in range(1, 3):
            try:
                result = _request(
                    f"{args.api_base}/v1/admin/kb/documents",
                    method="POST",
                    token=args.token,
                    payload=payload,
                    timeout=600,
                )
                break
            except Exception as exc:
                last_error = str(exc)
                time.sleep(3 * attempt)
        if result is None:
            failed.append(f"{article_id}: {last_error}")
            print(f"[{index}/{len(documents)}] fail {article_id}: {last_error}", flush=True)
            continue
        created += 1
        print(f"[{index}/{len(documents)}] ok {article_id} doc={result.get('doc_id')}")
        if args.sleep:
            time.sleep(args.sleep)
    print(
        json.dumps(
            {
                "official": len(documents),
                "created": created,
                "skipped": skipped,
                "failed": len(failed),
                "failures": failed[:20],
            },
            ensure_ascii=False,
        )
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
