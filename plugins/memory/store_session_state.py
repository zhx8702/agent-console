"""Pure compaction and commitment tracking for session-scoped memory."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

SESSION_RECENT_TURN_LIMIT = 8
SESSION_OPEN_ITEM_LIMIT = 12
SESSION_DECISION_LIMIT = 20
SESSION_SUMMARY_LIMIT = 1200
SESSION_COMPACTED_CONTEXT_LIMIT = 600
SESSION_COMPACTED_SNIPPET_LIMIT = 10
SESSION_STATE_VERSION = 1


def _normalize_line(value: str) -> str:
    return " ".join(str(value or "").strip().split())


def _normalize_key(value: str) -> str:
    normalized = _normalize_line(value).lower()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def _safe_json_loads(raw: Any, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except Exception:
        return fallback


def _memory_retrieval_tokens(query: str) -> list[str]:
    text_value = _normalize_line(query).lower()
    if not text_value:
        return []
    raw_tokens = re.findall(r"[a-z0-9][a-z0-9_-]{1,40}|[\u4e00-\u9fff]{2,}", text_value)
    tokens: list[str] = []
    for raw_token in raw_tokens:
        if re.fullmatch(r"[\u4e00-\u9fff]{2,}", raw_token):
            candidates: list[str] = []
            if len(raw_token) <= 4:
                candidates.append(raw_token)
            for size in (4, 3, 2):
                if len(raw_token) < size:
                    continue
                candidates.extend(
                    raw_token[start : start + size]
                    for start in range(0, len(raw_token) - size + 1)
                )
            for token in candidates:
                if token not in tokens:
                    tokens.append(token)
                if len(tokens) >= 12:
                    return tokens
            continue
        if raw_token not in tokens:
            tokens.append(raw_token)
        if len(tokens) >= 12:
            return tokens
    if not tokens and len(text_value) >= 2:
        tokens.append(text_value[:40])
    return tokens[:12]


def _build_short_term_summary(items: list[dict[str, str]]) -> str:
    lines: list[str] = []
    for item in items[-6:]:
        user_text = _normalize_line(item.get("user_text") or "")
        assistant_text = _normalize_line(item.get("assistant_text") or "")
        if user_text:
            lines.append(f"用户最近说：{user_text}")
        if assistant_text:
            lines.append(f"系统最近回复：{assistant_text}")
    return "\n".join(lines[-8:])[:2000]


def _bounded_text(value: Any, limit: int) -> str:
    return _normalize_line(str(value or ""))[:limit]


def _session_sentence_chunks(user_text: str, assistant_text: str = "") -> list[str]:
    text_value = "\n".join(part for part in (user_text, assistant_text) if str(part or "").strip())
    chunks: list[str] = []
    for raw in re.split(r"[\n。！？!?;；]+", text_value):
        chunk = _bounded_text(raw, 240)
        if chunk:
            chunks.append(chunk)
    return chunks[:8]


def _session_item_key(value: str) -> str:
    normalized = _normalize_line(value).lower()
    normalized = re.sub(
        r"^(?:todo|to do|待办|下一步|next step|决定|decided|confirmed|确认)[:：\s-]*",
        "",
        normalized,
    )
    return _normalize_key(normalized)


def _as_session_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        value = _safe_json_loads(value, [])
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            rows.append(dict(item))
        elif isinstance(item, str) and _normalize_line(item):
            rows.append({"text": _normalize_line(item)})
    return rows


_OPEN_ITEM_MARKERS = (
    "todo", "to do", "need continue", "need to continue", "next step", "follow up",
    "later", "unfinished", "待办", "需要继续", "继续处理", "下一步", "稍后", "晚点",
    "未完成", "还没完成", "后续",
)
_DECISION_MARKERS = (
    "decided", "confirmed", "adopt", "not use", "won't use", "决定", "确认", "采用",
    "选择", "改用", "就用", "不用", "不使用",
)
_CLOSE_ITEM_MARKERS = (
    "done", "completed", "finished", "cancel", "cancelled", "canceled", "已完成",
    "已经完成", "完成了", "做完", "搞定", "已搞定", "取消", "关闭",
)


def _has_marker(text_value: str, markers: tuple[str, ...]) -> bool:
    normalized = _normalize_line(text_value)
    if not normalized:
        return False
    lowered = normalized.lower()
    if (
        re.search(r"[?？]\s*$", lowered)
        or re.search(r"(?:吗|么)\s*$", lowered)
        or re.search(r"(?:如何|怎么|什么|是否|能否|可否|有没有|是不是)", lowered)
    ):
        return False
    for marker in markers:
        value = marker.strip().lower()
        if not value:
            continue
        if value.isascii():
            if re.search(rf"(?<![a-z0-9_]){re.escape(value)}(?![a-z0-9_])", lowered):
                return True
        elif value in lowered:
            return True
    return False


def _item_overlap(left: str, right: str) -> int:
    left_tokens = set(_memory_retrieval_tokens(left))
    right_tokens = set(_memory_retrieval_tokens(right))
    if left_tokens and right_tokens:
        return len(left_tokens & right_tokens)
    left_norm = _normalize_line(left).lower()
    right_norm = _normalize_line(right).lower()
    if left_norm and right_norm and (left_norm in right_norm or right_norm in left_norm):
        return 1
    return 0


def _append_unique_session_item(
    items: list[dict[str, Any]],
    *,
    text: str,
    created_at: str,
    limit: int,
    extra: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    normalized = _bounded_text(text, 240)
    if not normalized:
        return items[-limit:]
    key = _session_item_key(normalized)
    for item in items:
        if item.get("key") == key or _session_item_key(str(item.get("text") or "")) == key:
            item["text"] = normalized
            item["updated_at"] = created_at
            if extra:
                item.update(extra)
            return items[-limit:]
    row: dict[str, Any] = {
        "key": key,
        "text": normalized,
        "created_at": created_at,
        "updated_at": created_at,
    }
    if extra:
        row.update(extra)
    return [*items, row][-limit:]


def _close_matching_open_items(
    open_items: list[dict[str, Any]],
    *,
    text: str,
    created_at: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    active_items = [item for item in open_items if str(item.get("status") or "open") == "open"]
    if not active_items:
        return open_items, []
    scored = [
        (_item_overlap(text, str(item.get("text") or "")), index, item)
        for index, item in enumerate(open_items)
    ]
    scored.sort(key=lambda row: row[0], reverse=True)
    close_indexes: set[int] = set()
    if scored and scored[0][0] > 0:
        close_indexes.add(scored[0][1])
    if not close_indexes:
        return open_items, []
    next_items: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    for index, item in enumerate(open_items):
        if index in close_indexes:
            updated = dict(item)
            updated["status"] = "closed"
            updated["closed_at"] = created_at
            updated["updated_at"] = created_at
            updated["closed_by"] = _bounded_text(text, 240)
            closed.append(updated)
            continue
        next_items.append(item)
    return next_items[-SESSION_OPEN_ITEM_LIMIT:], closed


def _extract_compacted_context(summary: Any) -> str:
    for raw_line in str(summary or "").splitlines():
        line = _normalize_line(raw_line)
        if line.lower().startswith("earlier context:"):
            return _normalize_line(line.split(":", 1)[1])
    return ""


def _merge_compacted_context(
    *,
    previous_summary: Any,
    evicted_turns: list[dict[str, Any]],
) -> str:
    snippets = [
        _bounded_text(snippet, 180)
        for snippet in _extract_compacted_context(previous_summary).split(" / ")
        if _bounded_text(snippet, 180)
    ]
    for turn in evicted_turns:
        user_text = _bounded_text(turn.get("user_text"), 180)
        if user_text:
            snippets.append(user_text)

    deduplicated: list[str] = []
    seen: set[str] = set()
    for snippet in snippets:
        key = _normalize_line(snippet).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        deduplicated.append(snippet)

    selected: list[str] = []
    used = 0
    for snippet in reversed(deduplicated):
        added = len(snippet) + (3 if selected else 0)
        if selected and used + added > SESSION_COMPACTED_CONTEXT_LIMIT:
            break
        selected.append(snippet[:SESSION_COMPACTED_CONTEXT_LIMIT])
        used += added
        if len(selected) >= SESSION_COMPACTED_SNIPPET_LIMIT:
            break
    return " / ".join(reversed(selected))[:SESSION_COMPACTED_CONTEXT_LIMIT]


def _build_session_summary(
    *,
    recent_turns: list[dict[str, Any]],
    open_items: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    compacted_context: str = "",
) -> str:
    lines: list[str] = []
    active_open = [
        _bounded_text(item.get("text"), 160)
        for item in open_items
        if str(item.get("status") or "open") == "open" and _bounded_text(item.get("text"), 160)
    ]
    recent_decisions = [_bounded_text(item.get("text"), 160) for item in decisions[-5:]]
    recent_user_lines = [
        _bounded_text(item.get("user_text"), 160)
        for item in recent_turns[-3:]
        if _bounded_text(item.get("user_text"), 160)
    ]
    if active_open:
        lines.append("Open items: " + "; ".join(active_open[:5]))
    if recent_decisions:
        lines.append("Decisions: " + "; ".join(recent_decisions[-5:]))
    if recent_user_lines:
        lines.append("Recent context: " + " / ".join(recent_user_lines))
    if compacted_context:
        lines.append("Earlier context: " + _bounded_text(compacted_context, 600))
    return "\n".join(lines)[:SESSION_SUMMARY_LIMIT]


def _update_session_state(
    profile: dict[str, Any],
    *,
    session_id: str,
    user_text: str,
    assistant_text: str,
    created_at: str,
) -> dict[str, Any]:
    recent_turns = _as_session_list(profile.get("recent_turns") or profile.get("recent_turns_json"))
    if not recent_turns:
        recent_turns = _as_session_list(
            profile.get("short_term_items") or profile.get("short_term_items_json")
        )
    open_items = _as_session_list(profile.get("open_items") or profile.get("open_items_json"))
    decisions = _as_session_list(profile.get("decisions") or profile.get("decisions_json"))

    recent_turns.append(
        {
            "session_id": session_id,
            "user_text": _bounded_text(user_text, 500),
            "assistant_text": _bounded_text(assistant_text, 500),
            "created_at": created_at,
        }
    )
    evicted_turns = recent_turns[:-SESSION_RECENT_TURN_LIMIT]
    recent_turns = recent_turns[-SESSION_RECENT_TURN_LIMIT:]
    compacted_context = _merge_compacted_context(
        previous_summary=profile.get("session_summary"),
        evicted_turns=evicted_turns,
    )
    previous_summary_version = max(
        SESSION_STATE_VERSION,
        int(profile.get("summary_version") or SESSION_STATE_VERSION),
    )
    last_compacted_at = profile.get("last_compacted_at")
    summary_version = previous_summary_version
    if evicted_turns:
        last_compacted_at = created_at
        summary_version = previous_summary_version + 1

    # Assistant suggestions stay in context but never become user commitments.
    for chunk in _session_sentence_chunks(user_text):
        if _has_marker(chunk, _OPEN_ITEM_MARKERS):
            open_items = _append_unique_session_item(
                open_items,
                text=chunk,
                created_at=created_at,
                limit=SESSION_OPEN_ITEM_LIMIT,
                extra={"status": "open"},
            )
        if _has_marker(chunk, _CLOSE_ITEM_MARKERS):
            open_items, closed_items = _close_matching_open_items(
                open_items, text=chunk, created_at=created_at
            )
            if closed_items:
                for item in closed_items:
                    decisions = _append_unique_session_item(
                        decisions,
                        text=f"Closed open item: {item.get('text') or chunk}",
                        created_at=created_at,
                        limit=SESSION_DECISION_LIMIT,
                        extra={"kind": "close"},
                    )
            else:
                decisions = _append_unique_session_item(
                    decisions,
                    text=chunk,
                    created_at=created_at,
                    limit=SESSION_DECISION_LIMIT,
                    extra={"kind": "close"},
                )
        elif _has_marker(chunk, _DECISION_MARKERS):
            decisions = _append_unique_session_item(
                decisions,
                text=chunk,
                created_at=created_at,
                limit=SESSION_DECISION_LIMIT,
                extra={"kind": "decision"},
            )

    open_items = [item for item in open_items if str(item.get("status") or "open") == "open"][
        -SESSION_OPEN_ITEM_LIMIT:
    ]
    decisions = decisions[-SESSION_DECISION_LIMIT:]
    return {
        "recent_turns": recent_turns,
        "open_items": open_items,
        "decisions": decisions,
        "session_summary": _build_session_summary(
            recent_turns=recent_turns,
            open_items=open_items,
            decisions=decisions,
            compacted_context=compacted_context,
        ),
        "last_compacted_at": last_compacted_at,
        "summary_version": summary_version,
    }
