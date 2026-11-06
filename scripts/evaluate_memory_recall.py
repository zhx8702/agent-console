"""Read-only retrieval acceptance against a frozen, locally held historical case file.

Run inside the application environment with --cases and --output. --candidate
overrides embedding settings for this process only, without changing live config.
No historical text, member identifiers or credentials are included in the report.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path

from app.common.config import get_settings
from app.common.runtime_llm_config import load_runtime_llm_config
from app.infra.db import get_engine
from app.kb.vector.qdrant_store import QdrantVectorStore
from app.llm.service import _build_embed_provider
from plugins.memory.store import MemoryStore, _is_prompt_eligible_memory_item


async def evaluate(settings, fixture):
    provider = _build_embed_provider(settings)
    vector = QdrantVectorStore(settings.qdrant_url, settings.qdrant_api_key)
    store = MemoryStore(settings, llm_service=provider, vector_store=vector)
    baseline = MemoryStore(settings.model_copy(update={"memory_vector_index_enabled": False}))
    results, boundaries = [], []
    try:
        for case in fixture["cases"]:
            source = await store.get_memory_item(case["expected_ids"][0])
            if (not source or not _is_prompt_eligible_memory_item(source) or
                hashlib.sha256(source["content"].encode()).hexdigest() != case["source_sha256"]):
                results.append({"id": case["id"], "error": "source_changed_or_ineligible"})
                continue
            options = {**case["scope"], "query": case["query"], "limit": 5,
                       "include_graph": False, "debug": True}
            old = await baseline.retrieve_memory_hybrid(**options)
            started = time.monotonic()
            new = await store.retrieve_memory_hybrid(**options)
            old_ids = [int(x["id"]) for x in old.get("items", [])]
            new_ids = [int(x["id"]) for x in new.get("items", [])]
            expected = set(case["expected_ids"])
            rank = next((i + 1 for i, item in enumerate(new_ids) if item in expected), 0)
            results.append({"id": case["id"], "kind": case["kind"], "hit": bool(rank),
                            "baseline_hit": bool(expected.intersection(old_ids)),
                            "rank": rank, "selected_ids": new_ids,
                            "latency_ms": round((time.monotonic() - started) * 1000, 2)})
            if case["kind"] == "keyword":
                for kind, change in [
                    ("other_user", {"user_id": "embedding-eval-unrelated-member"}),
                    ("other_tenant", {"tenant_id": "embedding-eval-unrelated-tenant"}),
                    ("private_in_group", {"session_id": "embedding-eval-other@chatroom",
                                          "request_session_kind": "group"}),
                ]:
                    blocked = await store.retrieve_memory_hybrid(**{**options, **change})
                    ids = {int(x["id"]) for x in blocked.get("items", [])}
                    boundaries.append({"id": case["id"], "kind": kind,
                                       "passed": not bool(ids.intersection(expected))})
        valid = [r for r in results if "error" not in r]
        paraphrases = [r for r in valid if r["kind"] == "paraphrase"]
        recall = sum(r["hit"] for r in valid) / len(valid) if valid else 0
        semantic = sum(r["hit"] for r in paraphrases) / len(paraphrases) if paraphrases else 0
        summary = {
            "cases": len(results), "evaluated": len(valid),
            "recall_at_5": recall, "paraphrase_recall_at_5": semantic,
            "baseline_recall_at_5": sum(r["baseline_hit"] for r in valid) / len(valid) if valid else 0,
            "mrr_at_5": sum(1 / r["rank"] if r["rank"] else 0 for r in valid) / len(valid) if valid else 0,
            "boundary_cases": len(boundaries), "boundary_failures": sum(not r["passed"] for r in boundaries),
        }
        summary["passed"] = bool(
            len(valid) == len(results) and len(valid) >= 40 and
            recall >= fixture["minimum_recall_at_5"] and
            semantic >= fixture["minimum_paraphrase_recall_at_5"] and
            boundaries and all(r["passed"] for r in boundaries))
        return {"summary": summary, "results": results, "boundaries": boundaries}
    finally:
        await provider.close()
        await vector._client.close()


async def main(args):
    raw = Path(args.cases).read_bytes()
    fixture = json.loads(raw)
    settings = (await load_runtime_llm_config(get_settings())).settings
    if args.candidate:
        model = args.candidate_model or settings.memory_vector_embed_model or settings.llm_embed_model
        settings = settings.model_copy(update={
            "llm_embed_model": model, "memory_vector_embed_model": model,
            "memory_vector_size": args.candidate_dimensions or settings.memory_vector_size,
            "memory_vector_index_enabled": True, "memory_hybrid_retrieval_enabled": True,
            "memory_vector_collection": args.candidate,
        })
    try:
        report = await evaluate(settings, fixture)
        report["fixture_sha256"] = hashlib.sha256(raw).hexdigest()
        report["collection"] = settings.memory_vector_collection
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report["summary"], ensure_ascii=False), flush=True)
        if not report["summary"]["passed"]:
            raise SystemExit(1)
    finally:
        await get_engine().dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--candidate")
    parser.add_argument("--candidate-model", help="Defaults to the configured memory embedding model")
    parser.add_argument("--candidate-dimensions", type=int, help="Defaults to MEMORY_VECTOR_SIZE")
    args = parser.parse_args()
    if args.candidate_dimensions is not None and args.candidate_dimensions <= 0:
        parser.error("--candidate-dimensions must be positive")
    if not args.candidate and (args.candidate_model or args.candidate_dimensions is not None):
        parser.error("Candidate model/dimensions require --candidate")
    asyncio.run(main(args))
