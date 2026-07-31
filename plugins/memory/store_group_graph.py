"""Group relationship graph reads, evidence, extraction, review, and synchronization."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import timedelta
from typing import Any

from app.common.types import ChatMessage, ChatRequest, Role
from plugins.memory import store as _store_runtime
from plugins.memory.store import (
    DETERMINISTIC_GROUP_WINDOW_SOURCE_TYPE,
    GROUP_GRAPH_EDGE_TYPES,
    GROUP_GRAPH_NODE_TYPES,
    GROUP_GRAPH_SCHEMA_VERSION,
    GROUP_HISTORY_USER_ID_SCOPE,
    GROUP_WINDOW_DETERMINISTIC_MAX_PAIRS,
    GROUP_WINDOW_DETERMINISTIC_MAX_SENDERS,
    LLM_GROUP_WINDOW_SOURCE_TYPE,
    MEMORY_ACCEPTANCE_REVIEW_ACTIONS,
    _append_unique_int,
    _build_group_relationship_edge_evidence_payload,
    _clamp_int,
    _clamp_score,
    _coerce_datetime,
    _coerce_int_set,
    _daily_relationship_run_key,
    _extract_group_event_sender_id,
    _group_graph_acceptance_status,
    _group_graph_default_acceptance_allowed,
    _group_graph_edge_id,
    _group_graph_entity_aliases,
    _group_graph_entity_display_label,
    _group_graph_node_id,
    _group_graph_scope,
    _group_graph_timestamp,
    _group_history_user_scope,
    _is_group_session_id,
    _loads_json_object_or_array,
    _looks_like_wechat_username,
    _merge_group_graph_aliases,
    _merge_int_lists,
    _normalize_key,
    _normalize_line,
    _parse_daily_relationship_date,
    _safe_int,
    _safe_json_loads,
    _sanitize_db_text,
    _split_group_event_text,
    _truncate_error,
    _wechat_contact_display_label,
    _window_relationship_normalized_key,
    logger,
)


async def _exec(sql: str, params: dict | None = None) -> list[dict]:
    return await _store_runtime._exec(sql, params)


def monotonic() -> float:
    return _store_runtime.monotonic()


class MemoryGroupGraphStoreMixin:
    async def list_memory_graph_entities(
        self,
        *,
        tenant_id: str,
        channel: str | None = None,
        source_key: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions = ["entity.tenant_id = :tid"]
        params: dict[str, Any] = {"tid": tenant_id, "lim": max(1, min(int(limit or 100), 500))}
        if channel is not None:
            conditions.append("entity.channel = :channel")
            params["channel"] = channel
        if source_key is not None:
            conditions.append("entity.source_key = :source_key")
            params["source_key"] = source_key
        if user_id is not None:
            conditions.append("entity.user_id = :uid")
            params["uid"] = user_id
        if session_id is not None:
            conditions.append(
                "EXISTS ("
                "SELECT 1 FROM plugin_memory_fact scope_fact "
                "JOIN plugin_memory_item scope_item "
                "ON scope_item.id = scope_fact.memory_item_id "
                "AND scope_item.tenant_id = scope_fact.tenant_id "
                "AND scope_item.channel = scope_fact.channel "
                "AND scope_item.source_key = scope_fact.source_key "
                "AND scope_item.user_id = scope_fact.user_id "
                "WHERE scope_fact.tenant_id = entity.tenant_id "
                "AND scope_fact.channel = entity.channel "
                "AND scope_fact.source_key = entity.source_key "
                "AND scope_fact.user_id = entity.user_id "
                "AND (scope_fact.subject_entity_id = entity.id "
                "OR scope_fact.object_entity_id = entity.id) "
                "AND scope_item.session_id = :sid "
                "AND scope_item.deleted_at IS NULL "
                "AND scope_item.status NOT IN ('deleted', 'invalidated')"
                ")"
            )
            params["sid"] = session_id
        if status is not None:
            conditions.append("entity.status = :status")
            params["status"] = status
        rows = await _exec(
            "SELECT entity.id, entity.tenant_id, entity.channel, entity.source_key, "
            "entity.user_id, entity.entity_type, entity.name, entity.normalized_name, "
            "entity.aliases_json, entity.confidence, entity.status, "
            "entity.created_at, entity.updated_at "
            "FROM plugin_memory_entity entity "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY entity.updated_at DESC, entity.id DESC LIMIT :lim",
            params,
        )
        for row in rows:
            row["aliases"] = _safe_json_loads(row.get("aliases_json"), [])
            row["confidence"] = float(row.get("confidence") or 0.0)
        return rows

    async def list_memory_graph_facts(
        self,
        *,
        tenant_id: str,
        channel: str | None = None,
        source_key: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions = ["fact.tenant_id = :tid"]
        params: dict[str, Any] = {"tid": tenant_id, "lim": max(1, min(int(limit or 100), 500))}
        if channel is not None:
            conditions.append("fact.channel = :channel")
            params["channel"] = channel
        if source_key is not None:
            conditions.append("fact.source_key = :source_key")
            params["source_key"] = source_key
        if user_id is not None:
            conditions.append("fact.user_id = :uid")
            params["uid"] = user_id
        if session_id is not None:
            conditions.append(
                "EXISTS ("
                "SELECT 1 FROM plugin_memory_item scope_item "
                "WHERE scope_item.id = fact.memory_item_id "
                "AND scope_item.tenant_id = fact.tenant_id "
                "AND scope_item.channel = fact.channel "
                "AND scope_item.source_key = fact.source_key "
                "AND scope_item.user_id = fact.user_id "
                "AND scope_item.session_id = :sid "
                "AND scope_item.deleted_at IS NULL "
                "AND scope_item.status NOT IN ('deleted', 'invalidated')"
                ")"
            )
            params["sid"] = session_id
        if status is not None:
            conditions.append("fact.status = :status")
            params["status"] = status
        rows = await _exec(
            "SELECT fact.id, fact.tenant_id, fact.channel, fact.source_key, fact.user_id, "
            "fact.subject_entity_id, subject.name AS subject_name, fact.predicate, "
            "fact.object_entity_id, object_entity.name AS object_name, fact.object_value, "
            "fact.memory_item_id, fact.source_event_id, fact.confidence, fact.status, "
            "fact.valid_at, fact.invalid_at, fact.created_at, fact.updated_at "
            "FROM plugin_memory_fact fact "
            "LEFT JOIN plugin_memory_entity subject ON subject.id = fact.subject_entity_id "
            "AND subject.tenant_id = fact.tenant_id AND subject.channel = fact.channel "
            "AND subject.source_key = fact.source_key AND subject.user_id = fact.user_id "
            "LEFT JOIN plugin_memory_entity object_entity ON object_entity.id = fact.object_entity_id "
            "AND object_entity.tenant_id = fact.tenant_id AND object_entity.channel = fact.channel "
            "AND object_entity.source_key = fact.source_key AND object_entity.user_id = fact.user_id "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY fact.updated_at DESC, fact.id DESC LIMIT :lim",
            params,
        )
        for row in rows:
            row["confidence"] = float(row.get("confidence") or 0.0)
        return rows

    async def list_memory_graph_episodes(
        self,
        *,
        tenant_id: str,
        channel: str | None = None,
        source_key: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        conditions = ["tenant_id = :tid"]
        params: dict[str, Any] = {"tid": tenant_id, "lim": max(1, min(int(limit or 100), 500))}
        if channel is not None:
            conditions.append("channel = :channel")
            params["channel"] = channel
        if source_key is not None:
            conditions.append("source_key = :source_key")
            params["source_key"] = source_key
        if user_id is not None:
            conditions.append("user_id = :uid")
            params["uid"] = user_id
        if session_id is not None:
            conditions.append("session_id = :sid")
            params["sid"] = session_id
        if status is not None:
            conditions.append("status = :status")
            params["status"] = status
        rows = await _exec(
            "SELECT id, tenant_id, channel, source_key, user_id, session_id, title, summary, "
            "event_ids_json, memory_item_ids_json, importance, status, created_at, updated_at "
            "FROM plugin_memory_episode "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY updated_at DESC, id DESC LIMIT :lim",
            params,
        )
        for row in rows:
            row["event_ids"] = _safe_json_loads(row.get("event_ids_json"), [])
            row["memory_item_ids"] = _safe_json_loads(row.get("memory_item_ids_json"), [])
            row["importance"] = int(row.get("importance") or 0)
        return rows

    async def get_group_relationship_graph(
        self,
        *,
        tenant_id: str,
        channel: str | None = None,
        source_key: str | None = None,
        session_id: str | None = None,
        from_: Any = None,
        to: Any = None,
        node_type: str | None = None,
        edge_type: str | None = None,
        acceptance_status: str | None = None,
        min_confidence: float | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit or 500), 500))
        fetch_limit = min(max(safe_limit * 3, safe_limit), 500)
        requested_acceptance = {
            value.strip().lower()
            for value in str(acceptance_status or "").split(",")
            if value.strip()
        }
        confidence_floor = (
            _clamp_score(min_confidence, default=0.0) if min_confidence is not None else None
        )
        from_dt = _coerce_datetime(from_)
        to_dt = _coerce_datetime(to)
        status_filter = None if requested_acceptance - {"accepted"} else "active"
        generated_from = ["plugin_memory_entity", "plugin_memory_fact", "plugin_memory_episode"]
        scope = _group_graph_scope(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            session_id=session_id,
        )
        filters = {
            "tenant_id": tenant_id,
            "channel": channel,
            "source_key": source_key,
            "session_id": session_id,
            "from": from_,
            "to": to,
            "node_type": node_type,
            "edge_type": edge_type,
            "acceptance_status": sorted(requested_acceptance) if requested_acceptance else None,
            "min_confidence": min_confidence,
            "limit": safe_limit,
        }

        entities = await self.list_memory_graph_entities(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=None,
            status=status_filter,
            limit=fetch_limit,
        )
        facts = await self.list_memory_graph_facts(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=None,
            status=status_filter,
            limit=fetch_limit,
        )
        episodes = await self.list_memory_graph_episodes(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=None,
            session_id=session_id,
            status=status_filter,
            limit=fetch_limit,
        )

        memory_item_ids: set[int] = set()
        for fact in facts:
            memory_item_ids.update(_coerce_int_set([fact.get("memory_item_id")]))
        for episode in episodes:
            memory_item_ids.update(_coerce_int_set(episode.get("memory_item_ids") or []))
        backing_items = (
            await self._get_sanitized_memory_items_by_ids(memory_item_ids)
            if memory_item_ids
            else []
        )
        item_by_id = {int(item["id"]): item for item in backing_items if item.get("id") is not None}

        event_ids_for_metadata: set[int] = set()
        if session_id is not None:
            for fact in facts:
                event_ids_for_metadata.update(_coerce_int_set([fact.get("source_event_id")]))
            for item in backing_items:
                event_ids_for_metadata.update(_coerce_int_set([item.get("source_event_id")]))
            for episode in episodes:
                event_ids_for_metadata.update(_coerce_int_set(episode.get("event_ids") or []))
        event_metadata = (
            await self._get_memory_event_metadata_by_ids(event_ids_for_metadata)
            if event_ids_for_metadata
            else []
        )
        event_session_by_id = {
            int(event["id"]): str(event.get("session_id") or "")
            for event in event_metadata
            if event.get("id") is not None
        }

        event_ids_by_item_id: dict[int, list[int]] = {}
        memory_ids_by_item_id: dict[int, list[int]] = {}
        for episode in episodes:
            event_ids = sorted(_coerce_int_set(episode.get("event_ids") or []))
            episode_memory_ids = sorted(_coerce_int_set(episode.get("memory_item_ids") or []))
            for item_id in episode_memory_ids:
                event_ids_by_item_id.setdefault(item_id, [])
                memory_ids_by_item_id.setdefault(item_id, [])
                for event_id in event_ids:
                    _append_unique_int(event_ids_by_item_id[item_id], event_id)
                for memory_id in episode_memory_ids:
                    _append_unique_int(memory_ids_by_item_id[item_id], memory_id)

        entity_by_raw_id = {row.get("id"): row for row in entities if row.get("id") is not None}
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        connected_node_ids: set[str] = set()

        for entity in entities:
            node_id = _group_graph_node_id(entity)
            status = str(entity.get("status") or "active")
            confidence = _clamp_score(entity.get("confidence"))
            node_acceptance = _group_graph_acceptance_status(entity)
            if node_type and str(entity.get("entity_type") or "") != node_type:
                continue
            if confidence_floor is not None and confidence < confidence_floor:
                continue
            nodes[node_id] = {
                "id": node_id,
                "type": str(entity.get("entity_type") or "thing"),
                "label": str(entity.get("name") or entity.get("normalized_name") or ""),
                "display_label": _group_graph_entity_display_label(entity),
                "technical_label": str(
                    entity.get("normalized_name") or entity.get("name") or node_id
                ),
                "aliases": _group_graph_entity_aliases(entity),
                "status": status,
                "acceptance_status": node_acceptance,
                "confidence": confidence,
                "evidence_count": 0,
                "first_seen": _group_graph_timestamp(entity, "created_at"),
                "last_seen": _group_graph_timestamp(entity, "updated_at", "created_at"),
                "source_ref_count": 0,
            }

        for fact in facts:
            predicate = str(fact.get("predicate") or "")
            if edge_type and predicate != edge_type:
                continue
            confidence = _clamp_score(fact.get("confidence"))
            if confidence_floor is not None and confidence < confidence_floor:
                continue
            memory_item_id = next(iter(_coerce_int_set([fact.get("memory_item_id")])), None)
            backing_item = item_by_id.get(memory_item_id) if memory_item_id is not None else None
            if session_id is not None:
                fact_session_matches = False
                if backing_item and str(backing_item.get("session_id") or "") == str(session_id):
                    fact_session_matches = True
                event_ids_for_fact = set(_coerce_int_set([fact.get("source_event_id")]))
                if backing_item:
                    event_ids_for_fact.update(
                        _coerce_int_set([backing_item.get("source_event_id")])
                    )
                if any(
                    event_session_by_id.get(event_id) == str(session_id)
                    for event_id in event_ids_for_fact
                ):
                    fact_session_matches = True
                if not fact_session_matches:
                    continue
            timestamp = _group_graph_timestamp(fact, "valid_at", "created_at", "updated_at")
            timestamp_dt = _coerce_datetime(timestamp)
            if from_dt is not None and timestamp_dt is not None and timestamp_dt < from_dt:
                continue
            if to_dt is not None and timestamp_dt is not None and timestamp_dt > to_dt:
                continue

            acceptance_row = backing_item or fact
            edge_acceptance = _group_graph_acceptance_status(acceptance_row)
            if requested_acceptance and edge_acceptance not in requested_acceptance:
                continue
            if not requested_acceptance and not _group_graph_default_acceptance_allowed(
                acceptance_row
            ):
                continue

            subject = entity_by_raw_id.get(fact.get("subject_entity_id"), {})
            source_node_id = _group_graph_node_id({**subject, "id": fact.get("subject_entity_id")})
            if source_node_id not in nodes:
                continue
            object_entity_id = fact.get("object_entity_id")
            if object_entity_id is not None:
                object_entity = {
                    **entity_by_raw_id.get(object_entity_id, {}),
                    "id": object_entity_id,
                }
                target_node_id = _group_graph_node_id(object_entity)
                if target_node_id not in nodes:
                    continue
            else:
                if node_type and node_type != "value":
                    continue
                value_key = str(fact.get("object_value") or predicate)
                target_node_id = f"value:{_normalize_key(value_key)}"
                if target_node_id not in nodes:
                    nodes[target_node_id] = {
                        "id": target_node_id,
                        "type": "value",
                        "label": str(predicate or "value"),
                        "display_label": str(predicate or "value"),
                        "technical_label": target_node_id,
                        "aliases": [],
                        "status": str(fact.get("status") or "active"),
                        "acceptance_status": edge_acceptance,
                        "confidence": confidence,
                        "evidence_count": 0,
                        "first_seen": timestamp,
                        "last_seen": _group_graph_timestamp(
                            fact,
                            "updated_at",
                            "valid_at",
                            "created_at",
                        ),
                        "source_ref_count": 0,
                    }

            source_event_ids: list[int] = []
            memory_item_ids_for_edge: list[int] = []
            _append_unique_int(source_event_ids, fact.get("source_event_id"))
            if memory_item_id is not None:
                _append_unique_int(memory_item_ids_for_edge, memory_item_id)
                for event_id in event_ids_by_item_id.get(memory_item_id, []):
                    _append_unique_int(source_event_ids, event_id)
                for evidence_item_id in memory_ids_by_item_id.get(memory_item_id, []):
                    _append_unique_int(memory_item_ids_for_edge, evidence_item_id)

            source_ref_count = len(set(source_event_ids)) + len(set(memory_item_ids_for_edge))
            edge = {
                "id": _group_graph_edge_id(fact),
                "source": source_node_id,
                "target": target_node_id,
                "type": predicate,
                "label": predicate,
                "confidence": confidence,
                "acceptance_status": edge_acceptance,
                "evidence_count": max(1, source_ref_count),
                "first_seen": timestamp,
                "last_seen": _group_graph_timestamp(fact, "updated_at", "valid_at", "created_at"),
                "source_event_ids": source_event_ids,
                "memory_item_ids": memory_item_ids_for_edge,
                "extraction_method": str((backing_item or {}).get("source_type") or "graph"),
            }
            edges.append(edge)
            connected_node_ids.update({source_node_id, target_node_id})
            for node_id in (source_node_id, target_node_id):
                if node_id in nodes:
                    evidence_count = int(nodes[node_id].get("evidence_count") or 0)
                    source_refs = int(nodes[node_id].get("source_ref_count") or 0)
                    nodes[node_id]["evidence_count"] = evidence_count + edge["evidence_count"]
                    nodes[node_id]["source_ref_count"] = source_refs + source_ref_count

            if len(edges) >= safe_limit:
                break

        if session_id is not None:
            nodes = {
                node_id: node for node_id, node in nodes.items() if node_id in connected_node_ids
            }

        if (
            str(channel or "").strip().lower() == "wechat"
            and session_id
            and _is_group_session_id(session_id)
        ):
            username_candidates: dict[str, str] = {}
            for node in nodes.values():
                if str(node.get("type") or "") != "person":
                    continue
                for candidate in (
                    node.get("technical_label"),
                    node.get("label"),
                    *(node.get("aliases") or []),
                ):
                    username = _normalize_line(_sanitize_db_text(candidate))
                    if username and _looks_like_wechat_username(username):
                        username_candidates.setdefault(username, str(node.get("id") or ""))
            contact_kwargs: dict[str, Any] = {
                "session_id": session_id,
                "usernames": username_candidates.keys(),
            }
            if bool(getattr(self, "runtime_scope_gates_required", False)):
                contact_kwargs["tenant_id"] = str(tenant_id or "")
            contact_map = await self._load_wechat_group_contact_display_map(
                **contact_kwargs,
            )
            for node in nodes.values():
                if str(node.get("type") or "") != "person":
                    continue
                matched_username = ""
                for candidate in (
                    node.get("technical_label"),
                    node.get("label"),
                    *(node.get("aliases") or []),
                ):
                    username = _normalize_line(_sanitize_db_text(candidate))
                    if username in contact_map:
                        matched_username = username
                        break
                if not matched_username:
                    continue
                metadata = contact_map.get(matched_username) or {}
                contact_display = _wechat_contact_display_label(metadata)
                if contact_display:
                    node["display_label"] = contact_display
                    node["aliases"] = _merge_group_graph_aliases(
                        node.get("aliases") or [],
                        (metadata.get("remark"), metadata.get("nick_name"), metadata.get("alias")),
                    )

        node_items = list(nodes.values())[:safe_limit]
        return {
            "schema": {
                "version": GROUP_GRAPH_SCHEMA_VERSION,
                "node_types": list(GROUP_GRAPH_NODE_TYPES),
                "edge_types": list(GROUP_GRAPH_EDGE_TYPES),
            },
            "scope": scope,
            "filters": filters,
            "nodes": node_items,
            "edges": edges[:safe_limit],
            "counts": {
                "nodes": len(node_items),
                "edges": len(edges[:safe_limit]),
            },
            "generated_from": generated_from,
        }

    async def get_group_relationship_edge_evidence(
        self,
        *,
        edge_id: str,
        tenant_id: str,
        channel: str | None = None,
        source_key: str | None = None,
        session_id: str | None = None,
        include_raw: bool = False,
    ) -> dict[str, Any] | None:
        raw_edge_id = str(edge_id or "").strip()
        if not raw_edge_id:
            return None
        fact_id: int | None = None
        if re.fullmatch(r"fact:\d+", raw_edge_id):
            fact_id = _safe_int(raw_edge_id.split(":", 1)[1], 0) or None
        if fact_id is None:
            fact_id = _safe_int(raw_edge_id, 0) or None

        conditions = ["fact.id = :fact_id", "fact.tenant_id = :tid"]
        params: dict[str, Any] = {"fact_id": fact_id, "tid": tenant_id}
        if channel is not None:
            conditions.append("fact.channel = :channel")
            params["channel"] = channel
        if source_key is not None:
            conditions.append("fact.source_key = :source_key")
            params["source_key"] = source_key
        fact_select_sql = (
            "SELECT fact.id, fact.tenant_id, fact.channel, fact.source_key, fact.user_id, "
            "fact.subject_entity_id, subject.name AS subject_name, fact.predicate, "
            "fact.object_entity_id, object_entity.name AS object_name, fact.object_value, "
            "fact.memory_item_id, fact.source_event_id, fact.confidence, fact.status, "
            "fact.valid_at, fact.invalid_at, fact.created_at, fact.updated_at "
            "FROM plugin_memory_fact fact "
            "LEFT JOIN plugin_memory_entity subject ON subject.id = fact.subject_entity_id "
            "AND subject.tenant_id = fact.tenant_id AND subject.channel = fact.channel "
            "AND subject.source_key = fact.source_key AND subject.user_id = fact.user_id "
            "LEFT JOIN plugin_memory_entity object_entity ON object_entity.id = fact.object_entity_id "
            "AND object_entity.tenant_id = fact.tenant_id AND object_entity.channel = fact.channel "
            "AND object_entity.source_key = fact.source_key AND object_entity.user_id = fact.user_id "
        )
        rows = []
        if fact_id is not None:
            rows = await _exec(
                fact_select_sql + f"WHERE {' AND '.join(conditions)} LIMIT 1",
                params,
            )
        if not rows:
            scan_conditions = ["fact.tenant_id = :tid"]
            scan_params: dict[str, Any] = {"tid": tenant_id, "lim": 500}
            if channel is not None:
                scan_conditions.append("fact.channel = :channel")
                scan_params["channel"] = channel
            if source_key is not None:
                scan_conditions.append("fact.source_key = :source_key")
                scan_params["source_key"] = source_key
            candidates = await _exec(
                fact_select_sql
                + f"WHERE {' AND '.join(scan_conditions)} "
                + "ORDER BY fact.updated_at DESC, fact.id DESC LIMIT :lim",
                scan_params,
            )
            rows = [row for row in candidates if _group_graph_edge_id(row) == raw_edge_id]
        if not rows:
            return None
        fact = rows[0]
        fact["confidence"] = float(fact.get("confidence") or 0.0)
        memory_item_id = next(iter(_coerce_int_set([fact.get("memory_item_id")])), None)
        if memory_item_id is None:
            return None
        backing_items = await self._get_sanitized_memory_items_by_ids([memory_item_id])
        backing_item = next(
            (item for item in backing_items if int(item.get("id") or 0) == memory_item_id), None
        )
        if session_id is not None:
            if not backing_item or str(backing_item.get("session_id") or "") != str(session_id):
                return None

        episodes = await self.list_memory_graph_episodes(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=str(fact.get("user_id") or "") or None,
            session_id=session_id,
            status=None,
            limit=100,
        )
        event_ids: list[int] = []
        memory_item_ids: list[int] = []
        _append_unique_int(event_ids, fact.get("source_event_id"))
        _append_unique_int(memory_item_ids, memory_item_id)
        evidence_episodes: list[dict[str, Any]] = []
        for episode in episodes:
            episode_memory_ids = _coerce_int_set(episode.get("memory_item_ids") or [])
            if memory_item_id not in episode_memory_ids:
                continue
            evidence_episodes.append(episode)
            for event_id in episode.get("event_ids") or []:
                _append_unique_int(event_ids, event_id)
            for item_id in episode_memory_ids:
                _append_unique_int(memory_item_ids, item_id)

        evidence_items = await self._get_sanitized_memory_items_by_ids(memory_item_ids)
        events = await self._get_memory_events_by_ids(event_ids)
        payload = _build_group_relationship_edge_evidence_payload(
            fact=fact,
            backing_item=backing_item,
            evidence_items=evidence_items,
            events=events,
            evidence_episodes=evidence_episodes,
            memory_item_ids=memory_item_ids,
            event_ids=event_ids,
        )
        if include_raw:
            raw_items = await self._get_memory_items_by_ids(memory_item_ids)
            payload["raw"] = {
                "fact": fact,
                "memory_items": raw_items,
                "events": events,
                "episodes": evidence_episodes,
            }
        return payload

    def _build_group_relationship_windows(
        self,
        event_rows: list[dict[str, Any]],
        *,
        window_size: int,
    ) -> list[dict[str, Any]]:
        windows: list[dict[str, Any]] = []
        for index, start in enumerate(range(0, len(event_rows), window_size), start=1):
            rows = event_rows[start : start + window_size]
            if not rows:
                continue
            event_ids = sorted(_coerce_int_set(row.get("id") for row in rows))
            sender_ids: list[str] = []
            transcript_lines: list[str] = []
            prompt_chars = 0
            for row in rows:
                event_id = row.get("id")
                sender_id, body = _split_group_event_text(row.get("user_text"))
                sender_id = sender_id or str(row.get("user_id") or "unknown")
                if sender_id not in sender_ids:
                    sender_ids.append(sender_id)
                body = _normalize_line(_sanitize_db_text(body))[:500]
                if not body:
                    continue
                line = f"[event_id={event_id}] {sender_id}: {body}"
                if prompt_chars + len(line) + 1 > 12000:
                    break
                transcript_lines.append(line)
                prompt_chars += len(line) + 1
            windows.append(
                {
                    "index": index,
                    "rows": rows,
                    "event_ids": event_ids,
                    "first_event_id": event_ids[0] if event_ids else None,
                    "last_event_id": event_ids[-1] if event_ids else None,
                    "sender_ids": sender_ids,
                    "transcript": "\n".join(transcript_lines),
                }
            )
        return windows

    @staticmethod
    def _normalize_group_participant_id(value: Any) -> str:
        participant = _normalize_line(_sanitize_db_text(value))[:200]
        if not participant or participant.lower() in {"unknown", "none", "null"}:
            return ""
        return participant

    def _extract_addressed_participant_ids(
        self,
        body: str,
        *,
        participants: Iterable[str],
    ) -> list[str]:
        if not body:
            return []
        participant_set = {self._normalize_group_participant_id(item) for item in participants}
        participant_set.discard("")
        if not participant_set:
            return []
        targets: list[str] = []

        for mention in re.findall(r"@([^\s\u2005\u00a0:：,，;；]+)", body):
            normalized = self._normalize_group_participant_id(mention)
            if normalized in participant_set and normalized not in targets:
                targets.append(normalized)

        stripped = body.strip()
        for participant in sorted(participant_set, key=len, reverse=True):
            if participant in targets:
                continue
            escaped = re.escape(participant)
            if re.search(rf"(^|[\s@]){escaped}([:：,，\s]|$)", stripped):
                targets.append(participant)
                continue
            if re.search(rf"(回复|回|问|告诉|建议)\s*@?{escaped}", stripped):
                targets.append(participant)

        return targets[:5]

    def _build_deterministic_group_window_candidates(
        self,
        window: dict[str, Any],
    ) -> list[dict[str, Any]]:
        rows = window.get("rows") if isinstance(window.get("rows"), list) else []
        events: list[dict[str, Any]] = []
        participants: list[str] = []
        for row in rows:
            sender_id, body = _split_group_event_text(row.get("user_text"))
            sender_id = self._normalize_group_participant_id(sender_id or row.get("user_id"))
            if not sender_id:
                continue
            event_id = next(iter(_coerce_int_set([row.get("id")])), None)
            if event_id is None:
                continue
            if sender_id not in participants:
                participants.append(sender_id)
            events.append({"id": event_id, "sender": sender_id, "body": str(body or "")})

        candidates_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}

        def add_candidate(
            *,
            subject: str,
            predicate: str,
            object_value: str,
            confidence: float,
            evidence_event_ids: Iterable[Any],
            reason: str,
        ) -> None:
            subject = self._normalize_group_participant_id(subject)
            object_value = self._normalize_group_participant_id(object_value)
            if not subject or not object_value or subject == object_value:
                return
            evidence_ids = sorted(_coerce_int_set(evidence_event_ids))
            if not evidence_ids:
                return
            key = (subject, predicate, object_value)
            existing = candidates_by_key.get(key)
            if existing:
                existing["confidence"] = max(
                    _clamp_score(existing.get("confidence"), 0.0),
                    _clamp_score(confidence, 0.0),
                )
                existing["evidence_event_ids"] = _merge_int_lists(
                    existing.get("evidence_event_ids"),
                    evidence_ids,
                    max_items=200,
                )
                return
            candidates_by_key[key] = {
                "subject": subject,
                "subject_type": "person",
                "predicate": predicate,
                "object": object_value,
                "object_type": "person",
                "confidence": _clamp_score(confidence, 0.5),
                "evidence_event_ids": evidence_ids,
                "reason": reason,
                "extraction_method": DETERMINISTIC_GROUP_WINDOW_SOURCE_TYPE,
            }

        for index, event in enumerate(events):
            sender = str(event["sender"])
            event_id = int(event["id"])
            for target in self._extract_addressed_participant_ids(
                str(event.get("body") or ""),
                participants=participants,
            ):
                add_candidate(
                    subject=sender,
                    predicate="addressed",
                    object_value=target,
                    confidence=0.72,
                    evidence_event_ids=[event_id],
                    reason="deterministic_addressed_participant",
                )

            previous = events[index - 1] if index > 0 else None
            body = str(event.get("body") or "").strip()
            if (
                previous
                and previous.get("sender") != sender
                and re.search(r"^(?:回复|回|接着|关于)(?:\s|[:：])", body)
            ):
                add_candidate(
                    subject=sender,
                    predicate="replied_to",
                    object_value=str(previous.get("sender") or ""),
                    confidence=0.62,
                    evidence_event_ids=[previous.get("id"), event_id],
                    reason="deterministic_adjacent_reply_window",
                )

        participant_counts = {
            participant: sum(1 for event in events if event.get("sender") == participant)
            for participant in participants
        }
        repeated_participants = [
            participant for participant in participants if participant_counts[participant] >= 2
        ]
        if 1 < len(repeated_participants) <= GROUP_WINDOW_DETERMINISTIC_MAX_SENDERS:
            evidence_ids = sorted(_coerce_int_set(event.get("id") for event in events))[:20]
            for left_index, subject in enumerate(repeated_participants):
                for object_value in repeated_participants[left_index + 1 :]:
                    add_candidate(
                        subject=subject,
                        predicate="co_participated",
                        object_value=object_value,
                        confidence=0.45,
                        evidence_event_ids=evidence_ids,
                        reason="deterministic_same_window_participation",
                    )
                    if len(candidates_by_key) >= GROUP_WINDOW_DETERMINISTIC_MAX_PAIRS:
                        return list(candidates_by_key.values())

        return list(candidates_by_key.values())

    async def _extract_group_relationship_window_candidates(
        self,
        *,
        tenant_id: str,
        trace_id: str,
        target_date: str,
        session_id: str,
        event_ids: list[int],
        transcript: str,
    ) -> Any:
        llm_service = getattr(self.graph_extractor, "llm_service", None)
        if llm_service is None:
            return {"relations": []}
        system = (
            "Extract conservative group-chat relationship candidates from a bounded transcript. "
            "Return JSON only with key relations. Each relation must include subject, subject_type, "
            "predicate, object, object_type, confidence, evidence_event_ids, and optional reason. "
            "Allowed predicates: "
            + ", ".join(GROUP_GRAPH_EDGE_TYPES)
            + ". Evidence ids must come from the provided event ids. Do not quote raw messages."
        )
        payload = {
            "date": target_date,
            "session_id": session_id,
            "event_ids": event_ids,
            "transcript": transcript,
        }
        request = ChatRequest(
            tenant_id=tenant_id,
            trace_id=trace_id,
            model_tier="tier-3",
            messages=[ChatMessage(role=Role.USER, content=json.dumps(payload, ensure_ascii=False))],
            system=system,
            temperature=0.0,
            max_tokens=1200,
            metadata={"purpose": "memory_group_relationship_window_extraction"},
        )
        chat = getattr(llm_service, "chat", None)
        if chat is None:
            raise RuntimeError("llm_service has no chat method")
        response = chat(request)
        if hasattr(response, "__await__"):
            response = await response
        return getattr(response, "content", response)

    def _validate_group_window_candidate(
        self,
        raw_candidate: Any,
        *,
        allowed_event_ids: set[int],
    ) -> dict[str, Any] | None:
        if not isinstance(raw_candidate, dict):
            return None
        predicate = str(raw_candidate.get("predicate") or "").strip().lower()
        if predicate not in GROUP_GRAPH_EDGE_TYPES:
            return None
        subject = _normalize_line(str(raw_candidate.get("subject") or ""))[:200]
        object_value = _normalize_line(str(raw_candidate.get("object") or ""))[:200]
        if not subject or not object_value:
            return None
        evidence_ids = sorted(_coerce_int_set(raw_candidate.get("evidence_event_ids") or []))
        evidence_ids = [event_id for event_id in evidence_ids if event_id in allowed_event_ids]
        if not evidence_ids:
            return None
        subject_type = str(raw_candidate.get("subject_type") or "person").strip().lower()
        object_type = str(raw_candidate.get("object_type") or "person").strip().lower()
        if subject_type not in GROUP_GRAPH_NODE_TYPES:
            subject_type = "person"
        if object_type not in GROUP_GRAPH_NODE_TYPES:
            object_type = "person"
        return {
            "subject": subject,
            "subject_type": subject_type,
            "predicate": predicate,
            "object": object_value,
            "object_type": object_type,
            "confidence": _clamp_score(raw_candidate.get("confidence"), 0.5),
            "evidence_event_ids": evidence_ids,
            "reason": _normalize_line(str(raw_candidate.get("reason") or ""))[:240],
        }

    def _parse_group_window_candidates(
        self,
        raw_payload: Any,
        *,
        allowed_event_ids: set[int],
    ) -> tuple[list[dict[str, Any]], int]:
        try:
            payload = (
                raw_payload
                if isinstance(raw_payload, (dict, list))
                else _loads_json_object_or_array(raw_payload)
            )
        except Exception:
            return [], 1
        raw_candidates = payload
        if isinstance(payload, dict):
            raw_candidates = payload.get("relations") or payload.get("candidates") or []
        if not isinstance(raw_candidates, list):
            return [], 1
        candidates: list[dict[str, Any]] = []
        skipped = 0
        for raw_candidate in raw_candidates:
            candidate = self._validate_group_window_candidate(
                raw_candidate, allowed_event_ids=allowed_event_ids
            )
            if candidate is None:
                skipped += 1
                continue
            candidates.append(candidate)
        return candidates, skipped

    @staticmethod
    def _merge_group_window_candidates(
        *candidate_groups: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged: dict[tuple[str, str, str], dict[str, Any]] = {}
        for candidates in candidate_groups:
            for candidate in candidates:
                subject = _normalize_line(str(candidate.get("subject") or ""))
                predicate = str(candidate.get("predicate") or "").strip().lower()
                object_value = _normalize_line(str(candidate.get("object") or ""))
                key = (subject, predicate, object_value)
                if not subject or not predicate or not object_value:
                    continue
                existing = merged.get(key)
                if existing is None:
                    merged[key] = dict(candidate)
                    continue
                existing["confidence"] = max(
                    _clamp_score(existing.get("confidence"), 0.0),
                    _clamp_score(candidate.get("confidence"), 0.0),
                )
                existing["evidence_event_ids"] = _merge_int_lists(
                    existing.get("evidence_event_ids"),
                    candidate.get("evidence_event_ids"),
                    max_items=200,
                )
                if not existing.get("reason") and candidate.get("reason"):
                    existing["reason"] = candidate.get("reason")
        return list(merged.values())

    async def _apply_group_relationship_window_candidate(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        session_id: str,
        target_date: str,
        window: dict[str, Any],
        candidate: dict[str, Any],
    ) -> dict[str, Any] | None:
        normalized_key = _window_relationship_normalized_key(
            target_date=target_date,
            session_id=session_id,
            predicate=candidate["predicate"],
            subject=candidate["subject"],
            object_value=candidate["object"],
        )
        evidence_event_ids = _merge_int_lists(candidate.get("evidence_event_ids"), max_items=200)
        relation_payload = dict(candidate)
        relation_payload["evidence_event_ids"] = evidence_event_ids
        evidence_dates = [target_date]

        existing_items = await self._find_memory_item_by_normalized_key(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            scope_type="session",
            session_id=session_id,
            normalized_key=normalized_key,
            limit=1,
        )
        if existing_items:
            existing_value = existing_items[0].get("value")
            if not isinstance(existing_value, dict):
                existing_value = _safe_json_loads(existing_items[0].get("value_json"), {})
            if not isinstance(existing_value, dict):
                existing_value = {}
            existing_relation = (
                existing_value.get("relation")
                if isinstance(existing_value.get("relation"), dict)
                else {}
            )
            evidence_event_ids = _merge_int_lists(
                existing_relation.get("evidence_event_ids"),
                existing_value.get("source_event_ids"),
                candidate.get("evidence_event_ids"),
                max_items=200,
            )
            relation_payload = {**existing_relation, **candidate}
            relation_payload["confidence"] = max(
                _clamp_score(existing_relation.get("confidence"), 0.0),
                _clamp_score(candidate.get("confidence"), 0.0),
            )
            relation_payload["evidence_event_ids"] = evidence_event_ids
            existing_dates = existing_value.get("evidence_dates")
            if not isinstance(existing_dates, list):
                existing_dates = []
            evidence_dates = list(
                dict.fromkeys(
                    [
                        *[str(value) for value in existing_dates if str(value).strip()],
                        target_date,
                    ]
                )
            )[-90:]

        relation_source_type = str(
            relation_payload.get("extraction_method") or LLM_GROUP_WINDOW_SOURCE_TYPE
        )
        if relation_source_type not in {
            LLM_GROUP_WINDOW_SOURCE_TYPE,
            DETERMINISTIC_GROUP_WINDOW_SOURCE_TYPE,
        }:
            relation_source_type = LLM_GROUP_WINDOW_SOURCE_TYPE
        # Deterministic means reproducible, not necessarily true. Adjacency and
        # same-window co-participation are weak evidence and must not enter the
        # prompt graph without review.
        acceptance_status = "needs_review"
        value_payload = {
            "kind": "group_window_relation",
            "date": target_date,
            "evidence_dates": evidence_dates,
            "first_seen_date": min(evidence_dates),
            "last_seen_date": max(evidence_dates),
            "window": {
                "index": window["index"],
                "first_event_id": window["first_event_id"],
                "last_event_id": window["last_event_id"],
            },
            "relation": relation_payload,
            "source_event_ids": evidence_event_ids,
            "acceptance": {
                "status": acceptance_status,
                "score": relation_payload["confidence"],
                "reason": str(relation_payload.get("reason") or "group_window_relation")[:80],
                "extraction_confidence": relation_payload["confidence"],
            },
        }
        is_group_history_scope = user_id == GROUP_HISTORY_USER_ID_SCOPE
        return await self._insert_or_touch_memory_item(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            scope_type="session",
            source_type=relation_source_type,
            memory_type="note",
            content=(
                "Group window relation: "
                f"{candidate['subject']} {candidate['predicate']} {candidate['object']}"
            ),
            value_json=value_payload,
            normalized_key=normalized_key,
            confidence=relation_payload["confidence"],
            status="active" if acceptance_status == "accepted" else "pending",
            pinned=False,
            priority=0,
            sensitivity="normal",
            origin_session_kind="group",
            audience_scope="session" if is_group_history_scope else "private",
            allowed_session_ids=[session_id] if is_group_history_scope else [],
            sensitivity_category="normal",
            source_kind="graph",
            source_event_id=evidence_event_ids[0] if evidence_event_ids else None,
            source_trace_id=normalized_key,
            original_text="",
        )

    async def run_group_relationship_window_extraction(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        session_id: str,
        date: str,
        user_id: str | None = None,
        window_size: int | None = None,
        max_windows: int | None = None,
        cursor_event_id: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        session_id = str(session_id or "").strip()
        if not session_id:
            raise RuntimeError("session_id required")
        target_day = _parse_daily_relationship_date(date)
        target_date = target_day.date().isoformat()
        start_at = target_day
        end_at = start_at + timedelta(days=1)
        effective_window_size = _clamp_int(window_size, 50, minimum=10, maximum=100)
        effective_max_windows = _clamp_int(max_windows, 1, minimum=1, maximum=10)
        effective_cursor = max(0, int(cursor_event_id or 0))
        user_id_scope, user_id_auto = _group_history_user_scope(session_id, user_id)
        if not user_id_scope:
            raise RuntimeError("user_id required")

        fetch_limit = effective_window_size * effective_max_windows + 1
        event_rows = await _exec(
            "SELECT id, tenant_id, channel, source_key, user_id, session_id, user_text, "
            "assistant_text, trace_id, event_key, created_at "
            "FROM plugin_memory_event "
            "WHERE tenant_id = :tid AND channel = :channel "
            "AND source_key IN (:source_key, '*') "
            "AND user_id = :uid AND session_id = :sid "
            "AND created_at >= :start_at AND created_at < :end_at "
            "AND id > :cursor_event_id "
            "ORDER BY created_at ASC, id ASC "
            "LIMIT :lim",
            {
                "tid": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "uid": user_id_scope,
                "sid": session_id,
                "start_at": start_at,
                "end_at": end_at,
                "cursor_event_id": effective_cursor,
                "lim": fetch_limit,
            },
        )
        more_remain = len(event_rows) > effective_window_size * effective_max_windows
        event_rows = event_rows[: effective_window_size * effective_max_windows]
        windows = self._build_group_relationship_windows(
            event_rows, window_size=effective_window_size
        )
        window_summaries = [
            {
                "index": window["index"],
                "event_count": len(window["event_ids"]),
                "first_event_id": window["first_event_id"],
                "last_event_id": window["last_event_id"],
                "sender_count": len(window["sender_ids"]),
                "candidate_count": 0,
                "applied_count": 0,
                "skipped_count": 0,
            }
            for window in windows
        ]
        next_cursor_event_id = max(
            [effective_cursor, *[int(window["last_event_id"] or 0) for window in windows]]
        )
        base_payload = {
            "ok": True,
            "status": "dry_run" if dry_run else "completed",
            "scope": {
                "tenant_id": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "session_id": session_id,
                "user_id": user_id_scope,
                "user_id_scope": user_id_scope,
                "user_id_auto": user_id_auto,
            },
            "date": target_date,
            "controls": {
                "window_size": effective_window_size,
                "max_windows": effective_max_windows,
                "cursor_event_id": effective_cursor,
                "dry_run": bool(dry_run),
            },
            "windows": window_summaries,
            "totals": {
                "events": sum(len(window["event_ids"]) for window in windows),
                "windows": len(windows),
                "candidates": 0,
                "applied": 0,
                "skipped": 0,
            },
            "next_cursor_event_id": next_cursor_event_id,
            "more_remain": more_remain,
            "generated_from": ["plugin_memory_event"],
        }
        if dry_run or not windows:
            if not windows:
                base_payload["status"] = "skipped"
            return base_payload

        llm_available = bool(
            self.graph_extractor.config.enabled and self.graph_extractor.llm_service is not None
        )

        total_candidates = 0
        total_applied = 0
        total_skipped = 0
        generated_from = ["plugin_memory_event", "deterministic_window_participants"]
        if llm_available:
            generated_from.append("llm_window_extractor")
        for window, summary in zip(windows, window_summaries, strict=True):
            deterministic_candidates = self._build_deterministic_group_window_candidates(window)
            llm_candidates: list[dict[str, Any]] = []
            if llm_available:
                try:
                    raw_payload = await self._extract_group_relationship_window_candidates(
                        tenant_id=tenant_id,
                        trace_id=f"group-window:{target_date}:{window['first_event_id']}:{window['last_event_id']}",
                        target_date=target_date,
                        session_id=session_id,
                        event_ids=window["event_ids"],
                        transcript=window["transcript"],
                    )
                except Exception as exc:
                    logger.warning(
                        "memory.group_window_llm_failed",
                        tenant_id=tenant_id,
                        channel=channel,
                        source_key=source_key,
                        session_id=session_id,
                        date=target_date,
                        error_type=exc.__class__.__name__,
                        error=_truncate_error(exc),
                    )
                    summary["skipped_count"] += 1
                    total_skipped += 1
                else:
                    llm_candidates, skipped = self._parse_group_window_candidates(
                        raw_payload,
                        allowed_event_ids=set(window["event_ids"]),
                    )
                    summary["skipped_count"] += skipped
                    total_skipped += skipped
            candidates = self._merge_group_window_candidates(
                deterministic_candidates,
                llm_candidates,
            )
            if not candidates:
                summary["skipped_count"] += 1
                total_skipped += 1
                continue
            summary["candidate_count"] = len(candidates)
            total_candidates += len(candidates)
            for candidate in candidates:
                item = await self._apply_group_relationship_window_candidate(
                    tenant_id=tenant_id,
                    channel=channel,
                    source_key=source_key,
                    user_id=user_id_scope,
                    session_id=session_id,
                    target_date=target_date,
                    window=window,
                    candidate=candidate,
                )
                if not item or item.get("id") is None:
                    summary["skipped_count"] += 1
                    total_skipped += 1
                    continue
                summary["applied_count"] += 1
                total_applied += 1
                await self._refresh_legacy_cache_for_item_scope(item)
                await self._sync_memory_graph_for_item_safe(item)
                await self._sync_memory_vector_for_item_safe(item)

        if total_applied == 0 and total_candidates == 0:
            base_payload["status"] = "skipped"
            if not llm_available:
                base_payload["skipped_reason"] = "no_deterministic_candidates"
        else:
            base_payload["status"] = "completed" if total_skipped == 0 else "partial"
        base_payload["generated_from"] = generated_from
        base_payload["totals"] = {
            "events": sum(len(window["event_ids"]) for window in windows),
            "windows": len(windows),
            "candidates": total_candidates,
            "applied": total_applied,
            "skipped": total_skipped,
        }
        return base_payload

    async def run_group_relationship_window_catchup(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        session_id: str,
        date: str,
        user_id: str | None = None,
        window_size: int | None = None,
        max_windows_per_run: int | None = None,
        cursor_event_id: int | None = None,
        dry_run: bool = False,
        time_budget_seconds: int | None = None,
    ) -> dict[str, Any]:
        effective_window_size = _clamp_int(window_size, 50, minimum=10, maximum=100)
        effective_max_windows = _clamp_int(max_windows_per_run, 20, minimum=1, maximum=100)
        effective_cursor = max(0, int(cursor_event_id or 0))
        effective_time_budget = _clamp_int(time_budget_seconds, 60, minimum=1, maximum=180)
        started_at = monotonic()
        totals = {"events": 0, "windows": 0, "candidates": 0, "applied": 0, "skipped": 0}
        controls = {
            "window_size": effective_window_size,
            "max_windows_per_run": effective_max_windows,
            "cursor_event_id": effective_cursor,
            "dry_run": bool(dry_run),
            "time_budget_seconds": effective_time_budget,
        }
        windows_processed = 0
        more_remain = False
        stop_reason = "no_more_events"
        status = "completed"
        next_cursor_event_id = effective_cursor

        while windows_processed < effective_max_windows:
            if monotonic() - started_at >= effective_time_budget:
                stop_reason = "time_budget_reached"
                more_remain = True
                break
            batch_limit = min(effective_max_windows - windows_processed, 10)
            result = await self.run_group_relationship_window_extraction(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                session_id=session_id,
                user_id=user_id,
                date=date,
                window_size=effective_window_size,
                max_windows=batch_limit,
                cursor_event_id=next_cursor_event_id,
                dry_run=dry_run,
            )
            result_totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
            processed = int(result_totals.get("windows") or 0)
            for key in totals:
                totals[key] += int(result_totals.get(key) or 0)
            next_cursor_event_id = int(result.get("next_cursor_event_id") or next_cursor_event_id)
            more_remain = bool(result.get("more_remain"))
            result_status = str(result.get("status") or "")
            if processed == 0:
                stop_reason = (
                    "empty_day"
                    if effective_cursor == 0 and totals["events"] == 0
                    else "no_more_events"
                )
                status = "skipped" if result_status == "skipped" else status
                break
            windows_processed += processed
            if not more_remain:
                stop_reason = "no_more_events"
                break
        else:
            stop_reason = "max_windows_reached"
            more_remain = True

        return {
            "ok": True,
            "status": "dry_run" if dry_run else status,
            "scope": {
                "tenant_id": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "session_id": session_id,
                "user_id": user_id or _group_history_user_scope(session_id, user_id)[0],
            },
            "date": _parse_daily_relationship_date(date).date().isoformat(),
            "controls": controls,
            "totals": totals,
            "windows_processed": windows_processed,
            "next_cursor_event_id": next_cursor_event_id,
            "more_remain": more_remain,
            "stop_reason": stop_reason,
            "generated_from": [
                "plugin_memory_event",
                "deterministic_window_participants",
                "llm_window_extractor",
            ],
        }

    async def get_group_relationship_window_stats(
        self,
        *,
        tenant_id: str,
        channel: str | None = None,
        source_key: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        date: str | None = None,
        limit: int = 5000,
    ) -> dict[str, Any]:
        items = await self._list_memory_acceptance_audit_items(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=session_id,
            scope_type="session",
            source_type=None,
            include_deleted=False,
            limit=max(1, min(int(limit or 5000), 10000)),
        )
        target_date = _parse_daily_relationship_date(date).date().isoformat() if date else ""
        totals = {
            "items": 0,
            "events": 0,
            "windows": 0,
            "accepted": 0,
            "needs_review": 0,
            "rejected": 0,
            "candidate": 0,
            "superseded": 0,
            "expired": 0,
            "unknown_acceptance": 0,
        }
        status_counts: dict[str, int] = {}
        acceptance_counts: dict[str, int] = {}
        predicate_counts: dict[str, int] = {}
        event_ids: set[int] = set()
        window_keys: set[tuple[Any, Any, Any]] = set()
        for item in items:
            value = _safe_json_loads(item.get("value_json"), {})
            if not isinstance(value, dict):
                value = item.get("value") if isinstance(item.get("value"), dict) else {}
            if str(value.get("kind") or "") != "group_window_relation":
                continue
            if target_date and str(value.get("date") or "") != target_date:
                continue
            totals["items"] += 1
            status = str(item.get("status") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
            acceptance = (
                value.get("acceptance") if isinstance(value.get("acceptance"), dict) else {}
            )
            acceptance_status = str(
                acceptance.get("status") or item.get("acceptance_status") or "unknown"
            )
            if acceptance_status in totals:
                totals[acceptance_status] += 1
            else:
                totals["unknown_acceptance"] += 1
            acceptance_counts[acceptance_status] = acceptance_counts.get(acceptance_status, 0) + 1
            relation = value.get("relation") if isinstance(value.get("relation"), dict) else {}
            predicate = str(relation.get("predicate") or "unknown")
            predicate_counts[predicate] = predicate_counts.get(predicate, 0) + 1
            for event_id in _coerce_int_set(
                relation.get("evidence_event_ids") or value.get("source_event_ids") or []
            ):
                event_ids.add(event_id)
            window = value.get("window") if isinstance(value.get("window"), dict) else {}
            window_keys.add(
                (value.get("date"), window.get("first_event_id"), window.get("last_event_id"))
            )
        totals["events"] = len(event_ids)
        totals["windows"] = len(
            [key for key in window_keys if key[1] is not None or key[2] is not None]
        )
        return {
            "ok": True,
            "scope": {
                "tenant_id": tenant_id,
                "channel": channel or "",
                "source_key": source_key or "",
                "session_id": session_id or "",
                "user_id": user_id or "",
                "date": target_date,
            },
            "totals": totals,
            "status_counts": status_counts,
            "acceptance_counts": acceptance_counts,
            "predicate_counts": predicate_counts,
            "generated_from": [
                "plugin_memory_item",
                DETERMINISTIC_GROUP_WINDOW_SOURCE_TYPE,
                LLM_GROUP_WINDOW_SOURCE_TYPE,
            ],
        }

    async def run_daily_group_relationship_extraction(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        session_id: str,
        date: str,
        user_id: str | None = None,
        limit: int | None = None,
        batch_limit: int | None = None,
        max_jobs: int | None = None,
        continuous: bool | None = None,
        time_budget_seconds: int | None = None,
    ) -> dict[str, Any]:
        session_id = str(session_id or "").strip()
        if not session_id:
            raise RuntimeError("session_id required")
        target_day = _parse_daily_relationship_date(date)
        target_date = target_day.date().isoformat()
        start_at = target_day
        end_at = start_at + timedelta(days=1)
        legacy_limit_only = (
            limit is not None
            and batch_limit is None
            and max_jobs is None
            and continuous is None
            and time_budget_seconds is None
        )
        if legacy_limit_only:
            effective_batch_limit = _clamp_int(limit, 5, minimum=1, maximum=20)
            effective_continuous = False
            effective_max_jobs = effective_batch_limit
        else:
            effective_batch_limit = _clamp_int(batch_limit, 50, minimum=1, maximum=100)
            effective_continuous = bool(continuous)
            default_max_jobs = 200 if effective_continuous else effective_batch_limit
            effective_max_jobs = _clamp_int(max_jobs, default_max_jobs, minimum=1, maximum=500)
        effective_time_budget_seconds = _clamp_int(
            time_budget_seconds,
            60,
            minimum=1,
            maximum=180,
        )
        user_id_scope, user_id_auto = _group_history_user_scope(session_id, user_id)
        if not user_id_scope:
            raise RuntimeError("user_id required")
        run_key = _daily_relationship_run_key(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            session_id=session_id,
            user_id=user_id_scope,
            target_date=target_date,
        )

        event_rows = await _exec(
            "SELECT id, user_text, trace_id, event_key, created_at "
            "FROM plugin_memory_event "
            "WHERE tenant_id = :tid AND channel = :channel "
            "AND source_key IN (:source_key, '*') "
            "AND user_id = :uid AND session_id = :sid "
            "AND created_at >= :start_at AND created_at < :end_at "
            "ORDER BY created_at ASC, id ASC",
            {
                "tid": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "uid": user_id_scope,
                "sid": session_id,
                "start_at": start_at,
                "end_at": end_at,
            },
        )
        source_event_ids = sorted(_coerce_int_set(row.get("id") for row in event_rows))
        sender_ids = sorted(
            {
                sender
                for sender in (
                    _extract_group_event_sender_id(row.get("user_text")) for row in event_rows
                )
                if sender
            }
        )
        raw_message_count = 0
        if str(channel or "").strip().lower() == "wechat":
            try:
                raw_message_count = len(
                    await self._collect_session_history(
                        tenant_id=tenant_id,
                        session_id=session_id,
                        user_id=user_id_scope,
                        cutoff_ts=int(start_at.timestamp()),
                        end_ts=int(end_at.timestamp()),
                        max_messages=10000,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "memory.daily_relationship.raw_count_failed",
                    tenant_id=tenant_id,
                    channel=channel,
                    source_key=source_key,
                    session_id=session_id,
                    user_id=user_id_scope,
                    date=target_date,
                    error_type=exc.__class__.__name__,
                    error=_truncate_error(exc),
                )

        existing_run_items = await self._find_memory_item_by_normalized_key(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id_scope,
            scope_type="session",
            session_id=session_id,
            normalized_key=run_key,
            limit=1,
        )
        llm_available = bool(
            self.graph_extractor.config.enabled and self.graph_extractor.llm_service is not None
        )
        skipped_reason = "" if llm_available else "no_llm"
        status = "rule_only" if event_rows else "skipped"
        result_status = status if event_rows else "skipped"
        created_count = 0
        updated_count = 0
        memory_item_ids = sorted(_coerce_int_set(item.get("id") for item in existing_run_items))
        job_counts_before = await self.get_llm_extraction_job_status_counts_for_day(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id_scope,
            session_id=session_id,
            start_at=start_at,
            end_at=end_at,
        )
        processed_job_counts = {"claimed": 0, "succeeded": 0, "failed": 0, "dead": 0, "batches": 0}
        stop_reason = "single_batch_complete"
        if not event_rows:
            stop_reason = "empty_day"
        elif not llm_available:
            stop_reason = "llm_unavailable"
        else:
            run_started_at = monotonic()
            while True:
                elapsed = monotonic() - run_started_at
                if elapsed >= effective_time_budget_seconds:
                    stop_reason = "time_budget_reached"
                    break
                remaining_jobs = effective_max_jobs - processed_job_counts["claimed"]
                if remaining_jobs <= 0:
                    stop_reason = "max_jobs_reached"
                    break
                claim_limit = min(effective_batch_limit, remaining_jobs)
                jobs = await self.claim_llm_extraction_jobs_for_day(
                    tenant_id=tenant_id,
                    channel=channel,
                    source_key=source_key,
                    user_id=user_id_scope,
                    session_id=session_id,
                    start_at=start_at,
                    end_at=end_at,
                    limit=claim_limit,
                )
                if not jobs:
                    stop_reason = "no_ready_jobs"
                    break
                processed_job_counts["claimed"] += len(jobs)
                processed_job_counts["batches"] += 1
                for job in jobs:
                    status = await self.process_llm_extraction_job(job)
                    if status in processed_job_counts:
                        processed_job_counts[status] += 1
                if not effective_continuous:
                    stop_reason = "single_batch_complete"
                    break
                if processed_job_counts["claimed"] >= effective_max_jobs:
                    stop_reason = "max_jobs_reached"
                    break
                if monotonic() - run_started_at >= effective_time_budget_seconds:
                    stop_reason = "time_budget_reached"
                    break
                if len(jobs) < claim_limit:
                    stop_reason = "no_ready_jobs"
                    break
        job_counts_after = await self.get_llm_extraction_job_status_counts_for_day(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id_scope,
            session_id=session_id,
            start_at=start_at,
            end_at=end_at,
        )
        more_remain = bool(
            job_counts_after.get("pending", 0)
            or job_counts_after.get("running", 0)
            or job_counts_after.get("failed", 0)
        )

        if event_rows:
            acceptance = {
                "status": "needs_review",
                "score": 0.5,
                "reason": "daily_relationship_stats_mvp",
                "signals": {
                    "message_count": len(event_rows),
                    "sender_count": len(sender_ids),
                    "raw_message_count": raw_message_count,
                },
                "extraction_confidence": 0.5,
            }
            value_payload = {
                "kind": "daily_group_relationship_run",
                "run_key": run_key,
                "date": target_date,
                "window": {
                    "start": start_at.isoformat(),
                    "end": end_at.isoformat(),
                },
                "counts": {
                    "raw_messages": raw_message_count,
                    "imported_messages": len(event_rows),
                    "senders": len(sender_ids),
                },
                "sender_ids": sender_ids[:50],
                "source_event_ids": source_event_ids[:200],
                "status": result_status,
                "skipped_reason": skipped_reason,
                "acceptance": acceptance,
            }
            is_group_history_scope = user_id_scope == GROUP_HISTORY_USER_ID_SCOPE
            item = await self._insert_or_touch_memory_item(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id_scope,
                session_id=session_id,
                scope_type="session",
                source_type="backfill",
                memory_type="note",
                content=f"Daily group relationship extraction metadata {target_date}",
                value_json=value_payload,
                normalized_key=run_key,
                confidence=0.5,
                status="pending",
                pinned=False,
                priority=0,
                sensitivity="normal",
                origin_session_kind="group",
                audience_scope="session" if is_group_history_scope else "private",
                allowed_session_ids=[session_id] if is_group_history_scope else [],
                sensitivity_category="normal",
                source_kind="graph",
                source_event_id=source_event_ids[0] if source_event_ids else None,
                source_trace_id=run_key,
                original_text="",
            )
            if item and item.get("id") is not None:
                item_id = int(item["id"])
                memory_item_ids = [item_id]
                if existing_run_items:
                    updated_count = 1
                else:
                    created_count = 1
                await self._refresh_legacy_cache_for_item_scope(item)
                await self._sync_memory_graph_for_item_safe(item)
                await self._sync_memory_vector_for_item_safe(item)

        return {
            "ok": True,
            "status": result_status,
            "result_status": result_status,
            "skipped_reason": skipped_reason,
            "run_key": run_key,
            "idempotency_key": run_key,
            "scope": {
                "tenant_id": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "session_id": session_id,
                "user_id": user_id_scope,
                "user_id_scope": user_id_scope,
                "user_id_auto": user_id_auto,
            },
            "date": target_date,
            "window": {
                "start": start_at.isoformat(),
                "end": end_at.isoformat(),
            },
            "counts": {
                "raw_messages": raw_message_count,
                "imported_messages": len(event_rows),
                "senders": len(sender_ids),
                "source_events": len(source_event_ids),
                "created": created_count,
                "updated": updated_count,
                "memory_items": len(memory_item_ids),
                "facts": 0,
                "episodes": 0,
                "jobs": processed_job_counts["claimed"],
            },
            "job_counts_before": job_counts_before,
            "job_counts_after": job_counts_after,
            "job_counts": job_counts_after,
            "jobs": processed_job_counts,
            "controls": {
                "batch_limit": effective_batch_limit,
                "max_jobs": effective_max_jobs,
                "continuous": effective_continuous,
                "time_budget_seconds": effective_time_budget_seconds,
                "stop_reason": stop_reason,
            },
            "limit": effective_batch_limit,
            "more_remain": more_remain,
            "source_event_ids": source_event_ids[:200],
            "sender_ids": sender_ids[:50],
            "sender_count": len(sender_ids),
            "memory_item_ids": memory_item_ids,
            "created_count": created_count,
            "updated_count": updated_count,
            "generated_from": ["plugin_memory_event", "plugin_memory_item"],
        }

    async def review_group_relationship_edge(
        self,
        *,
        edge_id: str,
        tenant_id: str,
        action: str,
        review_reason: str = "",
        reviewed_by: str = "",
        channel: str | None = None,
        source_key: str | None = None,
        session_id: str | None = None,
        superseded_by_item_id: int | None = None,
        supersedes_item_id: int | None = None,
    ) -> dict[str, Any] | None:
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in MEMORY_ACCEPTANCE_REVIEW_ACTIONS:
            raise ValueError(f"unsupported acceptance review action: {normalized_action}")
        evidence = await self.get_group_relationship_edge_evidence(
            edge_id=edge_id,
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            session_id=session_id,
            include_raw=False,
        )
        if not evidence:
            return None
        evidence_ids = (
            evidence.get("evidence_ids") if isinstance(evidence.get("evidence_ids"), dict) else {}
        )
        backing_item_id = _safe_int(evidence_ids.get("backing_memory_item_id"), 0)
        memory_item_ids = (
            [backing_item_id]
            if backing_item_id > 0
            else sorted(_coerce_int_set(evidence_ids.get("memory_item_ids")))[:1]
        )
        reviewed_items: list[dict[str, Any]] = []
        for item_id in memory_item_ids:
            reviewed = await self.review_memory_item_acceptance(
                item_id,
                action=normalized_action,
                review_reason=review_reason,
                reviewed_by=reviewed_by,
                superseded_by_item_id=superseded_by_item_id,
                supersedes_item_id=supersedes_item_id,
            )
            if reviewed:
                reviewed_items.append(reviewed)
        if not reviewed_items:
            return None
        item_statuses = [
            {
                "id": item.get("id"),
                "status": item.get("status"),
                "acceptance_status": item.get("acceptance_status"),
            }
            for item in reviewed_items
        ]
        return {
            "ok": True,
            "edge": evidence.get("edge") or {"id": str(edge_id or "")},
            "edge_id": str(edge_id or ""),
            "action": normalized_action,
            "reviewed_by": reviewed_by,
            "review_reason": _normalize_line(str(review_reason or ""))[:240],
            "result": {
                "reviewed_item_count": len(reviewed_items),
                "memory_item_ids": [
                    int(item["id"]) for item in reviewed_items if item.get("id") is not None
                ],
                "item_statuses": item_statuses,
                "evidence_counts": evidence.get("evidence_counts") or {},
            },
            "evidence_ids": evidence.get("evidence_ids") or {},
        }

    async def sync_memory_graph(
        self,
        *,
        tenant_id: str,
        channel: str | None = None,
        source_key: str | None = None,
        user_id: str | None = None,
        limit: int = 500,
    ) -> dict[str, int]:
        conditions = ["tenant_id = :tid"]
        params: dict[str, Any] = {"tid": tenant_id, "lim": max(1, min(int(limit or 500), 1000))}
        if channel:
            conditions.append("channel = :channel")
            params["channel"] = channel
        if source_key:
            conditions.append("source_key = :source_key")
            params["source_key"] = source_key
        if user_id:
            conditions.append("user_id = :uid")
            params["uid"] = user_id
        rows = await _exec(
            "SELECT id, tenant_id, channel, source_key, user_id, session_id, scope_type, source_type, "
            "memory_type, content, value_json, normalized_key, confidence, status, pinned, priority, "
            "sensitivity, source_event_id, source_trace_id, original_text, occurrence_count, "
            "first_seen_at, last_seen_at, created_at, updated_at, deleted_at "
            "FROM plugin_memory_item "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY updated_at DESC LIMIT :lim",
            params,
        )
        synced = 0
        failed = 0
        for row in rows:
            try:
                await self._sync_memory_graph_for_item(self._finalize_memory_item(row))
                synced += 1
            except Exception as exc:
                failed += 1
                logger.warning(
                    "memory.graph_sync_failed",
                    item_id=row.get("id"),
                    error_type=exc.__class__.__name__,
                    error=_truncate_error(exc),
                )
        return {"scanned": len(rows), "synced": synced, "failed": failed}
