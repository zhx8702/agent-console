"""Hybrid memory and graph retrieval plus vector-index maintenance."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from plugins.memory import store as _store_runtime
from plugins.memory.store import (
    HYBRID_GRAPH_CANDIDATE_MULTIPLIER,
    HYBRID_GRAPH_EPISODE_BUDGET,
    HYBRID_GRAPH_FACT_BUDGET,
    HYBRID_ITEM_SQL_CANDIDATE_MULTIPLIER,
    _attach_hybrid_item_score,
    _coerce_int_set,
    _finalize_graph_episode,
    _graph_query_match_count,
    _graph_reason,
    _looks_like_memory_item_row,
    _memory_item_visible_for_audience,
    _memory_retrieval_tokens,
    _needs_source_fallback,
    _normalize_line,
    _normalize_vector_score,
    _rank_retrieved_memory_items,
    _safe_float,
    _safe_int,
    _safe_json_loads,
    _settings_bool,
    _settings_int,
    _timestamp_sort_value,
    _truncate_error,
    logger,
)


async def _exec(sql: str, params: dict | None = None) -> list[dict]:
    return await _store_runtime._exec(sql, params)


ScopeExecutionAllowed = Callable[[str, str], Awaitable[bool]]


class MemoryRetrievalStoreMixin:
    def _vector_scope_gate(self) -> ScopeExecutionAllowed | None:
        gate = getattr(self, "scope_execution_allowed", None)
        if callable(gate):
            return gate
        if not bool(getattr(self, "runtime_scope_gates_required", False)):
            return None

        async def deny(_tenant_id: str, _session_id: str) -> bool:
            return False

        return deny

    async def _require_vector_scope(self, *, tenant_id: str, session_id: str = "") -> None:
        gate = self._vector_scope_gate()
        if gate is None:
            return
        try:
            allowed = await gate(str(tenant_id or ""), str(session_id or ""))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise RuntimeError("memory plugin runtime disabled for vector operation") from exc
        if allowed is not True:
            raise RuntimeError("memory plugin runtime disabled for vector operation")

    async def retrieve_memory_items(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        session_id: str,
        query: str = "",
        limit: int = 6,
        debug: bool = False,
        request_session_kind: str | None = None,
    ) -> list[dict[str, Any]]:
        if _settings_bool(self.settings, "memory_hybrid_retrieval_enabled", False):
            hybrid = await self.retrieve_memory_hybrid(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                session_id=session_id,
                query=query,
                limit=limit,
                include_graph=False,
                debug=debug,
                request_session_kind=request_session_kind,
            )
            return list(hybrid.get("items") or [])
        if self.vector_index.is_enabled and _normalize_line(query):
            try:
                vector_items = await self._retrieve_memory_items_vector(
                    tenant_id=tenant_id,
                    channel=channel,
                    source_key=source_key,
                    user_id=user_id,
                    session_id=session_id,
                    query=query,
                    limit=limit,
                    request_session_kind=request_session_kind,
                )
                if vector_items:
                    return vector_items
            except Exception as exc:
                logger.warning(
                    "memory.vector_retrieve_failed",
                    tenant_id=tenant_id,
                    channel=channel,
                    source_key=source_key,
                    user_id=user_id,
                    error_type=exc.__class__.__name__,
                    error=_truncate_error(exc),
                )
        return await self._retrieve_memory_items_sql(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            query=query,
            limit=limit,
            request_session_kind=request_session_kind,
        )

    async def retrieve_memory_hybrid(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        session_id: str,
        query: str = "",
        limit: int = 6,
        fact_top_k: int | None = None,
        episode_top_k: int | None = None,
        budget_chars: int | None = None,
        include_graph: bool = True,
        debug: bool = False,
        request_session_kind: str | None = None,
    ) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit or 6), 20))
        tokens = _memory_retrieval_tokens(query)
        candidate_limit = max(safe_limit * HYBRID_ITEM_SQL_CANDIDATE_MULTIPLIER, safe_limit)
        items_by_id: dict[int, dict[str, Any]] = {}
        sources_by_id: dict[int, set[str]] = {}
        vector_error = ""

        sql_items = await self._retrieve_memory_items_sql(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            query=query,
            limit=candidate_limit,
            request_session_kind=request_session_kind,
        )
        for item in sql_items:
            item_id = _safe_int(item.get("id"), 0)
            if item_id <= 0:
                continue
            items_by_id[item_id] = dict(item)
            sources_by_id.setdefault(item_id, set()).add("sql")

        if self.vector_index.is_enabled and _normalize_line(query):
            source_keys = [source_key or "*"]
            if _needs_source_fallback(source_key) and "*" not in source_keys:
                source_keys.append("*")
            try:
                hit_pairs = await self.vector_index.search_item_ids(
                    tenant_id=tenant_id,
                    channel=channel,
                    source_keys=source_keys,
                    user_id=user_id,
                    session_id=session_id,
                    query=query,
                    top_k=max(safe_limit * 4, self.vector_index.default_top_k),
                )
                for item_id, score in hit_pairs:
                    item = items_by_id.get(int(item_id))
                    if item is None:
                        fetched = await self.get_memory_item(int(item_id))
                        if not fetched:
                            continue
                        item = dict(fetched)
                    item["vector_score"] = max(
                        _safe_float(item.get("vector_score"), 0.0), _safe_float(score, 0.0)
                    )
                    item["match_count"] = max(_safe_int(item.get("match_count"), 0), 0)
                    items_by_id[int(item_id)] = item
                    sources_by_id.setdefault(int(item_id), set()).add("vector")
            except Exception as exc:
                vector_error = f"{exc.__class__.__name__}:{_truncate_error(exc)}"
                logger.warning(
                    "memory.hybrid_item_vector_retrieve_failed",
                    tenant_id=tenant_id,
                    channel=channel,
                    source_key=source_key,
                    user_id=user_id,
                    error_type=exc.__class__.__name__,
                    error=_truncate_error(exc),
                )

        safe_candidates = _rank_retrieved_memory_items(
            list(items_by_id.values()),
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            has_query=bool(tokens),
            limit=max(len(items_by_id), safe_limit),
            request_session_kind=request_session_kind,
        )
        scored_items: list[dict[str, Any]] = []
        for item in safe_candidates:
            item_id = _safe_int(item.get("id"), 0)
            scored = _attach_hybrid_item_score(
                item,
                source_key=source_key,
                session_id=session_id,
                has_query=bool(tokens),
            )
            scored["retrieval_sources"] = sorted(sources_by_id.get(item_id, set()))
            scored_items.append(scored)
        scored_items.sort(
            key=lambda item: (
                _safe_float(item.get("hybrid_score"), 0.0),
                _safe_int(item.get("match_count"), 0),
                _timestamp_sort_value(
                    item.get("last_seen_at") or item.get("updated_at") or item.get("created_at")
                ),
                -_safe_int(item.get("id"), 0),
            ),
            reverse=True,
        )
        selected_items = scored_items[:safe_limit]
        selected_ids = {int(item["id"]) for item in selected_items if item.get("id") is not None}

        facts: list[dict[str, Any]] = []
        episodes: list[dict[str, Any]] = []
        safe_budget = max(
            100,
            min(
                int(
                    budget_chars
                    if budget_chars is not None
                    else _settings_int(
                        self.settings,
                        "memory_graph_retrieval_budget_chars",
                        600,
                        minimum=100,
                        maximum=3000,
                    )
                ),
                3000,
            ),
        )
        graph_debug: dict[str, Any] = {}
        if include_graph:
            safe_fact_top_k = max(
                0,
                min(
                    int(
                        fact_top_k
                        if fact_top_k is not None
                        else _settings_int(
                            self.settings,
                            "memory_graph_retrieval_fact_top_k",
                            HYBRID_GRAPH_FACT_BUDGET,
                            minimum=1,
                            maximum=10,
                        )
                    ),
                    HYBRID_GRAPH_FACT_BUDGET,
                    10,
                ),
            )
            safe_episode_top_k = max(
                0,
                min(
                    int(
                        episode_top_k
                        if episode_top_k is not None
                        else _settings_int(
                            self.settings,
                            "memory_graph_retrieval_episode_top_k",
                            HYBRID_GRAPH_EPISODE_BUDGET,
                            minimum=1,
                            maximum=10,
                        )
                    ),
                    HYBRID_GRAPH_EPISODE_BUDGET,
                    10,
                ),
            )
            graph = await self._retrieve_memory_graph_hybrid(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                session_id=session_id,
                query=query,
                fact_top_k=safe_fact_top_k,
                episode_top_k=safe_episode_top_k,
                budget_chars=safe_budget,
                exclude_memory_item_ids=selected_ids,
                debug=debug,
                request_session_kind=request_session_kind,
            )
            facts = list(graph.get("facts") or [])
            episodes = list(graph.get("episodes") or [])
            safe_budget = _safe_int(graph.get("budget_chars"), safe_budget)
            graph_debug = dict(graph.get("debug") or {})

        result: dict[str, Any] = {
            "items": selected_items,
            "facts": facts,
            "episodes": episodes,
            "budget_chars": safe_budget,
            "item_count": len(selected_items),
            "fact_count": len(facts),
            "episode_count": len(episodes),
        }
        if debug:
            result["debug"] = {
                "mode": "hybrid",
                "sql_item_candidates": len(sql_items),
                "item_candidates": len(scored_items),
                "vector_error": vector_error,
                "selected_item_ids": sorted(selected_ids),
                **graph_debug,
            }
        return result

    async def _retrieve_memory_items_sql(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        session_id: str,
        query: str = "",
        limit: int = 6,
        request_session_kind: str | None = None,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 6), 100))
        tokens = _memory_retrieval_tokens(query)
        conditions = [
            "tenant_id = :tid",
            "channel = :channel",
            "user_id = :uid",
            "deleted_at IS NULL",
            "status = 'active'",
            "sensitivity = 'normal'",
            "sensitivity_category = 'normal'",
            "(scope_type = 'identity' OR (scope_type = 'session' AND session_id = :sid))",
        ]
        params: dict[str, Any] = {
            "tid": tenant_id,
            "channel": channel,
            "uid": user_id,
            "sid": session_id,
            "lim": max(safe_limit * 8, 40),
        }
        if _needs_source_fallback(source_key):
            conditions.append("source_key IN (:source_key, '*')")
            params["source_key"] = source_key
        else:
            conditions.append("source_key = :source_key")
            params["source_key"] = source_key or "*"

        match_exprs: list[str] = []
        for index, token in enumerate(tokens):
            key = f"token_{index}"
            params[key] = f"%{token}%"
            match_exprs.append(f"(LOWER(content) LIKE :{key} OR LOWER(normalized_key) LIKE :{key})")
        match_score = (
            " + ".join(f"CASE WHEN {expr} THEN 1 ELSE 0 END" for expr in match_exprs) or "0"
        )
        rows = await _exec(
            "SELECT id, tenant_id, channel, source_key, user_id, session_id, scope_type, source_type, "
            "memory_type, content, value_json, normalized_key, confidence, status, pinned, priority, "
            "sensitivity, audience_scope, origin_session_kind, allowed_session_ids, source_kind, "
            "sensitivity_category, expires_at, source_event_id, source_trace_id, original_text, occurrence_count, "
            "first_seen_at, last_seen_at, created_at, updated_at, deleted_at, "
            f"({match_score}) AS match_count "
            "FROM plugin_memory_item "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY "
            f"CASE WHEN ({match_score}) > 0 THEN 0 ELSE 1 END, "
            "pinned DESC, priority DESC, updated_at DESC "
            "LIMIT :lim",
            params,
        )
        items = [
            self._finalize_memory_item(row) for row in rows if _looks_like_memory_item_row(row)
        ]
        return _rank_retrieved_memory_items(
            items,
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            has_query=bool(tokens),
            limit=safe_limit,
            request_session_kind=request_session_kind,
        )

    async def _retrieve_memory_items_vector(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        session_id: str,
        query: str,
        limit: int = 6,
        request_session_kind: str | None = None,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 6), 20))
        source_keys = [source_key or "*"]
        if _needs_source_fallback(source_key) and "*" not in source_keys:
            source_keys.append("*")
        hit_pairs = await self.vector_index.search_item_ids(
            tenant_id=tenant_id,
            channel=channel,
            source_keys=source_keys,
            user_id=user_id,
            session_id=session_id,
            query=query,
            top_k=max(safe_limit * 4, self.vector_index.default_top_k),
        )
        if not hit_pairs:
            return []

        items_by_id: dict[int, dict[str, Any]] = {}
        scores: dict[int, float] = {}
        for item_id, score in hit_pairs:
            item = await self.get_memory_item(item_id)
            if not item:
                continue
            if str(item.get("tenant_id") or "") != tenant_id:
                continue
            if str(item.get("channel") or "") != channel:
                continue
            item["vector_score"] = score
            # Vector similarity is not a keyword match.  Keeping the signals
            # separate lets the common relevance gate abstain on weak ANN hits.
            item["match_count"] = 0
            items_by_id[item_id] = item
            scores[item_id] = score

        ranked = _rank_retrieved_memory_items(
            list(items_by_id.values()),
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            has_query=True,
            limit=safe_limit,
            request_session_kind=request_session_kind,
        )
        score_order = {item_id: index for index, (item_id, _score) in enumerate(hit_pairs)}
        ranked.sort(
            key=lambda item: (
                scores.get(int(item.get("id") or 0), 0.0),
                -score_order.get(int(item.get("id") or 0), 999999),
            ),
            reverse=True,
        )
        return ranked[:safe_limit]

    async def rebuild_memory_item_vector_index(
        self,
        *,
        tenant_id: str | None = None,
        channel: str | None = None,
        source_key: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        limit: int = 1000,
        dry_run: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        if bool(getattr(self, "runtime_scope_gates_required", False)) and not tenant_id:
            raise RuntimeError("tenant_id required for memory vector rebuild")
        normalized_session_id: str | None = None
        if session_id is not None:
            normalized_session_id = str(session_id or "").strip()
            if not normalized_session_id:
                raise ValueError("session_id must be non-empty when supplied")
        if tenant_id:
            await self._require_vector_scope(
                tenant_id=tenant_id,
                session_id=normalized_session_id or "",
            )
        safe_limit = max(1, min(int(limit or 1000), 5000))
        conditions = [
            "status = 'active'",
            "sensitivity = 'normal'",
            "sensitivity_category = 'normal'",
            "deleted_at IS NULL",
            "(expires_at IS NULL OR expires_at > NOW())",
            "scope_type IN ('identity', 'session')",
        ]
        params: dict[str, Any] = {"lim": safe_limit}
        if tenant_id:
            conditions.append("tenant_id = :tid")
            params["tid"] = tenant_id
        if channel:
            conditions.append("channel = :channel")
            params["channel"] = channel
        if source_key:
            conditions.append("source_key = :source_key")
            params["source_key"] = source_key
        if user_id:
            conditions.append("user_id = :uid")
            params["uid"] = user_id
        if normalized_session_id is not None:
            # A group-scoped maintenance request must not turn its authorized
            # session into a tenant-wide embedding pass. Identity rows use an
            # empty session_id and are deliberately excluded as well.
            conditions.append("scope_type = 'session'")
            conditions.append("session_id = :sid")
            params["sid"] = normalized_session_id
        rows = await _exec(
            "SELECT id, tenant_id, channel, source_key, user_id, session_id, scope_type, source_type, "
            "memory_type, content, value_json, normalized_key, confidence, status, pinned, priority, "
            "sensitivity, audience_scope, origin_session_kind, allowed_session_ids, source_kind, "
            "sensitivity_category, expires_at, source_evidence_json, source_event_id, "
            "source_trace_id, original_text, occurrence_count, "
            "first_seen_at, last_seen_at, created_at, updated_at, deleted_at "
            "FROM plugin_memory_item "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY updated_at DESC, id DESC LIMIT :lim",
            params,
        )
        items = [
            self._finalize_memory_item(row) for row in rows if _looks_like_memory_item_row(row)
        ]
        scope_gate = self._vector_scope_gate()
        if dry_run:
            return await self.vector_index.rebuild_items(
                items,
                dry_run=True,
                force=force,
                scope_execution_allowed=scope_gate,
            )

        result: dict[str, Any] = {
            "enabled": self.vector_index.is_enabled,
            "available": self.vector_index.is_available,
            "dry_run": False,
            "collection": self.vector_index.collection,
            "scanned": len(items),
            "indexed": 0,
            "deleted": 0,
            "skipped": 0,
            "errors": 0,
        }
        available = self.vector_index.is_available if force else self.vector_index.is_enabled
        if not available:
            result["skipped"] = len(items)
            return result

        # Candidate rows are only an inventory. Each external publication
        # reopens the committed DB state, takes the same member fence used by
        # erasure, and rechecks expiry/member control immediately before the
        # vector upsert. This prevents a stale rebuild snapshot from restoring
        # a point after forget/expiry has already removed its source row.
        for item in items:
            try:
                status = await self._publish_current_memory_vectors(
                    int(item["id"]),
                    fallback_item=item,
                    force=force,
                    scope_execution_allowed=scope_gate,
                )
                if status in {"published", "indexed"}:
                    result["indexed"] += 1
                elif status == "deleted":
                    result["deleted"] += 1
                else:
                    result["skipped"] += 1
            except Exception as exc:
                result["errors"] += 1
                logger.warning(
                    "memory.vector_rebuild_item_failed",
                    item_id=item.get("id"),
                    error_type=exc.__class__.__name__,
                    error=str(exc)[:500],
                )
        return result

    async def smoke_memory_item_vector_search(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        session_id: str = "",
        query: str,
        limit: int = 3,
        force: bool = False,
    ) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit or 3), 20))
        source_keys = [source_key or "*"]
        if _needs_source_fallback(source_key) and "*" not in source_keys:
            source_keys.append("*")
        vector_hit_pairs: list[tuple[int, float]] = []
        vector_error = ""
        try:
            vector_hit_pairs = await self.vector_index.search_item_ids(
                tenant_id=tenant_id,
                channel=channel,
                source_keys=source_keys,
                user_id=user_id,
                session_id=session_id,
                query=query,
                top_k=max(safe_limit * 4, self.vector_index.default_top_k),
                force=force,
                scope_execution_allowed=self._vector_scope_gate(),
            )
        except Exception as exc:
            vector_error = f"{exc.__class__.__name__}:{_truncate_error(exc)}"

        fallback_items = await self._retrieve_memory_items_sql(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            query=query,
            limit=safe_limit,
        )
        fallback_ids = [int(item["id"]) for item in fallback_items if item.get("id") is not None]
        vector_ids = [item_id for item_id, _score in vector_hit_pairs[:safe_limit]]
        vector_hit = bool(vector_ids)
        fallback_ok = bool(fallback_ids)
        return {
            "enabled": self.vector_index.is_enabled,
            "available": self.vector_index.is_available,
            "collection": self.vector_index.collection,
            "query": query,
            "vector_hit": vector_hit,
            "vector_ids": vector_ids,
            "vector_scores": [
                {"id": item_id, "score": score} for item_id, score in vector_hit_pairs[:safe_limit]
            ],
            "vector_error": vector_error,
            "fallback_ok": fallback_ok,
            "fallback_ids": fallback_ids,
            "ok": vector_hit or fallback_ok,
            "behavior": "vector_hit" if vector_hit else "fallback" if fallback_ok else "miss",
        }

    async def smoke_memory_vector_enable(
        self,
        *,
        tenant_id: str | None = None,
        channel: str | None = None,
        source_key: str | None = None,
        user_id: str | None = None,
        session_id: str = "",
        query: str = "memory vector smoke",
        limit: int = 3,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        if bool(getattr(self, "runtime_scope_gates_required", False)) and not tenant_id:
            raise RuntimeError("tenant_id required for memory vector smoke")
        if tenant_id:
            await self._require_vector_scope(
                tenant_id=tenant_id,
                session_id=session_id,
            )
        safe_limit = max(1, min(int(limit or 3), 20))
        preflight = await self.vector_index.smoke_enable_preflight(
            tenant_id=str(tenant_id or ""),
            session_id=session_id,
            scope_execution_allowed=self._vector_scope_gate(),
        )
        rebuild = await self.rebuild_memory_item_vector_index(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=str(session_id or "").strip() or None,
            limit=safe_limit,
            dry_run=dry_run,
            force=True,
        )
        search: dict[str, Any] = {
            "ok": False,
            "skipped": True,
            "reason": "tenant_id_channel_user_id_query_required",
        }
        if tenant_id and channel and user_id and query.strip():
            search = await self.smoke_memory_item_vector_search(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key or "*",
                user_id=user_id,
                session_id=session_id,
                query=query,
                limit=safe_limit,
                force=True,
            )
        reasons = list(preflight.get("reasons") or [])
        if int(rebuild.get("errors") or 0) > 0:
            reasons.append("rebuild_errors")
        if not bool(search.get("skipped")) and search.get("vector_error"):
            reasons.append("search_smoke_vector_error")
        return {
            "safe_to_enable": bool(preflight.get("safe_to_enable"))
            and int(rebuild.get("errors") or 0) == 0
            and not search.get("vector_error"),
            "preflight": preflight,
            "rebuild": rebuild,
            "search": search,
            "reasons": reasons,
        }

    async def rebuild_memory_graph_vector_index(
        self,
        *,
        tenant_id: str | None = None,
        channel: str | None = None,
        source_key: str | None = None,
        user_id: str | None = None,
        limit: int = 1000,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if bool(getattr(self, "runtime_scope_gates_required", False)) and not tenant_id:
            raise RuntimeError("tenant_id required for memory graph vector rebuild")
        if tenant_id:
            await self._require_vector_scope(tenant_id=tenant_id)
        safe_limit = max(1, min(int(limit or 1000), 5000))
        fact_conditions = ["fact.status = 'active'", "fact.invalid_at IS NULL"]
        episode_conditions = ["status = 'active'"]
        params: dict[str, Any] = {"lim": safe_limit}
        if tenant_id:
            fact_conditions.append("fact.tenant_id = :tid")
            episode_conditions.append("tenant_id = :tid")
            params["tid"] = tenant_id
        if channel:
            fact_conditions.append("fact.channel = :channel")
            episode_conditions.append("channel = :channel")
            params["channel"] = channel
        if source_key:
            fact_conditions.append("fact.source_key = :source_key")
            episode_conditions.append("source_key = :source_key")
            params["source_key"] = source_key
        if user_id:
            fact_conditions.append("fact.user_id = :uid")
            episode_conditions.append("user_id = :uid")
            params["uid"] = user_id
        fact_rows = await _exec(
            "SELECT fact.id, fact.tenant_id, fact.channel, fact.source_key, fact.user_id, "
            "fact.subject_entity_id, subject.name AS subject_name, "
            "subject.normalized_name AS subject_normalized_name, fact.predicate, "
            "fact.object_entity_id, object_entity.name AS object_name, "
            "object_entity.normalized_name AS object_normalized_name, fact.object_value, "
            "fact.memory_item_id, fact.source_event_id, fact.confidence, fact.status, "
            "fact.valid_at, fact.invalid_at, fact.created_at, fact.updated_at "
            "FROM plugin_memory_fact fact "
            "LEFT JOIN plugin_memory_entity subject ON subject.id = fact.subject_entity_id "
            "AND subject.tenant_id = fact.tenant_id AND subject.channel = fact.channel "
            "AND subject.source_key = fact.source_key AND subject.user_id = fact.user_id "
            "LEFT JOIN plugin_memory_entity object_entity ON object_entity.id = fact.object_entity_id "
            "AND object_entity.tenant_id = fact.tenant_id AND object_entity.channel = fact.channel "
            "AND object_entity.source_key = fact.source_key AND object_entity.user_id = fact.user_id "
            f"WHERE {' AND '.join(fact_conditions)} "
            "ORDER BY fact.updated_at DESC, fact.id DESC LIMIT :lim",
            params,
        )
        episode_rows = await _exec(
            "SELECT id, tenant_id, channel, source_key, user_id, session_id, title, summary, "
            "event_ids_json, memory_item_ids_json, importance, status, created_at, updated_at "
            "FROM plugin_memory_episode "
            f"WHERE {' AND '.join(episode_conditions)} "
            "ORDER BY updated_at DESC, id DESC LIMIT :lim",
            params,
        )
        facts = list(fact_rows)
        episodes = [_finalize_graph_episode(row) for row in episode_rows]
        backing_item_ids: set[int] = set()
        for fact in facts:
            backing_item_ids.update(_coerce_int_set([fact.get("memory_item_id")]))
        for episode in episodes:
            backing_item_ids.update(_coerce_int_set(episode.get("memory_item_ids") or []))
        backing_items = await self._get_memory_items_by_ids(backing_item_ids)
        backing_by_id = {
            int(item["id"]): item for item in backing_items if item.get("id") is not None
        }
        graph_objects: list[dict[str, Any]] = []
        for fact in facts:
            memory_item_id = next(iter(_coerce_int_set([fact.get("memory_item_id")])), None)
            graph_objects.append(
                {
                    "object_type": "fact",
                    "row": fact,
                    "backing_item": backing_by_id.get(memory_item_id or 0),
                }
            )
        for episode in episodes:
            episode_item_ids = _coerce_int_set(episode.get("memory_item_ids") or [])
            graph_objects.append(
                {
                    "object_type": "episode",
                    "row": episode,
                    "backing_items": [
                        backing_by_id[item_id]
                        for item_id in sorted(episode_item_ids)
                        if item_id in backing_by_id
                    ],
                }
            )
        if dry_run:
            enabled = bool(self.vector_index.is_enabled)
            return {
                "enabled": enabled,
                "collection": self.vector_index.collection,
                "dry_run": True,
                "scanned": len(graph_objects),
                "would_index": len(graph_objects) if enabled else 0,
                "indexed": 0,
                "deleted": 0,
                "skipped": 0 if enabled else len(graph_objects),
                "errors": 0,
            }
        result: dict[str, Any] = {
            "enabled": self.vector_index.is_enabled,
            "collection": self.vector_index.collection,
            "dry_run": False,
            "scanned": len(graph_objects),
            "indexed": 0,
            "deleted": 0,
            "skipped": 0,
            "errors": 0,
        }
        if not self.vector_index.is_enabled:
            result["skipped"] = len(graph_objects)
            return result

        scope_gate = self._vector_scope_gate()
        # Facts and episodes must each be reloaded and published under the
        # erase/expiry-compatible fences. A batch snapshot followed by direct
        # vector upserts can otherwise restore a point after its relational
        # source has already been forgotten or physically expired.
        for graph_object in graph_objects:
            object_type = str(graph_object.get("object_type") or "")
            row = graph_object.get("row")
            object_id = _safe_int(
                row.get("id") if isinstance(row, dict) else None,
                0,
            )
            if object_type not in {"fact", "episode"} or object_id <= 0:
                result["skipped"] += 1
                continue
            try:
                status = await self._publish_current_memory_graph_vector(
                    object_type,
                    object_id,
                    fallback_row=row,
                    scope_execution_allowed=scope_gate,
                )
                if status in {"published", "indexed"}:
                    result["indexed"] += 1
                elif status == "deleted":
                    result["deleted"] += 1
                else:
                    result["skipped"] += 1
            except Exception as exc:
                result["errors"] += 1
                logger.warning(
                    "memory.vector_rebuild_graph_failed",
                    object_type=object_type,
                    object_id=object_id,
                    error_type=exc.__class__.__name__,
                    error=str(exc)[:500],
                )
        return result

    async def retrieve_memory_graph(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        session_id: str,
        query: str = "",
        fact_top_k: int = 3,
        episode_top_k: int = 2,
        budget_chars: int = 600,
        exclude_memory_item_ids: Iterable[Any] | None = None,
        request_session_kind: str | None = None,
    ) -> dict[str, Any]:
        if self.vector_index.is_enabled and _normalize_line(query):
            try:
                vector_graph = await self._retrieve_memory_graph_vector(
                    tenant_id=tenant_id,
                    channel=channel,
                    source_key=source_key,
                    user_id=user_id,
                    session_id=session_id,
                    query=query,
                    fact_top_k=fact_top_k,
                    episode_top_k=episode_top_k,
                    budget_chars=budget_chars,
                    exclude_memory_item_ids=exclude_memory_item_ids,
                    request_session_kind=request_session_kind,
                )
                if vector_graph.get("facts") or vector_graph.get("episodes"):
                    return vector_graph
            except Exception as exc:
                logger.warning(
                    "memory.graph_vector_retrieve_failed",
                    tenant_id=tenant_id,
                    channel=channel,
                    source_key=source_key,
                    user_id=user_id,
                    error_type=exc.__class__.__name__,
                    error=_truncate_error(exc),
                )
        return await self._retrieve_memory_graph_sql(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            query=query,
            fact_top_k=fact_top_k,
            episode_top_k=episode_top_k,
            budget_chars=budget_chars,
            exclude_memory_item_ids=exclude_memory_item_ids,
            request_session_kind=request_session_kind,
        )

    async def _retrieve_memory_graph_hybrid(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        session_id: str,
        query: str,
        fact_top_k: int,
        episode_top_k: int,
        budget_chars: int,
        exclude_memory_item_ids: Iterable[Any] | None,
        debug: bool = False,
        request_session_kind: str | None = None,
    ) -> dict[str, Any]:
        safe_fact_top_k = max(0, min(int(fact_top_k or 0), HYBRID_GRAPH_FACT_BUDGET, 10))
        safe_episode_top_k = max(0, min(int(episode_top_k or 0), HYBRID_GRAPH_EPISODE_BUDGET, 10))
        safe_budget = max(100, min(int(budget_chars or 600), 3000))
        fact_sources: dict[int, set[str]] = {}
        episode_sources: dict[int, set[str]] = {}
        fact_vector_scores: dict[int, float] = {}
        episode_vector_scores: dict[int, float] = {}
        vector_error = ""

        sql_graph = await self._retrieve_memory_graph_sql(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            query=query,
            fact_top_k=max(safe_fact_top_k * HYBRID_GRAPH_CANDIDATE_MULTIPLIER, safe_fact_top_k),
            episode_top_k=max(
                safe_episode_top_k * HYBRID_GRAPH_CANDIDATE_MULTIPLIER, safe_episode_top_k
            ),
            budget_chars=safe_budget,
            exclude_memory_item_ids=exclude_memory_item_ids,
            request_session_kind=request_session_kind,
        )
        facts_by_id: dict[int, dict[str, Any]] = {}
        episodes_by_id: dict[int, dict[str, Any]] = {}
        for fact in sql_graph.get("facts") or []:
            fact_id = _safe_int(fact.get("id"), 0)
            if fact_id <= 0:
                continue
            facts_by_id[fact_id] = dict(fact)
            fact_sources.setdefault(fact_id, set()).add("sql")
        for episode in sql_graph.get("episodes") or []:
            episode_id = _safe_int(episode.get("id"), 0)
            if episode_id <= 0:
                continue
            episodes_by_id[episode_id] = dict(episode)
            episode_sources.setdefault(episode_id, set()).add("sql")

        if (
            self.vector_index.is_enabled
            and _normalize_line(query)
            and (safe_fact_top_k > 0 or safe_episode_top_k > 0)
        ):
            source_keys = [source_key or "*"]
            if _needs_source_fallback(source_key) and "*" not in source_keys:
                source_keys.append("*")
            try:
                fact_hits: list[tuple[int, float]] = []
                episode_hits: list[tuple[int, float]] = []
                if safe_fact_top_k > 0:
                    fact_hits = await self.vector_index.search_graph_ids(
                        tenant_id=tenant_id,
                        channel=channel,
                        source_keys=source_keys,
                        user_id=user_id,
                        session_id=session_id,
                        query=query,
                        object_type="fact",
                        top_k=max(safe_fact_top_k * 4, self.vector_index.graph_top_k),
                    )
                if safe_episode_top_k > 0:
                    episode_hits = await self.vector_index.search_graph_ids(
                        tenant_id=tenant_id,
                        channel=channel,
                        source_keys=source_keys,
                        user_id=user_id,
                        session_id=session_id,
                        query=query,
                        object_type="episode",
                        top_k=max(safe_episode_top_k * 4, self.vector_index.graph_top_k),
                    )
                vector_graph = await self._retrieve_memory_graph_sql(
                    tenant_id=tenant_id,
                    channel=channel,
                    source_key=source_key,
                    user_id=user_id,
                    session_id=session_id,
                    query=query,
                    fact_top_k=max(len(fact_hits), safe_fact_top_k),
                    episode_top_k=max(len(episode_hits), safe_episode_top_k),
                    budget_chars=safe_budget,
                    exclude_memory_item_ids=exclude_memory_item_ids,
                    request_session_kind=request_session_kind,
                    candidate_fact_ids=[item_id for item_id, _score in fact_hits]
                    if fact_hits
                    else [],
                    candidate_episode_ids=[item_id for item_id, _score in episode_hits]
                    if episode_hits
                    else [],
                )
                fact_vector_scores = {
                    int(item_id): _safe_float(score, 0.0) for item_id, score in fact_hits
                }
                episode_vector_scores = {
                    int(item_id): _safe_float(score, 0.0) for item_id, score in episode_hits
                }
                for fact in vector_graph.get("facts") or []:
                    fact_id = _safe_int(fact.get("id"), 0)
                    if fact_id <= 0:
                        continue
                    merged = facts_by_id.get(fact_id, dict(fact))
                    merged.update(
                        {
                            key: value
                            for key, value in fact.items()
                            if key not in {"score", "reason"}
                        }
                    )
                    facts_by_id[fact_id] = merged
                    fact_sources.setdefault(fact_id, set()).add("vector")
                for episode in vector_graph.get("episodes") or []:
                    episode_id = _safe_int(episode.get("id"), 0)
                    if episode_id <= 0:
                        continue
                    merged = episodes_by_id.get(episode_id, dict(episode))
                    merged.update(
                        {
                            key: value
                            for key, value in episode.items()
                            if key not in {"score", "reason"}
                        }
                    )
                    episodes_by_id[episode_id] = merged
                    episode_sources.setdefault(episode_id, set()).add("vector")
            except Exception as exc:
                vector_error = f"{exc.__class__.__name__}:{_truncate_error(exc)}"
                logger.warning(
                    "memory.hybrid_graph_vector_retrieve_failed",
                    tenant_id=tenant_id,
                    channel=channel,
                    source_key=source_key,
                    user_id=user_id,
                    error_type=exc.__class__.__name__,
                    error=_truncate_error(exc),
                )

        def graph_score(row: dict[str, Any], *, object_type: str) -> tuple[float, int, str]:
            row_id = _safe_int(row.get("id"), 0)
            vector_score = _normalize_vector_score(
                fact_vector_scores.get(row_id)
                if object_type == "fact"
                else episode_vector_scores.get(row_id)
            )
            match_count = _safe_int(row.get("match_count"), 0)
            base_score = _safe_float(row.get("score"), 0.0)
            hybrid_score = base_score + vector_score * 80.0 + match_count * 20.0
            breakdown = {
                "hybrid_score": round(hybrid_score, 3),
                "vector_score": round(vector_score, 6),
                "keyword_score": round(float(match_count) * 20.0, 3),
                "match_count": match_count,
                "source_score": round(base_score, 3),
                "graph_budget": object_type,
            }
            row["hybrid_score"] = breakdown["hybrid_score"]
            row["hybrid_score_breakdown"] = breakdown
            row["retrieval_sources"] = sorted(
                (fact_sources if object_type == "fact" else episode_sources).get(row_id, set())
            )
            return (
                _safe_float(row.get("hybrid_score"), 0.0),
                match_count,
                str(row.get("updated_at") or row.get("created_at") or row.get("id") or ""),
            )

        facts = list(facts_by_id.values())
        episodes = list(episodes_by_id.values())
        facts.sort(key=lambda row: graph_score(row, object_type="fact"), reverse=True)
        episodes.sort(key=lambda row: graph_score(row, object_type="episode"), reverse=True)
        result: dict[str, Any] = {
            "facts": facts[:safe_fact_top_k],
            "episodes": episodes[:safe_episode_top_k],
            "budget_chars": safe_budget,
            "fact_count": min(len(facts), safe_fact_top_k),
            "episode_count": min(len(episodes), safe_episode_top_k),
        }
        if debug:
            result["debug"] = {
                "sql_graph_fact_candidates": len(sql_graph.get("facts") or []),
                "sql_graph_episode_candidates": len(sql_graph.get("episodes") or []),
                "graph_fact_candidates": len(facts),
                "graph_episode_candidates": len(episodes),
                "graph_vector_error": vector_error,
            }
        return result

    async def _retrieve_memory_graph_vector(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        session_id: str,
        query: str,
        fact_top_k: int,
        episode_top_k: int,
        budget_chars: int,
        exclude_memory_item_ids: Iterable[Any] | None,
        request_session_kind: str | None = None,
    ) -> dict[str, Any]:
        safe_fact_top_k = max(0, min(int(fact_top_k or 0), 10))
        safe_episode_top_k = max(0, min(int(episode_top_k or 0), 10))
        source_keys = [source_key or "*"]
        if _needs_source_fallback(source_key) and "*" not in source_keys:
            source_keys.append("*")
        fact_hits: list[tuple[int, float]] = []
        episode_hits: list[tuple[int, float]] = []
        if safe_fact_top_k > 0:
            fact_hits = await self.vector_index.search_graph_ids(
                tenant_id=tenant_id,
                channel=channel,
                source_keys=source_keys,
                user_id=user_id,
                session_id=session_id,
                query=query,
                object_type="fact",
                top_k=max(safe_fact_top_k * 4, self.vector_index.graph_top_k),
            )
        if safe_episode_top_k > 0:
            episode_hits = await self.vector_index.search_graph_ids(
                tenant_id=tenant_id,
                channel=channel,
                source_keys=source_keys,
                user_id=user_id,
                session_id=session_id,
                query=query,
                object_type="episode",
                top_k=max(safe_episode_top_k * 4, self.vector_index.graph_top_k),
            )
        if not fact_hits and not episode_hits:
            return {
                "facts": [],
                "episodes": [],
                "budget_chars": max(100, min(int(budget_chars or 600), 3000)),
                "fact_count": 0,
                "episode_count": 0,
            }
        return await self._retrieve_memory_graph_sql(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            query=query,
            fact_top_k=fact_top_k,
            episode_top_k=episode_top_k,
            budget_chars=budget_chars,
            exclude_memory_item_ids=exclude_memory_item_ids,
            candidate_fact_ids=(
                [item_id for item_id, _score in fact_hits]
                if fact_hits or safe_fact_top_k <= 0
                else [0]
            ),
            candidate_episode_ids=(
                [item_id for item_id, _score in episode_hits]
                if episode_hits or safe_episode_top_k <= 0
                else [0]
            ),
            request_session_kind=request_session_kind,
        )

    async def _retrieve_memory_graph_sql(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        session_id: str,
        query: str = "",
        fact_top_k: int = 3,
        episode_top_k: int = 2,
        budget_chars: int = 600,
        exclude_memory_item_ids: Iterable[Any] | None = None,
        candidate_fact_ids: Iterable[Any] | None = None,
        candidate_episode_ids: Iterable[Any] | None = None,
        request_session_kind: str | None = None,
    ) -> dict[str, Any]:
        safe_fact_top_k = max(0, min(int(fact_top_k or 0), 10))
        safe_episode_top_k = max(0, min(int(episode_top_k or 0), 10))
        safe_budget = max(100, min(int(budget_chars or 600), 3000))
        excluded_ids = _coerce_int_set(exclude_memory_item_ids)
        tokens = _memory_retrieval_tokens(query)
        fact_candidate_ids = sorted(_coerce_int_set(candidate_fact_ids))
        episode_candidate_ids = sorted(_coerce_int_set(candidate_episode_ids))

        scope_conditions = [
            "tenant_id = :tid",
            "channel = :channel",
            "user_id = :uid",
            "status = 'active'",
        ]
        fact_scope_conditions = [
            "fact.tenant_id = :tid",
            "fact.channel = :channel",
            "fact.user_id = :uid",
            "fact.status = 'active'",
        ]
        scoped_params: dict[str, Any] = {
            "tid": tenant_id,
            "channel": channel,
            "uid": user_id,
            "sid": session_id,
            "lim": 80,
        }
        if _needs_source_fallback(source_key):
            scope_conditions.append("source_key IN (:source_key, '*')")
            fact_scope_conditions.append("fact.source_key IN (:source_key, '*')")
            scoped_params["source_key"] = source_key
        else:
            scope_conditions.append("source_key = :source_key")
            fact_scope_conditions.append("fact.source_key = :source_key")
            scoped_params["source_key"] = source_key or "*"
        if candidate_fact_ids is not None:
            if fact_candidate_ids:
                fact_scope_conditions.append("fact.id = ANY(:fact_candidate_ids)")
                scoped_params["fact_candidate_ids"] = fact_candidate_ids
            else:
                fact_scope_conditions.append("1 = 0")
        if candidate_episode_ids is not None:
            if episode_candidate_ids:
                scope_conditions.append("id = ANY(:episode_candidate_ids)")
                scoped_params["episode_candidate_ids"] = episode_candidate_ids
            else:
                scope_conditions.append("1 = 0")

        fact_rows: list[dict[str, Any]] = []
        if safe_fact_top_k > 0:
            fact_rows = await _exec(
                "SELECT fact.id, fact.tenant_id, fact.channel, fact.source_key, fact.user_id, "
                "fact.subject_entity_id, subject.name AS subject_name, "
                "subject.normalized_name AS subject_normalized_name, fact.predicate, "
                "fact.object_entity_id, object_entity.name AS object_name, "
                "object_entity.normalized_name AS object_normalized_name, fact.object_value, "
                "fact.memory_item_id, fact.source_event_id, fact.confidence, fact.status, "
                "fact.valid_at, fact.invalid_at, fact.created_at, fact.updated_at, "
                "item.source_type AS item_source_type, item.pinned AS item_pinned, "
                "item.priority AS item_priority, item.confidence AS item_confidence, "
                "item.deleted_at AS item_deleted_at, item.status AS item_status, "
                "item.sensitivity AS item_sensitivity, "
                "item.audience_scope AS item_audience_scope, "
                "item.origin_session_kind AS item_origin_session_kind, "
                "item.allowed_session_ids AS item_allowed_session_ids, "
                "item.sensitivity_category AS item_sensitivity_category, "
                "item.expires_at AS item_expires_at, item.session_id AS item_session_id, "
                "item.value_json AS item_value_json "
                "FROM plugin_memory_fact fact "
                "LEFT JOIN plugin_memory_entity subject ON subject.id = fact.subject_entity_id "
                "AND subject.tenant_id = fact.tenant_id AND subject.channel = fact.channel "
                "AND subject.source_key = fact.source_key AND subject.user_id = fact.user_id "
                "LEFT JOIN plugin_memory_entity object_entity ON object_entity.id = fact.object_entity_id "
                "AND object_entity.tenant_id = fact.tenant_id AND object_entity.channel = fact.channel "
                "AND object_entity.source_key = fact.source_key AND object_entity.user_id = fact.user_id "
                "JOIN plugin_memory_item item ON item.id = fact.memory_item_id "
                "AND item.tenant_id = fact.tenant_id AND item.channel = fact.channel "
                "AND item.source_key = fact.source_key AND item.user_id = fact.user_id "
                "WHERE " + " AND ".join(fact_scope_conditions) + " AND fact.invalid_at IS NULL "
                "AND item.deleted_at IS NULL AND item.status = 'active' "
                "AND item.sensitivity = 'normal' AND item.sensitivity_category = 'normal' "
                "AND (NULLIF(item.value_json, '') IS NULL "
                "OR NULLIF(item.value_json, '')::jsonb #>> '{acceptance,status}' IS NULL "
                "OR NULLIF(item.value_json, '')::jsonb #>> '{acceptance,status}' = 'accepted') "
                "AND (item.scope_type = 'identity' OR (item.scope_type = 'session' AND item.session_id = :sid)) "
                "ORDER BY fact.updated_at DESC, fact.id DESC LIMIT :lim",
                scoped_params,
            )

        episode_rows: list[dict[str, Any]] = []
        if safe_episode_top_k > 0:
            episode_where = [
                *scope_conditions,
                "(session_id = '' OR session_id = :sid)",
            ]
            episode_rows = await _exec(
                "SELECT id, tenant_id, channel, source_key, user_id, session_id, title, summary, "
                "event_ids_json, memory_item_ids_json, importance, status, created_at, updated_at "
                "FROM plugin_memory_episode "
                f"WHERE {' AND '.join(episode_where)} "
                "ORDER BY updated_at DESC, id DESC LIMIT :lim",
                scoped_params,
            )

        visible_episode_item_ids: set[int] = set()
        episode_item_ids: set[int] = set()
        for row in episode_rows:
            episode_item_ids.update(
                _coerce_int_set(_safe_json_loads(row.get("memory_item_ids_json"), []))
            )
        if episode_item_ids:
            item_params = dict(scoped_params)
            item_params["memory_item_ids"] = list(episode_item_ids)
            item_rows = await _exec(
                "SELECT id, user_id, session_id, sensitivity, audience_scope, origin_session_kind, "
                "allowed_session_ids, sensitivity_category, expires_at FROM plugin_memory_item "
                "WHERE tenant_id = :tid AND channel = :channel AND user_id = :uid "
                "AND id = ANY(:memory_item_ids) "
                "AND deleted_at IS NULL AND status = 'active' "
                "AND sensitivity = 'normal' AND sensitivity_category = 'normal' "
                "AND (scope_type = 'identity' OR (scope_type = 'session' AND session_id = :sid)) "
                "AND (NULLIF(value_json, '') IS NULL "
                "OR NULLIF(value_json, '')::jsonb #>> '{acceptance,status}' IS NULL "
                "OR NULLIF(value_json, '')::jsonb #>> '{acceptance,status}' = 'accepted') "
                "AND ("
                + (
                    "source_key IN (:source_key, '*')"
                    if _needs_source_fallback(source_key)
                    else "source_key = :source_key"
                )
                + ")",
                item_params,
            )
            visible_episode_item_ids = _coerce_int_set(
                row.get("id")
                for row in item_rows
                if _memory_item_visible_for_audience(
                    row,
                    session_id=session_id,
                    user_id=user_id,
                    request_session_kind=request_session_kind,
                )
            )

        facts: list[dict[str, Any]] = []
        for index, row in enumerate(fact_rows):
            if str(row.get("status") or "") != "active":
                continue
            if row.get("invalid_at") is not None:
                continue
            if not _memory_item_visible_for_audience(
                {
                    "user_id": row.get("user_id"),
                    "session_id": row.get("item_session_id"),
                    "sensitivity": row.get("item_sensitivity"),
                    "audience_scope": row.get("item_audience_scope"),
                    "origin_session_kind": row.get("item_origin_session_kind"),
                    "allowed_session_ids": row.get("item_allowed_session_ids"),
                    "sensitivity_category": row.get("item_sensitivity_category"),
                    "expires_at": row.get("item_expires_at"),
                },
                session_id=session_id,
                user_id=user_id,
                request_session_kind=request_session_kind,
            ):
                continue
            item_id = row.get("memory_item_id")
            try:
                item_id_int = int(item_id)
            except (TypeError, ValueError):
                item_id_int = None
            if item_id_int is not None and item_id_int in excluded_ids:
                continue
            match_count = _graph_query_match_count(
                (
                    row.get("subject_name"),
                    row.get("subject_normalized_name"),
                    row.get("object_name"),
                    row.get("object_normalized_name"),
                    row.get("predicate"),
                    row.get("object_value"),
                ),
                tokens,
            )
            if tokens and match_count <= 0:
                continue
            source_type = str(row.get("item_source_type") or "")
            try:
                priority = int(row.get("item_priority") or 0)
            except (TypeError, ValueError):
                priority = 0
            try:
                confidence = max(
                    float(row.get("item_confidence") or 0.0), float(row.get("confidence") or 0.0)
                )
            except (TypeError, ValueError):
                confidence = float(row.get("confidence") or 0.0)
            recency_boost = max(0.0, (len(fact_rows) - index) / max(len(fact_rows), 1))
            score = 0.0
            score += match_count * 100.0
            score += {"manual": 22.0, "explicit_user": 18.0, "auto": 4.0, "backfill": 1.0}.get(
                source_type,
                0.0,
            )
            if row.get("item_pinned"):
                score += 24.0
            score += min(max(priority, 0), 100) * 0.35
            score += confidence * 12.0
            score += 2.0
            score += recency_boost
            facts.append(
                {
                    "id": row.get("id"),
                    "tenant_id": row.get("tenant_id"),
                    "channel": row.get("channel"),
                    "source_key": row.get("source_key"),
                    "user_id": row.get("user_id"),
                    "subject_name": row.get("subject_name") or "",
                    "subject_normalized_name": row.get("subject_normalized_name") or "",
                    "predicate": row.get("predicate") or "",
                    "object_name": row.get("object_name") or "",
                    "object_normalized_name": row.get("object_normalized_name") or "",
                    "object_value": row.get("object_value") or "",
                    "memory_item_id": item_id_int,
                    "source_event_id": row.get("source_event_id"),
                    "confidence": float(row.get("confidence") or 0.0),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                    "score": round(score, 3),
                    "reason": _graph_reason(
                        match_count=match_count,
                        source_type=source_type,
                        pinned=bool(row.get("item_pinned")),
                        priority=priority,
                        confidence=confidence,
                    ),
                    "match_count": match_count,
                    "recency_boost": round(recency_boost, 3),
                }
            )

        episodes: list[dict[str, Any]] = []
        for index, row in enumerate(episode_rows):
            episode = _finalize_graph_episode(row)
            memory_item_ids = _coerce_int_set(episode.get("memory_item_ids"))
            if memory_item_ids and not memory_item_ids.issubset(visible_episode_item_ids):
                continue
            effective_session_kind = str(request_session_kind or "").strip().lower()
            if effective_session_kind not in {"private", "group"}:
                effective_session_kind = (
                    "group" if _store_runtime._is_group_session_id(session_id) else "private"
                )
            if effective_session_kind == "group" and not memory_item_ids:
                continue
            if memory_item_ids and memory_item_ids.issubset(excluded_ids):
                continue
            match_count = _graph_query_match_count(
                (episode.get("title"), episode.get("summary")), tokens
            )
            if tokens and match_count <= 0:
                continue
            importance = int(episode.get("importance") or 0)
            recency_boost = max(0.0, (len(episode_rows) - index) / max(len(episode_rows), 1))
            score = match_count * 100.0 + min(max(importance, 0), 100) * 0.8 + 2.0 + recency_boost
            if memory_item_ids and memory_item_ids.intersection(excluded_ids):
                score -= 20.0
            episodes.append(
                {
                    "id": episode.get("id"),
                    "tenant_id": episode.get("tenant_id"),
                    "channel": episode.get("channel"),
                    "source_key": episode.get("source_key"),
                    "user_id": episode.get("user_id"),
                    "session_id": episode.get("session_id") or "",
                    "title": episode.get("title") or "",
                    "summary": episode.get("summary") or "",
                    "event_ids": episode.get("event_ids") or [],
                    "memory_item_ids": sorted(memory_item_ids),
                    "importance": importance,
                    "created_at": episode.get("created_at"),
                    "updated_at": episode.get("updated_at"),
                    "score": round(score, 3),
                    "reason": _graph_reason(match_count=match_count, importance=importance),
                    "match_count": match_count,
                    "recency_boost": round(recency_boost, 3),
                }
            )

        facts.sort(
            key=lambda item: (
                float(item.get("score") or 0.0),
                int(item.get("match_count") or 0),
                str(item.get("id") or ""),
            ),
            reverse=True,
        )
        episodes.sort(
            key=lambda item: (
                float(item.get("score") or 0.0),
                int(item.get("match_count") or 0),
                str(item.get("id") or ""),
            ),
            reverse=True,
        )
        return {
            "facts": facts[:safe_fact_top_k],
            "episodes": episodes[:safe_episode_top_k],
            "budget_chars": safe_budget,
            "fact_count": min(len(facts), safe_fact_top_k),
            "episode_count": min(len(episodes), safe_episode_top_k),
        }
