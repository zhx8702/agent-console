"""
Memory hooks.

Loads per-user memory before capability execution and persists updated memory
after a reply is produced.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from app.common.intent import IntentDecision, IntentDomain
from app.common.intent_runtime import decision_from_pre, is_confident, slot_int, slot_text
from app.common.logging import get_logger
from app.common.types import (
    CapabilityResult,
    Channel,
    MessageType,
    RouteType,
    channel_id_value,
)
from app.orchestrator.effect_handlers import effect_handler_opt_in_enabled
from app.orchestrator.flow import MessageEffect, StepResult
from app.orchestrator.pipeline import PipelineContext
from app.plugin.hooks import HookAbort, HookPoint
from app.preprocessing.pii import detect_and_mask
from app.social.contracts import MemberPrivacyValues
from plugins.memory.observability import build_safe_memory_profile_signal
from plugins.memory.store import (
    GROUP_HISTORY_USER_ID_SCOPE,
    MemoryItemConflictError,
    MemoryItemProtectedError,
    MemoryMutationError,
    MemoryStore,
)

logger = get_logger(__name__)


MEMORY_CONTROL_CANDIDATE_LIMIT = 5
_GENERIC_MEMORY_REFERENCES = frozenset(
    {
        "it",
        "this",
        "that",
        "about it",
        "this thing",
        "that thing",
        "这个",
        "那个",
        "这件事",
        "那件事",
        "它",
        "算了",
    }
)


def _scope_from_ctx(ctx: PipelineContext) -> tuple[str, str, str, str]:
    event = ctx.event
    return (
        event.tenant_id,
        channel_id_value(event.channel),
        str(event.metadata.get("source") or "*"),
        event.user_id,
    )


def _is_group_session(ctx: PipelineContext) -> bool:
    session_id = str(ctx.event.session_id or "")
    metadata = dict(ctx.event.metadata or {})
    session_kind = str(metadata.get("session_kind") or "").strip().lower()
    return session_kind in {"group", "chatroom", "channel", "guild"} or session_id.endswith(
        "@chatroom"
    )


def _should_load_group_memory(ctx: PipelineContext, user_id: str) -> bool:
    user_id = str(user_id or "").strip()
    if not _is_group_session(ctx):
        return False
    if ctx.event.channel != Channel.WECHAT:
        return False
    if not str(ctx.event.session_id or "").endswith("@chatroom"):
        return False
    if not user_id or user_id == GROUP_HISTORY_USER_ID_SCOPE:
        return False
    return user_id != str(ctx.event.session_id or "").strip()


def _group_identity_memory_enabled(store: MemoryStore) -> bool:
    return _settings_bool(
        getattr(store, "settings", object()),
        "memory_group_identity_memory_enabled",
        False,
    )


async def _group_member_privacy(
    store: MemoryStore,
    ctx: PipelineContext,
) -> MemberPrivacyValues | None:
    if not _is_group_session(ctx):
        return None
    loader = getattr(store, "get_group_member_privacy_policy", None)
    if not callable(loader):
        # Every group-capable channel shares the same consent boundary.  A
        # newly added transport must not become memory-enabled merely because
        # its adapter has not wired the privacy loader yet.
        ctx.signals.setdefault("memory", {})["privacy_fail_closed"] = True
        return MemberPrivacyValues()
    try:
        return await loader(
            tenant_id=ctx.event.tenant_id,
            session_id=ctx.event.session_id,
            user_id=ctx.event.user_id,
        )
    except Exception as exc:
        # Absence or corruption of the privacy control plane can never make
        # group memory more permissive.
        logger.warning(
            "memory.member_privacy_policy_unavailable",
            tenant_id=ctx.event.tenant_id,
            session_id=ctx.event.session_id,
            user_id=ctx.event.user_id,
            error_class=exc.__class__.__name__,
        )
        ctx.signals.setdefault("memory", {})["privacy_fail_closed"] = True
        return MemberPrivacyValues()


def _group_memory_capture_allowed(
    policy: MemberPrivacyValues | None,
    *,
    session_id: str,
) -> bool:
    if policy is None:
        return True
    if not policy.memory_enabled:
        return False
    if policy.audience_scope == "session":
        return True
    if policy.audience_scope == "explicit":
        return session_id in policy.allowed_session_ids
    return False


def _group_memory_recall_allowed(
    policy: MemberPrivacyValues | None,
    *,
    session_id: str,
) -> bool:
    return (
        bool(
            _group_memory_capture_allowed(policy, session_id=session_id)
            and policy is not None
            and policy.allow_group_recall
        )
        if policy is not None
        else True
    )


async def _private_member_memory_write_block_reason(
    store: MemoryStore,
    ctx: PipelineContext,
) -> str | None:
    """Return a fail-closed reason for a private WeChat explicit-memory write."""

    if _is_group_session(ctx) or ctx.event.channel != Channel.WECHAT:
        return None
    checker = getattr(store, "member_memory_write_blocked", None)
    if not callable(checker):
        checker = getattr(store, "_member_memory_write_blocked", None)
    if not callable(checker):
        ctx.signals.setdefault("memory", {})["member_control_fail_closed"] = True
        return "member_control_unavailable"
    try:
        blocked = await checker(
            tenant_id=ctx.event.tenant_id,
            user_id=ctx.event.user_id,
        )
    except Exception as exc:
        logger.warning(
            "memory.member_write_control_unavailable",
            tenant_id=ctx.event.tenant_id,
            user_id=ctx.event.user_id,
            error_class=exc.__class__.__name__,
        )
        ctx.signals.setdefault("memory", {})["member_control_fail_closed"] = True
        return "member_control_unavailable"
    return "member_control_blocked" if blocked else None


async def _memory_read_block_reason(
    store: MemoryStore,
    ctx: PipelineContext,
) -> str | None:
    """Return why memory contents must not be disclosed for this request."""

    if _is_group_session(ctx):
        member_privacy = await _group_member_privacy(store, ctx)
        if _group_memory_recall_allowed(
            member_privacy,
            session_id=ctx.event.session_id,
        ):
            return None
        return "member_privacy_blocked"
    return await _private_member_memory_write_block_reason(store, ctx)


async def _blocked_memory_read_control_reply(
    store: MemoryStore,
    ctx: PipelineContext,
    *,
    intent: str,
) -> str | None:
    """Gate chat-level memory disclosure behind the active privacy policy."""

    reason = await _memory_read_block_reason(store, ctx)
    if reason is None:
        return None
    if _is_group_session(ctx):
        reply = "当前群未开启成员记忆召回，未操作记忆。"
    else:
        reply = "当前未开启个人记忆召回，未操作记忆。"
    ctx.signals.setdefault("memory", {})["member_recall_blocked"] = True
    ctx.signals["memory_control"] = {
        "matched": True,
        "intent": intent,
        "blocked": True,
        "reason": reason,
    }
    ctx.extras["memory_control_handled"] = True
    return reply


def _memory_audience_write_context(
    ctx: PipelineContext,
    policy: MemberPrivacyValues | None,
) -> dict[str, object]:
    """Build the immutable audience contract captured with a memory write."""

    if not _is_group_session(ctx):
        return {
            "origin_session_kind": "private",
            "audience_scope": "private",
            "allowed_session_ids": [],
            "sensitivity_category": "normal",
            "expires_at": None,
            "source_kind": "conversation",
        }
    if policy is None:
        # Generic/unknown group channels have no member privacy control plane
        # yet. Persisting as private makes the row non-recallable in groups.
        return {
            "origin_session_kind": "group",
            "audience_scope": "private",
            "allowed_session_ids": [],
            "sensitivity_category": "normal",
            "expires_at": None,
            "source_kind": "conversation",
        }
    session_id = str(ctx.event.session_id or "").strip()
    allowed = (
        [session_id] if policy.audience_scope == "session" else list(policy.allowed_session_ids)
    )
    expires_at = datetime.now(UTC) + timedelta(days=int(policy.retention_days))
    return {
        "origin_session_kind": "group",
        "audience_scope": policy.audience_scope,
        "allowed_session_ids": allowed,
        "sensitivity_category": "normal",
        "expires_at": expires_at.isoformat(),
        "source_kind": "conversation",
    }


def _group_session_only_profile(profile: dict, *, session_id: str) -> dict:
    """Remove identity-wide data before a member profile enters a group prompt.

    Identity memory is shared across a user's sessions and may have originated
    in a private chat. Session-scoped memory for this group remains available.
    """

    filtered = dict(profile)
    filtered["long_term_memory"] = ""
    filtered["identity_manual_notes"] = ""
    filtered["manual_notes"] = str(filtered.get("session_manual_notes") or "")
    filtered["identity_message_count"] = 0
    filtered["audience_scope"] = "group_session_only"

    identity_profile = dict(filtered.get("identity_profile") or {})
    identity_profile.update(
        {
            "long_term_memory": "",
            "manual_notes": "",
            "long_term_items": [],
            "long_term_items_json": "[]",
        }
    )
    filtered["identity_profile"] = identity_profile

    memory_items = dict(filtered.get("memory_items") or {})
    memory_items["identity"] = []
    memory_items["session"] = [
        item
        for item in (memory_items.get("session") or [])
        if isinstance(item, dict) and str(item.get("session_id") or session_id) == session_id
    ]
    filtered["memory_items"] = memory_items
    filtered["relevant_memory_items"] = [
        item
        for item in (filtered.get("relevant_memory_items") or [])
        if isinstance(item, dict)
        and str(item.get("scope_type") or "") == "session"
        and str(item.get("session_id") or "") == session_id
    ]
    # Graph projections can be rooted in an identity-scoped item. Until graph
    # rows carry explicit audience metadata, fail closed in group prompts.
    filtered["relevant_graph_facts"] = []
    filtered["relevant_graph_episodes"] = []
    filtered["memory_graph_budget_chars"] = 0
    return filtered


def _json_safe(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value


def _settings_bool(settings: object, name: str, default: bool) -> bool:
    return bool(getattr(settings, name, default))


def _settings_int(
    settings: object, name: str, default: int, *, minimum: int = 1, maximum: int = 20
) -> int:
    try:
        value = int(getattr(settings, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _safe_runtime_label(value: object, *, default: str = "") -> str:
    normalized = str(value or "").strip().lower()
    if re.fullmatch(r"[a-z0-9_.:-]{1,64}", normalized):
        return normalized
    return default


def _callable_supports_keyword(callback: object, keyword: str) -> bool:
    try:
        parameters = inspect.signature(callback).parameters.values()  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or (
            parameter.name == keyword
            and parameter.kind
            in {
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            }
        )
        for parameter in parameters
    )


def _memory_runtime_signal(ctx: PipelineContext) -> dict:
    memory_signal = ctx.signals.setdefault("memory", {})
    runtime = memory_signal.setdefault("runtime", {})
    metadata = dict(ctx.event.metadata or {})
    source_present = bool(str(metadata.get("source") or "").strip())
    event_connection_present = bool(
        str(getattr(ctx.event, "connection_id", "") or "").strip()
    )
    canonical_participant = str(
        getattr(ctx.event, "canonical_participant_id", "")
        or metadata.get("canonical_participant_id")
        or ""
    ).strip()
    canonical_conversation = str(
        getattr(ctx.event, "canonical_conversation_id", "")
        or metadata.get("canonical_conversation_id")
        or ""
    ).strip()
    canonical_identity_present = bool(
        (
            canonical_participant
            and canonical_participant != str(ctx.event.user_id or "").strip()
        )
        or (
            canonical_conversation
            and canonical_conversation != str(ctx.event.session_id or "").strip()
        )
    )
    canonical_connection_scoped = bool(event_connection_present or canonical_identity_present)
    if canonical_connection_scoped:
        source_scope = "canonical_connection_scoped"
    elif not source_present:
        source_scope = "legacy_wildcard"
    else:
        source_scope = "legacy_source"
    runtime.setdefault(
        "scope",
        {
            "session_kind": "group" if _is_group_session(ctx) else "private",
            "source_scope": source_scope,
            # Canonical participant/conversation IDs already include the
            # connection namespace. Only a wildcard source paired with legacy
            # identifiers is genuinely ambiguous.
            "legacy_or_ambiguous": bool(
                not source_present and not canonical_connection_scoped
            ),
        },
    )
    runtime.setdefault("load", {"status": "not_run", "reason": "not_run", "scopes": {}})
    runtime.setdefault("save", {"status": "not_run", "reason": "not_run"})
    return runtime


def _set_memory_runtime_stage(
    ctx: PipelineContext,
    stage: str,
    *,
    status: str,
    reason: str,
    error_type: str = "",
) -> dict:
    runtime = _memory_runtime_signal(ctx)
    payload = runtime.setdefault(stage, {})
    payload["status"] = _safe_runtime_label(status, default="unknown")
    payload["reason"] = _safe_runtime_label(reason, default="unknown")
    if error_type:
        payload["error_type"] = _safe_runtime_label(error_type, default="error")
    else:
        payload.pop("error_type", None)
    return payload


def _safe_observation_ids(values: object, *, limit: int = 50) -> list[int | str]:
    if not isinstance(values, list):
        return []
    result: list[int | str] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            if value < 0:
                continue
            safe_value: int | str = value
        else:
            text = str(value or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9:_-]{1,64}", text):
                continue
            safe_value = text
        marker = str(safe_value)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(safe_value)
        if len(result) >= limit:
            break
    return result


def _refresh_memory_load_aggregate(ctx: PipelineContext) -> None:
    load_signal = _memory_runtime_signal(ctx).setdefault("load", {})
    scopes = load_signal.get("scopes")
    if not isinstance(scopes, dict):
        scopes = {}
        load_signal["scopes"] = scopes
    selected_item_ids: list[int | str] = []
    selected_fact_ids: list[int | str] = []
    selected_episode_ids: list[int | str] = []
    candidate_count = 0
    selected_count = 0
    selected_fact_count = 0
    selected_episode_count = 0
    budget_chars = 0
    truncated = False
    for scope_payload in scopes.values():
        if not isinstance(scope_payload, dict):
            continue
        candidate_count += max(0, int(scope_payload.get("candidate_count") or 0))
        selected_count += max(0, int(scope_payload.get("selected_count") or 0))
        selected_fact_count += max(0, int(scope_payload.get("selected_fact_count") or 0))
        selected_episode_count += max(
            0,
            int(scope_payload.get("selected_episode_count") or 0),
        )
        budget_chars += max(0, int(scope_payload.get("budget_chars") or 0))
        truncated = truncated or bool(scope_payload.get("truncated"))
        selected_item_ids.extend(scope_payload.get("selected_item_ids") or [])
        selected_fact_ids.extend(scope_payload.get("selected_graph_fact_ids") or [])
        selected_episode_ids.extend(scope_payload.get("selected_graph_episode_ids") or [])
    load_signal["loaded_scopes"] = sorted(
        str(scope)
        for scope, payload in scopes.items()
        if isinstance(payload, dict) and payload.get("status") == "loaded"
    )
    load_signal["candidate_count"] = candidate_count
    load_signal["selected_count"] = selected_count
    load_signal["selected_fact_count"] = selected_fact_count
    load_signal["selected_episode_count"] = selected_episode_count
    load_signal["selected_item_ids"] = _safe_observation_ids(selected_item_ids)
    load_signal["selected_graph_fact_ids"] = _safe_observation_ids(selected_fact_ids)
    load_signal["selected_graph_episode_ids"] = _safe_observation_ids(selected_episode_ids)
    load_signal["budget_chars"] = budget_chars
    load_signal["truncated"] = truncated


def _record_memory_load_scope(
    ctx: PipelineContext,
    *,
    scope: str,
    profile: dict,
    retrieval_mode: str,
    candidate_count: int,
    budget_chars: int,
    truncated: bool,
) -> None:
    safe_profile = build_safe_memory_profile_signal(profile)
    selected_item_ids = list(safe_profile.get("selected_item_ids") or [])
    selected_fact_ids = list(safe_profile.get("selected_graph_fact_ids") or [])
    selected_episode_ids = list(safe_profile.get("selected_graph_episode_ids") or [])
    load_signal = _memory_runtime_signal(ctx).setdefault("load", {})
    scopes = load_signal.setdefault("scopes", {})
    scopes[scope] = {
        "status": "loaded",
        "retrieval_mode": _safe_runtime_label(retrieval_mode, default="none"),
        "candidate_count": max(0, int(candidate_count or 0)),
        "selected_count": len(selected_item_ids),
        "selected_fact_count": len(selected_fact_ids),
        "selected_episode_count": len(selected_episode_ids),
        "selected_item_ids": _safe_observation_ids(selected_item_ids),
        "selected_graph_fact_ids": _safe_observation_ids(selected_fact_ids),
        "selected_graph_episode_ids": _safe_observation_ids(selected_episode_ids),
        "budget_chars": max(0, int(budget_chars or 0)),
        "truncated": bool(truncated or safe_profile.get("truncated")),
    }
    _refresh_memory_load_aggregate(ctx)


def _sync_memory_control_runtime(ctx: PipelineContext) -> None:
    raw = ctx.signals.get("memory_control")
    signal = raw if isinstance(raw, dict) else {}
    matched = bool(signal.get("matched"))
    payload = {
        "status": "matched" if matched else "not_matched",
        "intent": _safe_runtime_label(signal.get("intent"), default="none"),
        "reason": _safe_runtime_label(signal.get("reason"), default="none"),
        "outcome": _safe_runtime_label(signal.get("outcome"), default="none"),
        "item_status": _safe_runtime_label(signal.get("item_status"), default="none"),
        "error_type": _safe_runtime_label(signal.get("error_type"), default="none"),
        "blocked": bool(signal.get("blocked")),
        "protected": bool(signal.get("protected")),
        "complete": bool(signal.get("complete")),
        "partial": bool(signal.get("partial")),
        "candidate_details_redacted": bool(signal.get("candidate_details_redacted")),
        "residual_count": max(0, int(signal.get("residual_count") or 0)),
        "truncated": bool(signal.get("truncated")),
        "candidate_count": max(
            0,
            int(signal.get("candidate_count") or signal.get("count") or 0),
        ),
        "affected_count": max(0, int(signal.get("count") or 0)),
        "selected_ids": _safe_observation_ids(list(signal.get("ids") or [])),
    }
    runtime = _memory_runtime_signal(ctx)
    runtime["control"] = payload
    if matched:
        _set_memory_runtime_stage(
            ctx,
            "load",
            status="skipped",
            reason="memory_control_handled",
        )
        _set_memory_runtime_stage(
            ctx,
            "save",
            status="skipped",
            reason="memory_control_handled",
        )


def _current_user_text(ctx: PipelineContext) -> str:
    pre = getattr(ctx, "pre", None)
    cleaned = str(getattr(pre, "cleaned_text", "") or "").strip()
    if cleaned:
        return cleaned
    return str(ctx.event.message.content or "").strip()


def _normalize_memory_control_text(value: str) -> str:
    return " ".join(str(value or "").strip().split())


def _trim_memory_text(value: str, *, limit: int = 240) -> str:
    return _normalize_memory_control_text(value).strip(" ：:，,。.!！?？")[:limit]


def _is_question_shaped_memory_command(text: str, content: str) -> bool:
    text_value = _normalize_memory_control_text(text)
    content_value = _normalize_memory_control_text(content).lower()
    if text_value.endswith(("?", "？", "吗", "么")):
        return True
    question_prefixes = (
        "吗",
        "么",
        "没",
        "没有",
        "什么",
        "谁",
        "哪",
        "怎么",
        "为何",
        "为什么",
        "是否",
        "when ",
        "what ",
        "who ",
        "where ",
        "why ",
        "how ",
        "whether ",
        "if ",
    )
    return content_value.startswith(question_prefixes)


def _is_meaningful_memory_operand(value: str) -> bool:
    normalized = _trim_memory_text(value)
    return bool(normalized and normalized.lower() not in _GENERIC_MEMORY_REFERENCES)


def _memory_decision(decision: IntentDecision | None) -> IntentDecision | None:
    if decision is None or decision.domain is not IntentDomain.MEMORY:
        return None
    if not is_confident(decision):
        return None
    return decision


def _parse_remember_intent(
    text: str,
    *,
    decision: IntentDecision | None = None,
) -> str | None:
    _ = text
    parsed = _memory_decision(decision)
    if parsed is None or parsed.action != "remember":
        return None
    content = _trim_memory_text(slot_text(parsed, "content", "query") or parsed.query)
    if not _is_meaningful_memory_operand(content):
        return None
    return content


def _parse_forget_intent(
    text: str,
    *,
    decision: IntentDecision | None = None,
) -> tuple[str, int | None] | None:
    _ = text
    parsed = _memory_decision(decision)
    if parsed is None or parsed.action != "forget":
        return None
    query = _trim_memory_text(slot_text(parsed, "query", "content") or parsed.query)
    item_id = slot_int(parsed, "item_id")
    if item_id is None and query.isdigit():
        item_id = int(query)
    if item_id is None and not _is_meaningful_memory_operand(query):
        return None
    return query, item_id


def _parse_full_forget_intent(
    text: str,
    *,
    decision: IntentDecision | None = None,
) -> bool:
    _ = text
    parsed = _memory_decision(decision)
    return parsed is not None and parsed.action == "forget_all"


def _parse_list_intent(
    text: str,
    *,
    decision: IntentDecision | None = None,
) -> bool:
    _ = text
    parsed = _memory_decision(decision)
    return parsed is not None and parsed.action == "list"


def _parse_search_intent(
    text: str,
    *,
    decision: IntentDecision | None = None,
) -> str | None:
    _ = text
    parsed = _memory_decision(decision)
    if parsed is None or parsed.action != "search":
        return None
    query = _trim_memory_text(slot_text(parsed, "query") or parsed.query, limit=120)
    return query if _is_meaningful_memory_operand(query) else None


def _is_visible_memory_item(item: dict) -> bool:
    return (
        item.get("deleted_at") is None
        and str(item.get("status") or "") == "active"
        and str(item.get("sensitivity") or "normal") == "normal"
    )


def _memory_control_query_tokens(query: str) -> list[str]:
    text_value = _normalize_memory_control_text(query).lower()
    if not text_value:
        return []
    tokens = re.findall(r"[a-z0-9][a-z0-9_-]{1,40}|[\u4e00-\u9fff]{2,}", text_value)
    return tokens or [text_value[:40]]


def _memory_control_item_matches_query(item: dict, query: str) -> bool:
    if not _normalize_memory_control_text(query):
        return True
    # Real MemoryStore rows include match_count for explicit retrieval queries.
    # Treat it as authoritative so context-injection fallback memories are not
    # exposed or deleted by chat-level search/forget intents.
    if "match_count" in item:
        try:
            return int(item.get("match_count") or 0) > 0
        except (TypeError, ValueError):
            return False
    tokens = _memory_control_query_tokens(query)
    if not tokens:
        return False
    haystack = _normalize_memory_control_text(
        " ".join(str(item.get(key) or "") for key in ("content", "original_text"))
    ).lower()
    return any(token in haystack for token in tokens)


def _filter_memory_control_matches(items: list[dict], query: str) -> list[dict]:
    if not _normalize_memory_control_text(query):
        return list(items)
    return [item for item in items if _memory_control_item_matches_query(item, query)]


def _is_protected_memory_item(item: dict) -> bool:
    return bool(item.get("pinned")) or str(item.get("source_type") or "") == "manual"


def _format_memory_summary(item: dict, *, limit: int = 80) -> str:
    content = _normalize_memory_control_text(str(item.get("content") or ""))
    if len(content) > limit:
        return f"{content[:limit].rstrip()}..."
    return content


def _format_memory_candidates(
    items: list[dict], *, header: str = "找到多条匹配记忆，请指定要删除哪一条："
) -> str:
    lines = [header]
    for item in items[:MEMORY_CONTROL_CANDIDATE_LIMIT]:
        item_id = item.get("id")
        prefix = f"#{item_id} " if item_id is not None else ""
        lines.append(f"- {prefix}{_format_memory_summary(item)}")
    return "\n".join(lines)


def _format_search_results(items: list[dict]) -> str:
    visible = [item for item in items if _is_visible_memory_item(item)]
    if not visible:
        return "没有找到匹配的记忆"
    lines = [f"找到 {len(visible)} 条记忆："]
    for item in visible[:MEMORY_CONTROL_CANDIDATE_LIMIT]:
        item_id = item.get("id")
        prefix = f"#{item_id} " if item_id is not None else ""
        lines.append(f"- {prefix}{_format_memory_summary(item)}")
    return "\n".join(lines)


def _memory_control_scope(ctx: PipelineContext) -> dict[str, str]:
    tenant_id, channel, source_key, user_id = _scope_from_ctx(ctx)
    return {
        "tenant_id": tenant_id,
        "channel": channel,
        "source_key": source_key,
        "user_id": user_id,
        "session_id": ctx.event.session_id,
    }


async def _forget_memory_item_for_current_user(
    store: MemoryStore,
    scope: dict[str, str],
    item_id: int,
    *,
    session_id: str,
) -> dict:
    target_scope = dict(scope)
    target_scope["session_id"] = session_id
    return await store.forget_memory_items(
        **target_scope,
        item_id=item_id,
        query="",
        allow_pinned=False,
        limit=1,
    )


def _remember_item_feedback(item: dict, content: str) -> tuple[str, str]:
    status = str(item.get("status") or "").strip().lower()
    acceptance_status = str(item.get("acceptance_status") or "").strip().lower()
    duplicate = bool(
        item.get("duplicate")
        or item.get("deduplicated")
        or item.get("already_exists")
        or int(item.get("occurrence_count") or 0) > 1
    )
    privacy_blocked = bool(
        item.get("blocked")
        or item.get("privacy_blocked")
        or status in {"blocked", "rejected", "deleted", "invalidated"}
        or acceptance_status in {"rejected", "hidden"}
    )
    needs_review = acceptance_status in {"candidate", "needs_review", "review"}
    pending = status == "pending"
    if privacy_blocked:
        return "该内容未保存：当前隐私或记忆策略不允许写入。", "privacy_blocked"
    if duplicate and needs_review:
        return "这条记忆已存在，仍处于待审核状态；未新增重复记录。", "duplicate_review"
    if duplicate and pending:
        return "这条记忆已存在，仍处于待处理状态；未新增重复记录。", "duplicate_pending"
    if duplicate:
        return "这条记忆已存在，未新增重复记录。", "duplicate"
    if needs_review:
        return "已提交这条记忆，当前待审核；审核通过前不会用于正常召回。", "review"
    if pending:
        return "已提交这条记忆，当前待处理；生效后才会用于正常召回。", "pending"
    if status == "active":
        return f"已记住：{content}", "saved"
    return "该内容未确认落库，请稍后查看记忆列表后重试。", "not_saved"


def _forget_residual_count(result: dict) -> int:
    for key in ("residual_count", "remaining_count", "residuals_count"):
        if key in result:
            try:
                return max(0, int(result.get(key) or 0))
            except (TypeError, ValueError):
                return 0
    residual = result.get(
        "residuals",
        result.get("residual", result.get("residual_by_table")),
    )

    def count_residual(value: object) -> int:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return max(0, value)
        if isinstance(value, (list, tuple, set)):
            return len(value)
        if isinstance(value, dict):
            return sum(count_residual(item) for item in value.values())
        return 0

    if isinstance(residual, bool):
        return 1 if residual else 0
    if isinstance(residual, (list, tuple, set)):
        return count_residual(residual)
    if isinstance(residual, dict):
        return count_residual(residual)
    return 0


def _forget_result_feedback(result: dict) -> tuple[str, dict[str, object]]:
    count = max(0, int(result.get("count") or 0))
    residual_count = _forget_residual_count(result)
    partial = bool(
        result.get("partial")
        or result.get("complete") is False
        or residual_count > 0
    )
    observation: dict[str, object] = {
        "count": count,
        "ids": _safe_observation_ids(list(result.get("ids") or [])),
        "partial": partial,
        "residual_count": residual_count,
        "outcome": "partial" if partial else ("deleted" if count else "not_found"),
    }
    if count <= 0:
        if partial:
            return (
                "未删除匹配记忆记录，且本次清理未完整完成；请稍后重试或到记忆管理页检查。",
                observation,
            )
        return "没有找到匹配的记忆", observation
    base = f"已删除 {count} 条匹配记忆记录"
    if partial:
        if residual_count:
            return (
                f"{base}，但仍检测到 {residual_count} 项相关残留，尚未完全清除。",
                observation,
            )
        return f"{base}，但本次操作仅部分完成，仍可能存在相关残留。", observation
    return f"{base}。相关摘要或派生信息可能需要稍后完成清理。", observation


def _full_forget_result_feedback(result: object) -> tuple[str, dict[str, object]]:
    if isinstance(result, bool):
        payload: dict[str, object] = {"count": int(result), "complete": True}
    elif isinstance(result, int):
        payload = {"count": max(0, result), "complete": True}
    elif isinstance(result, dict):
        payload = dict(result)
    else:
        dumper = getattr(result, "model_dump", None)
        payload = dict(dumper()) if callable(dumper) else {}

    count = 0
    for key in (
        "count",
        "changed",
        "deleted",
        "deleted_count",
        "affected",
        "affected_count",
    ):
        if key not in payload:
            continue
        try:
            count = max(0, int(payload.get(key) or 0))
        except (TypeError, ValueError):
            count = 0
        break
    residual_count = _forget_residual_count(payload)
    status = str(payload.get("status") or "").strip().lower()
    explicit_complete = payload.get("complete")
    partial = bool(
        payload.get("partial")
        or explicit_complete is False
        or residual_count > 0
        or status in {"partial", "incomplete", "failed", "error"}
    )
    complete = bool(
        not partial
        and (
            explicit_complete is True
            or isinstance(result, int | bool)
        )
    )
    observation: dict[str, object] = {
        "count": count,
        "ids": [],
        "complete": complete,
        "partial": partial,
        "residual_count": residual_count,
        "outcome": (
            "partial"
            if partial
            else ("deleted_all" if complete and count else "already_empty" if complete else "unknown")
        ),
    }
    if partial:
        if residual_count:
            return (
                f"已清理 {count} 项记忆数据，但仍检测到 {residual_count} 项残留，"
                "尚未完全清除；请稍后重试或到记忆管理页检查。",
                observation,
            )
        return (
            f"已清理 {count} 项记忆数据，但本次全量清理未完整完成；"
            "请稍后重试或到记忆管理页检查。",
            observation,
        )
    if complete:
        if count:
            return f"已清除你的全部记忆数据（共 {count} 项）。", observation
        return "已完成全量清理，当前没有可删除的记忆数据。", observation
    return (
        "本次全量清理结果未确认，请稍后重试或到记忆管理页检查。",
        observation,
    )


async def handle_memory_control_intent(store: MemoryStore, ctx: PipelineContext) -> str | None:
    _memory_runtime_signal(ctx)
    if ctx.event.message.type != MessageType.TEXT:
        ctx.signals.setdefault(
            "memory_control",
            {"matched": False, "reason": "non_text"},
        )
        return None
    if bool(ctx.event.metadata.get("is_self_sent")):
        ctx.signals.setdefault(
            "memory_control",
            {"matched": False, "reason": "self_sent"},
        )
        return None
    text = _current_user_text(ctx)
    if not text:
        return None
    scope = _memory_control_scope(ctx)
    decision = decision_from_pre(ctx.pre)

    remember_content = _parse_remember_intent(text, decision=decision)
    if remember_content:
        is_group = _is_group_session(ctx)
        member_privacy = await _group_member_privacy(store, ctx)
        if is_group and (
            member_privacy is None
            or not _group_memory_capture_allowed(
                member_privacy,
                session_id=ctx.event.session_id,
            )
        ):
            ctx.signals.setdefault("memory", {})["member_capture_blocked"] = True
            ctx.signals["memory_control"] = {
                "matched": True,
                "intent": "remember",
                "blocked": True,
                "reason": "member_privacy_blocked",
            }
            ctx.extras["memory_control_handled"] = True
            return "当前群未开启成员记忆，未保存。"

        private_block_reason = await _private_member_memory_write_block_reason(store, ctx)
        if private_block_reason is not None:
            ctx.signals.setdefault("memory", {})["member_capture_blocked"] = True
            ctx.signals["memory_control"] = {
                "matched": True,
                "intent": "remember",
                "blocked": True,
                "reason": private_block_reason,
            }
            ctx.extras["memory_control_handled"] = True
            return "当前未开启个人记忆，未保存。"

        audience_context = (
            _memory_audience_write_context(ctx, member_privacy)
            if is_group
            else {}
        )
        try:
            item = await store.create_memory_item(
                tenant_id=scope["tenant_id"],
                channel=scope["channel"],
                source_key=scope["source_key"],
                user_id=scope["user_id"],
                # A command issued in a group is never promoted into the member's
                # private identity memory. It remains bound to this group session
                # and carries the immutable audience contract used by every read
                # path. Private-chat commands retain the historical identity scope.
                session_id=ctx.event.session_id if is_group else "",
                scope_type="session" if is_group else "identity",
                source_type="explicit_user",
                memory_type="note",
                content=remember_content,
                value_json={},
                confidence=1.0,
                status="active",
                pinned=False,
                priority=50,
                sensitivity="normal",
                source_trace_id=ctx.event.trace_id,
                original_text=text,
                **audience_context,
            )
        except MemoryItemConflictError:
            ctx.signals["memory_control"] = {
                "matched": True,
                "intent": "remember",
                "outcome": "duplicate",
                "ids": [],
            }
            ctx.extras["memory_control_handled"] = True
            return "这条记忆已存在，未新增重复记录。"
        except MemoryMutationError as exc:
            if str(exc.detail or "") != "member_memory_write_blocked":
                raise
            ctx.signals.setdefault("memory", {})["member_capture_blocked"] = True
            ctx.signals["memory_control"] = {
                "matched": True,
                "intent": "remember",
                "blocked": True,
                "reason": "member_control_blocked",
            }
            ctx.extras["memory_control_handled"] = True
            return "当前未开启个人记忆，未保存。"
        if not item:
            ctx.signals["memory_control"] = {
                "matched": True,
                "intent": "remember",
                "outcome": "not_saved",
                "reason": "write_not_confirmed",
                "ids": [],
            }
            ctx.extras["memory_control_handled"] = True
            return "该内容未确认落库，请稍后查看记忆列表后重试。"
        reply, outcome = _remember_item_feedback(item, remember_content)
        ctx.signals["memory_control"] = {
            "matched": True,
            "intent": "remember",
            "ids": [item.get("id")] if item.get("id") is not None else [],
            "outcome": outcome,
            "item_status": _safe_runtime_label(item.get("status"), default="unknown"),
        }
        ctx.extras["memory_control_handled"] = True
        return reply

    if _parse_full_forget_intent(text, decision=decision):
        forget_member = getattr(store, "forget_member_detailed", None)
        if not callable(forget_member):
            forget_member = getattr(store, "forget_member", None)
        if not callable(forget_member):
            ctx.signals["memory_control"] = {
                "matched": True,
                "intent": "forget_all",
                "outcome": "unsupported",
                "reason": "full_forget_unavailable",
                "blocked": True,
                "ids": [],
            }
            ctx.extras["memory_control_handled"] = True
            return "当前无法执行全量记忆清理，请到记忆管理页操作。"
        operation_id = str(ctx.event.message_id or ctx.event.trace_id or "").strip()
        if not operation_id:
            operation_id = datetime.now(UTC).isoformat()
        operation_digest = hashlib.sha256(
            "\x1f".join(
                (
                    scope["tenant_id"],
                    scope["channel"],
                    scope["source_key"],
                    scope["user_id"],
                    operation_id,
                )
            ).encode("utf-8")
        ).hexdigest()
        idempotency_key = f"memory-control:forget-all:{operation_digest}"
        try:
            forget_kwargs = {
                "tenant_id": scope["tenant_id"],
                "session_id": scope["session_id"],
                "user_id": scope["user_id"],
                "idempotency_key": idempotency_key,
            }
            if _callable_supports_keyword(forget_member, "channel"):
                forget_kwargs["channel"] = scope["channel"]
            result = await forget_member(**forget_kwargs)
        except Exception as exc:
            ctx.signals["memory_control"] = {
                "matched": True,
                "intent": "forget_all",
                "outcome": "error",
                "reason": "full_forget_failed",
                "error_type": _safe_runtime_label(
                    exc.__class__.__name__,
                    default="error",
                ),
                "blocked": True,
                "ids": [],
            }
            ctx.extras["memory_control_handled"] = True
            return "全量记忆清理未完成，请稍后重试或到记忆管理页检查。"
        reply, forget_observation = _full_forget_result_feedback(result)
        ctx.signals["memory_control"] = {
            "matched": True,
            "intent": "forget_all",
            **forget_observation,
        }
        ctx.extras["memory_control_handled"] = True
        return reply

    forget_intent = _parse_forget_intent(text, decision=decision)
    if forget_intent is not None:
        query, item_id = forget_intent
        if item_id is not None:
            try:
                result = await _forget_memory_item_for_current_user(
                    store,
                    scope,
                    item_id,
                    session_id=scope["session_id"],
                )
                if (
                    int(result.get("count") or 0) == 0
                    and scope["session_id"]
                    and not _is_group_session(ctx)
                ):
                    result = await _forget_memory_item_for_current_user(
                        store,
                        scope,
                        item_id,
                        session_id="",
                    )
            except MemoryItemProtectedError:
                ctx.signals["memory_control"] = {
                    "matched": True,
                    "intent": "forget",
                    "protected": True,
                }
                ctx.extras["memory_control_handled"] = True
                return "匹配到受保护记忆，不能自动删除。请到记忆管理页面操作，或使用明确确认。"
            reply, forget_observation = _forget_result_feedback(result)
            ctx.signals["memory_control"] = {
                "matched": True,
                "intent": "forget",
                **forget_observation,
            }
            ctx.extras["memory_control_handled"] = True
            return reply

        if not query:
            ctx.signals["memory_control"] = {"matched": True, "intent": "forget", "count": 0}
            ctx.extras["memory_control_handled"] = True
            return "没有找到匹配的记忆"
        read_block_reason = await _memory_read_block_reason(store, ctx)
        candidate_details_redacted = read_block_reason is not None
        if candidate_details_redacted:
            ctx.signals.setdefault("memory", {})["member_recall_blocked"] = True
        candidates = await store.retrieve_memory_items(
            **scope,
            query=query,
            limit=MEMORY_CONTROL_CANDIDATE_LIMIT * 4,
            request_session_kind="group" if _is_group_session(ctx) else "private",
        )
        candidates = _filter_memory_control_matches(
            [item for item in candidates if _is_visible_memory_item(item)],
            query,
        )
        if not candidates:
            ctx.signals["memory_control"] = {
                "matched": True,
                "intent": "forget",
                "count": 0,
                "outcome": "not_found",
                "candidate_details_redacted": candidate_details_redacted,
                "reason": read_block_reason or "",
            }
            ctx.extras["memory_control_handled"] = True
            return "没有找到匹配的记忆"
        protected = [item for item in candidates if _is_protected_memory_item(item)]
        if protected:
            ctx.signals["memory_control"] = {
                "matched": True,
                "intent": "forget",
                "protected": True,
                "candidate_count": len(candidates),
                "candidate_details_redacted": candidate_details_redacted,
                "reason": read_block_reason or "",
            }
            ctx.extras["memory_control_handled"] = True
            if candidate_details_redacted:
                return (
                    "匹配到受保护记忆，不能自动删除。当前隐私设置不回显候选内容，"
                    "请到记忆管理页操作。"
                )
            return _format_memory_candidates(
                protected,
                header="匹配到受保护记忆，不能自动删除。请到记忆管理页面操作，或使用明确确认：",
            )
        if len(candidates) > 1:
            ctx.signals["memory_control"] = {
                "matched": True,
                "intent": "forget",
                "candidate_count": len(candidates),
                "candidate_details_redacted": candidate_details_redacted,
                "reason": read_block_reason or "",
            }
            ctx.extras["memory_control_handled"] = True
            if candidate_details_redacted:
                return (
                    "找到多条匹配记忆。当前隐私设置不回显候选内容；"
                    "如已知记忆 ID，可发送“忘记 #ID”，否则请到记忆管理页操作。"
                )
            return _format_memory_candidates(candidates)

        candidate_id = candidates[0].get("id")
        if candidate_id is None:
            ctx.signals["memory_control"] = {
                "matched": True,
                "intent": "forget",
                "count": 0,
                "outcome": "not_found",
                "candidate_details_redacted": candidate_details_redacted,
                "reason": read_block_reason or "",
            }
            ctx.extras["memory_control_handled"] = True
            return "没有找到匹配的记忆"
        result = await _forget_memory_item_for_current_user(
            store,
            scope,
            item_id=int(candidate_id),
            session_id=str(candidates[0].get("session_id") or ""),
        )
        reply, forget_observation = _forget_result_feedback(result)
        ctx.signals["memory_control"] = {
            "matched": True,
            "intent": "forget",
            "candidate_details_redacted": candidate_details_redacted,
            "reason": read_block_reason or "",
            **forget_observation,
        }
        ctx.extras["memory_control_handled"] = True
        return reply

    list_intent = _parse_list_intent(text, decision=decision)
    search_query = _parse_search_intent(text, decision=decision)
    if list_intent or search_query is not None:
        query_intent = "list" if list_intent else "search"
        effective_query = "" if list_intent else str(search_query or "")
        blocked_reply = await _blocked_memory_read_control_reply(
            store,
            ctx,
            intent=query_intent,
        )
        if blocked_reply is not None:
            return blocked_reply
        rows = await store.retrieve_memory_items(
            **scope,
            query=effective_query,
            limit=MEMORY_CONTROL_CANDIDATE_LIMIT * 4,
            request_session_kind="group" if _is_group_session(ctx) else "private",
        )
        visible = _filter_memory_control_matches(
            [item for item in rows if _is_visible_memory_item(item)],
            effective_query,
        )
        ctx.signals["memory_control"] = {
            "matched": True,
            "intent": query_intent,
            "count": len(visible),
            "candidate_count": len(rows),
            "ids": [
                item.get("id")
                for item in visible[:MEMORY_CONTROL_CANDIDATE_LIMIT]
                if item.get("id") is not None
            ],
            "truncated": len(visible) > MEMORY_CONTROL_CANDIDATE_LIMIT,
            "outcome": "found" if visible else "not_found",
        }
        ctx.extras["memory_control_handled"] = True
        return _format_search_results(visible)

    ctx.signals.setdefault("memory_control", {"matched": False, "reason": "no_intent"})
    return None


async def _attach_relevant_memory_items(
    store: MemoryStore,
    ctx: PipelineContext,
    profile: dict,
    *,
    tenant_id: str,
    channel: str,
    source_key: str,
    user_id: str,
    load_scope: str,
) -> None:
    settings = getattr(store, "settings", None)
    item_limit = _settings_int(
        settings,
        "memory_retrieval_top_k",
        6,
        minimum=1,
        maximum=20,
    )
    graph_budget = _settings_int(
        settings,
        "memory_graph_retrieval_budget_chars",
        600,
        minimum=100,
        maximum=3000,
    )
    if not _settings_bool(settings, "memory_retrieval_enabled", True):
        profile["relevant_memory_items"] = []
        profile["relevant_graph_facts"] = []
        profile["relevant_graph_episodes"] = []
        profile["retrieval_mode"] = "none"
        _record_memory_load_scope(
            ctx,
            scope=load_scope,
            profile=profile,
            retrieval_mode="none",
            candidate_count=0,
            budget_chars=0,
            truncated=False,
        )
        return
    hybrid_enabled = _settings_bool(settings, "memory_hybrid_retrieval_enabled", False)
    retrieve_hybrid = getattr(store, "retrieve_memory_hybrid", None)
    retrieve = getattr(store, "retrieve_memory_items", None)
    if retrieve is None and not (hybrid_enabled and retrieve_hybrid is not None):
        profile["relevant_memory_items"] = []
        profile["relevant_graph_facts"] = []
        profile["relevant_graph_episodes"] = []
        profile["retrieval_mode"] = "none"
        _record_memory_load_scope(
            ctx,
            scope=load_scope,
            profile=profile,
            retrieval_mode="none",
            candidate_count=0,
            budget_chars=0,
            truncated=False,
        )
        return
    query = _current_user_text(ctx)
    if hybrid_enabled and retrieve_hybrid is not None:
        hybrid = await retrieve_hybrid(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            session_id=ctx.event.session_id,
            query=query,
            limit=item_limit,
            fact_top_k=_settings_int(
                settings, "memory_graph_retrieval_fact_top_k", 3, minimum=1, maximum=10
            ),
            episode_top_k=_settings_int(
                settings, "memory_graph_retrieval_episode_top_k", 2, minimum=1, maximum=10
            ),
            budget_chars=graph_budget,
            include_graph=_settings_bool(settings, "memory_graph_retrieval_enabled", False),
            debug=True,
            request_session_kind="group" if _is_group_session(ctx) else "private",
        )
        if isinstance(hybrid, dict):
            profile["relevant_memory_items"] = _json_safe(hybrid.get("items") or [])
            profile["relevant_graph_facts"] = _json_safe(hybrid.get("facts") or [])
            profile["relevant_graph_episodes"] = _json_safe(hybrid.get("episodes") or [])
            profile["memory_graph_budget_chars"] = hybrid.get("budget_chars") or graph_budget
            profile["retrieval_mode"] = (
                "hybrid_graph"
                if _settings_bool(settings, "memory_graph_retrieval_enabled", False)
                else "hybrid"
            )
            debug = hybrid.get("debug") if isinstance(hybrid.get("debug"), dict) else {}
            candidate_count = max(
                len(profile["relevant_memory_items"]),
                int(debug.get("item_candidates") or 0),
            )
            truncated = candidate_count > len(profile["relevant_memory_items"])
            profile["retrieval_truncated"] = truncated
            _record_memory_load_scope(
                ctx,
                scope=load_scope,
                profile=profile,
                retrieval_mode=profile["retrieval_mode"],
                candidate_count=candidate_count,
                budget_chars=int(profile["memory_graph_budget_chars"] or 0),
                truncated=truncated,
            )
            return

    relevant_items = await retrieve(
        tenant_id=tenant_id,
        channel=channel,
        source_key=source_key,
        user_id=user_id,
        session_id=ctx.event.session_id,
        query=query,
        limit=item_limit,
        request_session_kind="group" if _is_group_session(ctx) else "private",
    )
    profile["relevant_memory_items"] = relevant_items
    profile["relevant_graph_facts"] = []
    profile["relevant_graph_episodes"] = []
    profile["retrieval_mode"] = "sql"
    profile["retrieval_truncated"] = len(relevant_items) >= item_limit
    if not _settings_bool(settings, "memory_graph_retrieval_enabled", False):
        _record_memory_load_scope(
            ctx,
            scope=load_scope,
            profile=profile,
            retrieval_mode="sql",
            candidate_count=len(relevant_items),
            budget_chars=0,
            truncated=bool(profile["retrieval_truncated"]),
        )
        return
    retrieve_graph = getattr(store, "retrieve_memory_graph", None)
    if retrieve_graph is None:
        _record_memory_load_scope(
            ctx,
            scope=load_scope,
            profile=profile,
            retrieval_mode="sql",
            candidate_count=len(relevant_items),
            budget_chars=0,
            truncated=bool(profile["retrieval_truncated"]),
        )
        return
    excluded_ids = [item.get("id") for item in relevant_items if isinstance(item, dict)]
    graph = await retrieve_graph(
        tenant_id=tenant_id,
        channel=channel,
        source_key=source_key,
        user_id=user_id,
        session_id=ctx.event.session_id,
        query=query,
        fact_top_k=_settings_int(
            settings, "memory_graph_retrieval_fact_top_k", 3, minimum=1, maximum=10
        ),
        episode_top_k=_settings_int(
            settings, "memory_graph_retrieval_episode_top_k", 2, minimum=1, maximum=10
        ),
        budget_chars=graph_budget,
        exclude_memory_item_ids=excluded_ids,
        request_session_kind="group" if _is_group_session(ctx) else "private",
    )
    if isinstance(graph, dict):
        profile["relevant_graph_facts"] = _json_safe(graph.get("facts") or [])
        profile["relevant_graph_episodes"] = _json_safe(graph.get("episodes") or [])
        profile["memory_graph_budget_chars"] = graph.get("budget_chars") or graph_budget
        profile["retrieval_mode"] = "graph"
    _record_memory_load_scope(
        ctx,
        scope=load_scope,
        profile=profile,
        retrieval_mode=str(profile.get("retrieval_mode") or "sql"),
        candidate_count=len(relevant_items),
        budget_chars=int(profile.get("memory_graph_budget_chars") or 0),
        truncated=bool(profile["retrieval_truncated"]),
    )


def _session_memory_payload(
    *,
    user_id: str,
    channel: str,
    source_key: str,
    session_id: str,
    profile: dict,
) -> dict:
    return {
        "user_id": user_id,
        "channel": channel,
        "source_key": source_key,
        "session_id": session_id,
        "short_term": profile.get("short_term_memory") or "",
        "session_summary": profile.get("session_summary") or "",
        "open_items": _json_safe(profile.get("open_items") or []),
        "decisions": _json_safe(profile.get("decisions") or []),
        "recent_turns": _json_safe(profile.get("recent_turns") or []),
        "last_compacted_at": _json_safe(profile.get("last_compacted_at")),
        "summary_version": profile.get("summary_version") or 1,
        "long_term": profile.get("long_term_memory") or "",
        "manual_notes": profile.get("manual_notes") or "",
        "identity_manual_notes": profile.get("identity_manual_notes") or "",
        "session_manual_notes": profile.get("session_manual_notes") or "",
        "message_count": profile.get("message_count") or 0,
        "identity_message_count": profile.get("identity_message_count") or 0,
        "session_message_count": profile.get("session_message_count") or 0,
        "imported_message_count": profile.get("imported_message_count") or 0,
        "last_session_id": profile.get("last_session_id") or "",
        "identity_profile": _json_safe(profile.get("identity_profile") or {}),
        "session_profile": _json_safe(profile.get("session_profile") or {}),
        "memory_items": _json_safe(profile.get("memory_items") or {}),
        "relevant_memory_items": _json_safe(profile.get("relevant_memory_items") or []),
        "relevant_graph_facts": _json_safe(profile.get("relevant_graph_facts") or []),
        "relevant_graph_episodes": _json_safe(profile.get("relevant_graph_episodes") or []),
        "memory_graph_budget_chars": profile.get("memory_graph_budget_chars") or 0,
    }


@dataclass
class MemoryContextHook:
    store: MemoryStore
    name: str = "memory.context_loader"
    point: HookPoint = HookPoint.BEFORE_CAPABILITY
    priority: int = 35
    error_policy: str = "fail_open"
    # The vector backend has a 2s default timeout. Keep enough headroom for
    # its fail-open SQL fallback before the hook runner cancels this load.
    timeout_seconds: float = 3.5
    load_timeout_seconds: float = 3.0

    async def run(self, ctx: PipelineContext) -> None:
        _memory_runtime_signal(ctx)
        if ctx.session is None:
            _set_memory_runtime_stage(
                ctx,
                "load",
                status="skipped",
                reason="no_session",
            )
            return
        # Session variables survive across turns. Clear last turn's projection
        # before any database await so this fail-open hook cannot feed stale
        # memory into the current prompt when the store is unavailable.
        ctx.extras.pop("user_memory_profile", None)
        ctx.extras.pop("group_memory_profile", None)
        ctx.session.variables.pop("user_memory", None)
        ctx.session.variables.pop("group_memory", None)
        _set_memory_runtime_stage(ctx, "load", status="loading", reason="started")
        try:
            await asyncio.wait_for(
                self._load_context(ctx),
                timeout=max(0.05, float(self.load_timeout_seconds)),
            )
        except TimeoutError:
            _set_memory_runtime_stage(
                ctx,
                "load",
                status="error",
                reason="timeout",
                error_type="timeout_error",
            )
            raise
        except Exception as exc:
            _set_memory_runtime_stage(
                ctx,
                "load",
                status="error",
                reason="load_failed",
                error_type=exc.__class__.__name__,
            )
            raise

    async def _load_profile(
        self,
        ctx: PipelineContext,
        *,
        tenant_id: str,
        channel: str,
        source_key: str,
        user_id: str,
        load_scope: str,
        request_session_kind: str,
    ) -> dict:
        profile = await self.store.get_runtime_profile(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            session_id=ctx.event.session_id,
            user_id=user_id,
            request_session_kind=request_session_kind,
        )
        if (
            load_scope == "member"
            and _is_group_session(ctx)
            and not _group_identity_memory_enabled(self.store)
        ):
            profile = _group_session_only_profile(
                profile,
                session_id=ctx.event.session_id,
            )
        await _attach_relevant_memory_items(
            self.store,
            ctx,
            profile,
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=user_id,
            load_scope=load_scope,
        )
        return profile

    async def _load_context(self, ctx: PipelineContext) -> None:
        assert ctx.session is not None
        private_block_reason = await _private_member_memory_write_block_reason(self.store, ctx)
        if private_block_reason is not None:
            memory_signal = ctx.signals.setdefault("memory", {})
            memory_signal["member_recall_blocked"] = True
            memory_signal["member_recall_block_reason"] = private_block_reason
            _set_memory_runtime_stage(
                ctx,
                "load",
                status="blocked",
                reason=private_block_reason,
            )
            return
        tenant_id, channel, source_key, user_id = _scope_from_ctx(ctx)
        member_privacy = await _group_member_privacy(self.store, ctx)
        recall_allowed = _group_memory_recall_allowed(
            member_privacy,
            session_id=ctx.event.session_id,
        )
        if not recall_allowed:
            ctx.signals.setdefault("memory", {})["member_recall_blocked"] = True
            _set_memory_runtime_stage(
                ctx,
                "load",
                status="blocked",
                reason="member_privacy_blocked",
            )
            return

        should_load_group = _should_load_group_memory(ctx, user_id)
        load_specs = [
            (
                "member",
                user_id,
                "group" if _is_group_session(ctx) else "private",
            )
        ]
        if should_load_group:
            load_specs.append(("group", GROUP_HISTORY_USER_ID_SCOPE, "group"))
        tasks = [
            asyncio.create_task(
                self._load_profile(
                    ctx,
                    tenant_id=tenant_id,
                    channel=channel,
                    source_key=source_key,
                    user_id=scope_user_id,
                    load_scope=load_scope,
                    request_session_kind=request_kind,
                )
            )
            for load_scope, scope_user_id, request_kind in load_specs
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        profiles: dict[str, dict] = {}
        errors: list[Exception] = []
        for (load_scope, _scope_user_id, _request_kind), result in zip(
            load_specs,
            results,
            strict=True,
        ):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, Exception):
                errors.append(result)
                load_signal = _memory_runtime_signal(ctx).setdefault("load", {})
                scopes = load_signal.setdefault("scopes", {})
                scopes[load_scope] = {
                    "status": "error",
                    "reason": "scope_load_failed",
                    "error_type": _safe_runtime_label(
                        result.__class__.__name__,
                        default="error",
                    ),
                    "candidate_count": 0,
                    "selected_count": 0,
                    "selected_item_ids": [],
                    "selected_graph_fact_ids": [],
                    "selected_graph_episode_ids": [],
                    "budget_chars": 0,
                    "truncated": False,
                }
                continue
            profiles[load_scope] = result
        _refresh_memory_load_aggregate(ctx)
        if not profiles and errors:
            raise errors[0]

        profile = profiles.get("member")
        if profile is not None:
            ctx.extras["user_memory_profile"] = profile
            ctx.session.variables["user_memory"] = _session_memory_payload(
                user_id=user_id,
                channel=channel,
                source_key=source_key,
                session_id=ctx.event.session_id,
                profile=profile,
            )
        if _is_group_session(ctx) and not _group_identity_memory_enabled(self.store):
            ctx.signals.setdefault("memory", {})["audience_scope"] = "group_session_only"

        group_profile = profiles.get("group")
        if group_profile is not None:
            ctx.extras["group_memory_profile"] = group_profile
            ctx.session.variables["group_memory"] = _session_memory_payload(
                user_id=GROUP_HISTORY_USER_ID_SCOPE,
                channel=channel,
                source_key=source_key,
                session_id=ctx.event.session_id,
                profile=group_profile,
            )
        _set_memory_runtime_stage(
            ctx,
            "load",
            status="partial" if errors else "loaded",
            reason="scope_load_failed" if errors else "ok",
        )


@dataclass
class MemoryControlHook:
    store: MemoryStore
    name: str = "memory.control_intents"
    point: HookPoint = HookPoint.BEFORE_ROUTE
    # The channel reply policy runs at priority 20.  Memory commands must not
    # mutate state for a group message the bot was configured to ignore.
    priority: int = 25
    error_policy: str = "fail_closed"

    async def run(self, ctx: PipelineContext) -> None:
        try:
            reply = await handle_memory_control_intent(self.store, ctx)
        except Exception as exc:
            runtime = _memory_runtime_signal(ctx)
            runtime["control"] = {
                "status": "error",
                "intent": "unknown",
                "reason": "control_failed",
                "error_type": _safe_runtime_label(
                    exc.__class__.__name__,
                    default="error",
                ),
                "blocked": True,
                "protected": False,
                "candidate_count": 0,
                "selected_ids": [],
            }
            raise
        _sync_memory_control_runtime(ctx)
        if reply is None:
            return
        raise HookAbort(reply, reason="memory_control_intent")


@dataclass
class MemoryPersistenceHook:
    store: MemoryStore
    name: str = "memory.persistence"
    point: HookPoint = HookPoint.AFTER_POSTPROCESS
    priority: int = 98
    error_policy: str = "fail_open"

    async def run(self, ctx: PipelineContext) -> None:
        _memory_runtime_signal(ctx)
        if ctx.extras.get("memory_control_handled"):
            _mark_memory_save_suppressed(ctx, "memory_control_handled")
            return
        if _is_observation_only(ctx):
            ctx.signals.setdefault("memory", {})["observation_only_skipped"] = True
            _mark_memory_save_suppressed(ctx, "observation_only")
            return
        suppression_reason = _memory_save_suppression_reason(ctx)
        if suppression_reason is not None:
            _mark_memory_save_suppressed(ctx, suppression_reason)
            return
        if ctx.reply is None or ctx.pre is None:
            _mark_memory_save_suppressed(ctx, "missing_turn_context")
            return
        user_text = str(ctx.pre.cleaned_text or ctx.event.message.content or "").strip()
        assistant_text = str(ctx.reply.primary_text or "").strip()
        if not user_text or not assistant_text:
            if not assistant_text:
                _mark_memory_save_suppressed(ctx, "empty_assistant_reply")
            else:
                _mark_memory_save_suppressed(ctx, "empty_user_text")
            return
        user_text, assistant_text = _mask_memory_turn_text(ctx, user_text, assistant_text)
        tenant_id, channel, source_key, user_id = _scope_from_ctx(ctx)
        member_privacy = await _group_member_privacy(self.store, ctx)
        if not _group_memory_capture_allowed(
            member_privacy,
            session_id=ctx.event.session_id,
        ):
            ctx.signals.setdefault("memory", {})["member_capture_blocked"] = True
            _mark_memory_save_suppressed(ctx, "member_privacy_blocked")
            return
        before = ctx.extras.get("user_memory_profile")
        try:
            remember_kwargs = {
                "tenant_id": tenant_id,
                "channel": channel,
                "source_key": source_key,
                "user_id": user_id,
                "session_id": ctx.event.session_id,
                "user_text": user_text,
                "assistant_text": assistant_text,
                "trace_id": ctx.event.trace_id,
                "source_message_id": ctx.event.message_id,
                **_memory_audience_write_context(ctx, member_privacy),
            }
            if _is_group_session(ctx) and not _group_identity_memory_enabled(self.store):
                remember_kwargs["identity_scope"] = False
            profile = await self.store.remember_interaction(
                **remember_kwargs,
            )
            if _is_group_session(ctx) and not _group_identity_memory_enabled(self.store):
                profile = _group_session_only_profile(
                    profile,
                    session_id=ctx.event.session_id,
                )
        except Exception as exc:
            # Reply delivery is the primary transaction.  A best-effort memory
            # projection must never turn an already queued answer into a second
            # "busy" response from the top-level degradation path.
            logger.exception(
                "memory.persistence_failed",
                session_id=ctx.event.session_id,
                user_id=user_id,
                trace_id=ctx.event.trace_id,
                error=str(exc),
            )
            ctx.signals.setdefault("memory", {})["persistence_failed"] = True
            _set_memory_runtime_stage(
                ctx,
                "save",
                status="error",
                reason="persistence_failed",
                error_type=exc.__class__.__name__,
            )
            return
        if not isinstance(profile, dict) or not profile:
            _mark_memory_save_suppressed(ctx, "store_returned_empty")
            return
        if (
            int(profile.get("message_count") or 0) <= 0
            and int(profile.get("identity_message_count") or 0) <= 0
            and int(profile.get("session_message_count") or 0) <= 0
        ):
            _mark_memory_save_suppressed(ctx, "store_policy_blocked")
            return
        ctx.extras["user_memory_profile"] = profile
        if ctx.session is not None:
            ctx.session.variables["user_memory"] = _session_memory_payload(
                user_id=user_id,
                channel=channel,
                source_key=source_key,
                session_id=ctx.event.session_id,
                profile=profile,
            )
        _sync_memory_signal(ctx)
        _set_memory_runtime_stage(
            ctx,
            "save",
            status="success",
            reason="saved" if profile != before else "unchanged",
        )


def _sync_memory_signal(ctx: PipelineContext) -> dict:
    profile = ctx.extras.get("user_memory_profile")
    payload = dict(profile) if isinstance(profile, dict) else {}
    ctx.signals.setdefault("memory", {})["user_profile"] = build_safe_memory_profile_signal(
        payload
    )
    return payload


def _is_observation_only(ctx: PipelineContext) -> bool:
    reply_policy = ctx.extras.get("wxbot_reply_policy")
    return bool(
        ctx.extras.get("interaction_mode") == "observed"
        or (isinstance(reply_policy, dict) and reply_policy.get("allowed") is False)
    )


def _memory_save_suppression_reason(ctx: PipelineContext) -> str | None:
    result_metadata = dict(ctx.result.metadata or {}) if ctx.result is not None else {}
    if bool(ctx.extras.get("skip_assistant_turn")) or bool(
        result_metadata.get("skip_assistant_turn")
    ):
        return "skip_assistant_turn"
    if bool(
        result_metadata.get("suppress_outbound")
        or result_metadata.get("suppress_final_reply")
    ):
        return "result_suppressed_outbound"
    if bool(ctx.extras.get("suppress_outbound")):
        # The wxbot adapter suppresses the generic outbound stream after a
        # successful SDK enqueue. That reply was delivered and remains a valid
        # memory turn; only a suppression with no queued reply is unsent.
        queued_count = int(ctx.extras.get("wxbot_reply_queued_count") or 0)
        if queued_count <= 0:
            return "suppress_outbound"
    outbound_policy = (
        ctx.signals.get("channel", {}).get("wechat", {}).get("outbound_policy")
    )
    if isinstance(outbound_policy, dict) and (
        bool(outbound_policy.get("skip_assistant_turn"))
        or (
            bool(outbound_policy.get("suppress_outbound"))
            and not bool(outbound_policy.get("queued"))
        )
    ):
        return "outbound_not_queued"
    return None


def _mark_memory_save_suppressed(ctx: PipelineContext, reason: str) -> None:
    memory_signal = ctx.signals.setdefault("memory", {})
    memory_signal["save_suppressed"] = True
    memory_signal["save_suppression_reason"] = reason
    _set_memory_runtime_stage(
        ctx,
        "save",
        status="skipped",
        reason=reason,
    )


def _mask_memory_turn_text(
    ctx: PipelineContext,
    user_text: str,
    assistant_text: str,
) -> tuple[str, str]:
    masked_user, user_pii = detect_and_mask(user_text)
    masked_assistant, assistant_pii = detect_and_mask(assistant_text)
    if user_pii or assistant_pii:
        ctx.signals.setdefault("memory", {})["persistence_pii_masked"] = {
            "user": len(user_pii),
            "assistant": len(assistant_pii),
        }
    return masked_user, masked_assistant


def _memory_save_payload(
    ctx: PipelineContext,
    store: MemoryStore | None = None,
    member_privacy: MemberPrivacyValues | None = None,
) -> dict | None:
    if ctx.extras.get("memory_control_handled"):
        _mark_memory_save_suppressed(ctx, "memory_control_handled")
        return None
    if _is_observation_only(ctx):
        ctx.signals.setdefault("memory", {})["observation_only_skipped"] = True
        _mark_memory_save_suppressed(ctx, "observation_only")
        return None
    suppression_reason = _memory_save_suppression_reason(ctx)
    if suppression_reason is not None:
        _mark_memory_save_suppressed(ctx, suppression_reason)
        return None
    if ctx.reply is None or ctx.pre is None:
        _mark_memory_save_suppressed(ctx, "missing_turn_context")
        return None
    tenant_id, channel, source_key, user_id = _scope_from_ctx(ctx)
    user_text = str(ctx.pre.cleaned_text or "").strip()
    if not user_text:
        user_text = str(ctx.event.message.content or "").strip()
    if not user_text:
        _mark_memory_save_suppressed(ctx, "empty_user_text")
        return None
    assistant_text = str(ctx.reply.primary_text or "").strip()
    if not assistant_text:
        _mark_memory_save_suppressed(ctx, "empty_assistant_reply")
        return None
    user_text, assistant_text = _mask_memory_turn_text(ctx, user_text, assistant_text)
    payload = {
        "tenant_id": tenant_id,
        "channel": channel,
        "source_key": source_key,
        "session_id": ctx.event.session_id,
        "user_id": user_id,
        "user_text": user_text,
        "assistant_text": assistant_text,
        "trace_id": ctx.event.trace_id,
        "source_message_id": ctx.event.message_id,
        **_memory_audience_write_context(ctx, member_privacy),
    }
    if _is_group_session(ctx) and store is not None and not _group_identity_memory_enabled(store):
        payload["identity_scope"] = False
    return payload


def _apply_memory_profile(ctx: PipelineContext, profile: dict) -> None:
    ctx.extras["user_memory_profile"] = profile
    if ctx.session is not None:
        _tenant_id, channel, source_key, user_id = _scope_from_ctx(ctx)
        ctx.session.variables["user_memory"] = _session_memory_payload(
            user_id=user_id,
            channel=channel,
            source_key=source_key,
            session_id=ctx.event.session_id,
            profile=profile,
        )
    _sync_memory_signal(ctx)


async def _remember_interaction_from_payload(
    store: MemoryStore,
    payload: dict,
) -> dict:
    identity_scope = bool(payload.get("identity_scope", True))
    remember_kwargs = {
        "tenant_id": str(payload.get("tenant_id") or ""),
        "channel": str(payload.get("channel") or ""),
        "source_key": str(payload.get("source_key") or "*"),
        "user_id": str(payload.get("user_id") or ""),
        "session_id": str(payload.get("session_id") or ""),
        "user_text": str(payload.get("user_text") or ""),
        "assistant_text": str(payload.get("assistant_text") or ""),
        "trace_id": str(payload.get("trace_id") or ""),
        "source_message_id": str(payload.get("source_message_id") or ""),
        "origin_session_kind": str(payload.get("origin_session_kind") or "unknown"),
        "audience_scope": str(payload.get("audience_scope") or "private"),
        "allowed_session_ids": list(payload.get("allowed_session_ids") or []),
        "sensitivity_category": str(payload.get("sensitivity_category") or "normal"),
        "expires_at": payload.get("expires_at"),
        "source_kind": str(payload.get("source_kind") or "conversation"),
    }
    if not identity_scope:
        remember_kwargs["identity_scope"] = False
    profile = await store.remember_interaction(
        **remember_kwargs,
    )
    if not identity_scope:
        return _group_session_only_profile(
            profile,
            session_id=str(payload.get("session_id") or ""),
        )
    return profile


def _save_memory_effect(
    ctx: PipelineContext, payload: dict, profile: dict | None = None
) -> MessageEffect:
    effect_payload = dict(payload)
    profile = profile or {}
    effect_payload.update(
        {
            "message_count": profile.get("message_count") or 0,
            "identity_message_count": profile.get("identity_message_count") or 0,
            "session_message_count": profile.get("session_message_count") or 0,
        }
    )
    source_event_key = str(
        effect_payload.get("source_message_id")
        or effect_payload.get("trace_id")
        or ""
    )
    return MessageEffect(
        type="save_memory",
        owner="memory",
        payload=effect_payload,
        idempotency_key=(
            "memory:save:"
            f"{effect_payload['tenant_id']}:{effect_payload['channel']}:"
            f"{effect_payload['source_key']}:{effect_payload['session_id']}:"
            f"{effect_payload['user_id']}:{source_event_key}"
        ),
    )


@dataclass
class MemoryLoadStep:
    store: MemoryStore
    kind: str = "plugin.memory.load"
    owner: str = "memory"
    name: str = "Load user memory"
    permissions: list[str] = field(default_factory=lambda: ["storage:shared"])
    inputs: set[str] = field(default_factory=lambda: {"event", "session", "pre", "route"})
    outputs: set[str] = field(default_factory=lambda: {"signals.memory.user_profile"})
    timeout_seconds: float = 3.5
    error_policy: str = "fail_open"

    async def run(self, ctx: PipelineContext) -> StepResult:
        await MemoryContextHook(self.store).run(ctx)
        profile = _sync_memory_signal(ctx)
        return StepResult(reason="loaded" if profile else "no_session")


@dataclass
class MemoryControlStep:
    store: MemoryStore
    kind: str = "plugin.memory.control_intents"
    owner: str = "memory"
    name: str = "Handle memory control intents"
    permissions: list[str] = field(default_factory=lambda: ["storage:shared"])
    inputs: set[str] = field(default_factory=lambda: {"event", "session", "pre"})
    outputs: set[str] = field(default_factory=lambda: {"signals.memory_control", "result"})
    timeout_seconds: float = 2.0
    error_policy: str = "fail_closed"

    async def run(self, ctx: PipelineContext) -> StepResult:
        try:
            reply = await handle_memory_control_intent(self.store, ctx)
        except Exception as exc:
            runtime = _memory_runtime_signal(ctx)
            runtime["control"] = {
                "status": "error",
                "intent": "unknown",
                "reason": "control_failed",
                "error_type": _safe_runtime_label(
                    exc.__class__.__name__,
                    default="error",
                ),
                "blocked": True,
                "protected": False,
                "candidate_count": 0,
                "selected_ids": [],
            }
            raise
        _sync_memory_control_runtime(ctx)
        if reply is None:
            reason = str(ctx.signals.get("memory_control", {}).get("reason") or "no_intent")
            return StepResult(reason=reason)
        return StepResult(
            action="stop",
            reason="memory_control_intent",
            result=CapabilityResult(
                route=RouteType.CANNED,
                reply_text=reply,
                metadata={"response_guard_allow_echo": True},
            ),
            finalize=True,
            skip_output_safety=True,
            route_label=RouteType.CANNED.value,
        )


@dataclass
class MemorySaveStep:
    store: MemoryStore
    effect_handler_enabled: bool = False
    kind: str = "plugin.memory.save"
    owner: str = "memory"
    name: str = "Save user memory"
    permissions: list[str] = field(default_factory=lambda: ["storage:shared"])
    inputs: set[str] = field(default_factory=lambda: {"event", "session", "pre", "reply"})
    outputs: set[str] = field(default_factory=lambda: {"effects.save_memory"})
    timeout_seconds: float = 2.0
    error_policy: str = "fail_open"

    async def run(self, ctx: PipelineContext) -> StepResult:
        _memory_runtime_signal(ctx)
        member_privacy = await _group_member_privacy(self.store, ctx)
        if not _group_memory_capture_allowed(
            member_privacy,
            session_id=ctx.event.session_id,
        ):
            ctx.signals.setdefault("memory", {})["member_capture_blocked"] = True
            _mark_memory_save_suppressed(ctx, "member_privacy_blocked")
            return StepResult(reason="member_privacy_blocked")
        payload = _memory_save_payload(ctx, self.store, member_privacy)
        if payload is None:
            return StepResult(reason="not_saved")
        if self.effect_handler_enabled or effect_handler_opt_in_enabled(
            ctx,
            effect_type="save_memory",
            owner="memory",
        ):
            _set_memory_runtime_stage(
                ctx,
                "save",
                status="pending",
                reason="effect_pending",
            )
            return StepResult(
                reason="effect_pending",
                effects=[_save_memory_effect(ctx, payload)],
            )

        before = ctx.extras.get("user_memory_profile")
        try:
            profile = await _remember_interaction_from_payload(self.store, payload)
        except Exception as exc:
            _set_memory_runtime_stage(
                ctx,
                "save",
                status="error",
                reason="persistence_failed",
                error_type=exc.__class__.__name__,
            )
            raise
        if not profile:
            _mark_memory_save_suppressed(ctx, "store_returned_empty")
            return StepResult(reason="not_saved")
        if (
            int(profile.get("message_count") or 0) <= 0
            and int(profile.get("identity_message_count") or 0) <= 0
            and int(profile.get("session_message_count") or 0) <= 0
        ):
            _mark_memory_save_suppressed(ctx, "store_policy_blocked")
            return StepResult(reason="not_saved")
        _apply_memory_profile(ctx, profile)
        reason = "saved" if profile != before else "unchanged"
        _set_memory_runtime_stage(
            ctx,
            "save",
            status="success",
            reason=reason,
        )
        return StepResult(reason=reason, effects=[_save_memory_effect(ctx, payload, profile)])
