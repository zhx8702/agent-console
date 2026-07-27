from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from typing import Any

from app.common.logging import get_logger
from app.kb.vector.base import VectorRecord, VectorSearchHit, VectorStore
from app.llm.base import EmbedRequest

logger = get_logger(__name__)

ScopeExecutionAllowed = Callable[[str, str], Awaitable[bool]]


async def _scope_allowed(
    gate: ScopeExecutionAllowed | None,
    *,
    tenant_id: object,
    session_id: object,
) -> bool:
    if not callable(gate):
        return True
    try:
        return (
            await gate(str(tenant_id or ""), str(session_id or ""))
            is True
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return False


def memory_item_point_id(item_id: int | str) -> str:
    return f"memory_item:{item_id}"


def memory_fact_point_id(fact_id: int | str) -> str:
    return f"memory_fact:{fact_id}"


def memory_episode_point_id(episode_id: int | str) -> str:
    return f"memory_episode:{episode_id}"


def _settings_bool(settings: Any, name: str, default: bool) -> bool:
    return bool(getattr(settings, name, default))


def _settings_int(
    settings: Any,
    name: str,
    default: int,
    *,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    try:
        value = int(getattr(settings, name, default) or default)
    except (TypeError, ValueError):
        value = default
    value = max(value, minimum)
    if maximum is not None:
        value = min(value, maximum)
    return value


def _settings_float(
    settings: Any,
    name: str,
    default: float,
    *,
    minimum: float = 0.1,
    maximum: float | None = None,
) -> float:
    try:
        value = float(getattr(settings, name, default) or default)
    except (TypeError, ValueError):
        value = default
    value = max(value, minimum)
    if maximum is not None:
        value = min(value, maximum)
    return value


def _payload_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _normal_text(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _acceptance_status_from_item(item: dict[str, Any]) -> str:
    value = item.get("value")
    if not isinstance(value, dict):
        try:
            value = json.loads(item.get("value_json") or "{}")
        except Exception:
            value = {}
    acceptance = value.get("acceptance") if isinstance(value, dict) else None
    if isinstance(acceptance, dict):
        return str(acceptance.get("status") or "")
    return ""


class MemoryItemVectorIndex:
    """Best-effort vector index for memory items and graph projections."""

    def __init__(
        self,
        settings: Any,
        *,
        vector_store: VectorStore | None,
        llm_service: Any | None,
    ) -> None:
        self.settings = settings
        self.vector_store = vector_store
        self.llm_service = llm_service
        self.collection = str(
            getattr(settings, "memory_vector_collection", "agent_console_memory_items")
            or "agent_console_memory_items"
        )
        self.vector_size = _settings_int(settings, "memory_vector_size", 64, minimum=1)
        embed_model = str(getattr(settings, "memory_vector_embed_model", "") or "").strip()
        self.embed_model = embed_model or str(getattr(settings, "llm_embed_model", "") or "")
        self.timeout_seconds = _settings_float(
            settings,
            "memory_vector_timeout_seconds",
            2.0,
            minimum=0.1,
            maximum=30.0,
        )
        self.default_top_k = _settings_int(
            settings,
            "memory_vector_top_k",
            12,
            minimum=1,
            maximum=100,
        )
        self.graph_top_k = _settings_int(
            settings,
            "memory_graph_vector_top_k",
            self.default_top_k,
            minimum=1,
            maximum=100,
        )

    @property
    def is_enabled(self) -> bool:
        return (
            _settings_bool(self.settings, "memory_vector_index_enabled", False)
            and self.vector_store is not None
            and self.llm_service is not None
        )

    @property
    def is_available(self) -> bool:
        return self.vector_store is not None and self.llm_service is not None

    def _is_indexable_item_payload(self, item: dict[str, Any] | None) -> bool:
        if not item:
            return False
        if item.get("id") is None:
            return False
        if str(item.get("status") or "") != "active":
            return False
        if str(item.get("sensitivity") or "normal") != "normal":
            return False
        if item.get("deleted_at") is not None:
            return False
        if str(item.get("scope_type") or "") not in {"identity", "session"}:
            return False
        if _acceptance_status_from_item(item) not in {"", "accepted"}:
            return False
        return bool(_normal_text(item.get("content")))

    def is_indexable(self, item: dict[str, Any] | None) -> bool:
        return self.is_enabled and self._is_indexable_item_payload(item)

    def _is_visible_backing_item(self, item: dict[str, Any] | None) -> bool:
        if not item:
            return False
        if item.get("id") is None:
            return False
        if str(item.get("status") or "") != "active":
            return False
        if str(item.get("sensitivity") or "normal") != "normal":
            return False
        if item.get("deleted_at") is not None:
            return False
        if str(item.get("scope_type") or "") not in {"identity", "session"}:
            return False
        if _acceptance_status_from_item(item) not in {"", "accepted"}:
            return False
        return True

    def is_fact_indexable(
        self,
        fact: dict[str, Any] | None,
        *,
        backing_item: dict[str, Any] | None,
    ) -> bool:
        if not self.is_enabled or not fact or fact.get("id") is None:
            return False
        if str(fact.get("status") or "") != "active":
            return False
        if fact.get("invalid_at") is not None:
            return False
        if not self._is_visible_backing_item(backing_item):
            return False
        fact_scope = (
            str(fact.get("tenant_id") or ""),
            str(fact.get("channel") or ""),
            str(fact.get("source_key") or "*"),
            str(fact.get("user_id") or ""),
        )
        item_scope = (
            str(backing_item.get("tenant_id") or "") if backing_item else "",
            str(backing_item.get("channel") or "") if backing_item else "",
            str(backing_item.get("source_key") or "*") if backing_item else "*",
            str(backing_item.get("user_id") or "") if backing_item else "",
        )
        if fact_scope != item_scope:
            return False
        return bool(self.text_for_fact(fact))

    def is_episode_indexable(
        self,
        episode: dict[str, Any] | None,
        *,
        backing_items: list[dict[str, Any]],
    ) -> bool:
        if not self.is_enabled or not episode or episode.get("id") is None:
            return False
        if str(episode.get("status") or "") != "active":
            return False
        if not self.text_for_episode(episode):
            return False
        memory_item_ids = episode.get("memory_item_ids") or []
        if memory_item_ids:
            episode_scope = (
                str(episode.get("tenant_id") or ""),
                str(episode.get("channel") or ""),
                str(episode.get("source_key") or "*"),
                str(episode.get("user_id") or ""),
            )
            episode_session_id = str(episode.get("session_id") or "")
            for item in backing_items:
                if not self._is_visible_backing_item(item):
                    continue
                item_scope = (
                    str(item.get("tenant_id") or ""),
                    str(item.get("channel") or ""),
                    str(item.get("source_key") or "*"),
                    str(item.get("user_id") or ""),
                )
                if item_scope != episode_scope:
                    continue
                if (
                    episode_session_id
                    and str(item.get("scope_type") or "") == "session"
                    and str(item.get("session_id") or "") != episode_session_id
                ):
                    continue
                return True
            return False
        return bool(
            str(episode.get("tenant_id") or "")
            and str(episode.get("channel") or "")
            and str(episode.get("user_id") or "")
        )

    async def _with_timeout(self, awaitable: Any) -> Any:
        return await asyncio.wait_for(awaitable, timeout=self.timeout_seconds)

    async def _embed_raw(self, *, tenant_id: str, text: str, trace_id: str) -> list[float]:
        resp = await self._with_timeout(
            self.llm_service.embed(
                EmbedRequest(
                    tenant_id=tenant_id,
                    trace_id=trace_id,
                    model=self.embed_model,
                    texts=[text],
                )
            )
        )
        vectors = list(getattr(resp, "vectors", []) or [])
        if not vectors:
            raise RuntimeError("memory vector embed returned no vectors")
        return [float(value) for value in vectors[0]]

    async def _embed(self, *, tenant_id: str, text: str, trace_id: str) -> list[float]:
        vector = await self._embed_raw(tenant_id=tenant_id, text=text, trace_id=trace_id)
        if len(vector) != self.vector_size:
            raise ValueError(
                f"memory vector dim mismatch: expected {self.vector_size}, got {len(vector)}"
            )
        return vector

    async def _ensure_collection(self) -> None:
        await self._with_timeout(
            self.vector_store.ensure_collection(self.collection, self.vector_size)
        )

    def payload_for_item(self, item: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "tenant": item.get("tenant_id"),
            "tenant_id": item.get("tenant_id"),
            "channel": item.get("channel"),
            "source_key": item.get("source_key") or "*",
            "user_id": item.get("user_id"),
            "session_id": item.get("session_id") or "",
            "object_type": "item",
            "item_id": str(item.get("id")),
            "status": item.get("status") or "",
            "sensitivity": item.get("sensitivity") or "",
            "memory_type": item.get("memory_type") or "",
            "source_type": item.get("source_type") or "",
            "scope_type": item.get("scope_type") or "",
            "updated_at": item.get("updated_at"),
            "normalized_key": item.get("normalized_key") or "",
            "content": _normal_text(item.get("content"))[:500],
        }
        return {key: _payload_value(value) for key, value in payload.items()}

    def text_for_fact(self, fact: dict[str, Any]) -> str:
        return _normal_text(
            " ".join(
                str(value or "")
                for value in (
                    fact.get("subject_name"),
                    fact.get("subject_normalized_name"),
                    fact.get("predicate"),
                    fact.get("object_name"),
                    fact.get("object_normalized_name"),
                    fact.get("object_value"),
                )
            )
        )

    def text_for_episode(self, episode: dict[str, Any]) -> str:
        return _normal_text(f"{episode.get('title') or ''} {episode.get('summary') or ''}")

    def payload_for_fact(
        self,
        fact: dict[str, Any],
        *,
        backing_item: dict[str, Any] | None,
    ) -> dict[str, Any]:
        session_id = ""
        if backing_item and str(backing_item.get("scope_type") or "") == "session":
            session_id = str(backing_item.get("session_id") or "")
        payload = {
            "tenant": fact.get("tenant_id"),
            "tenant_id": fact.get("tenant_id"),
            "channel": fact.get("channel"),
            "source_key": fact.get("source_key") or "*",
            "user_id": fact.get("user_id"),
            "session_id": session_id,
            "object_type": "fact",
            "fact_id": str(fact.get("id")),
            "status": fact.get("status") or "",
            "confidence": fact.get("confidence"),
            "importance": "",
            "updated_at": fact.get("updated_at"),
            "predicate": _normal_text(fact.get("predicate"))[:200],
            "object_value": _normal_text(fact.get("object_value"))[:500],
            "title": "",
            "summary": "",
            "memory_item_id": str(fact.get("memory_item_id") or ""),
            "source_event_id": str(fact.get("source_event_id") or ""),
            "event_ids": "",
            "memory_item_ids": str(fact.get("memory_item_id") or ""),
        }
        return {key: _payload_value(value) for key, value in payload.items()}

    def payload_for_episode(self, episode: dict[str, Any]) -> dict[str, Any]:
        event_ids = episode.get("event_ids") or []
        memory_item_ids = episode.get("memory_item_ids") or []
        payload = {
            "tenant": episode.get("tenant_id"),
            "tenant_id": episode.get("tenant_id"),
            "channel": episode.get("channel"),
            "source_key": episode.get("source_key") or "*",
            "user_id": episode.get("user_id"),
            "session_id": episode.get("session_id") or "",
            "object_type": "episode",
            "episode_id": str(episode.get("id")),
            "fact_id": "",
            "status": episode.get("status") or "",
            "confidence": "",
            "importance": episode.get("importance"),
            "updated_at": episode.get("updated_at"),
            "predicate": "",
            "object_value": "",
            "title": _normal_text(episode.get("title"))[:500],
            "summary": _normal_text(episode.get("summary"))[:1000],
            "memory_item_id": "",
            "source_event_id": "",
            "event_ids": ",".join(str(value) for value in event_ids),
            "memory_item_ids": ",".join(str(value) for value in memory_item_ids),
        }
        return {key: _payload_value(value) for key, value in payload.items()}

    async def upsert_item(
        self,
        item: dict[str, Any] | None,
        *,
        force: bool = False,
        scope_execution_allowed: ScopeExecutionAllowed | None = None,
    ) -> str:
        available = self.is_available if force else self.is_enabled
        if not available or not item or item.get("id") is None:
            return "skipped"
        tenant_id = str(item.get("tenant_id") or "")
        session_id = str(item.get("session_id") or "")
        if not await _scope_allowed(
            scope_execution_allowed,
            tenant_id=tenant_id,
            session_id=session_id,
        ):
            return "scope_disabled"
        if not self._is_indexable_item_payload(item):
            if not await _scope_allowed(
                scope_execution_allowed,
                tenant_id=tenant_id,
                session_id=session_id,
            ):
                return "scope_disabled"
            await self.delete_item(item.get("id"))
            return "deleted"
        item_id = str(item["id"])
        content = _normal_text(item.get("content"))
        vector = await self._embed(
            tenant_id=tenant_id,
            text=content,
            trace_id=f"memory_item:{item_id}",
        )
        if not await _scope_allowed(
            scope_execution_allowed,
            tenant_id=tenant_id,
            session_id=session_id,
        ):
            return "scope_disabled"
        await self._ensure_collection()
        if not await _scope_allowed(
            scope_execution_allowed,
            tenant_id=tenant_id,
            session_id=session_id,
        ):
            return "scope_disabled"
        await self._with_timeout(
            self.vector_store.upsert(
                self.collection,
                [
                    VectorRecord(
                        id=memory_item_point_id(item_id),
                        vector=vector,
                        payload=self.payload_for_item(item),
                    )
                ],
            )
        )
        return "indexed"

    async def delete_item(
        self,
        item_id: int | str | None,
        *,
        force: bool = False,
    ) -> str:
        available = self.vector_store is not None if force else self.is_enabled
        if not available or item_id is None:
            return "skipped"
        await self._with_timeout(
            self.vector_store.delete(self.collection, [memory_item_point_id(item_id)])
        )
        return "deleted"

    async def upsert_fact(
        self,
        fact: dict[str, Any] | None,
        *,
        backing_item: dict[str, Any] | None,
        scope_execution_allowed: ScopeExecutionAllowed | None = None,
    ) -> str:
        if not self.is_enabled or not fact or fact.get("id") is None:
            return "skipped"
        tenant_id = str(fact.get("tenant_id") or "")
        session_id = (
            str((backing_item or {}).get("session_id") or "")
            if str((backing_item or {}).get("scope_type") or "") == "session"
            else ""
        )
        if not await _scope_allowed(
            scope_execution_allowed,
            tenant_id=tenant_id,
            session_id=session_id,
        ):
            return "scope_disabled"
        if not self.is_fact_indexable(fact, backing_item=backing_item):
            if not await _scope_allowed(
                scope_execution_allowed,
                tenant_id=tenant_id,
                session_id=session_id,
            ):
                return "scope_disabled"
            await self.delete_fact(fact.get("id"))
            return "deleted"
        fact_id = str(fact["id"])
        vector = await self._embed(
            tenant_id=tenant_id,
            text=self.text_for_fact(fact),
            trace_id=f"memory_fact:{fact_id}",
        )
        if not await _scope_allowed(
            scope_execution_allowed,
            tenant_id=tenant_id,
            session_id=session_id,
        ):
            return "scope_disabled"
        await self._ensure_collection()
        if not await _scope_allowed(
            scope_execution_allowed,
            tenant_id=tenant_id,
            session_id=session_id,
        ):
            return "scope_disabled"
        await self._with_timeout(
            self.vector_store.upsert(
                self.collection,
                [
                    VectorRecord(
                        id=memory_fact_point_id(fact_id),
                        vector=vector,
                        payload=self.payload_for_fact(fact, backing_item=backing_item),
                    )
                ],
            )
        )
        return "indexed"

    async def upsert_episode(
        self,
        episode: dict[str, Any] | None,
        *,
        backing_items: list[dict[str, Any]],
        scope_execution_allowed: ScopeExecutionAllowed | None = None,
    ) -> str:
        if not self.is_enabled or not episode or episode.get("id") is None:
            return "skipped"
        tenant_id = str(episode.get("tenant_id") or "")
        session_id = str(episode.get("session_id") or "")
        if not await _scope_allowed(
            scope_execution_allowed,
            tenant_id=tenant_id,
            session_id=session_id,
        ):
            return "scope_disabled"
        if not self.is_episode_indexable(episode, backing_items=backing_items):
            if not await _scope_allowed(
                scope_execution_allowed,
                tenant_id=tenant_id,
                session_id=session_id,
            ):
                return "scope_disabled"
            await self.delete_episode(episode.get("id"))
            return "deleted"
        episode_id = str(episode["id"])
        vector = await self._embed(
            tenant_id=tenant_id,
            text=self.text_for_episode(episode),
            trace_id=f"memory_episode:{episode_id}",
        )
        if not await _scope_allowed(
            scope_execution_allowed,
            tenant_id=tenant_id,
            session_id=session_id,
        ):
            return "scope_disabled"
        await self._ensure_collection()
        if not await _scope_allowed(
            scope_execution_allowed,
            tenant_id=tenant_id,
            session_id=session_id,
        ):
            return "scope_disabled"
        await self._with_timeout(
            self.vector_store.upsert(
                self.collection,
                [
                    VectorRecord(
                        id=memory_episode_point_id(episode_id),
                        vector=vector,
                        payload=self.payload_for_episode(episode),
                    )
                ],
            )
        )
        return "indexed"

    async def delete_fact(
        self,
        fact_id: int | str | None,
        *,
        force: bool = False,
    ) -> str:
        available = self.vector_store is not None if force else self.is_enabled
        if not available or fact_id is None:
            return "skipped"
        await self._with_timeout(
            self.vector_store.delete(self.collection, [memory_fact_point_id(fact_id)])
        )
        return "deleted"

    async def delete_episode(
        self,
        episode_id: int | str | None,
        *,
        force: bool = False,
    ) -> str:
        available = self.vector_store is not None if force else self.is_enabled
        if not available or episode_id is None:
            return "skipped"
        await self._with_timeout(
            self.vector_store.delete(self.collection, [memory_episode_point_id(episode_id)])
        )
        return "deleted"

    async def rebuild_graph(
        self,
        graph_objects: list[dict[str, Any]],
        *,
        scope_execution_allowed: ScopeExecutionAllowed | None = None,
    ) -> dict[str, int | bool | str]:
        result: dict[str, int | bool | str] = {
            "enabled": self.is_enabled,
            "collection": self.collection,
            "scanned": len(graph_objects),
            "indexed": 0,
            "deleted": 0,
            "skipped": 0,
            "errors": 0,
        }
        if not self.is_enabled:
            result["skipped"] = len(graph_objects)
            return result
        for graph_object in graph_objects:
            try:
                object_type = str(graph_object.get("object_type") or "")
                if object_type == "fact":
                    status = await self.upsert_fact(
                        graph_object.get("row"),
                        backing_item=graph_object.get("backing_item"),
                        scope_execution_allowed=scope_execution_allowed,
                    )
                elif object_type == "episode":
                    status = await self.upsert_episode(
                        graph_object.get("row"),
                        backing_items=list(graph_object.get("backing_items") or []),
                        scope_execution_allowed=scope_execution_allowed,
                    )
                else:
                    status = "skipped"
                if status == "indexed":
                    result["indexed"] = int(result["indexed"]) + 1
                elif status == "deleted":
                    result["deleted"] = int(result["deleted"]) + 1
                else:
                    result["skipped"] = int(result["skipped"]) + 1
            except Exception as exc:
                result["errors"] = int(result["errors"]) + 1
                logger.warning(
                    "memory.vector_rebuild_graph_failed",
                    object_type=(
                        graph_object.get("object_type")
                        if isinstance(graph_object, dict)
                        else None
                    ),
                    object_id=(
                        (graph_object.get("row") or {}).get("id")
                        if isinstance(graph_object, dict)
                        else None
                    ),
                    error_type=exc.__class__.__name__,
                    error=str(exc)[:500],
                )
        return result

    async def rebuild_items(
        self,
        items: list[dict[str, Any]],
        *,
        force: bool = False,
        dry_run: bool = False,
        scope_execution_allowed: ScopeExecutionAllowed | None = None,
    ) -> dict[str, int | bool | str]:
        result: dict[str, int | bool | str] = {
            "enabled": self.is_enabled,
            "available": self.is_available,
            "dry_run": dry_run,
            "collection": self.collection,
            "scanned": len(items),
            "indexed": 0,
            "deleted": 0,
            "skipped": 0,
            "errors": 0,
        }
        available = self.is_available if force else self.is_enabled
        if not available:
            result["skipped"] = len(items)
            return result
        for item in items:
            try:
                tenant_id = str(item.get("tenant_id") or "")
                session_id = str(item.get("session_id") or "")
                if not await _scope_allowed(
                    scope_execution_allowed,
                    tenant_id=tenant_id,
                    session_id=session_id,
                ):
                    result["skipped"] = int(result["skipped"]) + 1
                    continue
                if dry_run:
                    status = "indexed" if self._is_indexable_item_payload(item) else "skipped"
                else:
                    status = await self.upsert_item(
                        item,
                        force=force,
                        scope_execution_allowed=scope_execution_allowed,
                    )
                if status == "indexed":
                    result["indexed"] = int(result["indexed"]) + 1
                elif status == "deleted":
                    result["deleted"] = int(result["deleted"]) + 1
                else:
                    result["skipped"] = int(result["skipped"]) + 1
            except Exception as exc:
                result["errors"] = int(result["errors"]) + 1
                logger.warning(
                    "memory.vector_rebuild_item_failed",
                    item_id=item.get("id") if isinstance(item, dict) else None,
                    error_type=exc.__class__.__name__,
                    error=str(exc)[:500],
                )
        return result

    async def search_item_ids(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_keys: list[str],
        user_id: str,
        session_id: str,
        query: str,
        top_k: int | None = None,
        force: bool = False,
        scope_execution_allowed: ScopeExecutionAllowed | None = None,
    ) -> list[tuple[int, float]]:
        available = self.is_available if force else self.is_enabled
        if not available:
            return []
        query_text = _normal_text(query)
        if not query_text:
            return []
        if not await _scope_allowed(
            scope_execution_allowed,
            tenant_id=tenant_id,
            session_id=session_id,
        ):
            return []
        vector = await self._embed(
            tenant_id=tenant_id,
            text=query_text,
            trace_id=f"memory_item_search:{tenant_id}:{channel}:{user_id}",
        )
        if not await _scope_allowed(
            scope_execution_allowed,
            tenant_id=tenant_id,
            session_id=session_id,
        ):
            return []
        safe_top_k = max(1, min(int(top_k or self.default_top_k), 100))
        seen_keys: set[tuple[str, str]] = set()
        searches: list[dict[str, Any]] = []
        for source_key in source_keys:
            normalized_source = source_key or "*"
            for scope_type, scope_session_id in (
                ("identity", ""),
                ("session", session_id or ""),
            ):
                key = (normalized_source, f"{scope_type}:{scope_session_id}")
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                searches.append(
                    {
                        "tenant_id": tenant_id,
                        "channel": channel,
                        "source_key": normalized_source,
                        "user_id": user_id,
                        "status": "active",
                        "sensitivity": "normal",
                        "scope_type": scope_type,
                        "session_id": scope_session_id,
                    }
                )

        hits: list[VectorSearchHit] = []
        for filter_ in searches:
            if not await _scope_allowed(
                scope_execution_allowed,
                tenant_id=tenant_id,
                session_id=session_id,
            ):
                return []
            hits.extend(
                await self._with_timeout(
                    self.vector_store.search(
                        self.collection,
                        vector,
                        top_k=safe_top_k,
                        filter_=filter_,
                    )
                )
            )

        best: dict[int, float] = {}
        for hit in hits:
            raw_item_id = hit.payload.get("item_id")
            if raw_item_id is None:
                raw_id = str(hit.id)
                raw_item_id = raw_id.removeprefix("memory_item:")
            try:
                item_id = int(raw_item_id)
            except (TypeError, ValueError):
                continue
            best[item_id] = max(best.get(item_id, float("-inf")), float(hit.score))
        return sorted(best.items(), key=lambda pair: pair[1], reverse=True)

    async def smoke_enable_preflight(
        self,
        *,
        tenant_id: str = "",
        session_id: str = "",
        scope_execution_allowed: ScopeExecutionAllowed | None = None,
    ) -> dict[str, Any]:
        checks: dict[str, Any] = {
            "vector_backend": {
                "ok": False,
                "name": str(getattr(self.vector_store, "name", "none") or "none"),
            },
            "embedder": {"ok": False, "dimension": 0, "configured_dimension": self.vector_size},
            "dimension": {"ok": False, "configured": self.vector_size, "actual": 0},
            "collection": {"ok": False, "name": self.collection},
            "point_crud": {"ok": False},
            "cleanup": {"ok": False},
        }
        reasons: list[str] = []
        test_id = f"memory_vector_smoke:{uuid.uuid4()}"
        smoke_id = str(uuid.uuid4())
        vector: list[float] = []
        upserted = False

        backend_name = str(getattr(self.vector_store, "name", "") or "")
        if backend_name != "qdrant":
            reasons.append("qdrant_unreachable_or_not_active_backend")
        else:
            checks["vector_backend"]["ok"] = True

        if self.vector_store is None:
            reasons.append("vector_store_missing")
        if self.llm_service is None:
            reasons.append("embedder_missing")
        if self.vector_store is None or self.llm_service is None:
            return {"safe_to_enable": False, "checks": checks, "reasons": reasons}

        if not await _scope_allowed(
            scope_execution_allowed,
            tenant_id=tenant_id,
            session_id=session_id,
        ):
            reasons.append("scope_disabled")
            return {"safe_to_enable": False, "checks": checks, "reasons": reasons}

        try:
            vector = await self._embed_raw(
                tenant_id="memory-vector-smoke",
                text="memory vector enable smoke",
                trace_id=f"memory_vector_smoke:{smoke_id}",
            )
            checks["embedder"] = {
                "ok": bool(vector),
                "dimension": len(vector),
                "configured_dimension": self.vector_size,
            }
        except Exception as exc:
            reasons.append(f"embedder_error:{exc.__class__.__name__}")
            checks["embedder"]["error"] = str(exc)[:300]
            return {"safe_to_enable": False, "checks": checks, "reasons": reasons}

        if not await _scope_allowed(
            scope_execution_allowed,
            tenant_id=tenant_id,
            session_id=session_id,
        ):
            reasons.append("scope_disabled_after_embed")
            return {"safe_to_enable": False, "checks": checks, "reasons": reasons}

        checks["dimension"] = {
            "ok": len(vector) == self.vector_size,
            "configured": self.vector_size,
            "actual": len(vector),
        }
        if len(vector) != self.vector_size:
            reasons.append(f"dimension_mismatch:configured={self.vector_size}:actual={len(vector)}")
            return {"safe_to_enable": False, "checks": checks, "reasons": reasons}

        try:
            if not await _scope_allowed(
                scope_execution_allowed,
                tenant_id=tenant_id,
                session_id=session_id,
            ):
                reasons.append("scope_disabled_before_collection")
                return {"safe_to_enable": False, "checks": checks, "reasons": reasons}
            await self._ensure_collection()
            checks["collection"]["ok"] = True
        except Exception as exc:
            reasons.append(f"collection_error:{exc.__class__.__name__}")
            checks["collection"]["error"] = str(exc)[:300]
            return {"safe_to_enable": False, "checks": checks, "reasons": reasons}

        smoke_filter = {
            "__smoke": "memory_vector_enable",
            "smoke_id": smoke_id,
            "tenant_id": "memory-vector-smoke",
            "object_type": "smoke",
        }
        try:
            if not await _scope_allowed(
                scope_execution_allowed,
                tenant_id=tenant_id,
                session_id=session_id,
            ):
                reasons.append("scope_disabled_before_upsert")
                return {"safe_to_enable": False, "checks": checks, "reasons": reasons}
            await self._with_timeout(
                self.vector_store.upsert(
                    self.collection,
                    [
                        VectorRecord(
                            id=test_id,
                            vector=vector,
                            payload={
                                **smoke_filter,
                                "status": "active",
                                "content": "memory vector enable smoke",
                            },
                        )
                    ],
                )
            )
            upserted = True
            if not await _scope_allowed(
                scope_execution_allowed,
                tenant_id=tenant_id,
                session_id=session_id,
            ):
                reasons.append("scope_disabled_after_upsert")
                return {"safe_to_enable": False, "checks": checks, "reasons": reasons}
            hits = await self._with_timeout(
                self.vector_store.search(
                    self.collection,
                    vector,
                    top_k=3,
                    filter_=smoke_filter,
                )
            )
            hit_ids = [str(hit.id) for hit in hits]
            checks["point_crud"] = {"ok": test_id in hit_ids, "hit_ids": hit_ids}
            if test_id not in hit_ids:
                reasons.append("smoke_point_not_found")
        except Exception as exc:
            reasons.append(f"point_crud_error:{exc.__class__.__name__}")
            checks["point_crud"]["error"] = str(exc)[:300]
        finally:
            if upserted:
                try:
                    await self._with_timeout(self.vector_store.delete(self.collection, [test_id]))
                    remaining = await self._with_timeout(
                        self.vector_store.search(
                            self.collection,
                            vector,
                            top_k=3,
                            filter_=smoke_filter,
                        )
                    )
                    checks["cleanup"] = {"ok": not remaining, "remaining": len(remaining)}
                    if remaining:
                        reasons.append("smoke_point_cleanup_incomplete")
                except Exception as exc:
                    reasons.append(f"cleanup_error:{exc.__class__.__name__}")
                    checks["cleanup"] = {"ok": False, "error": str(exc)[:300]}

        safe = not reasons and all(
            bool(check.get("ok")) for check in checks.values() if isinstance(check, dict)
        )
        return {"safe_to_enable": safe, "checks": checks, "reasons": reasons}

    async def search_graph_ids(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_keys: list[str],
        user_id: str,
        session_id: str,
        query: str,
        object_type: str,
        top_k: int | None = None,
    ) -> list[tuple[int, float]]:
        if not self.is_enabled:
            return []
        query_text = _normal_text(query)
        if not query_text:
            return []
        if object_type not in {"fact", "episode"}:
            return []
        vector = await self._embed(
            tenant_id=tenant_id,
            text=query_text,
            trace_id=f"memory_graph_search:{object_type}:{tenant_id}:{channel}:{user_id}",
        )
        safe_top_k = max(1, min(int(self.graph_top_k if top_k is None else top_k), 100))
        id_key = "fact_id" if object_type == "fact" else "episode_id"
        seen_filters: set[tuple[str, str]] = set()
        filters: list[dict[str, Any]] = []
        for source_key in source_keys:
            normalized_source = source_key or "*"
            for scoped_session_id in ("", session_id or ""):
                filter_key = (normalized_source, scoped_session_id)
                if filter_key in seen_filters:
                    continue
                seen_filters.add(filter_key)
                filters.append(
                    {
                        "tenant_id": tenant_id,
                        "channel": channel,
                        "source_key": normalized_source,
                        "user_id": user_id,
                        "session_id": scoped_session_id,
                        "status": "active",
                        "object_type": object_type,
                    }
                )

        hits: list[VectorSearchHit] = []
        for filter_ in filters:
            hits.extend(
                await self._with_timeout(
                    self.vector_store.search(
                        self.collection,
                        vector,
                        top_k=safe_top_k,
                        filter_=filter_,
                    )
                )
            )

        best: dict[int, float] = {}
        for hit in hits:
            raw_object_id = hit.payload.get(id_key)
            if raw_object_id is None:
                prefix = "memory_fact:" if object_type == "fact" else "memory_episode:"
                raw_object_id = str(hit.id).removeprefix(prefix)
            try:
                object_id = int(raw_object_id)
            except (TypeError, ValueError):
                continue
            best[object_id] = max(best.get(object_id, float("-inf")), float(hit.score))
        return sorted(best.items(), key=lambda pair: pair[1], reverse=True)
