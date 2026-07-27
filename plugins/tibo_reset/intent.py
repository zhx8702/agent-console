
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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
_ASCII_RESET = r"(?<![A-Za-z])reset(?:s|ted)?(?![A-Za-z])"
_RESET_RE = re.compile(
    r"(?:重置|重制|刷新|回血|回满|放水|放(?:了)?(?:几|多少)(?:波|次)|"
    r"补(?:了)?(?:额度|限额|用量)|"
    r"恢复(?:了)?(?:额度|限额|用量)?|(?:额度|配额|限额|用量)(?:刷新|恢复|到账|回满)|"
    + _ASCII_RESET
    + r")",
    re.IGNORECASE,
)
_EXPLICIT_RESET_RE = re.compile(
    r"(?:重置|(?:额度|配额|限额|用量)(?:刷新|恢复|到账|回满)|"
    r"恢复(?:了)?(?:额度|配额|限额|用量)|"
    + _ASCII_RESET
    + r")",
    re.IGNORECASE,
)
_PRODUCT_SUBJECT_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"codex(?:\s+(?:cli|app))?|"
    r"chat\s*gpt(?:\s+(?:work|team|plus|pro))?"
    r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_UNRELATED_DOMAIN_RE = re.compile(
    r"(?:密码|设备(?:号)?|授权(?:码)?|激活码|插件|数据库|会话|上下文|配置|系统|容器|服务器)",
    re.IGNORECASE,
)
_HOW_TO_RE = re.compile(
    r"(?:怎么|如何|怎样|教程|命令|代码).{0,12}(?:重置|" + _ASCII_RESET + r")",
    re.IGNORECASE,
)
_RESET_HOW_TO_RE = re.compile(
    r"(?:重置|" + _ASCII_RESET + r").{0,10}(?:怎么|如何|怎样).{0,8}(?:操作|做|弄|执行)?",
    re.IGNORECASE,
)
_WEEK_RE = re.compile(
    r"(?:本周|这周|这个?星期|本星期|这星期|这个?礼拜|本礼拜|这礼拜|"
    r"一周内|近一周|this\s+week)",
    re.IGNORECASE,
)
_COUNT_RE = re.compile(
    r"(?:多少|几(?:次|回|波|轮|条)|次数|回数|波数|多少(?:次|回|波|轮)|"
    r"how\s+many|count)",
    re.IGNORECASE,
)
_TODAY_RE = re.compile(r"(?:今天|今日|今儿|当天|今早|今晚|today)", re.IGNORECASE)
_LATEST_RE = re.compile(
    r"(?:最近|最新|刚刚|刚才|上次|最后一次|什么时候|几点|多久前|"
    r"latest|last|when)",
    re.IGNORECASE,
)
_WHEN_RE = re.compile(
    r"(?:什么时候|啥时候|何时|几点|多久前|when)",
    re.IGNORECASE,
)
_RETENTION_RE = re.compile(
    r"(?:数据|记录|历史|公告).{0,8}(?:保留|保存|持久化|存多久|会丢|还在|查得到)|"
    r"(?:保留|保存|持久化|还在).{0,8}(?:数据|记录|历史|公告)",
    re.IGNORECASE,
)
_QUESTION_RE = re.compile(
    r"(?:[？?]|是否|有没有|有无|"
    r"(?:几|多少)(?:次|回|波|轮)?|"
    r"(?:吗|呢|了没|没(?:有)?|情况)\s*[？?]?$)"
)
_FOLLOWUP_MAX_CHARS = 28
_TODAY_FOLLOWUP_RE = re.compile(
    r"^(?:那|那么|然后|还有)?(?:今天|今日|今儿|当天|今早|今晚)"
    r"(?:呢|怎么样|有吗|有没|有没有|是否有|几(?:次|回|波|轮)|"
    r"多少(?:次|回|波|轮))?[？?]?$"
)
_WEEK_FOLLOWUP_RE = re.compile(
    r"^(?:那|那么|然后|还有)?(?:本周|这周|这个?星期|本星期|这星期|"
    r"这个?礼拜|本礼拜|这礼拜|一周内|近一周)"
    r"(?:呢|怎么样|有吗|有没|有没有|几(?:次|回|波|轮)|"
    r"多少(?:次|回|波|轮))?[？?]?$"
)
_LATEST_FOLLOWUP_RE = re.compile(
    r"^(?:那|那么|然后|还有)?(?:最近(?:一次)?|最新(?:一次)?|刚刚|刚才|上次|最后一次)"
    r"(?:呢|是什么时候|几点|多久前)?[？?]?$"
)
_COUNT_FOLLOWUP_RE = re.compile(
    r"^(?:那|那么|然后)?(?:一共|总共)?(?:有)?"
    r"(?:几(?:次|回|波|轮)|多少(?:次|回|波|轮))(?:了)?[？?]?$"
)

_NORMALIZATION_REPLACEMENTS = (
    (
        re.compile(
            r"(?<![A-Za-z])(?:codxe|cdoex|coedx|codx)(?![A-Za-z])",
            re.IGNORECASE,
        ),
        "codex",
    ),
    (re.compile(r"(?<![A-Za-z])code\s+x(?![A-Za-z])", re.IGNORECASE), "codex"),
    (
        re.compile(
            r"(?<![A-Za-z])chat\s*gpt\s*work(?![A-Za-z])",
            re.IGNORECASE,
        ),
        "chatgpt work",
    ),
)


