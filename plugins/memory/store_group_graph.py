"""Group relationship graph reads, evidence, extraction, review, and synchronization."""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
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
    GROUP_WINDOW_LLM_JOB_TRACE_PREFIX,
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
    _group_graph_label_is_technical,
    _group_graph_node_id,
    _group_graph_scope,
    _group_graph_timestamp,
    _group_history_user_scope,
    _is_group_session_id,
    _loads_json_object_or_array,
    _looks_like_wechat_username,
    _memory_status_for_acceptance,
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


def _group_graph_item_value(item: dict[str, Any] | None) -> dict[str, Any]:
    if not item:
        return {}
    value = item.get("value")
    if isinstance(value, dict):
        return value
    loaded = _safe_json_loads(item.get("value_json"), {})
    return loaded if isinstance(loaded, dict) else {}


EVIDENCE_SOURCE_MEMORY_EVENT = "memory_event"
EVIDENCE_SOURCE_OBSERVATION = "observation"
EVIDENCE_SOURCE_MIXED = "mixed"
GROUP_GRAPH_EVIDENCE_SOURCES = (EVIDENCE_SOURCE_MEMORY_EVENT, EVIDENCE_SOURCE_OBSERVATION)


def _group_graph_evidence_source(event_ids: Iterable[Any], observation_ids: Iterable[Any]) -> str:
    has_events = bool(_coerce_int_set(event_ids))
    has_observations = bool(_coerce_int_set(observation_ids))
    if has_events and has_observations:
        return EVIDENCE_SOURCE_MIXED
    if has_observations:
        return EVIDENCE_SOURCE_OBSERVATION
    return EVIDENCE_SOURCE_MEMORY_EVENT


def _group_graph_edge_quality(item: dict[str, Any] | None) -> dict[str, Any]:
    value = _group_graph_item_value(item)
    acceptance = value.get("acceptance") if isinstance(value.get("acceptance"), dict) else {}
    relation = value.get("relation") if isinstance(value.get("relation"), dict) else {}
    dates = value.get("evidence_dates")
    evidence_dates = (
        [str(entry).strip()[:10] for entry in dates if str(entry).strip()]
        if isinstance(dates, list)
        else []
    )
    evidence_dates = list(dict.fromkeys(evidence_dates))[:90]
    score = acceptance.get("score")
    if score is None:
        score = item.get("acceptance_score") if item else None
    reason = str(acceptance.get("reason") or (item or {}).get("acceptance_reason") or "").strip()
    event_ids = _coerce_int_set(
        relation.get("evidence_event_ids") or value.get("source_event_ids") or []
    )
    observation_ids = _coerce_int_set(
        relation.get("evidence_observation_ids") or value.get("source_observation_ids") or []
    )
    first_seen_date = str(value.get("first_seen_date") or "").strip()[:10] or (
        min(evidence_dates) if evidence_dates else None
    )
    last_seen_date = str(value.get("last_seen_date") or "").strip()[:10] or (
        max(evidence_dates) if evidence_dates else None
    )
    return {
        "evidence_dates": evidence_dates,
        "acceptance_score": _clamp_score(score) if score is not None else None,
        "acceptance_reason": reason[:80] or None,
        "evidence_event_count": len(event_ids),
        "evidence_observation_count": len(observation_ids),
        "evidence_day_count": len(evidence_dates),
        "evidence_source": str(value.get("evidence_source") or "").strip()
        or _group_graph_evidence_source(event_ids, observation_ids),
        "first_seen_date": first_seen_date or None,
        "last_seen_date": last_seen_date or None,
    }


SYMMETRIC_GROUP_PREDICATES = frozenset({"co_participated", "collaborated_with"})


def _end_of_day(value: datetime, date_text: Any) -> datetime:
    """Treat a date-only evidence boundary as the end of that day."""

    if date_text and len(str(date_text).strip()) <= 10:
        return value.replace(hour=23, minute=59, second=59, microsecond=999999)
    return value


