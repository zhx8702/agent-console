"""Help-desk overlay for Jev participation: funnel, channel conflicts, turn outcomes."""
from __future__ import annotations

import json
from typing import Any

from app.channel.reply_policy import match_reply_policy
from app.jev.models import JevPolicy, redact


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return []
        return decoded if isinstance(decoded, list) else []
    return []

LANES = ("observe", "reply_blocked", "reply_applied", "failed", "pending", "other")

CONFLICTS: dict[str, str] = {
    "participation_disabled": "群内求助评估已关闭",
    "not_in_help_sessions": "该群不在主动答疑白名单，Jev 不会判断求助",
    "jev_observe_only": "求助判断仍是观察模式，只记录建议，不会主动开口",
    "social_disabled": "群参与总开关未打开，Jev 判断了也不会发出去",
    "social_proactive_disabled": "该群主动参与未打开，Jev 不会判断求助",
    "channel_off": "微信回复策略是关闭，关键词和 Jev 都不会发出去",
    "contains_without_keywords": "回复模式是包含关键词，但当前没有关键词",
    "no_session_policy": "该群没有独立回复策略，继承全局默认",
}

OUTCOMES: dict[str, str] = {
    "sent": "微信已发出",
    "queued": "已进入发送队列",
    "send_failed": "发送失败",
    "suppressed": "参与策略拦截",
    "jev_blocked": "Jev 建议回复但未应用",
    "jev_observe": "Jev 判断继续旁观",
    "applied_not_sent": "Jev 已参与决策，未见发送记录",
    "no_delivery": "有处理记录，未见发送",
    "no_runtime": "没有对应的处理记录",
}


