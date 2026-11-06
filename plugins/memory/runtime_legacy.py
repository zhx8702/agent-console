"""Read legacy group graph records only with connection-scoped evidence.

An external group ID alone is not an authorization boundary. Every observation
supporting a legacy item must belong to this tenant and canonical conversation.
Ambiguous, mixed-account and pre-provenance records are deliberately excluded.
"""
from __future__ import annotations

import json
from typing import Any

from app.channel.identity import canonical_conversation_id


async def retrieve_legacy_group_memory(
    store: Any,
    *,
    tenant_id: str,
    connection_id: str,
    session_id: str,
    external_session_id: str,
    source_key: str,
    query: str,
    limit: int = 6,
) -> dict[str, Any]:
    empty: dict[str, Any] = {"items": [], "facts": []}
    if (
        not connection_id
        or not session_id.startswith("cx1:")
        or not external_session_id.endswith("@chatroom")
        or external_session_id.startswith("cx1:")
        or canonical_conversation_id(connection_id, external_session_id) != session_id
    ):
        return empty

    from plugins.memory import store as memory_store

    # The caller already passed the current group policy. Check both owners
    # again because this compatibility read consumes wxbot observation data.
    gate = getattr(store, "combined_history_scope_execution_allowed", None)
    if not callable(gate) or await gate(tenant_id, session_id) is not True:
        return empty

    result = await store.retrieve_memory_hybrid(
        tenant_id=tenant_id,
        channel="wechat",
        source_key=source_key,
        user_id=memory_store.GROUP_HISTORY_USER_ID_SCOPE,
        session_id=external_session_id,
        query=query,
        limit=limit,
        include_graph=bool(getattr(store.settings, "memory_graph_retrieval_enabled", False)),
        request_session_kind="group",
    )
    ids = {
        int(item_id)
        for row in [*result.get("items", []), *result.get("facts", [])]
        if (item_id := row.get("memory_item_id", row.get("id"))) is not None
        and str(item_id).isdigit()
    }
    if not ids:
        return empty
    candidates = await memory_store._exec(
        "SELECT id, value_json FROM plugin_memory_item "
        "WHERE tenant_id = :tid AND channel = 'wechat' AND source_key = :source "
        "AND user_id = :uid AND session_id = :sid AND id = ANY(:ids) "
        "AND status = 'active' AND deleted_at IS NULL "
        "AND scope_type = 'session' AND audience_scope = 'session' "
        "AND origin_session_kind = 'group' "
        "AND sensitivity = 'normal' AND sensitivity_category = 'normal' "
        "AND (expires_at IS NULL OR expires_at > NOW()) "
        "AND source_type IN ('llm_group_window', 'deterministic_group_window')",
        {"tid": tenant_id, "source": source_key, "uid": memory_store.GROUP_HISTORY_USER_ID_SCOPE,
         "sid": external_session_id, "ids": sorted(ids)},
    )
    evidence_by_item: dict[int, set[int]] = {}
    for row in candidates:
        try:
            value = row["value_json"]
            value = json.loads(value) if isinstance(value, str) else value
            evidence = value.get("source_observation_ids")
            if not isinstance(evidence, list) or not 1 <= len(evidence) <= 100:
                continue
            if any(isinstance(item, bool) or not str(item).isdigit() for item in evidence):
                continue
            evidence_by_item[int(row["id"])] = {int(item) for item in evidence}
        except (TypeError, ValueError, AttributeError, KeyError):
            continue
    observation_ids = set().union(*evidence_by_item.values()) if evidence_by_item else set()
    if not observation_ids:
        return empty
    observations = await memory_store._exec(
        "SELECT id FROM plugin_wxbot_group_observations "
        "WHERE tenant_id = :tid AND session_id = :sid AND id = ANY(:ids)",
        {"tid": tenant_id, "sid": session_id, "ids": sorted(observation_ids)},
    )
    verified = {int(row["id"]) for row in observations}
    eligible = {item_id for item_id, evidence in evidence_by_item.items() if evidence <= verified}
    return {
        "items": [row for row in result.get("items", []) if row.get("id") in eligible],
        "facts": [row for row in result.get("facts", []) if row.get("memory_item_id") in eligible],
    }
