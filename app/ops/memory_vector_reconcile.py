"""Reconcile memory vectors from committed source records without changing policy.

Run with ``python -m app.ops.memory_vector_reconcile``. Re-running is safe after
an interrupted run. Publication uses the same member/deletion fences as writes.
"""
from __future__ import annotations

import asyncio
import json

from sqlalchemy import text

from app.common.config import get_settings
from app.common.runtime_llm_config import load_runtime_llm_config
from app.infra.db import get_engine
from app.kb.vector.qdrant_store import QdrantVectorStore, _coerce_id
from app.llm.service import _build_embed_provider
from app.plugin.state import PluginStateStore
from plugins.memory.plugin import MemoryPlugin
from plugins.memory.store import MemoryStore
from plugins.memory.vector_index import memory_item_point_id


async def reconcile(settings, *, progress=None):
    if settings.llm_embed_provider == "fake" or not settings.memory_vector_index_enabled:
        raise RuntimeError("real embedding and vector indexing must be enabled")
    provider = _build_embed_provider(settings)
    vector = QdrantVectorStore(settings.qdrant_url, settings.qdrant_api_key)
    store = MemoryStore(settings, llm_service=provider, vector_store=vector)
    policy = PluginStateStore()

    async def allowed(tenant_id, session_id):
        return await policy.execution_snapshot_allowed(
            {"memory": MemoryPlugin.meta.version}, tenant_id=tenant_id, session_id=session_id)

    store.runtime_scope_gates_required = True
    store.scope_execution_allowed = allowed
    counts = {"scanned": 0, "published": 0, "deleted": 0, "skipped": 0, "errors": 0}
    cursor = 0
    try:
        while True:
            async with get_engine().connect() as connection:
                rows = (await connection.execute(text(
                    "SELECT id FROM plugin_memory_item WHERE id > :cursor "
                    "AND status='active' AND deleted_at IS NULL AND sensitivity='normal' "
                    "AND sensitivity_category='normal' ORDER BY id LIMIT 100"
                ), {"cursor": cursor})).mappings().all()
            if not rows:
                break
            for row in rows:
                cursor = int(row["id"])
                counts["scanned"] += 1
                try:
                    status = await store._publish_current_memory_vectors(
                        cursor, scope_execution_allowed=allowed)
                    # Normal write hooks deliberately swallow transient index errors.
                    # A repair job must verify publication rather than report those
                    # attempts as successful and hide a disconnected builder.
                    current = await store.get_memory_item(cursor)
                    if (status == "published" and current and store.vector_index.is_indexable(current)
                        and await allowed(current["tenant_id"], current.get("session_id") or "")
                        and not await store.member_memory_write_blocked(
                            tenant_id=current["tenant_id"], user_id=current["user_id"],
                            channel=current["channel"])):
                        points = await vector._client.retrieve(
                            collection_name=settings.memory_vector_collection,
                            ids=[_coerce_id(memory_item_point_id(cursor))],
                            with_payload=True, with_vectors=False)
                        expected = store.vector_index.payload_for_item(current)
                        if not points or any(
                            (points[0].payload or {}).get(key) != expected[key]
                            for key in ("updated_at", "content", "user_id", "tenant_id")
                        ):
                            raise RuntimeError("memory vector publication not confirmed")
                    elif status == "published":
                        status = "skipped"
                    counts[status if status in {"published", "deleted"} else "skipped"] += 1
                except Exception:
                    counts["errors"] += 1
                if counts["errors"] >= 5:
                    # Stop hitting an unavailable service; a later run retries the source inventory.
                    return counts
            if progress:
                progress(dict(counts))
        return counts
    finally:
        await provider.close()
        await vector.close()


async def main():
    settings = (await load_runtime_llm_config(get_settings())).settings
    try:
        result = await reconcile(settings, progress=lambda c: print(json.dumps(c), flush=True))
        print(json.dumps({"collection": settings.memory_vector_collection, **result}), flush=True)
        if result["errors"]:
            raise SystemExit(1)
    finally:
        await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(main())
