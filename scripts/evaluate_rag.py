"""Evaluate hybrid RAG retrieval against a production-style JSONL question set.

Each line must contain ``query`` and ``expected_doc_ids``; ``tenant_id`` and
``session_id`` are optional. The command uses the same configured database,
embedding provider, vector store, thresholds and reranker as the application.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.infra.redis_client import close_redis

from app.common.config import get_settings
from app.infra.db import dispose_engine
from app.main import build_container
from app.rag.evaluation import RAGEvaluationCase, evaluate_retriever


def _load_cases(path: Path) -> list[RAGEvaluationCase]:
    cases: list[RAGEvaluationCase] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            payload = json.loads(raw_line)
            query = str(payload.get("query") or "").strip()
            expected = frozenset(int(value) for value in payload.get("expected_doc_ids") or [])
            if not query or not expected:
                raise ValueError(
                    f"line {line_number}: query and expected_doc_ids are required"
                )
            cases.append(
                RAGEvaluationCase(
                    query=query,
                    expected_doc_ids=expected,
                    tenant_id=str(payload.get("tenant_id") or "default"),
                    session_id=str(payload.get("session_id") or "").strip() or None,
                )
            )
    return cases


async def _run(path: Path, top_k: int) -> None:
    settings = get_settings()
    if not settings.knowledge_features_enabled:
        raise RuntimeError("KNOWLEDGE_FEATURES_ENABLED must be true")
    container = await build_container(settings)
    try:
        rag_engine = getattr(container, "rag_engine", None)
        retriever = getattr(rag_engine, "_retriever", None)
        if retriever is None:
            raise RuntimeError("RAG retriever is unavailable")
        report = await evaluate_retriever(retriever, _load_cases(path), top_k=top_k)
        print(json.dumps(report.__dict__, ensure_ascii=False, indent=2))
    finally:
        registry = getattr(container, "plugin_registry", None)
        if registry is not None:
            await registry.shutdown_all()
        bus = getattr(container, "bus", None)
        if bus is not None:
            await bus.close()
        await close_redis()
        await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset", type=Path, help="JSONL WeChat evaluation set")
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()
    asyncio.run(_run(args.dataset, max(1, args.top_k)))


if __name__ == "__main__":
    main()