def normalize_query_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = _INVISIBLE_RE.sub("", value)
    value = _MENTION_PREFIX_RE.sub("", value)
    for pattern, replacement in _NORMALIZATION_REPLACEMENTS:
        value = pattern.sub(replacement, value)
    return _SPACE_RE.sub(" ", value).strip()


def classify_tibo_reset_intent(text: str) -> TiboResetIntent:
    normalized = normalize_query_text(text)
    if not normalized:
        return TiboResetIntent(TiboResetIntentType.NONE, 0.0, normalized)
    # A direct Tibo lookup must identify the product it is asking about.  Bare
    # reset language is common in group chat (devices, databases, games, etc.)
    # and must continue through the normal conversation flow instead.
    if not _PRODUCT_SUBJECT_RE.search(normalized):
        return TiboResetIntent(TiboResetIntentType.NONE, 0.0, normalized)

    has_week = bool(_WEEK_RE.search(normalized))
    has_today = bool(_TODAY_RE.search(normalized))
    has_when = bool(_WHEN_RE.search(normalized))
    has_latest = has_when or bool(_LATEST_RE.search(normalized))
    has_count = bool(_COUNT_RE.search(normalized))
    has_question = bool(_QUESTION_RE.search(normalized))
    status_query = (has_week and has_count) or (
        has_today and has_question
    ) or (
        has_latest and (has_when or has_question)
    )
    if (
        _UNRELATED_DOMAIN_RE.search(normalized)
        and _RESET_RE.search(normalized)
    ) or (
        (_HOW_TO_RE.search(normalized) or _RESET_HOW_TO_RE.search(normalized))
        and not status_query
    ):
        return TiboResetIntent(TiboResetIntentType.NONE, 0.0, normalized)

    has_reset = bool(_RESET_RE.search(normalized))
    has_explicit_reset = bool(_EXPLICIT_RESET_RE.search(normalized))
    if (
        _RETENTION_RE.search(normalized)
        and has_explicit_reset
        and has_question
    ):
        return TiboResetIntent(TiboResetIntentType.RETENTION, 0.99, normalized)
    if has_week and has_count and has_reset:
        return TiboResetIntent(TiboResetIntentType.WEEK_COUNT, 0.99, normalized)
    if has_latest and has_reset and (has_when or has_question):
        return TiboResetIntent(TiboResetIntentType.LATEST, 0.97, normalized)
    if has_today and has_reset and has_question:
        return TiboResetIntent(TiboResetIntentType.TODAY_STATUS, 0.99, normalized)
    if has_reset and has_question:
        return TiboResetIntent(TiboResetIntentType.SUMMARY, 0.92, normalized)
    return TiboResetIntent(TiboResetIntentType.NONE, 0.0, normalized)


def classify_tibo_reset_followup(
    text: str,
    previous_intent: TiboResetIntent,
) -> TiboResetIntent:
    """Resolve an explicitly scoped follow-up after a recent Tibo question."""

    normalized = normalize_query_text(text)
    if not normalized or not previous_intent.should_handle:
        return TiboResetIntent(TiboResetIntentType.NONE, 0.0, normalized)
    # Conversation context may supply the reset topic, but not the product
    # subject: every handled message must still say Codex or ChatGPT.
    if not _PRODUCT_SUBJECT_RE.search(normalized):
        return TiboResetIntent(TiboResetIntentType.NONE, 0.0, normalized)
    followup_text = _PRODUCT_SUBJECT_RE.sub("", normalized, count=1).strip(
        " ：:，,"
    )
    if not followup_text or len(followup_text) > _FOLLOWUP_MAX_CHARS:
        return TiboResetIntent(TiboResetIntentType.NONE, 0.0, normalized)
    if (
        _UNRELATED_DOMAIN_RE.search(followup_text)
        or _HOW_TO_RE.search(followup_text)
    ):
        return TiboResetIntent(TiboResetIntentType.NONE, 0.0, normalized)

    if _TODAY_FOLLOWUP_RE.fullmatch(followup_text):
        return TiboResetIntent(TiboResetIntentType.TODAY_STATUS, 0.95, normalized)
    if _WEEK_FOLLOWUP_RE.fullmatch(followup_text):
        return TiboResetIntent(TiboResetIntentType.WEEK_COUNT, 0.95, normalized)
    if _LATEST_FOLLOWUP_RE.fullmatch(followup_text):
        return TiboResetIntent(TiboResetIntentType.LATEST, 0.94, normalized)
    if _RETENTION_RE.search(followup_text) or (
        re.fullmatch(
            r"(?:那)?(?:历史|记录|数据)(?:呢|还在吗|会丢吗)?[？?]?",
            followup_text,
        )
    ):
        return TiboResetIntent(TiboResetIntentType.RETENTION, 0.94, normalized)
    if _COUNT_FOLLOWUP_RE.fullmatch(followup_text):
        inherited = (
            previous_intent.type
            if previous_intent.type
            in {TiboResetIntentType.WEEK_COUNT, TiboResetIntentType.TODAY_STATUS}
            else TiboResetIntentType.SUMMARY
        )
        return TiboResetIntent(inherited, 0.9, normalized)
    return TiboResetIntent(TiboResetIntentType.NONE, 0.0, normalized)


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