def _result(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("result")
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _audit(item: dict[str, Any]) -> dict[str, Any]:
    value = _result(item).get("_audit")
    return value if isinstance(value, dict) else {}


def _choice(item: dict[str, Any]) -> str:
    answers = _result(item).get("answers")
    decision = answers.get("decision") if isinstance(answers, dict) else None
    choice = decision.get("choice") if isinstance(decision, dict) else None
    return str(choice or "")


def evaluation_lane(item: dict[str, Any]) -> str:
    status = str(item.get("status") or "")
    error = str(item.get("error_type") or "")
    reason = str(_audit(item).get("reason") or "")
    if status in {"pending", "running"}:
        return "pending"
    if status == "failed" or error == "TimeoutError" or reason == "online_timeout":
        return "failed"
    choice = _choice(item)
    if choice == "observe":
        return "observe"
    if choice == "reply":
        return "reply_applied" if item.get("applied") else "reply_blocked"
    return "other"


def funnel_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    lanes = {name: 0 for name in LANES}
    blocked_reasons: dict[str, int] = {}
    for row in rows:
        lane = evaluation_lane(row)
        lanes[lane] += 1
        if lane == "reply_blocked":
            reason = str(_audit(row).get("reason") or "unknown")
            blocked_reasons[reason] = blocked_reasons.get(reason, 0) + 1
    return {
        "evaluated": len(rows),
        "lanes": lanes,
        "blocked_reasons": blocked_reasons,
    }


def desk_conflicts(
    policy: JevPolicy,
    session_id: str,
    channel: dict[str, Any] | None,
    *,
    aliases: list[str] | None = None,
    social: dict[str, Any] | None = None,
) -> list[str]:
    found: list[str] = []
    ids = [item for item in (aliases or [session_id]) if item]
    if not policy.participation:
        found.append("participation_disabled")
    if policy.participation_shadow_only:
        found.append("jev_observe_only")
    if social is not None:
        if not social.get("effective_enabled", True):
            found.append("social_disabled")
        elif not social.get("proactive_enabled"):
            found.append("social_proactive_disabled")
    if session_id:
        if not any(item in policy.help_sessions for item in ids):
            found.append("not_in_help_sessions")
        if channel is not None:
            if channel.get("reply_mode") == "off":
                found.append("channel_off")
            if channel.get("reply_mode") == "contains" and int(channel.get("keyword_count") or 0) == 0:
                found.append("contains_without_keywords")
            if not channel.get("has_session_row"):
                found.append("no_session_policy")
    return found


def build_desk(
    policy: JevPolicy,
    session_id: str,
    channel: dict[str, Any] | None,
    *,
    aliases: list[str] | None = None,
    social: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ids = [item for item in (aliases or [session_id]) if item]
    conflicts = desk_conflicts(policy, session_id, channel, aliases=ids, social=social)
    public_channel = None
    if channel is not None:
        public_channel = {
            "reply_mode": channel.get("reply_mode") or "off",
            "configured_reply_mode": channel.get("configured_reply_mode") or "inherit",
            "inherits_global_keywords": bool(channel.get("inherits_global_keywords")),
            "keyword_count": int(channel.get("keyword_count") or 0),
            "mention_sender": bool(channel.get("mention_sender")),
            "has_session_row": bool(channel.get("has_session_row")),
        }
    public_social = None
    if social is not None:
        public_social = {
            "effective_enabled": bool(social.get("effective_enabled")),
            "group_enabled": bool(social.get("group_enabled")),
            "proactive_enabled": bool(social.get("proactive_enabled")),
        }
    return {
        "session_id": session_id,
        "in_help_sessions": bool(ids) and any(item in policy.help_sessions for item in ids),
        "participation_enabled": bool(policy.enabled and policy.participation),
        "participation_shadow_only": bool(policy.participation_shadow_only),
        "min_confidence": policy.min_confidence,
        "channel": public_channel,
        "social": public_social,
        "conflicts": conflicts,
    }


def classify_turn(
    item: dict[str, Any],
    runtime: dict[str, Any] | None,
    *,
    keywords: list[str],
    reply_mode: str,
) -> dict[str, Any]:
    lane = evaluation_lane(item)
    content = None
    hidden = False
    if runtime is not None:
        hidden = bool(runtime.get("hidden"))
        raw = runtime.get("content")
        if raw is not None and not hidden:
            content = str(raw)
    keyword_hit = None
    keyword_reason = ""
    if content is not None and reply_mode:
        keyword_hit, keyword_reason = match_reply_policy(
            reply_mode, content, keywords, is_group=True
        )
    deliveries = _json_list((runtime or {}).get("deliveries"))
    decisions = _json_list((runtime or {}).get("decisions"))
    sent = any(str(row.get("status") or "") == "sent" for row in deliveries)
    queued = any(str(row.get("status") or "") in {"pending", "sending"} for row in deliveries)
    failed = any(str(row.get("status") or "") in {"failed", "cancelled"} for row in deliveries)
    latest = decisions[-1] if decisions else {}
    processing_status = str((runtime or {}).get("processing_status") or "")
    if sent:
        outcome = "sent"
    elif queued:
        outcome = "queued"
    elif failed:
        outcome = "send_failed"
    elif processing_status == "intentionally_suppressed":
        outcome = "suppressed"
    elif lane == "reply_blocked":
        outcome = "jev_blocked"
    elif lane == "observe":
        outcome = "jev_observe"
    elif lane == "reply_applied":
        outcome = "applied_not_sent"
    elif runtime:
        outcome = "no_delivery"
    else:
        outcome = "no_runtime"
    return {
        "lane": lane,
        "message": redact(content, limit=280) if content else None,
        "keyword_hit": keyword_hit,
        "keyword_reason": keyword_reason or None,
        "processing_status": processing_status or None,
        "processing_reason": redact(runtime.get("processing_reason"), limit=200) if runtime and runtime.get("processing_reason") else None,
        "participation_status": str(latest.get("status") or "") or None,
        "participation_reasons": [str(code) for code in (latest.get("reasons") or []) if str(code)],
        "delivery_status": "sent" if sent else ("queued" if queued else ("failed" if failed else None)),
        "outcome": outcome,
    }


def attach_turns(
    items: list[dict[str, Any]],
    runtime_by_trace: dict[str, dict[str, Any]],
    *,
    keywords: list[str],
    reply_mode: str,
) -> list[dict[str, Any]]:
    for item in items:
        if item.get("domain") != "participation":
            continue
        trace = str(_audit(item).get("trace_id") or "").strip()
        item["turn"] = classify_turn(
            item,
            runtime_by_trace.get(trace) if trace else None,
            keywords=keywords,
            reply_mode=reply_mode,
        )
    return items