def _merge_symmetric_group_edges(edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse A→B and B→A rows of symmetric predicates into one edge.

    Co-participation is stored directionally because the pair order depends on
    who spoke first in a window; on the wire it is one undirected relation.
    """

    merged: list[dict[str, Any]] = []
    by_pair: dict[tuple[str, str, str], dict[str, Any]] = {}
    for edge in edges:
        predicate = str(edge.get("type") or "")
        if predicate not in SYMMETRIC_GROUP_PREDICATES:
            merged.append(edge)
            continue
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        key = (predicate, *sorted((source, target)))
        existing = by_pair.get(key)
        if existing is None:
            edge = {**edge, "mirror_ids": []}
            by_pair[key] = edge
            merged.append(edge)
            continue
        existing["mirror_ids"] = [*existing.get("mirror_ids", []), edge.get("id")]
        existing["confidence"] = max(
            _clamp_score(existing.get("confidence"), 0.0), _clamp_score(edge.get("confidence"), 0.0)
        )
        existing["evidence_count"] = int(existing.get("evidence_count") or 0) + int(
            edge.get("evidence_count") or 0
        )
        existing["observation_count"] = int(existing.get("observation_count") or 0) + int(
            edge.get("observation_count") or 0
        )
        dates = list(dict.fromkeys([*(existing.get("evidence_dates") or []), *(edge.get("evidence_dates") or [])]))
        existing["evidence_dates"] = sorted(dates)[:90]
        existing["evidence_day_count"] = max(
            len(existing["evidence_dates"]), int(existing.get("evidence_day_count") or 0)
        )
        for key_name, pick in (("first_seen", min), ("last_seen", max)):
            values = [str(v) for v in (existing.get(key_name), edge.get(key_name)) if v]
            if values:
                existing[key_name] = pick(values)
        for key_name, pick in (("first_seen_date", min), ("last_seen_date", max)):
            values = [str(v) for v in (existing.get(key_name), edge.get(key_name)) if v]
            existing[key_name] = pick(values) if values else None
        strengths = [
            float(v) for v in (existing.get("strength"), edge.get("strength")) if v is not None
        ]
        if strengths:
            existing["strength"] = round(max(strengths), 4)
        existing["source_event_ids"] = _merge_int_lists(
            existing.get("source_event_ids"), edge.get("source_event_ids"), max_items=200
        )
        existing["memory_item_ids"] = _merge_int_lists(
            existing.get("memory_item_ids"), edge.get("memory_item_ids"), max_items=200
        )
        if existing.get("acceptance_status") != "accepted" and edge.get("acceptance_status") == "accepted":
            existing["acceptance_status"] = "accepted"
    return merged


def _group_graph_item_observation_ids(item: dict[str, Any] | None) -> list[int]:
    value = _group_graph_item_value(item)
    relation = value.get("relation") if isinstance(value.get("relation"), dict) else {}
    return sorted(
        _coerce_int_set(relation.get("evidence_observation_ids") or [])
        | _coerce_int_set(value.get("source_observation_ids") or [])
    )


def _split_window_evidence_ids(
    candidate_ids: Iterable[Any],
    observation_ids: set[int],
) -> tuple[list[int], list[int]]:
    """Split window evidence into memory-event ids and group-observation ids.

    Observation rows come from ``plugin_wxbot_group_observations`` whose id
    space is unrelated to ``plugin_memory_event``. Keeping them under their own
    key preserves the evidence trail without tricking the memory-event
    provenance check that runs when the item is written.
    """

    event_ids: list[int] = []
    observation_evidence: list[int] = []
    for event_id in sorted(_coerce_int_set(candidate_ids)):
        if event_id in observation_ids:
            observation_evidence.append(event_id)
        else:
            event_ids.append(event_id)
    return event_ids, observation_evidence


def _group_graph_auto_cursor_key(
    *,
    tenant_id: str,
    channel: str,
    source_key: str,
    session_id: str,
    target_date: str,
) -> str:
    scope = "\x1f".join((tenant_id, channel, source_key, session_id, target_date))
    return f"group-graph-auto-cursor:v1:{_normalize_key(scope)}"


def _normalize_evidence_source(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text if text in GROUP_GRAPH_EVIDENCE_SOURCES else None


# Deterministic interaction signals. ``direct`` signals are observable platform
# facts (a quoted reply, an @-mention, an explicit "回复" prefix); they may be
# accepted on first sight. Anything else needs repetition before acceptance.
SIGNAL_QUOTE = "quote"
SIGNAL_MENTION = "mention"
SIGNAL_PREFIX_REPLY = "prefix_reply"
SIGNAL_CO_PARTICIPATION = "co_participation"
SIGNAL_LLM = "llm"
DIRECT_GROUP_SIGNALS = (SIGNAL_QUOTE, SIGNAL_MENTION, SIGNAL_PREFIX_REPLY)
GROUP_SIGNAL_KEYS = (*DIRECT_GROUP_SIGNALS, SIGNAL_CO_PARTICIPATION, SIGNAL_LLM)
QUOTE_REPLY_CONFIDENCE = 0.9
MENTION_METADATA_CONFIDENCE = 0.85
MENTION_TEXT_CONFIDENCE = 0.72
PREFIX_REPLY_CONFIDENCE = 0.62
CO_PARTICIPATION_CONFIDENCE = 0.45
GROUP_RELATION_MIN_LLM_DAYS = 2
# A model-only relation is also accepted without a second day once this many
# distinct messages in the window support it; one-off claims stay in review.
GROUP_RELATION_MIN_LLM_EVIDENCE = 3
# Below this many supporting messages an LLM candidate is not stored at all:
# on the production data single-message claims were the bulk of the noise.
GROUP_RELATION_MIN_LLM_WINDOW_EVIDENCE = 2
GROUP_RELATION_MIN_WEAK_DAYS = 2
GROUP_RELATION_MIN_WEAK_COUNT = 3
# The rule layer owns these; a model re-stating them adds nothing but noise.
LLM_EXCLUDED_PREDICATES = frozenset({"co_participated"})
# Objects for the pseudo-entity "the group" ("asked the group") are dropped.
LLM_GROUP_PSEUDO_OBJECTS = frozenset({"group", "the group", "群", "群聊", "大家", "everyone", "all"})
LLM_TERM_MIN_LENGTH = 2
LLM_TERM_MAX_LENGTH = 40

_MENTION_TOKEN_RE = re.compile(r"@([^\s\u2005\u00a0@:：,，;；]+)")

# LLM window jobs live in plugin_memory_extraction_job next to per-event jobs
# (see GROUP_WINDOW_LLM_JOB_TRACE_PREFIX in store.py).
GROUP_WINDOW_LLM_JOB_KIND = "group_window_llm"
LLM_MODE_INLINE = "inline"
LLM_MODE_ENQUEUE = "enqueue"
GROUP_WINDOW_LLM_TIMEOUT_DEFAULT = 60
GROUP_WINDOW_LLM_TIMEOUT_MIN = 5
GROUP_WINDOW_LLM_TIMEOUT_MAX = 600
GROUP_WINDOW_LLM_MODEL_TIER_DEFAULT = "tier-1"
# LLM-proposed objects typed "person" that are not real participants are almost
# always terms ("缩水", "gpt", "4.8"); these predicates keep them as topics.
LLM_PERSON_TO_TOPIC_PREDICATES = frozenset(
    {"mentioned", "interested_in", "asked", "requested", "reported_issue", "works_on", "provided_resource"}
)


def _group_window_llm_job_key(
    *,
    tenant_id: str,
    channel: str,
    source_key: str,
    session_id: str,
    target_date: str,
    source: str,
    first_event_id: Any,
    last_event_id: Any,
) -> str:
    scope = "\x1f".join(
        (
            tenant_id,
            channel,
            source_key,
            session_id,
            target_date,
            source or EVIDENCE_SOURCE_MEMORY_EVENT,
            str(first_event_id or 0),
            str(last_event_id or 0),
        )
    )
    return f"{GROUP_WINDOW_LLM_JOB_TRACE_PREFIX}v1:{_normalize_key(scope)}"


def _normalize_llm_mode(value: Any) -> str:
    text = str(value or "").strip().lower()
    return LLM_MODE_ENQUEUE if text == LLM_MODE_ENQUEUE else LLM_MODE_INLINE


def _normalize_group_name(value: Any) -> str:
    text = _normalize_line(_sanitize_db_text(value))
    return re.sub(r"[\s\u2005\u00a0]+", "", text).lower()


_LLM_TERM_WORD_RE = re.compile(r"[^\W\d_]", re.UNICODE)
# wxid_xxx, bare QQ-style numbers and letter+digits account ids: member
# identifiers, never topics.
_PARTICIPANT_ID_RE = re.compile(r"^(?:wxid_[\w-]+|\d{5,}|[a-z]\d{6,})$", re.IGNORECASE)


def _looks_like_participant_id(value: str) -> bool:
    return bool(_PARTICIPANT_ID_RE.match(_normalize_line(str(value or ""))))


def _is_usable_llm_term(value: str) -> bool:
    """Reject model-proposed terms that cannot be a graph node.

    Sentences (too long), single characters, bare numbers/times ("9点", "4.8")
    and punctuation-only strings are dropped; a term must contain at least one
    letter or CJK character and stay within ``LLM_TERM_MAX_LENGTH``.
    """

    text = _normalize_line(str(value or ""))
    if len(text) < LLM_TERM_MIN_LENGTH or len(text) > LLM_TERM_MAX_LENGTH:
        return False
    letters = _LLM_TERM_WORD_RE.findall(text)
    if not letters:
        return False
    # "9点" / "5x" style: one letter riding on digits is still a number.
    if len(letters) < 2 and re.search(r"\d", text):
        return False
    return True


def _observation_interaction_metadata(
    raw_metadata: Any,
    *,
    sender_name: Any = None,
) -> dict[str, Any]:
    """Pull the interaction facts out of ``metadata_json`` without keeping text.

    Only identifiers, display names and message ids are retained: who was
    quoted (``quote.fromusr`` / ``quote.sender_name``), who was @-mentioned
    (``at_wxids``) and the bot's own id so it can be excluded as a target.
    """

    metadata = raw_metadata if isinstance(raw_metadata, dict) else _safe_json_loads(raw_metadata, {})
    if not isinstance(metadata, dict):
        metadata = {}
    quote = metadata.get("quote") if isinstance(metadata.get("quote"), dict) else {}
    at_wxids_raw = metadata.get("at_wxids")
    at_wxids = (
        [str(item).strip() for item in at_wxids_raw if str(item or "").strip()]
        if isinstance(at_wxids_raw, list)
        else []
    )
    return {
        "sender_name": _normalize_line(_sanitize_db_text(sender_name))[:80],
        "quote_from": str(quote.get("fromusr") or "").strip()[:200],
        "quote_sender_name": _normalize_line(_sanitize_db_text(quote.get("sender_name")))[:80],
        "quote_message_id": str(quote.get("refer_msg_svr_id") or "").strip()[:64],
        "at_wxids": at_wxids[:20],
        "bot_wxid": str(metadata.get("bot_wxid") or "").strip()[:200],
    }


class GroupMemberDirectory:
    """Resolve the different id/name forms a member shows up under in one group.

    ``sender_wxid`` is the canonical node id. Quote and @ metadata sometimes
    carry a member's alias id instead, so resolution goes: known sender id →
    nickname match → alias id known to the membership table → unresolved.
    Unresolved targets are dropped rather than turned into phantom nodes.
    """

    def __init__(self) -> None:
        self.names_by_id: dict[str, str] = {}
        self.ids_by_name: dict[str, str] = {}
        self.sender_ids: set[str] = set()
        self._sorted_names: list[str] | None = None

    def add_sender(self, member_id: Any, name: Any = None) -> None:
        canonical = _normalize_line(_sanitize_db_text(member_id))[:200]
        if not canonical:
            return
        self.sender_ids.add(canonical)
        self.add_member(canonical, name, prefer=True)

    def add_member(self, member_id: Any, name: Any = None, *, prefer: bool = False) -> None:
        canonical = _normalize_line(_sanitize_db_text(member_id))[:200]
        if not canonical:
            return
        display = _normalize_line(_sanitize_db_text(name))[:80]
        if display and display != canonical:
            self.names_by_id.setdefault(canonical, display)
            key = _normalize_group_name(display)
            if key and (
                key not in self.ids_by_name
                or (prefer and self.ids_by_name[key] not in self.sender_ids)
            ):
                self.ids_by_name[key] = canonical
                self._sorted_names = None

    def resolve_name(self, name: Any) -> str | None:
        key = _normalize_group_name(name)
        if not key:
            return None
        exact = self.ids_by_name.get(key)
        if exact:
            return exact
        if self._sorted_names is None:
            self._sorted_names = sorted(self.ids_by_name, key=len, reverse=True)
        for known in self._sorted_names:
            if len(known) >= 2 and key.startswith(known):
                return self.ids_by_name[known]
        return None

    def resolve_id(self, raw_id: Any, *, fallback_name: Any = None) -> str | None:
        candidate = _normalize_line(_sanitize_db_text(raw_id))[:200]
        if candidate.startswith("user:"):
            candidate = candidate[5:]
        if candidate and candidate in self.sender_ids:
            return candidate
        via_name = self.resolve_name(fallback_name) if fallback_name else None
        if via_name:
            return via_name
        if candidate and candidate in self.names_by_id:
            via_alias = self.resolve_name(self.names_by_id[candidate])
            return via_alias or candidate
        return None


def _empty_group_signals() -> dict[str, int]:
    return dict.fromkeys(GROUP_SIGNAL_KEYS, 0)


def _merge_group_signals(*values: Any) -> dict[str, int]:
    merged = _empty_group_signals()
    for value in values:
        if not isinstance(value, dict):
            continue
        for key in GROUP_SIGNAL_KEYS:
            merged[key] += max(0, _safe_int(value.get(key), 0))
    return merged


def _merge_signal_evidence(*values: Any, max_items: int = 200) -> dict[str, list[int]]:
    """Union per-signal evidence ids. Counting ids instead of occurrences keeps
    ``signals`` idempotent when a window is processed twice (retries, manual
    re-runs, the LLM pass following the deterministic pass)."""

    merged: dict[str, list[int]] = {key: [] for key in GROUP_SIGNAL_KEYS}
    for value in values:
        if not isinstance(value, dict):
            continue
        for key in GROUP_SIGNAL_KEYS:
            merged[key] = _merge_int_lists(merged[key], value.get(key), max_items=max_items)
    return merged


def _signals_from_evidence(signal_evidence: dict[str, list[int]]) -> dict[str, int]:
    signals = _empty_group_signals()
    for key in GROUP_SIGNAL_KEYS:
        signals[key] = len(_coerce_int_set(signal_evidence.get(key) or []))
    return signals


_LEGACY_REASON_SIGNALS = {
    "deterministic_quote_reply": SIGNAL_QUOTE,
    "deterministic_at_mention_metadata": SIGNAL_MENTION,
    "deterministic_addressed_participant": SIGNAL_MENTION,
    "deterministic_adjacent_reply_window": SIGNAL_PREFIX_REPLY,
    "deterministic_same_window_participation": SIGNAL_CO_PARTICIPATION,
}


def _infer_legacy_group_signals(relation: dict[str, Any], source_type: str) -> dict[str, int]:
    """Signal counts for relations written before ``signals`` existed."""

    signals = _empty_group_signals()
    reason = str(relation.get("reason") or "").strip()
    mapped = _LEGACY_REASON_SIGNALS.get(reason)
    if mapped is None:
        method = str(relation.get("extraction_method") or source_type or "")
        if method == DETERMINISTIC_GROUP_WINDOW_SOURCE_TYPE:
            mapped = (
                SIGNAL_CO_PARTICIPATION
                if str(relation.get("predicate") or "") == "co_participated"
                else SIGNAL_PREFIX_REPLY
            )
        else:
            mapped = SIGNAL_LLM
    # One legacy item = one known occurrence; evidence ids per occurrence vary
    # by rule, so they are not a reliable count.
    signals[mapped] = 1
    return signals


def _group_relation_strength(signals: dict[str, int], day_count: int) -> float:
    """Accumulated interaction strength in [0, 1]: more direct hits and more
    distinct days saturate towards 1 with diminishing returns."""

    direct = sum(signals.get(key, 0) for key in DIRECT_GROUP_SIGNALS)
    weak = signals.get(SIGNAL_CO_PARTICIPATION, 0) + signals.get(SIGNAL_LLM, 0)
    exponent = direct / 3.0 + weak / 10.0 + max(0, int(day_count) - 1) / 4.0
    return round(_clamp_score(1.0 - math.exp(-exponent), 0.0), 4)


def _group_relation_acceptance_decision(
    signals: dict[str, int],
    *,
    day_count: int,
    auto_accept: bool,
) -> tuple[str, str]:
    """Deterministic acceptance policy for group window relations."""

    if not auto_accept:
        return "needs_review", "group_window_relation"
    if any(signals.get(key, 0) > 0 for key in DIRECT_GROUP_SIGNALS):
        return "accepted", "group_window_direct_signal"
    llm_count = int(signals.get(SIGNAL_LLM, 0) or 0)
    if llm_count > 0 and int(day_count) >= GROUP_RELATION_MIN_LLM_DAYS:
        return "accepted", "group_window_llm_multi_day"
    if llm_count >= GROUP_RELATION_MIN_LLM_EVIDENCE:
        return "accepted", "group_window_llm_repeated_evidence"
    if (
        signals.get(SIGNAL_CO_PARTICIPATION, 0) >= GROUP_RELATION_MIN_WEAK_COUNT
        and int(day_count) >= GROUP_RELATION_MIN_WEAK_DAYS
    ):
        return "accepted", "group_window_repeated_weak_signal"
    return "needs_review", "group_window_weak_signal"


def _clean_session_ids(*values: Any) -> list[str]:
    cleaned: list[str] = []
    for value in values:
        if isinstance(value, (list, tuple, set)):
            cleaned.extend(_clean_session_ids(*value))
            continue
        text = str(value or "").strip()
        if text:
            cleaned.append(text)
    return list(dict.fromkeys(cleaned))


def _scope_session_ids(
    session_id: str | None,
    session_ids: list[str] | None,
) -> list[str] | None:
    if session_ids is not None:
        cleaned = _clean_session_ids(session_ids)
        if cleaned:
            return cleaned
    if session_id is None:
        return None
    return [str(session_id).strip()]


def _prefer_operator_group_session_id(
    session_ids: Iterable[str],
    fallback: str = "",
) -> str:
    ids = _clean_session_ids(list(session_ids), fallback)
    external = [
        item
        for item in ids
        if not item.startswith("cx1:") and _is_group_session_id(item)
    ]
    if external:
        return external[0]
    group_ids = [item for item in ids if _is_group_session_id(item)]
    if group_ids:
        return group_ids[0]
    return ids[0] if ids else str(fallback or "").strip()


class MemoryGroupGraphStoreMixin:
    async def list_memory_graph_entities(
        self,
        *,
        tenant_id: str,
        channel: str | None = None,
        source_key: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        session_ids: list[str] | None = None,
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
        scoped_session_ids = _scope_session_ids(session_id, session_ids)
        if scoped_session_ids is not None:
            if len(scoped_session_ids) == 1:
                session_clause = "scope_item.session_id = :sid"
                params["sid"] = scoped_session_ids[0]
            else:
                session_clause = "scope_item.session_id = ANY(:sids)"
                params["sids"] = scoped_session_ids
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
                f"AND {session_clause} "
                "AND scope_item.deleted_at IS NULL "
                "AND scope_item.status NOT IN ('deleted', 'invalidated')"
                ")"
            )
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

    def _group_graph_fact_filter_sql(
        self,
        params: dict[str, Any],
        *,
        predicates: list[str] | None,
        min_confidence: float | None,
        acceptance_statuses: list[str] | None,
        default_accepted_only: bool,
        from_date: str | None,
        to_date: str | None,
    ) -> list[str]:
        """SQL conditions for the graph read path, evaluated before LIMIT.

        Acceptance and evidence dates live on the backing memory item
        (``backing``), so the caller must LEFT JOIN it. Doing this in SQL keeps a
        500-row cap from silently hiding older or non-dominant edges.
        """

        conditions: list[str] = []
        acceptance_expr = (
            "COALESCE(NULLIF(NULLIF(backing.value_json, '')::jsonb #>> '{acceptance,status}', ''), "
            "CASE WHEN backing.status = 'active' THEN 'accepted' ELSE 'needs_review' END)"
        )
        if predicates:
            conditions.append("fact.predicate = ANY(:predicates)")
            params["predicates"] = list(predicates)
        if min_confidence is not None:
            conditions.append("fact.confidence >= :min_confidence")
            params["min_confidence"] = float(min_confidence)
        if acceptance_statuses:
            conditions.append(f"{acceptance_expr} = ANY(:acceptance_statuses)")
            params["acceptance_statuses"] = list(acceptance_statuses)
        elif default_accepted_only:
            conditions.append(
                f"{acceptance_expr} = 'accepted' "
                "AND backing.status = 'active' AND backing.deleted_at IS NULL"
            )
        first_seen_expr = (
            "COALESCE(NULLIF(NULLIF(backing.value_json, '')::jsonb ->> 'first_seen_date', ''), "
            "to_char(COALESCE(fact.valid_at, fact.created_at, fact.updated_at), 'YYYY-MM-DD'))"
        )
        last_seen_expr = (
            "COALESCE(NULLIF(NULLIF(backing.value_json, '')::jsonb ->> 'last_seen_date', ''), "
            "to_char(COALESCE(fact.valid_at, fact.created_at, fact.updated_at), 'YYYY-MM-DD'))"
        )
        if from_date:
            # An edge is in range when its evidence interval overlaps the window.
            conditions.append(f"{last_seen_expr} >= :from_date")
            params["from_date"] = from_date
        if to_date:
            conditions.append(f"{first_seen_expr} <= :to_date")
            params["to_date"] = to_date
        return conditions

    async def count_memory_graph_facts(
        self,
        *,
        tenant_id: str,
        channel: str | None = None,
        source_key: str | None = None,
        session_ids: list[str] | None = None,
        status: str | None = None,
        predicates: list[str] | None = None,
        min_confidence: float | None = None,
        acceptance_statuses: list[str] | None = None,
        default_accepted_only: bool = False,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> int:
        conditions = ["fact.tenant_id = :tid"]
        params: dict[str, Any] = {"tid": tenant_id}
        if channel is not None:
            conditions.append("fact.channel = :channel")
            params["channel"] = channel
        if source_key is not None:
            conditions.append("fact.source_key = :source_key")
            params["source_key"] = source_key
        if session_ids:
            conditions.append("backing.session_id = ANY(:sids)")
            params["sids"] = list(session_ids)
        if status is not None:
            conditions.append("fact.status = :status")
            params["status"] = status
        conditions.extend(
            self._group_graph_fact_filter_sql(
                params,
                predicates=predicates,
                min_confidence=min_confidence,
                acceptance_statuses=acceptance_statuses,
                default_accepted_only=default_accepted_only,
                from_date=from_date,
                to_date=to_date,
            )
        )
        try:
            rows = await _exec(
                "SELECT COUNT(*) AS count FROM plugin_memory_fact fact "
                "LEFT JOIN plugin_memory_item backing ON backing.id = fact.memory_item_id "
                f"WHERE {' AND '.join(conditions)}",
                params,
            )
        except Exception:
            logger.warning("memory.group_graph_fact_count_failed", exc_info=True)
            return 0
        return _safe_int((rows[0] if rows else {}).get("count"), 0)

    async def list_memory_graph_facts(
        self,
        *,
        tenant_id: str,
        channel: str | None = None,
        source_key: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        session_ids: list[str] | None = None,
        status: str | None = None,
        limit: int = 100,
        predicates: list[str] | None = None,
        min_confidence: float | None = None,
        acceptance_statuses: list[str] | None = None,
        default_accepted_only: bool = False,
        from_date: str | None = None,
        to_date: str | None = None,
        order_by_strength: bool = False,
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
        scoped_session_ids = _scope_session_ids(session_id, session_ids)
        if scoped_session_ids is not None:
            if len(scoped_session_ids) == 1:
                session_clause = "scope_item.session_id = :sid"
                params["sid"] = scoped_session_ids[0]
            else:
                session_clause = "scope_item.session_id = ANY(:sids)"
                params["sids"] = scoped_session_ids
            conditions.append(
                "EXISTS ("
                "SELECT 1 FROM plugin_memory_item scope_item "
                "WHERE scope_item.id = fact.memory_item_id "
                "AND scope_item.tenant_id = fact.tenant_id "
                "AND scope_item.channel = fact.channel "
                "AND scope_item.source_key = fact.source_key "
                "AND scope_item.user_id = fact.user_id "
                f"AND {session_clause} "
                "AND scope_item.deleted_at IS NULL "
                "AND scope_item.status NOT IN ('deleted', 'invalidated')"
                ")"
            )
        if status is not None:
            conditions.append("fact.status = :status")
            params["status"] = status
        graph_filters = self._group_graph_fact_filter_sql(
            params,
            predicates=predicates,
            min_confidence=min_confidence,
            acceptance_statuses=acceptance_statuses,
            default_accepted_only=default_accepted_only,
            from_date=from_date,
            to_date=to_date,
        )
        conditions.extend(graph_filters)
        needs_backing = bool(graph_filters) or order_by_strength
        backing_join = (
            "LEFT JOIN plugin_memory_item backing ON backing.id = fact.memory_item_id "
            if needs_backing
            else ""
        )
        order_sql = (
            "ORDER BY COALESCE((NULLIF(backing.value_json, '')::jsonb #>> '{relation,strength}')::float, 0) DESC, "
            "fact.updated_at DESC, fact.id DESC"
            if order_by_strength
            else "ORDER BY fact.updated_at DESC, fact.id DESC"
        )
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
            f"{backing_join}"
            f"WHERE {' AND '.join(conditions)} "
            f"{order_sql} LIMIT :lim",
            params,
        )
        for row in rows:
            row["confidence"] = float(row.get("confidence") or 0.0)
        return rows

    async def _list_memory_graph_entities_by_ids(
        self,
        *,
        tenant_id: str,
        entity_ids: Iterable[Any],
    ) -> list[dict[str, Any]]:
        """Fetch the endpoints of already-selected facts so no edge is dropped
        just because its entity fell outside the recency-ordered entity page."""

        ids = sorted(_coerce_int_set(entity_ids))
        if not ids:
            return []
        try:
            rows = await _exec(
                "SELECT entity.id, entity.tenant_id, entity.channel, entity.source_key, "
                "entity.user_id, entity.entity_type, entity.name, entity.normalized_name, "
                "entity.aliases_json, entity.confidence, entity.status, "
                "entity.created_at, entity.updated_at "
                "FROM plugin_memory_entity entity "
                "WHERE entity.tenant_id = :tid AND entity.id = ANY(:ids)",
                {"tid": tenant_id, "ids": ids[:1000]},
            )
        except Exception:
            logger.warning("memory.group_graph_entity_fetch_failed", exc_info=True)
            return []
        for row in rows or []:
            row["aliases"] = _safe_json_loads(row.get("aliases_json"), [])
            row["confidence"] = float(row.get("confidence") or 0.0)
        return list(rows or [])

    async def list_memory_graph_episodes(
        self,
        *,
        tenant_id: str,
        channel: str | None = None,
        source_key: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        session_ids: list[str] | None = None,
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
        scoped_session_ids = _scope_session_ids(session_id, session_ids)
        if scoped_session_ids is not None:
            if len(scoped_session_ids) == 1:
                conditions.append("session_id = :sid")
                params["sid"] = scoped_session_ids[0]
            else:
                conditions.append("session_id = ANY(:sids)")
                params["sids"] = scoped_session_ids
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
        requested_predicates = [
            value.strip()
            for value in str(edge_type or "").split(",")
            if value.strip()
        ]
        confidence_floor = (
            _clamp_score(min_confidence, default=0.0) if min_confidence is not None else None
        )
        from_dt = _coerce_datetime(from_)
        to_dt = _coerce_datetime(to)
        from_date = from_dt.date().isoformat() if from_dt is not None else None
        to_date = to_dt.date().isoformat() if to_dt is not None else None
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

        scoped_session_ids = (
            await self._resolve_group_graph_session_ids(
                tenant_id=tenant_id,
                session_id=session_id,
            )
            if session_id is not None
            else []
        )
        scoped_session_set = set(scoped_session_ids)
        entities = await self.list_memory_graph_entities(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=None,
            session_id=session_id,
            session_ids=scoped_session_ids or None,
            status=status_filter,
            limit=fetch_limit,
        )
        # Filters are applied in SQL before the row cap so the strongest matching
        # edges come back first instead of the most recently touched rows.
        fact_filter_kwargs: dict[str, Any] = {
            "predicates": requested_predicates or None,
            "min_confidence": confidence_floor,
            "acceptance_statuses": sorted(requested_acceptance) or None,
            "default_accepted_only": not requested_acceptance,
            "from_date": from_date,
            "to_date": to_date,
        }
        facts = await self.list_memory_graph_facts(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=None,
            session_id=session_id,
            session_ids=scoped_session_ids or None,
            status=status_filter,
            limit=fetch_limit,
            order_by_strength=True,
            **fact_filter_kwargs,
        )
        total_matching_facts = await self.count_memory_graph_facts(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            session_ids=scoped_session_ids or None,
            status=status_filter,
            **fact_filter_kwargs,
        )
        # Endpoints of the selected facts must be present even when they fall
        # outside the recency-ordered entity page.
        known_entity_ids = {row.get("id") for row in entities if row.get("id") is not None}
        missing_entity_ids = {
            entity_id
            for fact in facts
            for entity_id in (fact.get("subject_entity_id"), fact.get("object_entity_id"))
            if entity_id is not None and entity_id not in known_entity_ids
        }
        if missing_entity_ids:
            entities = [
                *entities,
                *await self._list_memory_graph_entities_by_ids(
                    tenant_id=tenant_id,
                    entity_ids=missing_entity_ids,
                ),
            ]
        episodes = await self.list_memory_graph_episodes(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=None,
            session_id=session_id,
            session_ids=scoped_session_ids or None,
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

        # node_type selects edges that touch a node of that type; the other
        # endpoint stays so a person -> tool graph filtered to "tool" still
        # shows who is attached to each tool. Unconnected nodes are pruned below.
        typed_node_ids: set[str] = set()
        for entity in entities:
            node_id = _group_graph_node_id(entity)
            status = str(entity.get("status") or "active")
            confidence = _clamp_score(entity.get("confidence"))
            node_acceptance = _group_graph_acceptance_status(entity)
            if confidence_floor is not None and confidence < confidence_floor:
                continue
            if node_type and str(entity.get("entity_type") or "") == node_type:
                typed_node_ids.add(node_id)
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
            if requested_predicates and predicate not in requested_predicates:
                continue
            confidence = _clamp_score(fact.get("confidence"))
            if confidence_floor is not None and confidence < confidence_floor:
                continue
            memory_item_id = next(iter(_coerce_int_set([fact.get("memory_item_id")])), None)
            backing_item = item_by_id.get(memory_item_id) if memory_item_id is not None else None
            if session_id is not None:
                fact_session_matches = False
                backing_session = str((backing_item or {}).get("session_id") or "")
                if backing_session and backing_session in scoped_session_set:
                    fact_session_matches = True
                event_ids_for_fact = set(_coerce_int_set([fact.get("source_event_id")]))
                if backing_item:
                    event_ids_for_fact.update(
                        _coerce_int_set([backing_item.get("source_event_id")])
                    )
                if any(
                    event_session_by_id.get(event_id) in scoped_session_set
                    for event_id in event_ids_for_fact
                ):
                    fact_session_matches = True
                if not fact_session_matches:
                    continue
            timestamp = _group_graph_timestamp(fact, "valid_at", "created_at", "updated_at")
            timestamp_dt = _coerce_datetime(timestamp)
            quality = _group_graph_edge_quality(backing_item)
            # Evidence dates (when the message was observed) win over fact
            # timestamps (when the relation was extracted) for time filtering.
            first_seen_dt = _coerce_datetime(quality["first_seen_date"]) or timestamp_dt
            last_seen_dt = _coerce_datetime(quality["last_seen_date"]) or timestamp_dt
            if from_dt is not None and last_seen_dt is not None:
                if _end_of_day(last_seen_dt, quality["last_seen_date"]) < from_dt:
                    continue
            if to_dt is not None and first_seen_dt is not None and first_seen_dt > to_dt:
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
                if node_type and not typed_node_ids.intersection(
                    {source_node_id, target_node_id}
                ):
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
            evidence_dates = quality["evidence_dates"]
            if not evidence_dates and timestamp:
                evidence_dates = [str(timestamp)[:10]]
            stored_evidence_count = int(quality["evidence_event_count"]) + int(
                quality["evidence_observation_count"]
            )
            relation_payload = _group_graph_item_value(backing_item).get("relation")
            relation_payload = relation_payload if isinstance(relation_payload, dict) else {}
            extracted_at = _group_graph_timestamp(fact, "updated_at", "valid_at", "created_at")
            edge = {
                "id": _group_graph_edge_id(fact),
                "source": source_node_id,
                "target": target_node_id,
                "type": predicate,
                "label": predicate,
                "confidence": confidence,
                "acceptance_status": edge_acceptance,
                "acceptance_score": quality["acceptance_score"],
                "acceptance_reason": quality["acceptance_reason"],
                "evidence_count": max(1, source_ref_count, stored_evidence_count),
                "evidence_dates": evidence_dates,
                "evidence_day_count": max(len(evidence_dates), int(quality["evidence_day_count"])),
                "evidence_source": quality["evidence_source"],
                "observation_count": int(quality["evidence_observation_count"]),
                # first/last_seen follow the evidence (message dates) when known;
                # the extraction timestamp is kept separately.
                "first_seen": quality["first_seen_date"] or timestamp,
                "last_seen": quality["last_seen_date"] or extracted_at,
                "first_seen_date": quality["first_seen_date"],
                "last_seen_date": quality["last_seen_date"],
                "extracted_at": extracted_at,
                "strength": _clamp_score(relation_payload.get("strength"), 0.0)
                if relation_payload.get("strength") is not None
                else None,
                "signals": (
                    dict(relation_payload.get("signals"))
                    if isinstance(relation_payload.get("signals"), dict)
                    else None
                ),
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

        edges = _merge_symmetric_group_edges(edges)
        connected_node_ids = {
            node_id for edge in edges for node_id in (edge["source"], edge["target"])
        }

        if session_id is not None or node_type:
            nodes = {
                node_id: node for node_id, node in nodes.items() if node_id in connected_node_ids
            }

        if (
            str(channel or "").strip().lower() == "wechat"
            and session_id
            and _is_group_session_id(session_id)
        ):
            await self._apply_group_graph_person_display_names(
                tenant_id=tenant_id,
                session_id=session_id,
                session_ids=scoped_session_ids or [str(session_id)],
                nodes=nodes,
            )

        node_items = list(nodes.values())[:safe_limit]
        visible_edges = edges[:safe_limit]
        # The COUNT runs with the same SQL filters, so anything above what is
        # shown was cut by the row cap rather than by a filter.
        total_edges = max(total_matching_facts, len(visible_edges))
        truncated = len(facts) >= fetch_limit or total_matching_facts > len(visible_edges)
        return {
            "schema": {
                "version": GROUP_GRAPH_SCHEMA_VERSION,
                "node_types": list(GROUP_GRAPH_NODE_TYPES),
                "edge_types": list(GROUP_GRAPH_EDGE_TYPES),
            },
            "scope": scope,
            "filters": filters,
            "nodes": node_items,
            "edges": visible_edges,
            "counts": {
                "nodes": len(node_items),
                "edges": len(visible_edges),
            },
            "page": {
                "limit": safe_limit,
                "total": total_edges,
                "truncated": bool(truncated),
                "order": "strength_desc",
                "next_cursor": None,
            },
            "generated_from": generated_from,
        }

    def _group_graph_person_usernames(self, nodes: dict[str, dict[str, Any]]) -> dict[str, str]:
        usernames: dict[str, str] = {}
        for node in nodes.values():
            if str(node.get("type") or "") != "person":
                continue
            for candidate in (
                node.get("technical_label"),
                node.get("label"),
                *(node.get("aliases") or []),
            ):
                username = _normalize_line(_sanitize_db_text(candidate))
                if username.startswith("user:"):
                    username = username[5:]
                if username and _looks_like_wechat_username(username):
                    usernames.setdefault(username, str(node.get("id") or ""))
        return usernames

    def _apply_group_graph_person_metadata(
        self,
        node: dict[str, Any],
        metadata: dict[str, Any],
        *,
        overwrite_technical: bool = False,
    ) -> None:
        display = _wechat_contact_display_label(metadata)
        if not display or _group_graph_label_is_technical(display):
            return
        current = _normalize_line(_sanitize_db_text(node.get("display_label")))
        if current and not _group_graph_label_is_technical(current) and not overwrite_technical:
            node["aliases"] = _merge_group_graph_aliases(
                node.get("aliases") or [],
                (display, metadata.get("remark"), metadata.get("nick_name"), metadata.get("alias")),
            )
            return
        node["display_label"] = display
        node["aliases"] = _merge_group_graph_aliases(
            node.get("aliases") or [],
            (display, metadata.get("remark"), metadata.get("nick_name"), metadata.get("alias")),
        )

    async def _load_group_local_person_display_map(
        self,
        *,
        tenant_id: str,
        session_ids: list[str],
        usernames: Iterable[str],
    ) -> dict[str, dict[str, str]]:
        unique_usernames = sorted(
            {
                _normalize_line(_sanitize_db_text(username))
                for username in usernames
                if _looks_like_wechat_username(username)
            }
        )[:500]
        rooms = [
            _normalize_line(_sanitize_db_text(session_id))
            for session_id in session_ids
            if _is_group_session_id(session_id)
        ]
        if not unique_usernames or not rooms:
            return {}
        display_map: dict[str, dict[str, str]] = {}
        try:
            member_rows = await _exec(
                "SELECT user_wxid, user_name "
                "FROM plugin_wxbot_group_membership "
                "WHERE tenant_id = :tid AND session_id = ANY(:sids) "
                "AND user_wxid = ANY(:wxids) "
                "AND user_name <> '' AND user_name IS DISTINCT FROM user_wxid",
                {
                    "tid": str(tenant_id or "").strip(),
                    "sids": rooms,
                    "wxids": unique_usernames,
                },
            )
        except Exception:
            member_rows = []
        for row in member_rows or []:
            username = _normalize_line(_sanitize_db_text(row.get("user_wxid")))
            name = _normalize_line(_sanitize_db_text(row.get("user_name")))[:80]
            if username and name and not _group_graph_label_is_technical(name):
                display_map[username] = {"nick_name": name}
        try:
            observation_rows = await _exec(
                "SELECT DISTINCT ON (sender_wxid) sender_wxid, sender_name "
                "FROM plugin_wxbot_group_observations "
                "WHERE tenant_id = :tid AND session_id = ANY(:sids) "
                "AND sender_wxid = ANY(:wxids) "
                "AND sender_name <> '' AND sender_name IS DISTINCT FROM sender_wxid "
                "ORDER BY sender_wxid, occurred_ts DESC, id DESC",
                {
                    "tid": str(tenant_id or "").strip(),
                    "sids": rooms,
                    "wxids": unique_usernames,
                },
            )
        except Exception:
            observation_rows = []
        for row in observation_rows or []:
            username = _normalize_line(_sanitize_db_text(row.get("sender_wxid")))
            name = _normalize_line(_sanitize_db_text(row.get("sender_name")))[:80]
            if username and name and not _group_graph_label_is_technical(name):
                display_map[username] = {"nick_name": name}
        return display_map

    async def _apply_group_graph_person_display_names(
        self,
        *,
        tenant_id: str,
        session_id: str,
        session_ids: list[str],
        nodes: dict[str, dict[str, Any]],
    ) -> None:
        username_candidates = self._group_graph_person_usernames(nodes)
        if not username_candidates:
            return
        contact_map: dict[str, dict[str, str]] = {}
        rooms = [str(session_id), *[item for item in session_ids if item != session_id]]
        for room in rooms:
            if not _is_group_session_id(room):
                continue
            contact_kwargs: dict[str, Any] = {
                "session_id": room,
                "usernames": username_candidates.keys(),
            }
            if bool(getattr(self, "runtime_scope_gates_required", False)):
                contact_kwargs["tenant_id"] = str(tenant_id or "")
            try:
                mapped = await self._load_wechat_group_contact_display_map(**contact_kwargs)
            except Exception:
                mapped = {}
            for username, metadata in (mapped or {}).items():
                current = contact_map.get(username) or {}
                contact_map[username] = {
                    "remark": current.get("remark") or metadata.get("remark") or "",
                    "nick_name": current.get("nick_name") or metadata.get("nick_name") or "",
                    "alias": current.get("alias") or metadata.get("alias") or "",
                }
        local_map = await self._load_group_local_person_display_map(
            tenant_id=tenant_id,
            session_ids=rooms,
            usernames=username_candidates.keys(),
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
                if username.startswith("user:"):
                    username = username[5:]
                if username in contact_map or username in local_map:
                    matched_username = username
                    break
            if not matched_username:
                continue
            if matched_username in contact_map:
                self._apply_group_graph_person_metadata(
                    node,
                    contact_map[matched_username],
                    overwrite_technical=True,
                )
            if matched_username in local_map:
                self._apply_group_graph_person_metadata(
                    node,
                    local_map[matched_username],
                    overwrite_technical=True,
                )

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
            scoped_session_ids = set(
                await self._resolve_group_graph_session_ids(
                    tenant_id=tenant_id,
                    session_id=session_id,
                )
            )
            if not backing_item or str(backing_item.get("session_id") or "") not in scoped_session_ids:
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
        quality = _group_graph_edge_quality(backing_item)
        observation_ids = _group_graph_item_observation_ids(backing_item)
        observations = (
            await self._get_group_observation_evidence_by_ids(observation_ids)
            if observation_ids
            else []
        )
        payload["evidence_ids"]["observation_ids"] = observation_ids[:200]
        payload["evidence_counts"]["observations"] = len(observation_ids)
        payload["evidence_counts"]["evidence_days"] = quality["evidence_day_count"]
        payload["observations"] = observations
        payload["evidence_source"] = quality["evidence_source"]
        payload["evidence_dates"] = quality["evidence_dates"]
        edge_payload = payload.get("edge")
        if isinstance(edge_payload, dict):
            edge_payload["extracted_at"] = edge_payload.get("first_seen")
            if quality["first_seen_date"]:
                edge_payload["first_seen"] = quality["first_seen_date"]
            if quality["last_seen_date"]:
                edge_payload["last_seen"] = quality["last_seen_date"]
            observed = [
                str(entry.get("occurred_at") or "")
                for entry in observations
                if entry.get("occurred_at")
            ]
            if observed:
                edge_payload["first_observed_at"] = min(observed)
                edge_payload["last_observed_at"] = max(observed)
        if include_raw:
            raw_items = await self._get_memory_items_by_ids(memory_item_ids)
            payload["raw"] = {
                "fact": fact,
                "memory_items": raw_items,
                "events": events,
                "episodes": evidence_episodes,
            }
        return payload

    async def _get_group_observation_evidence_by_ids(
        self,
        observation_ids: Iterable[Any],
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return display-safe metadata for group observations used as evidence.

        Only ids, the session, the sender's group nickname and the timestamp are
        exposed; message content never leaves this method.
        """

        ids = sorted(_coerce_int_set(observation_ids))[: max(1, min(int(limit or 200), 500))]
        if not ids:
            return []
        try:
            rows = await _exec(
                "SELECT id, session_id, sender_name, occurred_ts "
                "FROM plugin_wxbot_group_observations "
                "WHERE id = ANY(:ids) "
                "ORDER BY occurred_ts ASC, id ASC",
                {"ids": ids},
            )
        except Exception:
            logger.warning("memory.group_graph_observation_evidence_failed", exc_info=True)
            return []
        evidence: list[dict[str, Any]] = []
        for row in rows or []:
            occurred = _safe_int(row.get("occurred_ts"), 0)
            sender_name = _normalize_line(_sanitize_db_text(row.get("sender_name")))[:80]
            label = (
                sender_name
                if sender_name and not _group_graph_label_is_technical(sender_name)
                else ""
            )
            evidence.append(
                {
                    "id": _safe_int(row.get("id"), 0),
                    "session_id": str(row.get("session_id") or ""),
                    "sender_label": label or None,
                    "sender_is_technical": not label,
                    "occurred_at": (
                        datetime.fromtimestamp(occurred, UTC).replace(tzinfo=None).isoformat()
                        if occurred > 0
                        else None
                    ),
                    "source": EVIDENCE_SOURCE_OBSERVATION,
                }
            )
        return evidence

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
            observation_row_ids = {
                int(row.get("id") or 0)
                for row in rows
                if str(row.get("_graph_source") or "") == EVIDENCE_SOURCE_OBSERVATION
                and int(row.get("id") or 0)
            }
            windows.append(
                {
                    "index": index,
                    "rows": rows,
                    "event_ids": event_ids,
                    "first_event_id": event_ids[0] if event_ids else None,
                    "last_event_id": event_ids[-1] if event_ids else None,
                    "sender_ids": sender_ids,
                    "transcript": "\n".join(transcript_lines),
                    "source": (
                        EVIDENCE_SOURCE_OBSERVATION
                        if observation_row_ids and len(observation_row_ids) == len(event_ids)
                        else EVIDENCE_SOURCE_MIXED
                        if observation_row_ids
                        else EVIDENCE_SOURCE_MEMORY_EVENT
                    ),
                    "observation_ids": observation_row_ids,
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
        directory: GroupMemberDirectory | None = None,
    ) -> list[str]:
        """Resolve text ``@`` mentions to participant ids.

        WeChat renders mentions as ``@昵称`` (followed by U+2005), so the text
        is matched against the member directory's nicknames as well as against
        raw participant ids.
        """

        if not body:
            return []
        participant_set = {self._normalize_group_participant_id(item) for item in participants}
        participant_set.discard("")
        if not participant_set and directory is None:
            return []
        targets: list[str] = []

        def add_target(value: str | None) -> None:
            normalized = self._normalize_group_participant_id(value)
            if normalized and normalized not in targets:
                targets.append(normalized)

        for mention in _MENTION_TOKEN_RE.findall(body):
            normalized = self._normalize_group_participant_id(mention)
            if normalized in participant_set:
                add_target(normalized)
                continue
            if directory is not None:
                add_target(directory.resolve_name(mention))

        stripped = body.strip()
        for participant in sorted(participant_set, key=len, reverse=True):
            if participant in targets:
                continue
            escaped = re.escape(participant)
            if re.search(rf"(^|[\s@]){escaped}([:：,，\s]|$)", stripped):
                add_target(participant)
                continue
            if re.search(rf"(回复|回|问|告诉|建议)\s*@?{escaped}", stripped):
                add_target(participant)

        return targets[:5]

    def _build_deterministic_group_window_candidates(
        self,
        window: dict[str, Any],
        *,
        directory: GroupMemberDirectory | None = None,
        co_participation_edges: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Rule-based relation candidates for one window.

        Signals, strongest first:

        * quoted reply (``metadata_json.quote``) → ``replied_to``
        * ``@`` mention (``metadata_json.at_wxids`` or ``@昵称`` text) → ``addressed``
        * explicit "回复/回/接着/关于" prefix right after another member → ``replied_to``
        * same-window co-participation → ``co_participated`` (opt-in only; in a
          busy group it means little more than "both were online")

        Every candidate carries ``signals`` counts so acceptance and strength can
        be computed from what was actually observed rather than a flat confidence.
        """

        rows = window.get("rows") if isinstance(window.get("rows"), list) else []
        if co_participation_edges is None:
            co_participation_edges = bool(
                getattr(
                    getattr(self, "settings", None),
                    "memory_group_graph_co_participation_edges",
                    False,
                )
            )
        local_directory = directory or GroupMemberDirectory()
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
            observation = (
                row.get("_observation") if isinstance(row.get("_observation"), dict) else {}
            )
            local_directory.add_sender(sender_id, observation.get("sender_name"))
            if sender_id not in participants:
                participants.append(sender_id)
            events.append(
                {
                    "id": event_id,
                    "sender": sender_id,
                    "body": str(body or ""),
                    "observation": observation,
                }
            )

        candidates_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
        unresolved_targets = 0

        def add_candidate(
            *,
            subject: str,
            predicate: str,
            object_value: str,
            confidence: float,
            evidence_event_ids: Iterable[Any],
            reason: str,
            signal: str,
            signal_event_id: int,
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
                existing["signal_evidence"] = _merge_signal_evidence(
                    existing.get("signal_evidence"), {signal: [signal_event_id]}
                )
                existing["signals"] = _signals_from_evidence(existing["signal_evidence"])
                return
            signal_evidence = _merge_signal_evidence({signal: [signal_event_id]})
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
                "signals": _signals_from_evidence(signal_evidence),
                "signal_evidence": signal_evidence,
            }

        for index, event in enumerate(events):
            sender = str(event["sender"])
            event_id = int(event["id"])
            observation = event.get("observation") or {}
            bot_wxid = self._normalize_group_participant_id(observation.get("bot_wxid"))
            body = str(event.get("body") or "").strip()

            # 1. Quoted reply: the platform tells us exactly whose message this answers.
            quote_from = str(observation.get("quote_from") or "")
            quote_name = str(observation.get("quote_sender_name") or "")
            if (quote_from or quote_name) and self._normalize_group_participant_id(
                quote_from
            ) != bot_wxid:
                target = local_directory.resolve_id(quote_from, fallback_name=quote_name)
                if target and target != bot_wxid:
                    add_candidate(
                        subject=sender,
                        predicate="replied_to",
                        object_value=target,
                        confidence=QUOTE_REPLY_CONFIDENCE,
                        evidence_event_ids=[event_id],
                        reason="deterministic_quote_reply",
                        signal=SIGNAL_QUOTE,
                        signal_event_id=event_id,
                    )
                elif not target:
                    unresolved_targets += 1

            # 2. @ mentions: structured ids first, then nicknames in the text.
            mention_targets: list[str] = []
            for raw_id in observation.get("at_wxids") or []:
                if self._normalize_group_participant_id(raw_id) == bot_wxid:
                    continue
                target = local_directory.resolve_id(raw_id)
                if target and target != bot_wxid:
                    if target not in mention_targets:
                        mention_targets.append(target)
                elif not target:
                    unresolved_targets += 1
            metadata_mentions = set(mention_targets)
            for target in self._extract_addressed_participant_ids(
                body,
                participants=participants,
                directory=local_directory,
            ):
                if target != bot_wxid and target not in mention_targets:
                    mention_targets.append(target)
            for target in mention_targets[:5]:
                add_candidate(
                    subject=sender,
                    predicate="addressed",
                    object_value=target,
                    confidence=(
                        MENTION_METADATA_CONFIDENCE
                        if target in metadata_mentions
                        else MENTION_TEXT_CONFIDENCE
                    ),
                    evidence_event_ids=[event_id],
                    reason=(
                        "deterministic_at_mention_metadata"
                        if target in metadata_mentions
                        else "deterministic_addressed_participant"
                    ),
                    signal=SIGNAL_MENTION,
                    signal_event_id=event_id,
                )

            # 3. Explicit reply prefix right after somebody else spoke.
            previous = events[index - 1] if index > 0 else None
            if (
                previous
                and previous.get("sender") != sender
                and re.search(r"^(?:回复|回|接着|关于)(?:\s|[:：])", body)
            ):
                add_candidate(
                    subject=sender,
                    predicate="replied_to",
                    object_value=str(previous.get("sender") or ""),
                    confidence=PREFIX_REPLY_CONFIDENCE,
                    evidence_event_ids=[previous.get("id"), event_id],
                    reason="deterministic_adjacent_reply_window",
                    signal=SIGNAL_PREFIX_REPLY,
                    signal_event_id=event_id,
                )

        window["unresolved_targets"] = unresolved_targets
        if not co_participation_edges:
            return list(candidates_by_key.values())

        # 4. Legacy co-participation pairs, only when explicitly enabled.
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
                        confidence=CO_PARTICIPATION_CONFIDENCE,
                        evidence_event_ids=evidence_ids,
                        reason="deterministic_same_window_participation",
                        signal=SIGNAL_CO_PARTICIPATION,
                        # One co-participation observation per window.
                        signal_event_id=int(evidence_ids[0]),
                    )
                    if len(candidates_by_key) >= GROUP_WINDOW_DETERMINISTIC_MAX_PAIRS:
                        return list(candidates_by_key.values())

        return list(candidates_by_key.values())

    async def _load_group_member_directory(
        self,
        *,
        tenant_id: str,
        session_ids: Iterable[str],
        event_rows: Iterable[dict[str, Any]],
    ) -> GroupMemberDirectory:
        """Build the id/nickname directory for one group from the day's rows plus
        the membership table, so quote/@ targets resolve to sender ids."""

        directory = GroupMemberDirectory()
        for row in event_rows:
            sender_id, _body = _split_group_event_text(row.get("user_text"))
            observation = (
                row.get("_observation") if isinstance(row.get("_observation"), dict) else {}
            )
            directory.add_sender(sender_id or row.get("user_id"), observation.get("sender_name"))
        rooms = [
            _normalize_line(_sanitize_db_text(session_id))
            for session_id in session_ids
            if _is_group_session_id(session_id)
        ]
        if not rooms:
            return directory
        try:
            member_rows = await _exec(
                "SELECT user_wxid, user_name "
                "FROM plugin_wxbot_group_membership "
                "WHERE tenant_id = :tid AND session_id = ANY(:sids) "
                "AND user_wxid <> ''",
                {"tid": str(tenant_id or "").strip(), "sids": rooms},
            )
        except Exception:
            logger.warning("memory.group_graph_member_directory_failed", exc_info=True)
            member_rows = []
        for row in member_rows or []:
            directory.add_member(row.get("user_wxid"), row.get("user_name"))
        return directory

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
        allowed_predicates = [
            predicate
            for predicate in GROUP_GRAPH_EDGE_TYPES
            if predicate not in LLM_EXCLUDED_PREDICATES
        ]
        system = (
            "Extract conservative group-chat relationship candidates from a bounded transcript. "
            "Return JSON only with key relations. Each relation must include subject, subject_type, "
            "predicate, object, object_type, confidence, evidence_event_ids, and optional reason. "
            "Allowed predicates: "
            + ", ".join(allowed_predicates)
            + ". Evidence ids must come from the provided event ids. Do not quote raw messages. "
            "Rules: (1) subject is always a participant id exactly as written in the transcript; "
            "a person object must also be a participant id. "
            "(2) Only report a relation when at least two different messages support it and list "
            "each supporting message id in evidence_event_ids; skip one-off remarks. "
            "(3) Objects that are not people are short canonical terms, not sentences: "
            "products, models, services and software are object_type tool "
            "(e.g. Claude, DeepSeek, 阿里云); named efforts are project; everything else is topic. "
            "Write terms in the language the group uses (Chinese chat -> Chinese term), keep the "
            "common spelling of product names, at most 12 Chinese characters or 4 English words, "
            "and reuse one spelling for the same term. "
            "(4) Do not emit 'the group', '大家' or the chat itself as an object, and do not report "
            "who merely talked in the same time span; direct replies and @-mentions are already "
            "tracked, so prefer asked/answered/requested/provided_resource/collaborated_with between "
            "people and interested_in/reported_issue/works_on/maintains/tested/fixed_issue/asked "
            "between a person and a term. Return an empty list rather than guessing."
        )
        payload = {
            "date": target_date,
            "session_id": session_id,
            "event_ids": event_ids,
            "transcript": transcript,
        }
        # Structured extraction over a short transcript is a fast-model task: on
        # the production gateway the reasoning tier needs >60s per window while
        # the non-reasoning tier answers in 10-16s with valid JSON.
        model_tier = str(
            getattr(
                getattr(self, "settings", None),
                "memory_group_graph_llm_model_tier",
                GROUP_WINDOW_LLM_MODEL_TIER_DEFAULT,
            )
            or GROUP_WINDOW_LLM_MODEL_TIER_DEFAULT
        ).strip()
        request = ChatRequest(
            tenant_id=tenant_id,
            trace_id=trace_id,
            model_tier=model_tier,
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
        participants: Iterable[str] | None = None,
        directory: GroupMemberDirectory | None = None,
    ) -> dict[str, Any] | None:
        """Validate one LLM-proposed relation.

        Besides predicate/evidence checks, ``person`` endpoints must resolve to a
        real participant (window sender or directory member). A subject that does
        not resolve is dropped; an unresolvable ``person`` object is kept as a
        ``topic`` for mention-like predicates and dropped otherwise, so the model
        cannot mint phantom members out of terms it misclassified.
        """

        if not isinstance(raw_candidate, dict):
            return None
        predicate = str(raw_candidate.get("predicate") or "").strip().lower()
        if predicate not in GROUP_GRAPH_EDGE_TYPES or predicate in LLM_EXCLUDED_PREDICATES:
            return None
        subject = _normalize_line(str(raw_candidate.get("subject") or ""))[:200]
        object_value = _normalize_line(str(raw_candidate.get("object") or ""))[:200]
        if not subject or not object_value:
            return None
        evidence_ids = sorted(_coerce_int_set(raw_candidate.get("evidence_event_ids") or []))
        evidence_ids = [event_id for event_id in evidence_ids if event_id in allowed_event_ids]
        if len(evidence_ids) < GROUP_RELATION_MIN_LLM_WINDOW_EVIDENCE:
            return None
        subject_type = str(raw_candidate.get("subject_type") or "person").strip().lower()
        object_type = str(raw_candidate.get("object_type") or "person").strip().lower()
        if subject_type not in GROUP_GRAPH_NODE_TYPES:
            subject_type = "person"
        if object_type not in GROUP_GRAPH_NODE_TYPES:
            object_type = "person"
        if subject_type == "group" or object_type == "group":
            return None
        if object_value.strip().lower() in LLM_GROUP_PSEUDO_OBJECTS:
            return None

        participant_set = {
            self._normalize_group_participant_id(item) for item in (participants or [])
        }
        participant_set.discard("")
        can_resolve = bool(participant_set) or directory is not None

        def resolve_person(value: str) -> str | None:
            normalized = self._normalize_group_participant_id(value)
            if normalized in participant_set:
                return normalized
            if directory is not None:
                return directory.resolve_id(normalized) or directory.resolve_name(normalized)
            return None

        if can_resolve and subject_type == "person":
            resolved_subject = resolve_person(subject)
            if not resolved_subject:
                return None
            subject = resolved_subject
        if can_resolve and object_type == "person":
            resolved_object = resolve_person(object_value)
            if resolved_object:
                object_value = resolved_object
            elif predicate in LLM_PERSON_TO_TOPIC_PREDICATES and not _looks_like_participant_id(
                object_value
            ):
                object_type = "topic"
            else:
                # An unknown id-shaped "person" is a hallucinated member, not a term.
                return None
        if subject_type == object_type == "person" and subject == object_value:
            return None
        if object_type != "person" and not _is_usable_llm_term(object_value):
            return None
        signal_evidence = _merge_signal_evidence({SIGNAL_LLM: evidence_ids})
        return {
            "subject": subject,
            "subject_type": subject_type,
            "predicate": predicate,
            "object": object_value,
            "object_type": object_type,
            "confidence": _clamp_score(raw_candidate.get("confidence"), 0.5),
            "evidence_event_ids": evidence_ids,
            "reason": _normalize_line(str(raw_candidate.get("reason") or ""))[:240],
            "signals": _signals_from_evidence(signal_evidence),
            "signal_evidence": signal_evidence,
        }

    def _parse_group_window_candidates(
        self,
        raw_payload: Any,
        *,
        allowed_event_ids: set[int],
        participants: Iterable[str] | None = None,
        directory: GroupMemberDirectory | None = None,
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
                raw_candidate,
                allowed_event_ids=allowed_event_ids,
                participants=participants,
                directory=directory,
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
                existing["signal_evidence"] = _merge_signal_evidence(
                    existing.get("signal_evidence"), candidate.get("signal_evidence")
                )
                existing["signals"] = _signals_from_evidence(existing["signal_evidence"])
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
        evidence_observation_ids = _merge_int_lists(
            candidate.get("evidence_observation_ids"), max_items=200
        )
        relation_payload = dict(candidate)
        relation_payload["evidence_event_ids"] = evidence_event_ids
        relation_payload["evidence_observation_ids"] = evidence_observation_ids
        evidence_dates = [target_date]
        existing_item: dict[str, Any] | None = None
        existing_acceptance: dict[str, Any] = {}
        existing_relation: dict[str, Any] = {}

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
            existing_item = existing_items[0]
            existing_value = existing_item.get("value")
            if not isinstance(existing_value, dict):
                existing_value = _safe_json_loads(existing_item.get("value_json"), {})
            if not isinstance(existing_value, dict):
                existing_value = {}
            existing_acceptance = (
                dict(existing_value.get("acceptance"))
                if isinstance(existing_value.get("acceptance"), dict)
                else {}
            )
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
            evidence_observation_ids = _merge_int_lists(
                existing_relation.get("evidence_observation_ids"),
                existing_value.get("source_observation_ids"),
                candidate.get("evidence_observation_ids"),
                max_items=200,
            )
            relation_payload = {**existing_relation, **candidate}
            relation_payload["confidence"] = max(
                _clamp_score(existing_relation.get("confidence"), 0.0),
                _clamp_score(candidate.get("confidence"), 0.0),
            )
            relation_payload["evidence_event_ids"] = evidence_event_ids
            relation_payload["evidence_observation_ids"] = evidence_observation_ids
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
            (existing_item or {}).get("source_type")
            or relation_payload.get("extraction_method")
            or LLM_GROUP_WINDOW_SOURCE_TYPE
        )
        if relation_source_type not in {
            LLM_GROUP_WINDOW_SOURCE_TYPE,
            DETERMINISTIC_GROUP_WINDOW_SOURCE_TYPE,
        }:
            relation_source_type = LLM_GROUP_WINDOW_SOURCE_TYPE

        # Accumulate what was actually observed. Legacy items carry no signal
        # counts, so infer one from their reason/source before merging.
        candidate_signal_evidence = candidate.get("signal_evidence")
        if not isinstance(candidate_signal_evidence, dict):
            # Callers that only pass counts get one occurrence per flagged signal,
            # anchored on the first evidence id so re-runs stay idempotent.
            anchor_ids = sorted(
                _coerce_int_set(candidate.get("evidence_event_ids") or [])
                | _coerce_int_set(candidate.get("evidence_observation_ids") or [])
            )
            candidate_signal_evidence = {
                key: anchor_ids[:1]
                for key, count in (candidate.get("signals") or {}).items()
                if _safe_int(count, 0) > 0 and anchor_ids
            }
        signal_evidence = _merge_signal_evidence(
            existing_relation.get("signal_evidence"),
            candidate_signal_evidence,
        )
        signals = _signals_from_evidence(signal_evidence)
        # Items written before signal evidence existed contribute one known
        # occurrence per inferred signal; never less than what the ids show.
        if existing_item is not None and not isinstance(
            existing_relation.get("signal_evidence"), dict
        ):
            legacy = (
                existing_relation.get("signals")
                if isinstance(existing_relation.get("signals"), dict)
                else _infer_legacy_group_signals(existing_relation, relation_source_type)
            )
            for key in GROUP_SIGNAL_KEYS:
                signals[key] = max(signals[key], max(0, _safe_int(legacy.get(key), 0)))
        if not any(signals.values()):
            signals = _merge_group_signals(
                signals, _infer_legacy_group_signals(candidate, relation_source_type)
            )
        day_count = len(evidence_dates)
        strength = _group_relation_strength(signals, day_count)
        relation_payload["signals"] = signals
        relation_payload["signal_evidence"] = signal_evidence
        relation_payload["strength"] = strength

        # Human accept/reject decisions stick. Everything else follows the
        # deterministic policy, which may promote a needs_review relation to
        # accepted once it has repeated across days.
        prior_acceptance_status = str(
            existing_acceptance.get("status")
            or (existing_item or {}).get("acceptance_status")
            or ""
        ).strip().lower()
        auto_accept = bool(
            getattr(getattr(self, "settings", None), "memory_group_graph_auto_accept", True)
        )
        policy_status, policy_reason = _group_relation_acceptance_decision(
            signals,
            day_count=day_count,
            auto_accept=auto_accept,
        )
        if prior_acceptance_status in {"accepted", "rejected"}:
            acceptance_status = prior_acceptance_status
            acceptance_reason = str(
                existing_acceptance.get("reason") or relation_payload.get("reason") or policy_reason
            )[:80]
        else:
            acceptance_status = policy_status
            acceptance_reason = policy_reason
        acceptance_payload = {
            **existing_acceptance,
            "status": acceptance_status,
            "score": max(
                _clamp_score(existing_acceptance.get("score"), 0.0),
                _clamp_score(relation_payload.get("confidence"), 0.0),
            ),
            "reason": acceptance_reason,
            "extraction_confidence": max(
                _clamp_score(existing_acceptance.get("extraction_confidence"), 0.0),
                _clamp_score(relation_payload.get("confidence"), 0.0),
            ),
            "strength": strength,
            "signals": signals,
            "day_count": day_count,
            "policy": policy_reason,
        }
        if acceptance_status == "accepted" and prior_acceptance_status != "accepted":
            acceptance_payload.setdefault("reviewed_by", "system/auto")
            acceptance_payload.setdefault("review_reason", policy_reason)
        evidence_source = _group_graph_evidence_source(
            evidence_event_ids, evidence_observation_ids
        )
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
                "source": str(window.get("source") or EVIDENCE_SOURCE_MEMORY_EVENT),
            },
            "relation": relation_payload,
            "source_event_ids": evidence_event_ids,
            "source_observation_ids": evidence_observation_ids,
            "evidence_source": evidence_source,
            "acceptance": acceptance_payload,
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
            status=(
                str(existing_item.get("status") or "pending")
                if existing_item is not None
                and acceptance_status == prior_acceptance_status
                and acceptance_status in {"accepted", "rejected"}
                else _memory_status_for_acceptance(acceptance_status, sensitivity="normal")
            ),
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
        include_llm: bool = True,
        llm_timeout_seconds: int | None = None,
        source: str | None = None,
        llm_mode: str | None = None,
        deterministic: bool = True,
    ) -> dict[str, Any]:
        """Extract relations for up to ``max_windows`` windows of one day.

        ``llm_mode``: ``inline`` calls the LLM per window with a full
        ``llm_timeout_seconds``; ``enqueue`` writes one durable job per window
        instead, so the cheap deterministic pass never waits on the model and
        failed LLM windows are retried by ``run_group_window_llm_jobs``.
        ``deterministic=False`` skips the rule layer (used by that retry path).
        """

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
        effective_llm_timeout = _clamp_int(
            llm_timeout_seconds,
            self._group_window_llm_timeout_default(),
            minimum=1,
            maximum=GROUP_WINDOW_LLM_TIMEOUT_MAX,
        )
        effective_llm_mode = _normalize_llm_mode(llm_mode)
        effective_source = _normalize_evidence_source(source)
        user_id_scope, user_id_auto = _group_history_user_scope(session_id, user_id)
        if not user_id_scope:
            raise RuntimeError("user_id required")

        fetch_limit = effective_window_size * effective_max_windows + 1
        event_rows = await self._load_group_relationship_events(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            session_id=session_id,
            user_id_scope=user_id_scope,
            start_at=start_at,
            end_at=end_at,
            columns=(
                "id, tenant_id, channel, source_key, user_id, session_id, user_text, "
                "assistant_text, trace_id, event_key, created_at"
            ),
            cursor_event_id=effective_cursor,
            limit=fetch_limit,
            source=effective_source,
        )
        more_remain = len(event_rows) > effective_window_size * effective_max_windows
        event_rows = event_rows[: effective_window_size * effective_max_windows]
        observation_event_ids = {
            int(row.get("id") or 0)
            for row in event_rows
            if str(row.get("_graph_source") or "") == EVIDENCE_SOURCE_OBSERVATION
            and int(row.get("id") or 0)
        }
        resolved_source = (
            EVIDENCE_SOURCE_OBSERVATION
            if observation_event_ids and len(observation_event_ids) == len(event_rows)
            else EVIDENCE_SOURCE_MIXED
            if observation_event_ids
            else EVIDENCE_SOURCE_MEMORY_EVENT
        )
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
                "signal_counts": _empty_group_signals(),
                "unresolved_targets": 0,
            }
            for window in windows
        ]
        signal_totals = _empty_group_signals()
        unresolved_total = 0
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
                "include_llm": bool(include_llm),
                "llm_timeout_seconds": effective_llm_timeout,
                "llm_mode": effective_llm_mode,
                "deterministic": bool(deterministic),
                "source": effective_source,
            },
            "source": resolved_source,
            "llm_jobs_enqueued": 0,
            "llm_failures": 0,
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
            "generated_from": (
                ["plugin_wxbot_group_observations"]
                if observation_event_ids
                else ["plugin_memory_event"]
            ),
        }
        if dry_run or not windows:
            if not windows:
                base_payload["status"] = "skipped"
            return base_payload

        llm_available = bool(
            include_llm
            and self.graph_extractor.config.enabled
            and self.graph_extractor.llm_service is not None
        )
        llm_inline = llm_available and effective_llm_mode == LLM_MODE_INLINE
        llm_enqueue = llm_available and effective_llm_mode == LLM_MODE_ENQUEUE

        total_candidates = 0
        total_applied = 0
        total_skipped = 0
        llm_jobs_enqueued = 0
        llm_jobs_seen = 0
        llm_failures = 0
        generated_from = [
            (
                "plugin_wxbot_group_observations"
                if resolved_source == EVIDENCE_SOURCE_OBSERVATION
                else "plugin_memory_event"
            ),
        ]
        if resolved_source == EVIDENCE_SOURCE_MIXED:
            generated_from.append("plugin_wxbot_group_observations")
        if deterministic:
            generated_from.append("deterministic_window_participants")
        if llm_inline:
            generated_from.append("llm_window_extractor")
        if llm_enqueue:
            generated_from.append("llm_window_jobs")
        directory = (
            await self._load_group_member_directory(
                tenant_id=tenant_id,
                session_ids=await self._resolve_group_graph_session_ids(
                    tenant_id=tenant_id,
                    session_id=session_id,
                )
                or [session_id],
                event_rows=event_rows,
            )
            if (deterministic or llm_inline)
            else None
        )
        for window, summary in zip(windows, window_summaries, strict=True):
            deterministic_candidates = (
                self._build_deterministic_group_window_candidates(
                    window,
                    directory=directory,
                )
                if deterministic
                else []
            )
            summary["unresolved_targets"] = int(window.get("unresolved_targets") or 0)
            unresolved_total += summary["unresolved_targets"]
            llm_candidates: list[dict[str, Any]] = []
            if llm_enqueue:
                enqueued = await self._enqueue_group_window_llm_job(
                    tenant_id=tenant_id,
                    channel=channel,
                    source_key=source_key,
                    session_id=session_id,
                    target_date=target_date,
                    source=str(window.get("source") or resolved_source),
                    window=window,
                    window_size=effective_window_size,
                )
                if enqueued:
                    llm_jobs_enqueued += 1
                llm_jobs_seen += 1
                summary["llm_job"] = "enqueued" if enqueued else "exists"
            if llm_inline:
                try:
                    raw_payload = await asyncio.wait_for(
                        self._extract_group_relationship_window_candidates(
                            tenant_id=tenant_id,
                            trace_id=f"group-window:{target_date}:{window['first_event_id']}:{window['last_event_id']}",
                            target_date=target_date,
                            session_id=session_id,
                            event_ids=window["event_ids"],
                            transcript=window["transcript"],
                        ),
                        timeout=float(effective_llm_timeout),
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
                    summary["llm_failed"] = True
                    total_skipped += 1
                    llm_failures += 1
                else:
                    llm_candidates, skipped = self._parse_group_window_candidates(
                        raw_payload,
                        allowed_event_ids=set(window["event_ids"]),
                        participants=window.get("sender_ids") or [],
                        directory=directory,
                    )
                    summary["skipped_count"] += skipped
                    summary["llm_candidates"] = len(llm_candidates)
                    total_skipped += skipped
            candidates = self._merge_group_window_candidates(
                deterministic_candidates,
                llm_candidates,
            )
            if not candidates:
                if not summary.get("llm_job"):
                    # Nothing found and nothing deferred: the window is done.
                    summary["skipped_count"] += 1
                    total_skipped += 1
                continue
            summary["candidate_count"] = len(candidates)
            total_candidates += len(candidates)
            # Count what this run actually saw, rule signals and model claims alike.
            for candidate in candidates:
                for key, count in (candidate.get("signals") or {}).items():
                    if key in signal_totals:
                        summary["signal_counts"][key] += int(count or 0)
                        signal_totals[key] += int(count or 0)
            for candidate in candidates:
                if observation_event_ids:
                    # Observation ids are not memory-event ids; keep them as their
                    # own evidence list instead of throwing the trail away.
                    event_evidence, observation_evidence = _split_window_evidence_ids(
                        candidate.get("evidence_event_ids") or [],
                        observation_event_ids,
                    )
                    candidate = {
                        **candidate,
                        "evidence_event_ids": event_evidence,
                        "evidence_observation_ids": _merge_int_lists(
                            candidate.get("evidence_observation_ids"),
                            observation_evidence,
                            max_items=200,
                        ),
                    }
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

        if total_applied == 0 and total_candidates == 0 and llm_jobs_seen == 0:
            base_payload["status"] = "skipped"
            if not llm_available:
                base_payload["skipped_reason"] = "no_deterministic_candidates"
        else:
            base_payload["status"] = "completed" if total_skipped == 0 else "partial"
        base_payload["llm_jobs_enqueued"] = llm_jobs_enqueued
        base_payload["llm_failures"] = llm_failures
        base_payload["generated_from"] = generated_from
        base_payload["totals"] = {
            "events": sum(len(window["event_ids"]) for window in windows),
            "windows": len(windows),
            "candidates": total_candidates,
            "applied": total_applied,
            "skipped": total_skipped,
        }
        base_payload["signal_counts"] = signal_totals
        base_payload["unresolved_targets"] = unresolved_total
        return base_payload

    def _group_window_llm_timeout_default(self) -> int:
        return _clamp_int(
            getattr(
                getattr(self, "settings", None),
                "memory_group_graph_llm_timeout_seconds",
                GROUP_WINDOW_LLM_TIMEOUT_DEFAULT,
            ),
            GROUP_WINDOW_LLM_TIMEOUT_DEFAULT,
            minimum=GROUP_WINDOW_LLM_TIMEOUT_MIN,
            maximum=GROUP_WINDOW_LLM_TIMEOUT_MAX,
        )

    async def _enqueue_group_window_llm_job(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        session_id: str,
        target_date: str,
        source: str,
        window: dict[str, Any],
        window_size: int,
    ) -> bool:
        """Durably record that a window still needs its LLM pass.

        Idempotent per (scope, date, source, window bounds): a window whose job
        already exists (pending, running, succeeded or dead) is not re-queued.
        Returns True when a new job row was created.
        """

        first_event_id = int(window.get("first_event_id") or 0)
        last_event_id = int(window.get("last_event_id") or 0)
        if not first_event_id or not last_event_id:
            return False
        job_key = _group_window_llm_job_key(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            session_id=session_id,
            target_date=target_date,
            source=source,
            first_event_id=first_event_id,
            last_event_id=last_event_id,
        )
        payload = json.dumps(
            {
                "kind": GROUP_WINDOW_LLM_JOB_KIND,
                "version": 1,
                "scope": {
                    "tenant_id": tenant_id,
                    "channel": channel,
                    "source_key": source_key,
                    "session_id": session_id,
                    "date": target_date,
                    "source": source,
                },
                "window": {
                    "index": int(window.get("index") or 0),
                    "first_event_id": first_event_id,
                    "last_event_id": last_event_id,
                    "event_count": len(window.get("event_ids") or []),
                    "window_size": int(window_size),
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        max_attempts = _clamp_int(
            getattr(
                getattr(self, "settings", None),
                "memory_group_graph_llm_job_max_attempts",
                3,
            ),
            3,
            minimum=1,
            maximum=10,
        )
        rows = await _exec(
            "INSERT INTO plugin_memory_extraction_job "
            "(tenant_id, channel, source_key, user_id, session_id, source_event_id, "
            "source_trace_id, status, attempts, max_attempts, next_run_at, result_json, "
            "idempotency_key, created_at, updated_at) "
            "VALUES (:tid, :channel, :source_key, :group_uid, :sid, NULL, :trace, "
            "'pending', 0, :max_attempts, NOW(), :result_json, :job_key, NOW(), NOW()) "
            "ON CONFLICT (idempotency_key) DO NOTHING "
            "RETURNING id",
            {
                "tid": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "group_uid": GROUP_HISTORY_USER_ID_SCOPE,
                "sid": session_id,
                "trace": job_key[:128],
                "max_attempts": max_attempts,
                "result_json": payload,
                "job_key": job_key,
            },
        )
        return bool(rows)

    async def _claim_group_window_llm_jobs(
        self,
        *,
        limit: int,
        lock_ttl_seconds: int,
    ) -> list[dict[str, Any]]:
        rows = await _exec(
            "WITH candidate AS ("
            "  SELECT id FROM plugin_memory_extraction_job "
            "  WHERE source_trace_id LIKE :trace_prefix "
            "    AND (status IN ('pending', 'failed') "
            "         OR (status = 'running' AND locked_until < NOW())) "
            "    AND next_run_at <= NOW() "
            "    AND (locked_until IS NULL OR locked_until < NOW()) "
            "  ORDER BY next_run_at ASC, created_at ASC "
            "  LIMIT :limit "
            "  FOR UPDATE SKIP LOCKED"
            ") "
            "UPDATE plugin_memory_extraction_job job SET "
            "status = 'running', locked_until = NOW() + (:lock_ttl * INTERVAL '1 second'), "
            "locked_by = :locked_by, updated_at = NOW() "
            "FROM candidate WHERE job.id = candidate.id "
            "RETURNING job.id, job.tenant_id, job.channel, job.source_key, job.session_id, "
            "job.attempts, job.max_attempts, job.result_json, job.idempotency_key",
            {
                "trace_prefix": f"{GROUP_WINDOW_LLM_JOB_TRACE_PREFIX}%",
                "limit": max(1, int(limit)),
                "lock_ttl": max(1, int(lock_ttl_seconds)),
                "locked_by": f"group-window-llm:{monotonic():.0f}",
            },
        )
        return list(rows or [])

    async def _finish_group_window_llm_job(
        self,
        job: dict[str, Any],
        *,
        status: str,
        error: str = "",
        result: dict[str, Any] | None = None,
        retry_after_seconds: int = 0,
    ) -> None:
        payload = _safe_json_loads(job.get("result_json"), {})
        if not isinstance(payload, dict):
            payload = {}
        if result is not None:
            payload["last_result"] = result
        await _exec(
            "UPDATE plugin_memory_extraction_job SET "
            "status = :status, attempts = attempts + 1, "
            "next_run_at = NOW() + (:retry_after * INTERVAL '1 second'), "
            "locked_until = NULL, locked_by = '', last_error = :error, "
            "result_json = :result_json, updated_at = NOW() "
            "WHERE id = :id",
            {
                "id": int(job["id"]),
                "status": status,
                "retry_after": max(0, int(retry_after_seconds)),
                "error": _truncate_error(error) if error else "",
                "result_json": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            },
        )

    async def run_group_window_llm_jobs(
        self,
        *,
        limit: int | None = None,
        llm_timeout_seconds: int | None = None,
        time_budget_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Run the LLM pass for windows queued by the deterministic catch-up.

        Each job gets the full ``llm_timeout_seconds``; the run stops when the
        time budget cannot fit another attempt. Failures back off and retry
        until ``max_attempts``, then the job is marked ``dead``.
        """

        settings = getattr(self, "settings", None)
        effective_limit = _clamp_int(
            limit,
            _clamp_int(
                getattr(settings, "memory_group_graph_llm_jobs_per_tick", 6),
                6,
                minimum=0,
                maximum=50,
            ),
            minimum=0,
            maximum=50,
        )
        effective_timeout = _clamp_int(
            llm_timeout_seconds,
            self._group_window_llm_timeout_default(),
            minimum=GROUP_WINDOW_LLM_TIMEOUT_MIN,
            maximum=GROUP_WINDOW_LLM_TIMEOUT_MAX,
        )
        effective_budget = _clamp_int(
            time_budget_seconds,
            effective_timeout * max(1, effective_limit),
            minimum=effective_timeout,
            maximum=3600,
        )
        summary: dict[str, Any] = {
            "ok": True,
            "claimed": 0,
            "succeeded": 0,
            "failed": 0,
            "dead": 0,
            "deferred": 0,
            "applied": 0,
            "stop_reason": "no_jobs",
            "controls": {
                "limit": effective_limit,
                "llm_timeout_seconds": effective_timeout,
                "time_budget_seconds": effective_budget,
            },
        }
        if effective_limit <= 0:
            summary["stop_reason"] = "disabled"
            return summary
        llm_available = bool(
            self.graph_extractor.config.enabled and self.graph_extractor.llm_service is not None
        )
        if not llm_available:
            summary["stop_reason"] = "llm_unavailable"
            return summary
        jobs = await self._claim_group_window_llm_jobs(
            limit=effective_limit,
            lock_ttl_seconds=effective_timeout * 2 + 30,
        )
        summary["claimed"] = len(jobs)
        started_at = monotonic()
        for index, job in enumerate(jobs):
            remaining = effective_budget - (monotonic() - started_at)
            if index > 0 and remaining < effective_timeout:
                # Release the claim untouched; the next tick picks it up.
                await self._finish_group_window_llm_job(job, status="pending")
                summary["deferred"] += 1
                summary["stop_reason"] = "time_budget_reached"
                continue
            payload = _safe_json_loads(job.get("result_json"), {})
            scope = payload.get("scope") if isinstance(payload, dict) else None
            window = payload.get("window") if isinstance(payload, dict) else None
            if not isinstance(scope, dict) or not isinstance(window, dict):
                await self._finish_group_window_llm_job(
                    job, status="dead", error="malformed group window job payload"
                )
                summary["dead"] += 1
                continue
            tenant_id = str(scope.get("tenant_id") or job.get("tenant_id") or "")
            session_id = str(scope.get("session_id") or job.get("session_id") or "")
            if not await self._group_graph_auto_extract_scope_allowed(tenant_id, session_id):
                await self._finish_group_window_llm_job(
                    job, status="pending", retry_after_seconds=6 * 3600
                )
                summary["deferred"] += 1
                continue
            first_event_id = max(0, _safe_int(window.get("first_event_id"), 0))
            event_count = _safe_int(window.get("event_count"), 0)
            window_size = _clamp_int(
                event_count or window.get("window_size"), 50, minimum=10, maximum=100
            )
            try:
                result = await self.run_group_relationship_window_extraction(
                    tenant_id=tenant_id,
                    channel=str(scope.get("channel") or job.get("channel") or "wechat"),
                    source_key=str(scope.get("source_key") or job.get("source_key") or "wxbot"),
                    session_id=session_id,
                    date=str(scope.get("date") or ""),
                    window_size=window_size,
                    max_windows=1,
                    cursor_event_id=max(0, first_event_id - 1),
                    include_llm=True,
                    llm_timeout_seconds=effective_timeout,
                    source=_normalize_evidence_source(scope.get("source")),
                    llm_mode=LLM_MODE_INLINE,
                    deterministic=False,
                )
            except Exception as exc:
                attempts = _safe_int(job.get("attempts"), 0) + 1
                max_attempts = max(1, _safe_int(job.get("max_attempts"), 3))
                dead = attempts >= max_attempts
                await self._finish_group_window_llm_job(
                    job,
                    status="dead" if dead else "failed",
                    error=f"{exc.__class__.__name__}: {exc}",
                    retry_after_seconds=0 if dead else min(6 * 3600, 600 * attempts),
                )
                summary["dead" if dead else "failed"] += 1
                logger.warning(
                    "memory.group_window_llm_job_failed",
                    job_id=job.get("id"),
                    tenant_id=tenant_id,
                    session_id=session_id,
                    attempts=attempts,
                    dead=dead,
                    error_type=exc.__class__.__name__,
                    error=_truncate_error(exc),
                )
                continue
            totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
            if int(result.get("llm_failures") or 0) > 0:
                attempts = _safe_int(job.get("attempts"), 0) + 1
                max_attempts = max(1, _safe_int(job.get("max_attempts"), 3))
                dead = attempts >= max_attempts
                await self._finish_group_window_llm_job(
                    job,
                    status="dead" if dead else "failed",
                    error="llm_window_extraction_failed",
                    result={"totals": totals, "status": result.get("status")},
                    retry_after_seconds=0 if dead else min(6 * 3600, 600 * attempts),
                )
                summary["dead" if dead else "failed"] += 1
                continue
            await self._finish_group_window_llm_job(
                job,
                status="succeeded",
                result={
                    "totals": totals,
                    "status": result.get("status"),
                    "signal_counts": result.get("signal_counts"),
                },
            )
            summary["succeeded"] += 1
            summary["applied"] += int(totals.get("applied") or 0)
            summary["stop_reason"] = "completed"
        logger.info(
            "memory.group_window_llm_jobs",
            claimed=summary["claimed"],
            succeeded=summary["succeeded"],
            failed=summary["failed"],
            dead=summary["dead"],
            deferred=summary["deferred"],
            applied=summary["applied"],
            stop_reason=summary["stop_reason"],
            llm_timeout_seconds=effective_timeout,
        )
        return summary

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
        include_llm: bool = True,
        source: str | None = None,
        llm_timeout_seconds: int | None = None,
        llm_mode: str | None = None,
    ) -> dict[str, Any]:
        """Walk a day window by window until the budget or window cap is hit.

        The LLM timeout is a fixed per-window value (``llm_timeout_seconds``,
        default from settings). The time budget only decides how many windows
        fit in this run; it is never divided across windows, which is what used
        to leave the model 3 seconds per call.
        """

        effective_window_size = _clamp_int(window_size, 50, minimum=10, maximum=100)
        effective_max_windows = _clamp_int(max_windows_per_run, 20, minimum=1, maximum=100)
        effective_cursor = max(0, int(cursor_event_id or 0))
        effective_time_budget = _clamp_int(time_budget_seconds, 60, minimum=1, maximum=900)
        effective_source = _normalize_evidence_source(source)
        effective_llm_mode = _normalize_llm_mode(llm_mode)
        llm_inline = bool(include_llm) and effective_llm_mode == LLM_MODE_INLINE
        per_window_llm_timeout = _clamp_int(
            llm_timeout_seconds,
            self._group_window_llm_timeout_default(),
            minimum=1,
            maximum=GROUP_WINDOW_LLM_TIMEOUT_MAX,
        )
        started_at = monotonic()
        totals = {"events": 0, "windows": 0, "candidates": 0, "applied": 0, "skipped": 0}
        controls = {
            "window_size": effective_window_size,
            "max_windows_per_run": effective_max_windows,
            "cursor_event_id": effective_cursor,
            "dry_run": bool(dry_run),
            "time_budget_seconds": effective_time_budget,
            "include_llm": bool(include_llm),
            "llm_timeout_seconds": per_window_llm_timeout,
            "llm_mode": effective_llm_mode,
            "source": effective_source,
        }
        windows_processed = 0
        more_remain = False
        stop_reason = "no_more_events"
        status = "completed"
        next_cursor_event_id = effective_cursor
        resolved_source: str | None = effective_source
        signal_totals = _empty_group_signals()
        unresolved_total = 0
        llm_jobs_enqueued = 0
        llm_failures = 0

        while windows_processed < effective_max_windows:
            elapsed = monotonic() - started_at
            remaining_budget = effective_time_budget - elapsed
            if remaining_budget <= 0:
                stop_reason = "time_budget_reached"
                more_remain = True
                break
            if (
                llm_inline
                and windows_processed > 0
                and remaining_budget < min(per_window_llm_timeout, 10)
            ):
                # Not enough budget left for a real model call; stop cleanly
                # instead of starting a window that can only time out.
                stop_reason = "time_budget_reached"
                more_remain = True
                break
            remaining_budget = max(0.001, remaining_budget)
            # Keep inline LLM calls to one window so each successful return is
            # also a durable checkpoint for the automatic runner.
            batch_limit = (
                1
                if llm_inline
                else min(effective_max_windows - windows_processed, 10)
            )
            try:
                result = await asyncio.wait_for(
                    self.run_group_relationship_window_extraction(
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
                        include_llm=include_llm,
                        llm_timeout_seconds=max(
                            1,
                            min(per_window_llm_timeout, int(remaining_budget)),
                        ),
                        source=effective_source,
                        llm_mode=effective_llm_mode,
                    ),
                    timeout=remaining_budget,
                )
            except TimeoutError:
                stop_reason = "time_budget_reached"
                more_remain = True
                status = "partial"
                break
            result_totals = result.get("totals") if isinstance(result.get("totals"), dict) else {}
            processed = int(result_totals.get("windows") or 0)
            for key in totals:
                totals[key] += int(result_totals.get(key) or 0)
            if result.get("source") and processed:
                resolved_source = str(result.get("source"))
            signal_totals = _merge_group_signals(signal_totals, result.get("signal_counts"))
            unresolved_total += int(result.get("unresolved_targets") or 0)
            llm_jobs_enqueued += int(result.get("llm_jobs_enqueued") or 0)
            llm_failures += int(result.get("llm_failures") or 0)
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
            "source": resolved_source,
            "totals": totals,
            "signal_counts": signal_totals,
            "unresolved_targets": unresolved_total,
            "llm_jobs_enqueued": llm_jobs_enqueued,
            "llm_failures": llm_failures,
            "windows_processed": windows_processed,
            "next_cursor_event_id": next_cursor_event_id,
            "more_remain": more_remain,
            "stop_reason": stop_reason,
            "generated_from": [
                (
                    "plugin_wxbot_group_observations"
                    if resolved_source == EVIDENCE_SOURCE_OBSERVATION
                    else "plugin_memory_event"
                ),
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
        # Relations are stored under the operator session id, so a runtime
        # alias (cx1:...) must be expanded before counting, otherwise the panel
        # shows zeros for a group that has thousands of relations.
        scoped_session_ids = (
            await self._resolve_group_graph_session_ids(
                tenant_id=tenant_id,
                session_id=session_id,
            )
            if session_id
            else [None]
        ) or [session_id]
        safe_limit = max(1, min(int(limit or 5000), 10000))
        items: list[dict[str, Any]] = []
        seen_item_ids: set[int] = set()
        for scoped_session_id in scoped_session_ids:
            scoped_items = await self._list_memory_acceptance_audit_items(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
                session_id=scoped_session_id,
                scope_type="session",
                source_type=None,
                include_deleted=False,
                limit=safe_limit,
            )
            for item in scoped_items:
                item_id = _safe_int(item.get("id"), 0)
                if item_id and item_id in seen_item_ids:
                    continue
                if item_id:
                    seen_item_ids.add(item_id)
                items.append(item)
        target_date = _parse_daily_relationship_date(date).date().isoformat() if date else ""
        totals = {
            "items": 0,
            "events": 0,
            "observations": 0,
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
        source_counts: dict[str, int] = {}
        event_ids: set[int] = set()
        observation_ids: set[int] = set()
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
            for observation_id in _coerce_int_set(
                relation.get("evidence_observation_ids")
                or value.get("source_observation_ids")
                or []
            ):
                observation_ids.add(observation_id)
            evidence_source = str(value.get("evidence_source") or EVIDENCE_SOURCE_MEMORY_EVENT)
            source_counts[evidence_source] = source_counts.get(evidence_source, 0) + 1
            window = value.get("window") if isinstance(value.get("window"), dict) else {}
            window_keys.add(
                (value.get("date"), window.get("first_event_id"), window.get("last_event_id"))
            )
        totals["events"] = len(event_ids)
        totals["observations"] = len(observation_ids)
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
                "session_ids": [item for item in scoped_session_ids if item],
                "user_id": user_id or "",
                "date": target_date,
            },
            "totals": totals,
            "status_counts": status_counts,
            "acceptance_counts": acceptance_counts,
            "predicate_counts": predicate_counts,
            "evidence_source_counts": source_counts,
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

        event_rows = await self._load_group_relationship_events(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            session_id=session_id,
            user_id_scope=user_id_scope,
            start_at=start_at,
            end_at=end_at,
            columns="id, user_text, trace_id, event_key, created_at",
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

    async def auto_accept_pending_group_window_relations(
        self,
        *,
        tenant_id: str,
        channel: str | None = None,
        source_key: str | None = None,
        session_id: str | None = None,
        limit: int = 2000,
    ) -> dict[str, Any]:
        session_ids = (
            await self._resolve_group_graph_session_ids(
                tenant_id=tenant_id,
                session_id=session_id,
            )
            if session_id
            else []
        )
        params: dict[str, Any] = {
            "tid": str(tenant_id or "").strip(),
            "lim": max(1, min(int(limit or 2000), 5000)),
        }
        conditions = [
            "pending.tenant_id = :tid",
            "pending.deleted_at IS NULL",
            "pending.value_json::jsonb->>'kind' = 'group_window_relation'",
            "COALESCE(pending.value_json::jsonb->'acceptance'->>'status', '') "
            "IN ('needs_review', 'candidate', '')",
        ]
        if channel is not None:
            conditions.append("pending.channel = :channel")
            params["channel"] = channel
        if source_key is not None:
            conditions.append("pending.source_key = :source_key")
            params["source_key"] = source_key
        if session_ids:
            conditions.append("pending.session_id = ANY(:sids)")
            params["sids"] = session_ids
        rows = await _exec(
            "UPDATE plugin_memory_item AS item "
            "SET status = 'active', "
            "value_json = jsonb_set("
            "jsonb_set("
            "jsonb_set("
            "COALESCE(item.value_json::jsonb, '{}'::jsonb), "
            "'{acceptance,status}', '\"accepted\"'), "
            "'{acceptance,reason}', '\"group_window_auto_accept\"'), "
            "'{acceptance,reviewed_by}', '\"system/auto\"'"
            ")::text, "
            "updated_at = NOW() "
            "WHERE item.id IN ("
            "SELECT pending.id FROM plugin_memory_item AS pending "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY pending.id ASC LIMIT :lim"
            ") "
            "RETURNING item.id",
            params,
        )
        accepted = len(rows or [])
        item_ids = [int(row["id"]) for row in (rows or []) if row.get("id") is not None]
        facts_activated = 0
        entities_activated = 0
        if item_ids:
            fact_rows = await _exec(
                "UPDATE plugin_memory_fact AS fact "
                "SET status = 'active', updated_at = NOW() "
                "WHERE fact.memory_item_id = ANY(:ids) "
                "AND fact.status <> 'active' "
                "RETURNING fact.id",
                {"ids": item_ids},
            )
            facts_activated = len(fact_rows or [])
            entity_rows = await _exec(
                "UPDATE plugin_memory_entity AS entity "
                "SET status = 'active', updated_at = NOW() "
                "WHERE entity.status <> 'active' "
                "AND entity.id IN ("
                "SELECT fact.subject_entity_id FROM plugin_memory_fact AS fact "
                "WHERE fact.memory_item_id = ANY(:ids) AND fact.subject_entity_id IS NOT NULL "
                "UNION "
                "SELECT fact.object_entity_id FROM plugin_memory_fact AS fact "
                "WHERE fact.memory_item_id = ANY(:ids) AND fact.object_entity_id IS NOT NULL"
                ") "
                "RETURNING entity.id",
                {"ids": item_ids},
            )
            entities_activated = len(entity_rows or [])
        return {
            "ok": True,
            "pending_found": accepted,
            "accepted": accepted,
            "failed": 0,
            "facts_activated": facts_activated,
            "entities_activated": entities_activated,
            "session_ids": session_ids,
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

    async def _resolve_group_graph_session_ids(
        self,
        *,
        tenant_id: str,
        session_id: str | None,
    ) -> list[str]:
        requested = str(session_id or "").strip()
        if not requested:
            return []
        aliases = {requested}
        try:
            rows = await _exec(
                "SELECT session_id, "
                "COALESCE(metadata->>'external_conversation_id', '') AS external_id, "
                "COALESCE(metadata->>'external_session_id', '') AS external_session_id, "
                "COALESCE(metadata->>'canonical_conversation_id', '') AS canonical_id "
                "FROM sessions "
                "WHERE tenant_id = :tid "
                "AND ("
                "session_id = :sid "
                "OR COALESCE(metadata->>'external_conversation_id', '') = :sid "
                "OR COALESCE(metadata->>'external_session_id', '') = :sid "
                "OR COALESCE(metadata->>'canonical_conversation_id', '') = :sid"
                ")",
                {"tid": str(tenant_id or "").strip(), "sid": requested},
            )
        except Exception:
            logger.warning(
                "memory.group_graph_session_alias_failed",
                tenant_id=tenant_id,
                session_id=requested,
                exc_info=True,
            )
            return [requested]
        for row in rows or []:
            for key in ("session_id", "external_id", "external_session_id", "canonical_id"):
                value = str(row.get(key) or "").strip()
                if value:
                    aliases.add(value)
        extras = sorted(item for item in aliases if item != requested)
        return [requested, *extras]

    async def _operator_group_session_id(
        self,
        *,
        tenant_id: str,
        session_id: str,
    ) -> str:
        aliases = await self._resolve_group_graph_session_ids(
            tenant_id=tenant_id,
            session_id=session_id,
        )
        return _prefer_operator_group_session_id(aliases, session_id)

    async def _load_group_relationship_events(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        session_id: str,
        user_id_scope: str,
        start_at: datetime,
        end_at: datetime,
        columns: str,
        cursor_event_id: int | None = None,
        limit: int | None = None,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        """Load one day of group messages for window extraction.

        ``source`` pins the evidence source: ``memory_event`` reads only imported
        memory events, ``observation`` reads only live group observations. When
        omitted the legacy order is kept (scoped events, live events, then
        observations) so manual API callers keep working.
        """

        session_ids = await self._resolve_group_graph_session_ids(
            tenant_id=tenant_id,
            session_id=session_id,
        ) or [session_id]
        effective_source = _normalize_evidence_source(source)
        if effective_source == EVIDENCE_SOURCE_OBSERVATION:
            return await self._load_group_relationship_events_from_observations(
                tenant_id=tenant_id,
                session_ids=session_ids,
                start_at=start_at,
                end_at=end_at,
                cursor_event_id=cursor_event_id,
                limit=limit,
            )
        params: dict[str, Any] = {
            "tid": tenant_id,
            "channel": channel,
            "source_key": source_key,
            "uid": user_id_scope,
            "sids": session_ids,
            "start_at": start_at,
            "end_at": end_at,
        }
        cursor_sql = ""
        if cursor_event_id is not None:
            cursor_sql = "AND id > :cursor_event_id "
            params["cursor_event_id"] = int(cursor_event_id)
        limit_sql = ""
        if limit is not None:
            limit_sql = "LIMIT :lim"
            params["lim"] = int(limit)
        scoped_rows = await _exec(
            f"SELECT {columns} "
            "FROM plugin_memory_event "
            "WHERE tenant_id = :tid AND channel = :channel "
            "AND source_key IN (:source_key, '*') "
            "AND user_id = :uid AND session_id = ANY(:sids) "
            "AND created_at >= :start_at AND created_at < :end_at "
            f"{cursor_sql}"
            "ORDER BY created_at ASC, id ASC "
            f"{limit_sql}",
            params,
        )
        if scoped_rows:
            return scoped_rows
        live_params = dict(params)
        live_params.pop("uid", None)
        live_rows = await _exec(
            f"SELECT {columns} "
            "FROM plugin_memory_event "
            "WHERE tenant_id = :tid AND channel = :channel "
            "AND source_key IN (:source_key, '*') "
            "AND session_id = ANY(:sids) "
            "AND created_at >= :start_at AND created_at < :end_at "
            f"{cursor_sql}"
            "ORDER BY created_at ASC, id ASC "
            f"{limit_sql}",
            live_params,
        )
        if live_rows:
            return live_rows
        if effective_source == EVIDENCE_SOURCE_MEMORY_EVENT:
            return []
        return await self._load_group_relationship_events_from_observations(
            tenant_id=tenant_id,
            session_ids=session_ids,
            start_at=start_at,
            end_at=end_at,
            cursor_event_id=cursor_event_id,
            limit=limit,
        )

    async def _load_group_relationship_events_from_observations(
        self,
        *,
        tenant_id: str,
        session_ids: list[str],
        start_at: datetime,
        end_at: datetime,
        cursor_event_id: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        start_dt = start_at if isinstance(start_at, datetime) else datetime.combine(start_at, datetime.min.time())
        end_dt = end_at if isinstance(end_at, datetime) else datetime.combine(end_at, datetime.min.time())
        start_ts = int(start_dt.timestamp())
        end_ts = int(end_dt.timestamp())
        if end_ts <= start_ts or not session_ids:
            return []
        params: dict[str, Any] = {
            "tid": tenant_id,
            "sids": session_ids,
            "start_ts": start_ts,
            "end_ts": end_ts,
        }
        cursor_sql = ""
        if cursor_event_id is not None:
            cursor_sql = "AND id > :cursor_event_id "
            params["cursor_event_id"] = int(cursor_event_id)
        limit_sql = ""
        if limit is not None:
            limit_sql = "LIMIT :lim"
            params["lim"] = int(limit)
        rows = await _exec(
            "SELECT id, tenant_id, session_id, sender_wxid, sender_name, content, "
            "occurred_ts, is_self_sent, metadata_json "
            "FROM plugin_wxbot_group_observations "
            "WHERE tenant_id = :tid AND session_id = ANY(:sids) "
            "AND occurred_ts >= :start_ts AND occurred_ts < :end_ts "
            "AND COALESCE(is_self_sent, FALSE) = FALSE "
            f"{cursor_sql}"
            "ORDER BY occurred_ts ASC, id ASC "
            f"{limit_sql}",
            params,
        )
        events: list[dict[str, Any]] = []
        for row in rows or []:
            sender = str(row.get("sender_wxid") or "").strip()
            content = str(row.get("content") or "").strip()
            if not sender or not content:
                continue
            if str(row.get("is_self_sent") or "").strip().lower() in {"true", "t", "1"}:
                # Defensive: the bot's own replies are not group-member interactions.
                continue
            occurred = int(row.get("occurred_ts") or 0)
            created_at = (
                datetime.fromtimestamp(occurred, UTC).replace(tzinfo=None)
                if occurred > 0
                else start_at
            )
            events.append(
                {
                    "id": int(row.get("id") or 0),
                    "tenant_id": str(row.get("tenant_id") or tenant_id),
                    "channel": "wechat",
                    "source_key": "wxbot",
                    "user_id": sender,
                    "session_id": str(row.get("session_id") or ""),
                    "user_text": f"{sender}: {content}"[:1000],
                    "assistant_text": "",
                    "trace_id": "",
                    "event_key": f"observation:{row.get('id')}",
                    "created_at": created_at,
                    "_graph_source": EVIDENCE_SOURCE_OBSERVATION,
                    "_observation": _observation_interaction_metadata(
                        row.get("metadata_json"),
                        sender_name=row.get("sender_name"),
                    ),
                }
            )
        return events

    async def _group_graph_auto_extract_scope_allowed(
        self,
        tenant_id: str,
        session_id: str,
    ) -> bool:
        if not bool(getattr(self, "runtime_scope_gates_required", False)):
            return True
        gate = getattr(self, "combined_history_scope_execution_allowed", None)
        if not callable(gate):
            logger.warning(
                "memory.group_graph_auto_extract_scope_denied",
                tenant_id=tenant_id,
                session_id=session_id,
                reason="scope_gate_unavailable",
            )
            return False
        try:
            allowed = await gate(str(tenant_id or ""), str(session_id or "")) is True
        except Exception:
            logger.warning(
                "memory.group_graph_auto_extract_scope_failed",
                tenant_id=tenant_id,
                session_id=session_id,
                exc_info=True,
            )
            return False
        if not allowed:
            # Per-target detail stays at debug; the tick summary reports the
            # aggregated skipped_reasons at info level.
            logger.debug(
                "memory.group_graph_auto_extract_scope_denied",
                tenant_id=tenant_id,
                session_id=session_id,
                reason="plugin_scope_disabled",
            )
        return allowed

    async def list_known_group_graph_sessions(
        self,
        *,
        lookback_days: int = 30,
        max_sessions: int = 10,
        start_at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        effective_lookback = _clamp_int(lookback_days, 30, minimum=1, maximum=90)
        effective_max_sessions = _clamp_int(max_sessions, 10, minimum=1, maximum=20)
        cutoff = start_at or (
            datetime.now(UTC).replace(tzinfo=None) - timedelta(days=effective_lookback)
        )
        rows = await _exec(
            "SELECT tenant_id, channel, source_key, session_id, "
            "COUNT(*) AS event_count, MAX(created_at) AS last_seen "
            "FROM plugin_memory_event "
            "WHERE created_at >= :start_at "
            "AND session_id LIKE '%@chatroom' "
            "GROUP BY tenant_id, channel, source_key, session_id "
            "ORDER BY last_seen DESC, event_count DESC "
            "LIMIT :lim",
            {
                "start_at": cutoff,
                "lim": effective_max_sessions,
            },
        )
        observation_rows = await self._list_group_observation_session_counts(
            start_at=cutoff,
            limit=effective_max_sessions,
        )
        activity_rows = await _exec(
            "SELECT tenant_id, channel, session_id "
            "FROM sessions "
            "WHERE updated_at >= :start_at "
            "AND session_id LIKE '%@chatroom' "
            "ORDER BY updated_at DESC "
            "LIMIT :lim",
            {
                "start_at": cutoff,
                "lim": effective_max_sessions,
            },
        )
        sessions: list[dict[str, Any]] = []
        merged: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for row in list(rows or []) + list(observation_rows) + [
            {
                **item,
                "source_key": "wxbot",
                "event_count": 0,
            }
            for item in (activity_rows or [])
        ]:
            session_id = str(row.get("session_id") or "").strip()
            if not _is_group_session_id(session_id):
                continue
            tenant_id = str(row.get("tenant_id") or "").strip()
            channel = str(row.get("channel") or "").strip() or "wechat"
            source_key = str(row.get("source_key") or "").strip() or "wxbot"
            operator_session = await self._operator_group_session_id(
                tenant_id=tenant_id,
                session_id=session_id,
            )
            key = (tenant_id, channel, source_key, operator_session)
            current = merged.get(key)
            event_count = int(row.get("event_count") or 0)
            if current is None:
                merged[key] = {
                    "tenant_id": tenant_id,
                    "channel": channel,
                    "source_key": source_key,
                    "session_id": operator_session,
                    "event_count": event_count,
                }
                continue
            current["event_count"] = int(current.get("event_count") or 0) + event_count
        sessions = list(merged.values())
        sessions.sort(key=lambda item: int(item.get("event_count") or 0), reverse=True)
        return sessions[:effective_max_sessions]

    async def list_imported_group_graph_targets(
        self,
        *,
        lookback_days: int = 7,
        max_targets: int = 3,
        start_at: datetime | None = None,
    ) -> list[dict[str, Any]]:
        effective_lookback = _clamp_int(lookback_days, 7, minimum=1, maximum=14)
        effective_max_targets = _clamp_int(max_targets, 3, minimum=1, maximum=200)
        cutoff = start_at or (
            datetime.now(UTC).replace(tzinfo=None) - timedelta(days=effective_lookback)
        )
        fetch_limit = _clamp_int(
            effective_max_targets * max(effective_lookback, 1) * 4,
            40,
            minimum=10,
            maximum=200,
        )
        rows = await _exec(
            "SELECT tenant_id, channel, source_key, session_id, "
            "CAST(created_at AS date) AS day, COUNT(*) AS event_count, MAX(id) AS last_event_id "
            "FROM plugin_memory_event "
            "WHERE created_at >= :start_at "
            "AND session_id LIKE '%@chatroom' "
            "GROUP BY tenant_id, channel, source_key, session_id, CAST(created_at AS date) "
            "ORDER BY day DESC, event_count DESC "
            "LIMIT :lim",
            {
                "start_at": cutoff,
                "lim": fetch_limit,
            },
        )
        observation_rows = await self._list_group_observation_day_counts(
            start_at=cutoff,
            limit=fetch_limit,
        )
        targets: list[dict[str, Any]] = []
        merged: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        for row in [*rows, *observation_rows]:
            session_id = str(row.get("session_id") or "").strip()
            if not _is_group_session_id(session_id):
                continue
            day = row.get("day")
            if hasattr(day, "isoformat"):
                date_value = day.isoformat()
            else:
                date_value = str(day or "")[:10]
            if not date_value:
                continue
            tenant_id = str(row.get("tenant_id") or "").strip()
            channel = str(row.get("channel") or "").strip() or "wechat"
            source_key = str(row.get("source_key") or "").strip() or "wxbot"
            row_source = _normalize_evidence_source(row.get("source")) or EVIDENCE_SOURCE_MEMORY_EVENT
            operator_session = await self._operator_group_session_id(
                tenant_id=tenant_id,
                session_id=session_id,
            )
            key = (tenant_id, channel, source_key, operator_session, date_value)
            event_count = int(row.get("event_count") or 0)
            last_id = int(row.get("last_event_id") or 0)
            current = merged.get(key)
            if current is None:
                current = {
                    "tenant_id": tenant_id,
                    "channel": channel,
                    "source_key": source_key,
                    "session_id": operator_session,
                    "date": date_value,
                    "event_count": 0,
                    "last_event_id": 0,
                    "last_observation_id": 0,
                    "memory_event_count": 0,
                    "observation_count": 0,
                }
                merged[key] = current
            current["event_count"] = int(current.get("event_count") or 0) + event_count
            if row_source == EVIDENCE_SOURCE_OBSERVATION:
                current["observation_count"] = int(current.get("observation_count") or 0) + event_count
                current["last_observation_id"] = max(
                    int(current.get("last_observation_id") or 0), last_id
                )
            else:
                current["memory_event_count"] = (
                    int(current.get("memory_event_count") or 0) + event_count
                )
                current["last_event_id"] = max(int(current.get("last_event_id") or 0), last_id)
        targets = []
        for item in merged.values():
            # Imported memory events keep priority for a day; observations are the
            # source for every day that only exists in the live group stream.
            item["source"] = (
                EVIDENCE_SOURCE_MEMORY_EVENT
                if int(item.get("memory_event_count") or 0) > 0
                else EVIDENCE_SOURCE_OBSERVATION
            )
            targets.append(item)
        targets.sort(
            key=lambda item: (str(item.get("date") or ""), int(item.get("event_count") or 0)),
            reverse=True,
        )
        return targets[:effective_max_targets]

    async def _list_group_observation_day_counts(
        self,
        *,
        start_at: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Group observation counts per (session, UTC day) shaped like memory-event rows."""

        start_dt = (
            start_at
            if isinstance(start_at, datetime)
            else datetime.combine(start_at, datetime.min.time())
        )
        try:
            rows = await _exec(
                "SELECT tenant_id, session_id, "
                "CAST(to_timestamp(occurred_ts) AT TIME ZONE 'UTC' AS date) AS day, "
                "COUNT(*) AS event_count, MAX(id) AS last_event_id "
                "FROM plugin_wxbot_group_observations "
                "WHERE occurred_ts >= :start_ts "
                "AND session_id LIKE '%@chatroom' "
                "AND sender_wxid <> '' AND content <> '' "
                "GROUP BY tenant_id, session_id, "
                "CAST(to_timestamp(occurred_ts) AT TIME ZONE 'UTC' AS date) "
                "ORDER BY day DESC, event_count DESC "
                "LIMIT :lim",
                {
                    "start_ts": int(start_dt.replace(tzinfo=UTC).timestamp()),
                    "lim": int(limit),
                },
            )
        except Exception:
            logger.warning("memory.group_graph_observation_targets_failed", exc_info=True)
            return []
        return [
            {
                "tenant_id": row.get("tenant_id"),
                "channel": "wechat",
                "source_key": "wxbot",
                "session_id": row.get("session_id"),
                "day": row.get("day"),
                "event_count": row.get("event_count"),
                "last_event_id": row.get("last_event_id"),
                "source": EVIDENCE_SOURCE_OBSERVATION,
            }
            for row in rows or []
        ]

    async def _list_group_observation_session_counts(
        self,
        *,
        start_at: datetime,
        limit: int,
    ) -> list[dict[str, Any]]:
        start_dt = (
            start_at
            if isinstance(start_at, datetime)
            else datetime.combine(start_at, datetime.min.time())
        )
        try:
            rows = await _exec(
                "SELECT tenant_id, session_id, COUNT(*) AS event_count, "
                "MAX(occurred_ts) AS last_seen_ts "
                "FROM plugin_wxbot_group_observations "
                "WHERE occurred_ts >= :start_ts "
                "AND session_id LIKE '%@chatroom' "
                "GROUP BY tenant_id, session_id "
                "ORDER BY last_seen_ts DESC, event_count DESC "
                "LIMIT :lim",
                {
                    "start_ts": int(start_dt.replace(tzinfo=UTC).timestamp()),
                    "lim": int(limit),
                },
            )
        except Exception:
            logger.warning("memory.group_graph_observation_sessions_failed", exc_info=True)
            return []
        return [
            {
                "tenant_id": row.get("tenant_id"),
                "channel": "wechat",
                "source_key": "wxbot",
                "session_id": row.get("session_id"),
                "event_count": row.get("event_count"),
            }
            for row in rows or []
        ]

    async def _load_group_graph_auto_extract_cursor(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        session_id: str,
        target_date: str,
        source: str | None = None,
    ) -> int:
        cursor_key = _group_graph_auto_cursor_key(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            session_id=session_id,
            target_date=target_date,
        )
        rows = await _exec(
            "SELECT result_json FROM plugin_memory_extraction_job "
            "WHERE idempotency_key = :cursor_key LIMIT 1",
            {"cursor_key": cursor_key},
        )
        if not rows:
            return 0
        raw_payload = rows[0].get("result_json")
        payload = (
            raw_payload
            if isinstance(raw_payload, dict)
            else _safe_json_loads(raw_payload, {})
        )
        if not isinstance(payload, dict) or payload.get("kind") != "group_graph_auto_cursor":
            return 0
        expected_scope = {
            "tenant_id": tenant_id,
            "channel": channel,
            "source_key": source_key,
            "session_id": session_id,
            "date": target_date,
        }
        if payload.get("scope") != expected_scope:
            return 0
        requested_source = _normalize_evidence_source(source)
        stored_source = (
            _normalize_evidence_source(payload.get("source")) or EVIDENCE_SOURCE_MEMORY_EVENT
        )
        if requested_source is not None and stored_source != requested_source:
            # Memory-event ids and observation ids live in different id spaces; a
            # cursor from one source must never be applied to the other.
            return 0
        return max(0, _safe_int(payload.get("cursor_event_id"), 0))

    async def _save_group_graph_auto_extract_cursor(
        self,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        session_id: str,
        target_date: str,
        cursor_event_id: int,
        source: str | None = None,
    ) -> None:
        cursor = max(0, int(cursor_event_id or 0))
        cursor_source = _normalize_evidence_source(source) or EVIDENCE_SOURCE_MEMORY_EVENT
        cursor_key = _group_graph_auto_cursor_key(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            session_id=session_id,
            target_date=target_date,
        )
        payload = json.dumps(
            {
                "kind": "group_graph_auto_cursor",
                "version": 2,
                "scope": {
                    "tenant_id": tenant_id,
                    "channel": channel,
                    "source_key": source_key,
                    "session_id": session_id,
                    "date": target_date,
                },
                "source": cursor_source,
                "cursor_event_id": cursor,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        # A cursor only advances within the same source; switching source (for
        # example a day that used to read imported events and now reads live
        # observations) restarts from the new source's beginning.
        await _exec(
            "INSERT INTO plugin_memory_extraction_job "
            "(tenant_id, channel, source_key, user_id, session_id, source_event_id, "
            "source_trace_id, status, attempts, max_attempts, next_run_at, result_json, "
            "idempotency_key, created_at, updated_at) "
            "VALUES (:tid, :channel, :source_key, :group_uid, :sid, NULL, :trace, "
            "'succeeded', 0, 1, NOW(), :result_json, :cursor_key, NOW(), NOW()) "
            "ON CONFLICT (idempotency_key) DO UPDATE SET "
            "result_json = CASE WHEN "
            "COALESCE(NULLIF(plugin_memory_extraction_job.result_json, '')::jsonb ->> 'source', "
            "'memory_event') <> :cursor_source "
            "OR COALESCE(NULLIF("
            "plugin_memory_extraction_job.result_json, '')::jsonb ->> 'cursor_event_id', '0')::bigint "
            "< :cursor_event_id THEN EXCLUDED.result_json "
            "ELSE plugin_memory_extraction_job.result_json END, "
            "status = 'succeeded', locked_until = NULL, locked_by = '', updated_at = NOW()",
            {
                "tid": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "group_uid": GROUP_HISTORY_USER_ID_SCOPE,
                "sid": session_id,
                "trace": cursor_key[:128],
                "result_json": payload,
                "cursor_key": cursor_key,
                "cursor_event_id": cursor,
                "cursor_source": cursor_source,
            },
        )

    async def run_group_graph_auto_extract_tick(
        self,
        *,
        lookback_days: int = 7,
        max_sessions: int = 10,
        max_windows_per_session: int = 20,
        window_size: int = 50,
        time_budget_seconds: int = 180,
        include_llm: bool = True,
        sync_missing_history: bool = False,
        sync_max_messages: int = 200,
        llm_jobs_per_tick: int | None = None,
        llm_timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        """One scheduler tick: deterministic catch-up for every target, then a
        bounded number of queued LLM window jobs with a full per-job timeout."""

        effective_time_budget = _clamp_int(
            time_budget_seconds, 180, minimum=1, maximum=900
        )
        skipped: list[dict[str, Any]] = []
        synced: list[dict[str, Any]] = []
        if sync_missing_history:
            known_sessions = await self.list_known_group_graph_sessions(
                lookback_days=max(lookback_days, 14),
                max_sessions=max_sessions,
            )
            for session in known_sessions:
                tenant_id = str(session.get("tenant_id") or "")
                session_id = str(session.get("session_id") or "")
                if not tenant_id or not session_id:
                    skipped.append({**session, "reason": "incomplete_scope"})
                    continue
                if not await self._group_graph_auto_extract_scope_allowed(tenant_id, session_id):
                    skipped.append({**session, "reason": "scope_disabled"})
                    continue
                try:
                    backfill = await asyncio.wait_for(
                        self.backfill_from_sdk(
                            tenant_id=tenant_id,
                            channel=str(session.get("channel") or "wechat"),
                            source_key=str(session.get("source_key") or "wxbot"),
                            user_id=None,
                            session_ids=[session_id],
                            connection_id="legacy-wechat-default",
                            days_limit=lookback_days,
                            max_messages_per_session=_clamp_int(
                                sync_max_messages, 200, minimum=20, maximum=500
                            ),
                            enqueue_llm_jobs=bool(include_llm),
                        ),
                        timeout=float(effective_time_budget),
                    )
                except Exception as exc:
                    logger.warning(
                        "memory.group_graph_auto_extract_sync_failed",
                        tenant_id=tenant_id,
                        session_id=session_id,
                        error_type=exc.__class__.__name__,
                        error=_truncate_error(exc),
                    )
                    skipped.append(
                        {
                            **session,
                            "reason": "sync_failed",
                            "error_type": exc.__class__.__name__,
                        }
                    )
                    continue
                synced.append(
                    {
                        **session,
                        "imported_count": backfill.get("imported_count"),
                        "events_inserted": backfill.get("events_inserted"),
                        "ok": backfill.get("ok"),
                    }
                )
                logger.info(
                    "memory.group_graph_auto_extract_synced",
                    tenant_id=tenant_id,
                    session_id=session_id,
                    imported_count=backfill.get("imported_count"),
                    events_inserted=backfill.get("events_inserted"),
                    ok=backfill.get("ok"),
                )
        targets = await self.list_imported_group_graph_targets(
            lookback_days=lookback_days,
            max_targets=min(
                200,
                max(1, int(max_sessions or 10)) * max(1, int(lookback_days or 7)) * 4,
            ),
        )
        results: list[dict[str, Any]] = []
        attempted = 0
        for target in targets:
            tenant_id = str(target.get("tenant_id") or "")
            session_id = str(target.get("session_id") or "")
            if not tenant_id or not session_id:
                skipped.append({**target, "reason": "incomplete_scope"})
                continue
            if not await self._group_graph_auto_extract_scope_allowed(tenant_id, session_id):
                skipped.append({**target, "reason": "scope_disabled"})
                continue
            channel = str(target.get("channel") or "wechat")
            source_key = str(target.get("source_key") or "wxbot")
            target_date = str(target.get("date") or "")
            target_source = _normalize_evidence_source(target.get("source"))
            cursor_kwargs: dict[str, Any] = {
                "tenant_id": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "session_id": session_id,
                "target_date": target_date,
            }
            if target_source is not None:
                cursor_kwargs["source"] = target_source
            cursor_event_id = await self._load_group_graph_auto_extract_cursor(**cursor_kwargs)
            last_event_id = max(
                0,
                int(
                    (
                        target.get("last_observation_id")
                        if target_source == EVIDENCE_SOURCE_OBSERVATION
                        else target.get("last_event_id")
                    )
                    or 0
                ),
            )
            if last_event_id > 0 and cursor_event_id >= last_event_id:
                skipped.append({**target, "reason": "up_to_date"})
                continue
            if attempted >= _clamp_int(max_sessions, 10, minimum=1, maximum=20):
                break
            attempted += 1
            try:
                catchup = await self.run_group_relationship_window_catchup(
                    tenant_id=tenant_id,
                    channel=channel,
                    source_key=source_key,
                    session_id=session_id,
                    date=target_date,
                    window_size=window_size,
                    max_windows_per_run=max_windows_per_session,
                    cursor_event_id=cursor_event_id,
                    time_budget_seconds=effective_time_budget,
                    include_llm=include_llm,
                    source=target_source,
                    # The tick never waits on the model inline: windows get a
                    # durable LLM job and are processed below with a real timeout.
                    llm_mode=LLM_MODE_ENQUEUE,
                    llm_timeout_seconds=llm_timeout_seconds,
                )
            except Exception as exc:
                logger.warning(
                    "memory.group_graph_auto_extract_target_failed",
                    tenant_id=tenant_id,
                    session_id=session_id,
                    date=target.get("date"),
                    error_type=exc.__class__.__name__,
                    error=_truncate_error(exc),
                )
                results.append(
                    {
                        **target,
                        "status": "failed",
                        "error_type": exc.__class__.__name__,
                    }
                )
                continue
            next_cursor_event_id = max(
                cursor_event_id,
                int(catchup.get("next_cursor_event_id") or cursor_event_id),
            )
            if next_cursor_event_id > cursor_event_id:
                save_kwargs: dict[str, Any] = {
                    **cursor_kwargs,
                    "cursor_event_id": next_cursor_event_id,
                }
                await self._save_group_graph_auto_extract_cursor(**save_kwargs)
            results.append(
                {
                    **target,
                    "status": str(catchup.get("status") or ""),
                    "stop_reason": catchup.get("stop_reason"),
                    "totals": catchup.get("totals") or {},
                    "more_remain": bool(catchup.get("more_remain")),
                    "cursor_event_id": cursor_event_id,
                    "next_cursor_event_id": next_cursor_event_id,
                }
            )
        llm_jobs: dict[str, Any] = {"claimed": 0, "succeeded": 0, "failed": 0, "dead": 0}
        if include_llm:
            try:
                llm_jobs = await self.run_group_window_llm_jobs(
                    limit=llm_jobs_per_tick,
                    llm_timeout_seconds=llm_timeout_seconds,
                )
            except Exception as exc:
                logger.warning(
                    "memory.group_window_llm_jobs_failed",
                    error_type=exc.__class__.__name__,
                    error=_truncate_error(exc),
                )
                llm_jobs = {"claimed": 0, "succeeded": 0, "failed": 0, "dead": 0, "error": exc.__class__.__name__}
        skipped_reasons: dict[str, int] = {}
        for item in skipped:
            reason = str(item.get("reason") or "unknown")
            skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
        applied_total = sum(
            int((item.get("totals") or {}).get("applied") or 0) for item in results
        )
        applied_total += int(llm_jobs.get("applied") or 0)
        failed_total = len([item for item in results if item.get("status") == "failed"])
        # Always leave a trace, even for a no-op tick: an idle scheduler must be
        # distinguishable from a broken one in the logs.
        logger.info(
            "memory.group_graph_auto_extract_tick",
            target_count=len(targets),
            ran=len(results) - failed_total,
            failed=failed_total,
            applied=applied_total,
            synced=len(synced),
            skipped=len(skipped),
            skipped_reasons=skipped_reasons,
            sources={
                source: len([item for item in targets if item.get("source") == source])
                for source in GROUP_GRAPH_EVIDENCE_SOURCES
            },
            include_llm=bool(include_llm),
            lookback_days=_clamp_int(lookback_days, 7, minimum=1, maximum=14),
            llm_jobs={
                key: llm_jobs.get(key)
                for key in ("claimed", "succeeded", "failed", "dead", "deferred", "stop_reason")
                if key in llm_jobs
            },
        )
        return {
            "ok": True,
            "lookback_days": _clamp_int(lookback_days, 7, minimum=1, maximum=14),
            "include_llm": bool(include_llm),
            "sync_missing_history": bool(sync_missing_history),
            "target_count": len(targets),
            "ran": len(results) - failed_total,
            "applied": applied_total,
            "skipped_reasons": skipped_reasons,
            "llm_jobs": llm_jobs,
            "synced": synced,
            "skipped": skipped,
            "results": results,
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
