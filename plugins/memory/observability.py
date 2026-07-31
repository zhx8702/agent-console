"""Privacy-safe runtime observability helpers for the memory plugin."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9:_-]{1,64}$")
_SAFE_AUDIENCE_VALUES = {
    "private",
    "session",
    "group",
    "group_session_only",
    "identity",
    "unknown",
}
_SAFE_RETRIEVAL_MODES = {
    "none",
    "sql",
    "vector",
    "hybrid",
    "graph",
    "hybrid_graph",
}


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _safe_timestamp(value: Any) -> str | None:
    if isinstance(value, datetime | date):
        return value.isoformat()
    text = str(value or "").strip()
    if not text or len(text) > 64:
        return None
    return text


def _safe_id(value: Any) -> str | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    text = str(value or "").strip()
    return text if _SAFE_ID_RE.fullmatch(text) else None


def _selected_ids(rows: Any, *, limit: int = 50) -> list[str | int]:
    if not isinstance(rows, list):
        return []
    selected: list[str | int] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        candidate = _safe_id(row.get("id"))
        if candidate is None:
            continue
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        selected.append(candidate)
        if len(selected) >= limit:
            break
    return selected


def build_safe_memory_profile_signal(profile: Any) -> dict[str, Any]:
    """Return counts and opaque record IDs without memory text or user identifiers."""

    if not isinstance(profile, dict) or not profile:
        return {
            "loaded": False,
            "message_count": 0,
            "identity_message_count": 0,
            "session_message_count": 0,
            "imported_message_count": 0,
            "summary_version": 1,
            "memory_item_counts": {"identity": 0, "session": 0, "relevant": 0},
            "selected_item_ids": [],
            "selected_graph_fact_ids": [],
            "selected_graph_episode_ids": [],
        }

    memory_items = profile.get("memory_items")
    if not isinstance(memory_items, dict):
        memory_items = {}
    identity_items = memory_items.get("identity")
    session_items = memory_items.get("session")
    relevant_items = profile.get("relevant_memory_items")
    graph_facts = profile.get("relevant_graph_facts")
    graph_episodes = profile.get("relevant_graph_episodes")
    audience_scope = str(profile.get("audience_scope") or "").strip().lower()
    retrieval_mode = str(profile.get("retrieval_mode") or "").strip().lower()

    payload: dict[str, Any] = {
        "loaded": True,
        "message_count": _non_negative_int(profile.get("message_count")),
        "identity_message_count": _non_negative_int(profile.get("identity_message_count")),
        "session_message_count": _non_negative_int(profile.get("session_message_count")),
        "imported_message_count": _non_negative_int(profile.get("imported_message_count")),
        "summary_version": max(1, _non_negative_int(profile.get("summary_version"))),
        "memory_item_counts": {
            "identity": len(identity_items) if isinstance(identity_items, list) else 0,
            "session": len(session_items) if isinstance(session_items, list) else 0,
            "relevant": len(relevant_items) if isinstance(relevant_items, list) else 0,
        },
        "selected_item_ids": _selected_ids(relevant_items),
        "selected_graph_fact_ids": _selected_ids(graph_facts),
        "selected_graph_episode_ids": _selected_ids(graph_episodes),
        "has_session_summary": bool(str(profile.get("session_summary") or "").strip()),
        "has_manual_notes": bool(
            str(profile.get("manual_notes") or "").strip()
            or str(profile.get("identity_manual_notes") or "").strip()
            or str(profile.get("session_manual_notes") or "").strip()
        ),
        "truncated": bool(profile.get("truncated") or profile.get("retrieval_truncated")),
    }
    last_compacted_at = _safe_timestamp(profile.get("last_compacted_at"))
    if last_compacted_at is not None:
        payload["last_compacted_at"] = last_compacted_at
    if audience_scope in _SAFE_AUDIENCE_VALUES:
        payload["audience_scope"] = audience_scope
    if retrieval_mode in _SAFE_RETRIEVAL_MODES:
        payload["retrieval_mode"] = retrieval_mode
    return payload
