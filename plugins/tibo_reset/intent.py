
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.common.intent import IntentDecision, IntentDomain
from app.common.intent_runtime import is_confident


class TiboResetIntentType(StrEnum):
    NONE = "none"
    WEEK_COUNT = "week_count"
    TODAY_STATUS = "today_status"
    LATEST = "latest"
    RETENTION = "retention"
    SUMMARY = "summary"


@dataclass(frozen=True)
class TiboResetIntent:
    type: TiboResetIntentType
    confidence: float
    normalized_text: str

    @property
    def should_handle(self) -> bool:
        return self.type != TiboResetIntentType.NONE


_MENTION_PREFIX_RE = re.compile(r"^\s*(?:@\S+[\s\u2000-\u200a\u202f\u205f\u3000]+)+")
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_SPACE_RE = re.compile(r"\s+")


def normalize_query_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = _INVISIBLE_RE.sub("", value)
    value = _MENTION_PREFIX_RE.sub("", value)
    lowered = value.lower()
    for typo, canonical in (
        ("codxe", "codex"),
        ("cdoex", "codex"),
        ("coedx", "codex"),
        ("codx", "codex"),
        ("code x", "codex"),
        ("chatgpt work", "chatgpt work"),
    ):
        if typo in lowered:
            index = lowered.find(typo)
            value = value[:index] + canonical + value[index + len(typo) :]
            lowered = value.lower()
    return _SPACE_RE.sub(" ", value).strip()


def _intent_from_decision(
    text: str,
    decision: IntentDecision | None,
) -> TiboResetIntent:
    normalized = normalize_query_text(text)
    if decision is None or decision.domain is not IntentDomain.TIBO_RESET:
        return TiboResetIntent(TiboResetIntentType.NONE, 0.0, normalized)
    if not is_confident(decision):
        return TiboResetIntent(TiboResetIntentType.NONE, decision.confidence, normalized)
    try:
        intent_type = TiboResetIntentType(decision.action)
    except ValueError:
        return TiboResetIntent(TiboResetIntentType.NONE, decision.confidence, normalized)
    if intent_type is TiboResetIntentType.NONE:
        return TiboResetIntent(TiboResetIntentType.NONE, decision.confidence, normalized)
    return TiboResetIntent(intent_type, decision.confidence, normalized)


def classify_tibo_reset_intent(
    text: str,
    *,
    decision: IntentDecision | None = None,
) -> TiboResetIntent:
    return _intent_from_decision(text, decision)


def classify_tibo_reset_followup(
    text: str,
    previous_intent: TiboResetIntent,
    *,
    decision: IntentDecision | None = None,
) -> TiboResetIntent:
    """Resolve a follow-up from the current semantic decision."""

    _ = previous_intent
    return _intent_from_decision(text, decision)


def _as_int(stats: dict[str, Any], key: str) -> int:
    try:
        return max(0, int(stats.get(key) or 0))
    except (TypeError, ValueError):
        return 0


def _latest_line(stats: dict[str, Any]) -> str:
    raw = stats.get("latest_reset_at")
    if not raw:
        return ""
    try:
        value = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        timezone_name = str(stats.get("timezone") or "UTC")
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            timezone = ZoneInfo("UTC")
        local_value = value.astimezone(timezone)
        line = f"最近一次公告：{local_value:%m-%d %H:%M}（{timezone_name}）"
    except (TypeError, ValueError):
        line = f"最近一次公告：{raw}"
    source_url = str(stats.get("latest_source_url") or "").strip()
    if source_url:
        line = f"{line}\n原文：{source_url}"
    return line


def _week_line(stats: dict[str, Any]) -> str:
    week_count = _as_int(stats, "week_count")
    everyone = _as_int(stats, "week_everyone_count")
    weekly = _as_int(stats, "week_everyone_weekly_usage_count")
    banked = _as_int(stats, "week_everyone_banked_reset_count")
    subset = _as_int(stats, "week_subset_count")
    details: list[str] = []
    if weekly:
        details.append(f"周额度 {weekly} 次")
    if banked:
        details.append(f"banked reset {banked} 次")
    everyone_detail = f"（{'、'.join(details)}）" if details else ""
    subset_detail = f"，另有 {subset} 次仅部分用户" if subset else ""
    return (
        f"本周（周一 00:00 起）有 {week_count} 次已确认重置公告："
        f"面向所有用户 {everyone} 次{everyone_detail}{subset_detail}。"
    )


def _today_line(stats: dict[str, Any]) -> str:
    today_count = _as_int(stats, "today_count")
    if today_count <= 0:
        return "今天暂未发现已确认的重置公告。"
    everyone = _as_int(stats, "today_everyone_count")
    weekly = _as_int(stats, "today_everyone_weekly_usage_count")
    banked = _as_int(stats, "today_everyone_banked_reset_count")
    subset = _as_int(stats, "today_subset_count")
    details: list[str] = []
    if weekly:
        details.append(f"周额度 {weekly} 次")
    if banked:
        details.append(f"banked reset {banked} 次")
    if subset:
        details.append(f"部分用户 {subset} 次")
    detail_text = f"（{'、'.join(details)}）" if details else ""
    return f"今天有 {today_count} 次已确认重置公告，面向所有用户 {everyone} 次{detail_text}。"


def format_tibo_reset_reply(
    intent: TiboResetIntent,
    stats: dict[str, Any],
) -> str:
    history_count = _as_int(stats, "history_count")
    if history_count <= 0:
        return "当前还没有已确认的重置公告记录；监测会继续保存后续记录。"

    latest = _latest_line(stats)
    if intent.type == TiboResetIntentType.RETENTION:
        return (
            f"会保留。目前数据库中有 {history_count} 条已确认记录，按推文 ID 去重持久化，"
            "正常重启或更新容器不会丢失；只有主动删除 PostgreSQL 数据卷才会清空。"
        )
    if intent.type == TiboResetIntentType.TODAY_STATUS:
        parts = [_today_line(stats), _week_line(stats)]
    elif intent.type == TiboResetIntentType.LATEST:
        parts = [latest or "当前还没有最近一次重置公告时间。", _today_line(stats)]
        latest = ""
    elif intent.type == TiboResetIntentType.WEEK_COUNT:
        parts = [_week_line(stats), _today_line(stats)]
    else:
        parts = [_week_line(stats), _today_line(stats)]
    if latest:
        parts.append(latest)
    parts.append("注：这里统计的是已确认公告，不代表每个账号的额度一定已经即时到账。")
    return "\n".join(part for part in parts if part)
