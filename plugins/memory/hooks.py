"""
Memory hooks.

Loads per-user memory before capability execution and persists updated memory
after a reply is produced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

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
from app.social.contracts import MemberPrivacyValues
from plugins.memory.store import GROUP_HISTORY_USER_ID_SCOPE, MemoryItemProtectedError, MemoryStore

logger = get_logger(__name__)


MEMORY_CONTROL_CANDIDATE_LIMIT = 5
_REMEMBER_PATTERNS = (
    re.compile(r"^(?:请)?记住[：:\s]*(?P<content>.+)$"),
    re.compile(r"^帮我记一下[：:\s]*(?P<content>.+)$"),
    re.compile(r"^以后记住[：:\s]*(?P<content>.+)$"),
)
_FORGET_PATTERNS = (
    re.compile(r"^忘记[：:\s]*(?P<query>.+)$"),
    re.compile(r"^删除记忆[：:\s]*(?P<query>.+)$"),
    re.compile(r"^别记这个[：:\s]*(?P<query>.*)$"),
)
_SEARCH_PATTERNS = (
    re.compile(r"^我有哪些记忆[？?]?$"),
    re.compile(r"^查一下我的记忆[：:\s]*(?P<query>.*)$"),
    re.compile(r"^搜索记忆[：:\s]*(?P<query>.*)$"),
)
_ID_RE = re.compile(r"^(?:#|id[:：]?)\s*(?P<id>\d+)$", re.I)


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


def _parse_remember_intent(text: str) -> str | None:
    text_value = _normalize_memory_control_text(text)
    if text_value.startswith("我记得"):
        return None
    for pattern in _REMEMBER_PATTERNS:
        match = pattern.match(text_value)
        if match:
            content = _trim_memory_text(match.group("content"))
            return content or None
    return None


def _parse_forget_intent(text: str) -> tuple[str, int | None] | None:
    text_value = _normalize_memory_control_text(text)
    for pattern in _FORGET_PATTERNS:
        match = pattern.match(text_value)
        if not match:
            continue
        query = _trim_memory_text(match.group("query"))
        item_id: int | None = None
        id_match = _ID_RE.match(query)
        if id_match:
            item_id = int(id_match.group("id"))
        return query, item_id
    return None


def _parse_search_intent(text: str) -> str | None:
    text_value = _normalize_memory_control_text(text)
    for pattern in _SEARCH_PATTERNS:
        match = pattern.match(text_value)
        if match:
            return _trim_memory_text(match.groupdict().get("query") or "", limit=120)
    return None


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


async def handle_memory_control_intent(store: MemoryStore, ctx: PipelineContext) -> str | None:
    if ctx.event.message.type != MessageType.TEXT:
        return None
    if bool(ctx.event.metadata.get("is_self_sent")):
        return None
    text = _current_user_text(ctx)
    if not text:
        return None
    scope = _memory_control_scope(ctx)

    remember_content = _parse_remember_intent(text)
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

        audience_context = (
            _memory_audience_write_context(ctx, member_privacy)
            if is_group
            else {}
        )
        item = await store.create_memory_item(
            tenant_id=scope["tenant_id"],
            channel=scope["channel"],
            source_key=scope["source_key"],
            user_id=scope["user_id"],
            # A command issued in a group is never promoted into the member's
            # private identity memory.  It remains bound to this group session
            # and carries the immutable audience contract used by every read
            # path.  Private-chat commands retain the historical identity scope.
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
        if not item:
            return "没有可记住的内容"
        ctx.signals["memory_control"] = {
            "matched": True,
            "intent": "remember",
            "ids": [item.get("id")] if item.get("id") is not None else [],
        }
        ctx.extras["memory_control_handled"] = True
        return f"已记住：{remember_content}"

    forget_intent = _parse_forget_intent(text)
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
                if int(result.get("count") or 0) == 0 and scope["session_id"]:
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
            count = int(result.get("count") or 0)
            ctx.signals["memory_control"] = {
                "matched": True,
                "intent": "forget",
                "count": count,
                "ids": list(result.get("ids") or []),
            }
            ctx.extras["memory_control_handled"] = True
            return "已忘记 1 条记忆" if count == 1 else "没有找到匹配的记忆"

        if not query:
            ctx.signals["memory_control"] = {"matched": True, "intent": "forget", "count": 0}
            ctx.extras["memory_control_handled"] = True
            return "没有找到匹配的记忆"
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
            ctx.signals["memory_control"] = {"matched": True, "intent": "forget", "count": 0}
            ctx.extras["memory_control_handled"] = True
            return "没有找到匹配的记忆"
        protected = [item for item in candidates if _is_protected_memory_item(item)]
        if protected:
            ctx.signals["memory_control"] = {
                "matched": True,
                "intent": "forget",
                "protected": True,
                "candidate_count": len(candidates),
            }
            ctx.extras["memory_control_handled"] = True
            return _format_memory_candidates(
                protected,
                header="匹配到受保护记忆，不能自动删除。请到记忆管理页面操作，或使用明确确认：",
            )
        if len(candidates) > 1:
            ctx.signals["memory_control"] = {
                "matched": True,
                "intent": "forget",
                "candidate_count": len(candidates),
            }
            ctx.extras["memory_control_handled"] = True
            return _format_memory_candidates(candidates)

        candidate_id = candidates[0].get("id")
        if candidate_id is None:
            ctx.signals["memory_control"] = {"matched": True, "intent": "forget", "count": 0}
            ctx.extras["memory_control_handled"] = True
            return "没有找到匹配的记忆"
        result = await _forget_memory_item_for_current_user(
            store,
            scope,
            item_id=int(candidate_id),
            session_id=str(candidates[0].get("session_id") or ""),
        )
        count = int(result.get("count") or 0)
        ctx.signals["memory_control"] = {
            "matched": True,
            "intent": "forget",
            "count": count,
            "ids": list(result.get("ids") or []),
        }
        ctx.extras["memory_control_handled"] = True
        return "已忘记 1 条记忆" if count == 1 else "没有找到匹配的记忆"

    search_query = _parse_search_intent(text)
    if search_query is not None:
        rows = await store.retrieve_memory_items(
            **scope,
            query=search_query,
            limit=MEMORY_CONTROL_CANDIDATE_LIMIT * 4,
            request_session_kind="group" if _is_group_session(ctx) else "private",
        )
        visible = _filter_memory_control_matches(
            [item for item in rows if _is_visible_memory_item(item)],
            search_query,
        )
        ctx.signals["memory_control"] = {
            "matched": True,
            "intent": "search",
            "count": len(visible),
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
) -> None:
    settings = getattr(store, "settings", None)
    if not _settings_bool(settings, "memory_retrieval_enabled", True):
        profile["relevant_memory_items"] = []
        profile["relevant_graph_facts"] = []
        profile["relevant_graph_episodes"] = []
        return
    hybrid_enabled = _settings_bool(settings, "memory_hybrid_retrieval_enabled", False)
    retrieve_hybrid = getattr(store, "retrieve_memory_hybrid", None)
    retrieve = getattr(store, "retrieve_memory_items", None)
    if retrieve is None and not (hybrid_enabled and retrieve_hybrid is not None):
        profile["relevant_memory_items"] = []
        profile["relevant_graph_facts"] = []
        profile["relevant_graph_episodes"] = []
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
            limit=_settings_int(settings, "memory_retrieval_top_k", 6, minimum=1, maximum=20),
            fact_top_k=_settings_int(
                settings, "memory_graph_retrieval_fact_top_k", 3, minimum=1, maximum=10
            ),
            episode_top_k=_settings_int(
                settings, "memory_graph_retrieval_episode_top_k", 2, minimum=1, maximum=10
            ),
            budget_chars=_settings_int(
                settings, "memory_graph_retrieval_budget_chars", 600, minimum=100, maximum=3000
            ),
            include_graph=_settings_bool(settings, "memory_graph_retrieval_enabled", False),
            request_session_kind="group" if _is_group_session(ctx) else "private",
        )
        if isinstance(hybrid, dict):
            profile["relevant_memory_items"] = _json_safe(hybrid.get("items") or [])
            profile["relevant_graph_facts"] = _json_safe(hybrid.get("facts") or [])
            profile["relevant_graph_episodes"] = _json_safe(hybrid.get("episodes") or [])
            profile["memory_graph_budget_chars"] = hybrid.get("budget_chars") or _settings_int(
                settings,
                "memory_graph_retrieval_budget_chars",
                600,
                minimum=100,
                maximum=3000,
            )
            return

    relevant_items = await retrieve(
        tenant_id=tenant_id,
        channel=channel,
        source_key=source_key,
        user_id=user_id,
        session_id=ctx.event.session_id,
        query=query,
        limit=_settings_int(settings, "memory_retrieval_top_k", 6, minimum=1, maximum=20),
        request_session_kind="group" if _is_group_session(ctx) else "private",
    )
    profile["relevant_memory_items"] = relevant_items
    profile["relevant_graph_facts"] = []
    profile["relevant_graph_episodes"] = []
    if not _settings_bool(settings, "memory_graph_retrieval_enabled", False):
        return
    retrieve_graph = getattr(store, "retrieve_memory_graph", None)
    if retrieve_graph is None:
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
        budget_chars=_settings_int(
            settings, "memory_graph_retrieval_budget_chars", 600, minimum=100, maximum=3000
        ),
        exclude_memory_item_ids=excluded_ids,
        request_session_kind="group" if _is_group_session(ctx) else "private",
    )
    if isinstance(graph, dict):
        profile["relevant_graph_facts"] = _json_safe(graph.get("facts") or [])
        profile["relevant_graph_episodes"] = _json_safe(graph.get("episodes") or [])
        profile["memory_graph_budget_chars"] = graph.get("budget_chars") or _settings_int(
            settings,
            "memory_graph_retrieval_budget_chars",
            600,
            minimum=100,
            maximum=3000,
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

    async def run(self, ctx: PipelineContext) -> None:
        if ctx.session is None:
            return
        tenant_id, channel, source_key, user_id = _scope_from_ctx(ctx)
        member_privacy = await _group_member_privacy(self.store, ctx)
        recall_allowed = _group_memory_recall_allowed(
            member_privacy,
            session_id=ctx.event.session_id,
        )
        if recall_allowed:
            profile = await self.store.get_runtime_profile(
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                session_id=ctx.event.session_id,
                user_id=user_id,
                request_session_kind="group" if _is_group_session(ctx) else "private",
            )
            await _attach_relevant_memory_items(
                self.store,
                ctx,
                profile,
                tenant_id=tenant_id,
                channel=channel,
                source_key=source_key,
                user_id=user_id,
            )
        else:
            profile = {}
            ctx.signals.setdefault("memory", {})["member_recall_blocked"] = True
        if (
            recall_allowed
            and _is_group_session(ctx)
            and not _group_identity_memory_enabled(self.store)
        ):
            profile = _group_session_only_profile(
                profile,
                session_id=ctx.event.session_id,
            )
            ctx.signals.setdefault("memory", {})["audience_scope"] = "group_session_only"
        if recall_allowed:
            ctx.extras["user_memory_profile"] = profile
            ctx.session.variables["user_memory"] = _session_memory_payload(
                user_id=user_id,
                channel=channel,
                source_key=source_key,
                session_id=ctx.event.session_id,
                profile=profile,
            )
        else:
            ctx.extras.pop("user_memory_profile", None)
            ctx.session.variables.pop("user_memory", None)
        if not _should_load_group_memory(ctx, user_id):
            ctx.extras.pop("group_memory_profile", None)
            ctx.session.variables.pop("group_memory", None)
            return

        group_profile = await self.store.get_runtime_profile(
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            session_id=ctx.event.session_id,
            user_id=GROUP_HISTORY_USER_ID_SCOPE,
            request_session_kind="group",
        )
        await _attach_relevant_memory_items(
            self.store,
            ctx,
            group_profile,
            tenant_id=tenant_id,
            channel=channel,
            source_key=source_key,
            user_id=GROUP_HISTORY_USER_ID_SCOPE,
        )
        ctx.extras["group_memory_profile"] = group_profile
        ctx.session.variables["group_memory"] = _session_memory_payload(
            user_id=GROUP_HISTORY_USER_ID_SCOPE,
            channel=channel,
            source_key=source_key,
            session_id=ctx.event.session_id,
            profile=group_profile,
        )


@dataclass
class MemoryControlHook:
    store: MemoryStore
    name: str = "memory.control_intents"
    point: HookPoint = HookPoint.BEFORE_ROUTE
    # The channel reply policy runs at priority 20.  Memory commands must not
    # mutate state for a group message the bot was configured to ignore.
    priority: int = 25

    async def run(self, ctx: PipelineContext) -> None:
        reply = await handle_memory_control_intent(self.store, ctx)
        if reply is None:
            return
        raise HookAbort(reply, reason="memory_control_intent")


@dataclass
class MemoryPersistenceHook:
    store: MemoryStore
    name: str = "memory.persistence"
    point: HookPoint = HookPoint.AFTER_POSTPROCESS
    priority: int = 98

    async def run(self, ctx: PipelineContext) -> None:
        if ctx.extras.get("memory_control_handled"):
            return
        if _is_observation_only(ctx):
            ctx.signals.setdefault("memory", {})["observation_only_skipped"] = True
            return
        if ctx.reply is None or ctx.pre is None:
            return
        user_text = str(ctx.pre.cleaned_text or ctx.event.message.content or "").strip()
        assistant_text = str(ctx.reply.primary_text or "").strip()
        if not user_text:
            return
        tenant_id, channel, source_key, user_id = _scope_from_ctx(ctx)
        member_privacy = await _group_member_privacy(self.store, ctx)
        if not _group_memory_capture_allowed(
            member_privacy,
            session_id=ctx.event.session_id,
        ):
            ctx.signals.setdefault("memory", {})["member_capture_blocked"] = True
            return
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


def _sync_memory_signal(ctx: PipelineContext) -> dict:
    profile = ctx.extras.get("user_memory_profile")
    payload = dict(profile) if isinstance(profile, dict) else {}
    ctx.signals.setdefault("memory", {})["user_profile"] = payload
    return payload


def _is_observation_only(ctx: PipelineContext) -> bool:
    reply_policy = ctx.extras.get("wxbot_reply_policy")
    return bool(
        ctx.extras.get("interaction_mode") == "observed"
        or (isinstance(reply_policy, dict) and reply_policy.get("allowed") is False)
    )


def _memory_save_payload(
    ctx: PipelineContext,
    store: MemoryStore | None = None,
    member_privacy: MemberPrivacyValues | None = None,
) -> dict | None:
    if ctx.extras.get("memory_control_handled"):
        return None
    if _is_observation_only(ctx):
        ctx.signals.setdefault("memory", {})["observation_only_skipped"] = True
        return None
    if ctx.reply is None or ctx.pre is None:
        return None
    tenant_id, channel, source_key, user_id = _scope_from_ctx(ctx)
    user_text = str(ctx.pre.cleaned_text or "").strip()
    if not user_text:
        user_text = str(ctx.event.message.content or "").strip()
    if not user_text:
        return None
    payload = {
        "tenant_id": tenant_id,
        "channel": channel,
        "source_key": source_key,
        "session_id": ctx.event.session_id,
        "user_id": user_id,
        "user_text": user_text,
        "assistant_text": str(ctx.reply.primary_text or "").strip(),
        "trace_id": ctx.event.trace_id,
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
    return MessageEffect(
        type="save_memory",
        owner="memory",
        payload=effect_payload,
        idempotency_key=(
            "memory:save:"
            f"{effect_payload['tenant_id']}:{effect_payload['channel']}:"
            f"{effect_payload['source_key']}:{effect_payload['session_id']}:"
            f"{effect_payload['user_id']}:{effect_payload['trace_id']}"
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
    timeout_seconds: float = 1.5
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
        reply = await handle_memory_control_intent(self.store, ctx)
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
        member_privacy = await _group_member_privacy(self.store, ctx)
        if not _group_memory_capture_allowed(
            member_privacy,
            session_id=ctx.event.session_id,
        ):
            ctx.signals.setdefault("memory", {})["member_capture_blocked"] = True
            return StepResult(reason="member_privacy_blocked")
        payload = _memory_save_payload(ctx, self.store, member_privacy)
        if payload is None:
            return StepResult(reason="not_saved")
        if self.effect_handler_enabled or effect_handler_opt_in_enabled(
            ctx,
            effect_type="save_memory",
            owner="memory",
        ):
            return StepResult(
                reason="effect_pending",
                effects=[_save_memory_effect(ctx, payload)],
            )

        before = ctx.extras.get("user_memory_profile")
        profile = await _remember_interaction_from_payload(self.store, payload)
        _apply_memory_profile(ctx, profile)
        if not profile:
            return StepResult(reason="not_saved")
        reason = "saved" if profile != before else "unchanged"
        return StepResult(reason=reason, effects=[_save_memory_effect(ctx, payload, profile)])
