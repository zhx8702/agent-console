from __future__ import annotations

import asyncio
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException

from app.common.logging import get_logger
from app.common.types import ChatMessage, ChatRequest, Role
from app.common.wxbot_auth import wxbot_sdk_headers
from app.egress.safe_http import safe_trusted_service_request
from app.llm.activity import wait_for_llm_activity
from app.social.speech_ledger import GroupSpeechLedgerProtocol
from plugins.wxbot.store import WxbotStore

logger = get_logger(__name__)
_REPORT_MAX_CHARS_PER_CHUNK = 12_000
_REPORT_TRANSIENT_BACKOFF_SECONDS = 900.0
_REPORT_TRANSIENT_BACKOFF_MAX_SECONDS = 3600.0
_DEFAULT_TZ = "Asia/Shanghai"
_HOUR_RE = re.compile(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b")
_REPORT_TRANSIENT_ERROR_MARKERS = (
    "upstreamunavailable",
    "upstream unavailable",
    "circuit breaker",
    "502",
    "504",
    "bad gateway",
    "gateway timeout",
    "timeout",
    "timed out",
    "temporarily unavailable",
    "rate limit",
)


def normalize_report_send_text(
    report_type: str,
    text: str,
    *,
    footer: str = "",
) -> str:
    normalized_footer = str(footer or "").strip()
    if (
        str(report_type or "").strip().lower() != "daily"
        or not normalized_footer
    ):
        return str(text or "")
    body = str(text or "").replace(normalized_footer, "").rstrip()
    if not body:
        return normalized_footer
    return f"{body}\n\n{normalized_footer}"


def _safe_tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(str(name or _DEFAULT_TZ))
    except Exception:
        return ZoneInfo(_DEFAULT_TZ)


def _coerce_hour(value: Any, default: int = 9) -> int:
    try:
        hour = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(hour, 0), 23)


def _coerce_day(value: Any, default: int = 1) -> int:
    try:
        day = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(day, 1), 31)


def _coerce_weekday(value: Any, default: int = 1) -> int:
    try:
        day = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(day, 1), 7)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        next_month = datetime(year + 1, 1, 1)
    else:
        next_month = datetime(year, month + 1, 1)
    return (next_month - datetime(year, month, 1)).days


def _monthly_target(year: int, month: int, day: int, tz: ZoneInfo) -> datetime:
    safe_day = min(_coerce_day(day), _days_in_month(year, month))
    return datetime(year, month, safe_day, hour=0, minute=5, second=0, microsecond=0, tzinfo=tz)


def resolve_preview_period(
    report_type: str,
    *,
    date: str = "",
    year_month: str = "",
    tz: str = _DEFAULT_TZ,
) -> tuple[str, str]:
    report_kind = str(report_type or "daily").strip().lower()
    if report_kind not in {"daily", "weekly", "monthly"}:
        raise HTTPException(400, "report_type must be daily, weekly or monthly")
    now = datetime.now(_safe_tz(tz))
    if report_kind == "daily":
        period_key = date.strip() or (now.date() - timedelta(days=1)).strftime("%Y-%m-%d")
        return period_key, period_key
    if report_kind == "weekly":
        if date.strip():
            start = datetime.strptime(date.strip(), "%Y-%m-%d").date()
            start = start - timedelta(days=start.weekday())
        else:
            start = now.date() - timedelta(days=now.weekday() + 7)
        end = start + timedelta(days=6)
        period_key = f"{start.strftime('%Y-%m-%d')}..{end.strftime('%Y-%m-%d')}"
        return period_key, period_key
    if year_month.strip():
        period_key = year_month.strip()
        return period_key, period_key
    if now.month == 1:
        period_key = f"{now.year - 1}-12"
    else:
        period_key = f"{now.year}-{now.month - 1:02d}"
    return period_key, period_key


def resolve_due_period(subscription: dict[str, Any], report_type: str, now: datetime | None = None) -> tuple[str, str] | None:
    report_kind = str(report_type or "").strip().lower()
    if report_kind not in {"daily", "weekly", "monthly"}:
        return None
    tz = _safe_tz(subscription.get("tz"))
    now_local = (now or datetime.now(tz)).astimezone(tz)

    if report_kind == "daily":
        if not bool(subscription.get("daily_enabled")):
            return None
        hour = _coerce_hour(subscription.get("daily_hour"))
        fire_time = now_local.replace(hour=hour, minute=0, second=0, microsecond=0)
        if now_local < fire_time:
            return None
        target_day = now_local.date() - timedelta(days=1)
        period_key = target_day.strftime("%Y-%m-%d")
        return period_key, period_key

    if report_kind == "weekly":
        if not bool(subscription.get("weekly_enabled")):
            return None
        fire_day = _coerce_weekday(subscription.get("weekly_day")) - 1
        hour = _coerce_hour(subscription.get("weekly_hour"))
        if now_local.weekday() != fire_day:
            return None
        fire_time = now_local.replace(hour=hour, minute=0, second=0, microsecond=0)
        if now_local < fire_time:
            return None
        this_week_start = now_local.date() - timedelta(days=now_local.weekday())
        start = this_week_start - timedelta(days=7)
        end = start + timedelta(days=6)
        period_key = f"{start.strftime('%Y-%m-%d')}..{end.strftime('%Y-%m-%d')}"
        return period_key, period_key

    if not bool(subscription.get("monthly_enabled")):
        return None
    day = _coerce_day(subscription.get("monthly_day"))
    fire_time = _monthly_target(now_local.year, now_local.month, day, tz)
    if now_local < fire_time:
        return None
    if now_local.month == 1:
        year, month = now_local.year - 1, 12
    else:
        year, month = now_local.year, now_local.month - 1
    period_key = f"{year}-{month:02d}"
    return period_key, period_key


def seconds_to_next_subscription_fire(subscriptions: list[dict[str, Any]], now: datetime | None = None) -> float:
    if not subscriptions:
        return 3600.0
    now_dt = now or datetime.now(ZoneInfo(_DEFAULT_TZ))
    fire_times: list[float] = []
    for sub in subscriptions:
        tz = _safe_tz(sub.get("tz"))
        now_local = now_dt.astimezone(tz)
        if bool(sub.get("daily_enabled")):
            target = now_local.replace(hour=_coerce_hour(sub.get("daily_hour")), minute=0, second=0, microsecond=0)
            if target <= now_local:
                target += timedelta(days=1)
            fire_times.append(target.timestamp())
        if bool(sub.get("weekly_enabled")):
            fire_day = _coerce_weekday(sub.get("weekly_day")) - 1
            target_date = now_local.date() + timedelta(days=(fire_day - now_local.weekday()) % 7)
            target = datetime(
                target_date.year,
                target_date.month,
                target_date.day,
                hour=_coerce_hour(sub.get("weekly_hour")),
                minute=0,
                second=0,
                microsecond=0,
                tzinfo=tz,
            )
            if target <= now_local:
                target += timedelta(days=7)
            fire_times.append(target.timestamp())
        if bool(sub.get("monthly_enabled")):
            target = _monthly_target(now_local.year, now_local.month, _coerce_day(sub.get("monthly_day")), tz)
            if target <= now_local:
                if now_local.month == 12:
                    target = _monthly_target(now_local.year + 1, 1, _coerce_day(sub.get("monthly_day")), tz)
                else:
                    target = _monthly_target(now_local.year, now_local.month + 1, _coerce_day(sub.get("monthly_day")), tz)
            fire_times.append(target.timestamp())
    if not fire_times:
        return 3600.0
    return max(10.0, min(fire_times) - now_dt.timestamp())


def _report_message_text(item: dict[str, Any]) -> str:
    msg_type = str(item.get("msg_type") or "text").strip().lower()
    text = str(item.get("text") or "").strip()
    if text:
        return text
    placeholders = {
        "image": "[图片]",
        "audio": "[语音]",
        "video": "[视频]",
        "file": "[文件]",
        "event": "[事件]",
    }
    return placeholders.get(msg_type, f"[{msg_type or '消息'}]")


def report_message_lines(messages: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in messages:
        if bool(item.get("is_self_sent")):
            continue
        text = _report_message_text(item)
        if not text:
            continue
        sender_name = str(item.get("sender_name") or item.get("sender_wxid") or "未知成员").strip()
        timestamp = str(item.get("timestamp") or "").strip()
        if timestamp:
            lines.append(f"[{timestamp}] {sender_name}: {text}")
        else:
            lines.append(f"{sender_name}: {text}")
    return lines


def report_llm_metadata(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "wxbot_report_job": True,
        "openai_web_search": False,
        "web_search": False,
    }
    if extra:
        metadata.update(extra)
        metadata["openai_web_search"] = False
        metadata["web_search"] = False
        metadata.pop("disable_openai_fallback", None)
    return metadata


def report_chunk_max_chars(settings: Any) -> int:
    raw = getattr(settings, "wxbot_report_max_chars_per_chunk", _REPORT_MAX_CHARS_PER_CHUNK)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _REPORT_MAX_CHARS_PER_CHUNK
    return max(1000, value)


def _split_report_line(line: str, max_chars: int) -> list[str]:
    if len(line) <= max_chars:
        return [line]
    return [line[index : index + max_chars] for index in range(0, len(line), max_chars)]


def chunk_report_lines(lines: list[str], max_chars: int = _REPORT_MAX_CHARS_PER_CHUNK) -> list[str]:
    max_chars = max(1, int(max_chars or _REPORT_MAX_CHARS_PER_CHUNK))
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for raw_line in lines:
        for line in _split_report_line(raw_line, max_chars):
            line_len = len(line) + 1
            if current and current_len + line_len > max_chars:
                chunks.append("\n".join(current))
                current = [line]
                current_len = line_len
                continue
            current.append(line)
            current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def is_transient_report_error(exc: BaseException | str) -> bool:
    text = str(exc or "").strip().lower()
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    return any(marker in text for marker in _REPORT_TRANSIENT_ERROR_MARKERS)


def report_failure_payload(
    existing: dict[str, Any],
    *,
    error: str,
    current_stage: str,
    transient: bool,
    now: datetime | None = None,
    backoff_seconds: float = _REPORT_TRANSIENT_BACKOFF_SECONDS,
) -> dict[str, Any]:
    payload = dict(existing or {})
    payload["last_error"] = error
    payload["last_failed_stage"] = current_stage
    now_dt = now or datetime.now(UTC)
    payload["last_failed_at"] = now_dt.isoformat()
    payload["transient_error"] = transient
    if transient:
        attempts = int(payload.get("transient_attempts") or 0) + 1
        delay = min(float(backoff_seconds) * (2 ** max(0, attempts - 1)), _REPORT_TRANSIENT_BACKOFF_MAX_SECONDS)
        payload["transient_attempts"] = attempts
        payload["retry_after"] = (now_dt + timedelta(seconds=delay)).isoformat()
        payload["retry_backoff_seconds"] = delay
    else:
        payload.pop("retry_after", None)
        payload.pop("retry_backoff_seconds", None)
    return payload


def report_job_retry_after(job: dict[str, Any], now: datetime | None = None) -> datetime | None:
    payload = job.get("report_payload") if isinstance(job, dict) else {}
    if not isinstance(payload, dict) or not bool(payload.get("transient_error")):
        return None
    retry_after_raw = str(payload.get("retry_after") or "").strip()
    if not retry_after_raw:
        return None
    try:
        retry_after = datetime.fromisoformat(retry_after_raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if retry_after.tzinfo is None:
        retry_after = retry_after.replace(tzinfo=UTC)
    now_dt = now or datetime.now(UTC)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=UTC)
    return retry_after if retry_after > now_dt.astimezone(retry_after.tzinfo) else None


def report_job_in_transient_backoff(job: dict[str, Any], now: datetime | None = None) -> bool:
    return report_job_retry_after(job, now=now) is not None


def should_defer_report_job_retry(job: dict[str, Any], now: datetime | None = None) -> bool:
    return str(job.get("status") or "") == "failed" and report_job_in_transient_backoff(job, now=now)


def _report_model_metadata(container: Any, response: Any) -> dict[str, Any]:
    llm_service = getattr(container, "llm_service", None)
    provider_obj = getattr(llm_service, "_chat", None)
    return {
        "provider": str(getattr(provider_obj, "name", "") or getattr(llm_service, "name", "") or ""),
        "model": str(getattr(response, "model", "") or ""),
        "api_mode": str(getattr(provider_obj, "_api_mode", "") or ""),
    }


def _report_non_self_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in messages if isinstance(item, dict) and not bool(item.get("is_self_sent"))]


def _report_sender_name(item: dict[str, Any]) -> str:
    return str(item.get("sender_name") or item.get("sender_wxid") or "未知成员").strip() or "未知成员"


def _report_sender_id(item: dict[str, Any]) -> str:
    return str(item.get("sender_wxid") or item.get("sender_name") or "unknown").strip() or "unknown"


def _report_sample_text(item: dict[str, Any]) -> str:
    text = _report_message_text(item).replace("\n", " ").strip()
    if len(text) > 40:
        return text[:40] + "…"
    return text


def _parse_timestamp(item: dict[str, Any], tz: ZoneInfo) -> datetime | None:
    for key in ("ts", "occurred_ts"):
        value = item.get(key)
        if isinstance(value, (int, float)) and value:
            ts = float(value)
            if ts > 1_000_000_000_000:
                ts /= 1000.0
            try:
                return datetime.fromtimestamp(ts, tz=tz)
            except Exception:
                continue

    raw = str(item.get("timestamp") or item.get("occurred_at") or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=tz)
        return parsed.astimezone(tz)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=tz)
        except Exception:
            continue
    return None


def _extract_hour(item: dict[str, Any], tz: ZoneInfo) -> int | None:
    parsed = _parse_timestamp(item, tz)
    if parsed is not None:
        return parsed.hour
    raw = str(item.get("timestamp") or item.get("occurred_at") or "").strip()
    if not raw:
        return None
    match = _HOUR_RE.search(raw)
    if not match:
        return None
    hour = int(match.group(1))
    if 0 <= hour <= 23:
        return hour
    return None


def _delta_text(current: int, previous: int) -> str:
    if previous <= 0:
        return "+∞" if current > 0 else "—"
    pct = (current - previous) / previous * 100.0
    return f"+{pct:.0f}%" if pct >= 0 else f"{pct:.0f}%"


def _split_sectioned_text(text: str, title: str) -> tuple[str, str]:
    cleaned = str(text or "").strip()
    if not cleaned:
        return "", ""
    if "一句话总结：" in cleaned:
        head, tail = cleaned.split("一句话总结：", 1)
        topics = head.replace("今日话题：", "").strip()
        summary = tail.strip()
        return topics, summary
    if title and cleaned.startswith(title):
        return cleaned[len(title) :].strip(), ""
    return cleaned, ""


def _period_day_count(period_key: str) -> int:
    try:
        year, month = map(int, period_key.split("-", 1))
        if month == 12:
            return (datetime(year + 1, 1, 1) - datetime(year, month, 1)).days
        return (datetime(year, month + 1, 1) - datetime(year, month, 1)).days
    except Exception:
        return 30


def _previous_month_period(period_key: str) -> str:
    year, month = map(int, period_key.split("-", 1))
    if month == 1:
        return f"{year - 1}-12"
    return f"{year}-{month - 1:02d}"


def _parse_week_period(period_key: str) -> tuple[str, str]:
    if ".." in period_key:
        start, end = period_key.split("..", 1)
        datetime.strptime(start, "%Y-%m-%d")
        datetime.strptime(end, "%Y-%m-%d")
        return start, end
    start_date = datetime.strptime(period_key, "%Y-%m-%d").date()
    start_date = start_date - timedelta(days=start_date.weekday())
    end_date = start_date + timedelta(days=6)
    return start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")


def _daily_keys_between(start: str, end: str) -> list[str]:
    start_day = datetime.strptime(start, "%Y-%m-%d").date()
    end_day = datetime.strptime(end, "%Y-%m-%d").date()
    keys: list[str] = []
    cursor = start_day
    while cursor <= end_day:
        keys.append(cursor.strftime("%Y-%m-%d"))
        cursor += timedelta(days=1)
    return keys


def _weekly_keys_for_month(period_key: str) -> list[str]:
    year, month = map(int, period_key.split("-", 1))
    first = datetime(year, month, 1).date()
    last = datetime(year, month, _days_in_month(year, month)).date()
    cursor = first - timedelta(days=first.weekday())
    keys: list[str] = []
    while cursor <= last:
        end = cursor + timedelta(days=6)
        keys.append(f"{cursor.strftime('%Y-%m-%d')}..{end.strftime('%Y-%m-%d')}")
        cursor += timedelta(days=7)
    return keys


def _build_daily_report_text(
    session_name: str,
    period_key: str,
    messages: list[dict[str, Any]],
    *,
    topics_text: str = "",
    summary_text: str = "",
    tz_name: str = _DEFAULT_TZ,
    footer: str = "",
) -> str:
    tz = _safe_tz(tz_name)
    total = len(messages)
    if total <= 0:
        body = f"[{session_name}] 日报 · {period_key}\n\n暂无可用于总结的非机器人聊天记录"
        return normalize_report_send_text("daily", body, footer=footer)

    senders = Counter(_report_sender_id(item) for item in messages)
    names = {sender_id: _report_sender_name(item) for item in messages for sender_id in [_report_sender_id(item)]}
    hours = Counter(hour for item in messages for hour in [_extract_hour(item, tz)] if hour is not None)
    peak_hour, peak_count = (hours.most_common(1)[0] if hours else (0, 0))

    lines = [
        f"[{session_name}] 日报 · {period_key}",
        "",
        "━━━ 活跃概览 ━━━",
        f"今日发言 {total} 条，参与 {len(senders)} 人",
        f"高峰时段：{peak_hour:02d}:00 - {(peak_hour + 1) % 24:02d}:00（{peak_count} 条）",
        "",
        "━━━ 活跃之星 ━━━",
    ]
    for index, (sender_id, count) in enumerate(senders.most_common(5), start=1):
        sample = next((_report_sample_text(item) for item in messages if _report_sender_id(item) == sender_id and _report_sample_text(item)), "")
        if sample:
            lines.append(f"{index}. {names.get(sender_id, sender_id)} — {count} 条 —「{sample}」")
        else:
            lines.append(f"{index}. {names.get(sender_id, sender_id)} — {count} 条")

    if topics_text.strip():
        lines.extend(["", "━━━ 今日话题 ━━━", topics_text.strip()])
    if summary_text.strip():
        lines.extend(["", "━━━ 一句话总结 ━━━", f"「{summary_text.strip()}」"])
    return normalize_report_send_text(
        "daily",
        "\n".join(lines),
        footer=footer,
    )


def _build_monthly_report_text(
    session_name: str,
    period_key: str,
    messages: list[dict[str, Any]],
    *,
    previous_messages: list[dict[str, Any]] | None = None,
    trend_text: str = "",
) -> str:
    total = len(messages)
    if total <= 0:
        return f"[{session_name}] 月报 · {period_key}\n\n暂无可用于总结的非机器人聊天记录"

    previous_messages = previous_messages or []
    senders = Counter(_report_sender_id(item) for item in messages)
    names = {sender_id: _report_sender_name(item) for item in messages for sender_id in [_report_sender_id(item)]}
    prev_total = len(previous_messages)
    prev_unique = len({_report_sender_id(item) for item in previous_messages})
    lines = [
        f"[{session_name}] 月报 · {period_key}",
        "",
        "━━━ 月度总览 ━━━",
        f"发言总计 {total} 条 | 参与 {len(senders)} 人 | 日均 {total // max(_period_day_count(period_key), 1)} 条",
        f"vs 上月：发言 {_delta_text(total, prev_total)} | 活跃人数 {_delta_text(len(senders), prev_unique)}",
        "",
        "━━━ 月度最活跃 ━━━",
    ]
    for index, (sender_id, count) in enumerate(senders.most_common(3), start=1):
        lines.append(f"{index}. {names.get(sender_id, sender_id)} — {count} 条")
    if trend_text.strip():
        lines.extend(["", "━━━ 话题趋势 ━━━", trend_text.strip()])
    return "\n".join(lines)


def _build_rollup_no_data_text(session_name: str, report_type: str, period_key: str) -> str:
    title = "周报" if report_type == "weekly" else "月报"
    source_title = "已完成日报" if report_type == "weekly" else "已完成周报"
    return f"[{session_name}] {title} · {period_key}\n\n暂无{source_title}可用于汇总"


def _build_rollup_report_text(session_name: str, report_type: str, period_key: str, summary_text: str) -> str:
    title = "周报" if report_type == "weekly" else "月报"
    cleaned = str(summary_text or "").strip()
    if not cleaned:
        return _build_rollup_no_data_text(session_name, report_type, period_key)
    return f"[{session_name}] {title} · {period_key}\n\n{cleaned}"


class WxbotReportService:
    def __init__(
        self,
        store: WxbotStore,
        container: Any,
        *,
        bridge: Any | None = None,
        speech_ledger: GroupSpeechLedgerProtocol | None = None,
        scope_execution_allowed: Callable[[str, str], Awaitable[bool]] | None = None,
    ) -> None:
        self._store = store
        self._container = container
        self._bridge = bridge
        self._speech_ledger = speech_ledger or getattr(store, "speech_ledger", None)
        self._scope_gate = scope_execution_allowed
        self._last_llm_metadata: dict[str, Any] = {}

    async def _scope_execution_allowed(
        self,
        tenant_id: str,
        session_id: str,
    ) -> bool:
        if not callable(self._scope_gate):
            logger.error(
                "wxbot.report_scope_execution_gate_missing",
                tenant_id=tenant_id,
                session_id=session_id,
            )
            return False
        try:
            return await self._scope_gate(tenant_id, session_id) is True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "wxbot.report_scope_execution_gate_error",
                tenant_id=tenant_id,
                session_id=session_id,
                error_type=exc.__class__.__name__,
            )
            return False

    async def scope_execution_allowed(
        self,
        tenant_id: str,
        session_id: str,
    ) -> bool:
        """Expose the fail-closed lifecycle gate to report route orchestration."""

        return await self._scope_execution_allowed(tenant_id, session_id)

    async def _update_report_job_for_attempt(
        self,
        job_id: int,
        run_attempt: int,
        **changes: Any,
    ) -> bool:
        return bool(
            await self._store.update_report_job(
                job_id,
                expected_run_attempt=run_attempt,
                expected_status="running",
                **changes,
            )
        )

    async def _defer_report_job_for_scope(
        self,
        job_id: int,
        run_attempt: int,
    ) -> bool:
        return await self._update_report_job_for_attempt(
            job_id,
            run_attempt,
            status="pending",
            current_stage="scope_execution_denied",
            error="",
        )

    async def _scope_allowed_or_defer_report(
        self,
        *,
        job_id: int,
        run_attempt: int,
        tenant_id: str,
        session_id: str,
    ) -> bool:
        if await self._scope_execution_allowed(tenant_id, session_id):
            return True
        await self._defer_report_job_for_scope(job_id, run_attempt)
        return False

    async def _complete_rollup_report(
        self,
        job: dict[str, Any],
        *,
        run_attempt: int,
    ) -> bool:
        job_id = int(job["id"])
        tenant_id = str(job.get("tenant_id") or getattr(self._store.settings, "wxbot_default_tenant_id", "default") or "default")
        session_id = str(job["session_id"])
        session_name = str(job.get("session_name") or session_id)
        report_type = str(job.get("report_type") or "")
        period_key = str(job.get("period_key") or "")

        if report_type == "weekly":
            source_type = "daily"
            source_mode = "daily_rollup"
            period_start, period_end = _parse_week_period(period_key)
            expected_periods = _daily_keys_between(period_start, period_end)
            source_label = "已完成日报"
            prompt_title = "周报"
        elif report_type == "monthly":
            source_type = "weekly"
            source_mode = "weekly_rollup"
            _year, _month = map(int, period_key.split("-", 1))
            expected_periods = _weekly_keys_for_month(period_key)
            period_start, period_end = expected_periods[0], expected_periods[-1]
            source_label = "已完成周报"
            prompt_title = "月报"
        else:
            raise RuntimeError(f"unsupported rollup report type: {report_type}")

        source_jobs = await self._store.list_completed_report_jobs_for_period_range(
            tenant_id=tenant_id,
            session_id=session_id,
            report_type=source_type,
            period_start=period_start,
            period_end=period_end,
        )
        if not await self._scope_allowed_or_defer_report(
            job_id=job_id,
            run_attempt=run_attempt,
            tenant_id=tenant_id,
            session_id=session_id,
        ):
            return False
        by_period = {str(item.get("period_key") or ""): item for item in source_jobs}
        ordered_sources = [by_period[key] for key in expected_periods if key in by_period]
        missing_periods = [key for key in expected_periods if key not in by_period]
        source_job_ids = [int(item["id"]) for item in ordered_sources if item.get("id") is not None]
        coverage = {
            "period_start": period_start,
            "period_end": period_end,
            "expected_periods": expected_periods,
            "source_periods": [str(item.get("period_key") or "") for item in ordered_sources],
            "source_count": len(ordered_sources),
            "expected_count": len(expected_periods),
        }

        if not ordered_sources:
            report_text = _build_rollup_no_data_text(session_name, report_type, period_key)
            report_payload = {
                "session_id": session_id,
                "session_name": session_name,
                "report_type": report_type,
                "period": period_key,
                "count": 0,
                "source_mode": source_mode,
                "source_report_type": source_type,
                "source_job_ids": [],
                "coverage": coverage,
                "missing_periods": missing_periods,
                "skipped_periods": missing_periods,
                "no_data": True,
                "cached": False,
            }
            return await self._update_report_job_for_attempt(
                job_id,
                run_attempt,
                status="completed",
                current_stage="completed",
                msg_count=0,
                result_text=report_text,
                report_payload=report_payload,
                error="",
            )

        source_lines = [
            f"## {item.get('period_label') or item.get('period_key')}\n{str(item.get('result_text') or '').strip()}"
            for item in ordered_sources
            if str(item.get("result_text") or "").strip()
        ]
        chunks = chunk_report_lines(source_lines, max_chars=report_chunk_max_chars(self._store.settings))
        partials: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            if not await self._scope_allowed_or_defer_report(
                job_id=job_id,
                run_attempt=run_attempt,
                tenant_id=tenant_id,
                session_id=session_id,
            ):
                return False
            if not await self._update_report_job_for_attempt(
                job_id,
                run_attempt,
                status="running",
                current_stage=f"summarize_rollup_{index}",
                msg_count=len(ordered_sources),
                report_payload={
                    "source_mode": source_mode,
                    "source_report_type": source_type,
                    "source_job_ids": source_job_ids,
                    "coverage": coverage,
                    "missing_periods": missing_periods,
                    "skipped_periods": missing_periods,
                    "chunk_count": len(chunks),
                    "chunk_index": index,
                },
                error="",
            ):
                return False
            partial = await self._call_llm(
                trace_id=f"wxbot_report_{job_id}_rollup_{index}",
                system=f"你是微信群{prompt_title}整理助手。只能基于给定的{source_label}，不要编造缺失日期或周次的内容。",
                user=(
                    f"请把下面的{source_label}汇总成一份{prompt_title}。\n"
                    "要求：\n"
                    "1. 只使用已完成下级报告里的信息，不要推测缺失内容。\n"
                    "2. 缺失或未完成的周期视为跳过，不要用原始聊天记录补齐。\n"
                    "3. 输出中文 Markdown 正文，不要重复外层标题。\n\n"
                    f"{source_label}：\n{chunk}"
                ),
                max_tokens=1600 if report_type == "weekly" else 1800,
            )
            if not await self._scope_allowed_or_defer_report(
                job_id=job_id,
                run_attempt=run_attempt,
                tenant_id=tenant_id,
                session_id=session_id,
            ):
                return False
            partials.append(partial)

        summary_text = "\n\n".join(partials)
        if len(partials) > 1:
            summary_text = await self._call_llm(
                trace_id=f"wxbot_report_{job_id}_rollup_final",
                system=f"你是微信群{prompt_title}整理助手。只能基于给定的分段汇总。",
                user=(
                    f"请把下面的分段汇总合并成一份完整{prompt_title}正文。\n"
                    "要求：简洁、中文 Markdown、不要编造缺失周期内容。\n\n"
                    "分段汇总：\n" + summary_text
                ),
                max_tokens=1800,
            )
            if not await self._scope_allowed_or_defer_report(
                job_id=job_id,
                run_attempt=run_attempt,
                tenant_id=tenant_id,
                session_id=session_id,
            ):
                return False

        report_text = _build_rollup_report_text(session_name, report_type, period_key, summary_text)
        report_payload = {
            "session_id": session_id,
            "session_name": session_name,
            "report_type": report_type,
            "period": period_key,
            "count": len(ordered_sources),
            "source_mode": source_mode,
            "source_report_type": source_type,
            "source_job_ids": source_job_ids,
            "coverage": coverage,
            "missing_periods": missing_periods,
            "skipped_periods": missing_periods,
            "chunk_count": len(chunks),
            "chunk_max_chars": report_chunk_max_chars(self._store.settings),
            "llm": dict(self._last_llm_metadata),
            "cached": False,
        }
        if not await self._scope_allowed_or_defer_report(
            job_id=job_id,
            run_attempt=run_attempt,
            tenant_id=tenant_id,
            session_id=session_id,
        ):
            return False
        return await self._update_report_job_for_attempt(
            job_id,
            run_attempt,
            status="completed",
            current_stage="completed",
            msg_count=len(ordered_sources),
            result_text=report_text,
            report_payload=report_payload,
            error="",
        )

    async def sdk_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        request_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if self._bridge is not None and hasattr(self._bridge, "sdk_request"):
            payload = await self._bridge.sdk_request(
                method,
                path,
                params=params,
                json_body=json_body,
                request_headers=request_headers,
            )
            return payload if isinstance(payload, dict) else {"data": payload}

        base_url = getattr(self._store.settings, "wxbot_sdk_url", "http://127.0.0.1:5080").rstrip("/")
        normalized_method = str(method or "").upper()
        if normalized_method not in {"GET", "POST"}:
            raise HTTPException(405, "unsupported wxbot sdk method")
        try:
            async with httpx.AsyncClient(
                timeout=20.0,
                trust_env=False,
            ) as client:
                resp = await safe_trusted_service_request(
                    client,
                    normalized_method,
                    base_url,
                    path,
                    params=params,
                    json=json_body,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        **wxbot_sdk_headers(self._store.settings),
                        **(request_headers or {}),
                    },
                    timeout_seconds=20.0,
                    max_response_bytes=10 * 1024 * 1024,
                    allowed_response_content_types=(
                        "application/json",
                        "application/problem+json",
                        "text/plain",
                    ),
                )
        except httpx.HTTPError as exc:
            raise HTTPException(502, "wxbot sdk unavailable") from exc
        if resp.status_code >= 400:
            raise HTTPException(resp.status_code, "wxbot sdk returned an error")
        payload = resp.json()
        return payload if isinstance(payload, dict) else {"data": payload}

    async def canonical_group_session_name(
        self,
        session_id: str,
        *,
        fallback: str = "",
    ) -> str:
        """Resolve the current SDK roster name used by Linux UI automation."""

        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id.endswith("@chatroom"):
            return str(fallback or normalized_session_id).strip()
        payload = await self.sdk_request("GET", "/ext/roster/groups")
        sessions = payload.get("sessions")
        if not isinstance(sessions, list):
            raise RuntimeError("verified group roster unavailable")
        target = next(
            (
                item
                for item in sessions
                if isinstance(item, dict)
                and str(item.get("session_id") or "").strip()
                == normalized_session_id
            ),
            None,
        )
        if target is None:
            raise RuntimeError("target group is not present in verified roster")
        canonical_name = str(target.get("session_name") or "").strip()
        if not canonical_name:
            raise RuntimeError("verified group name unavailable")
        return canonical_name

    @staticmethod
    def sdk_outbound_id(sdk_result: dict[str, Any]) -> int:
        """Validate the queue acknowledgement returned by ``POST /send``."""

        if not isinstance(sdk_result, dict):
            raise RuntimeError("invalid SDK queue acknowledgement")
        raw_id = sdk_result.get("id")
        if sdk_result.get("queued") is not True or isinstance(raw_id, bool):
            raise RuntimeError("invalid SDK queue acknowledgement")
        try:
            row_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("invalid SDK queue acknowledgement") from exc
        if row_id <= 0:
            raise RuntimeError("invalid SDK queue acknowledgement")
        return row_id

    async def _sdk_outbound_row(
        self,
        sdk_row_id: int,
    ) -> tuple[dict[str, Any], str]:
        """Read one SDK row across detail- and list-only SDK versions."""

        try:
            return (
                await self.sdk_request(
                    "GET",
                    f"/queue/messages/{sdk_row_id}",
                ),
                "detail",
            )
        except HTTPException as detail_error:
            # The deployed Linux SDK exposes only the collection endpoint.
            # Its HTML 404 is normalized to 502 by the trusted-service client,
            # so both statuses must negotiate the compatible list contract.
            if detail_error.status_code not in {404, 405, 502}:
                raise
            try:
                listing = await self.sdk_request(
                    "GET",
                    "/queue/messages",
                    params={"status": "", "limit": 100},
                )
            except Exception as listing_error:
                raise detail_error from listing_error
            items = listing.get("items")
            if not isinstance(items, list):
                raise RuntimeError(
                    "invalid SDK outbound queue listing"
                ) from detail_error
            row = next(
                (
                    item
                    for item in items
                    if isinstance(item, dict)
                    and int(item.get("id") or 0) == sdk_row_id
                ),
                None,
            )
            if row is None:
                raise detail_error from None
            return row, "list"

    async def reconcile_report_delivery(self, job_id: int) -> dict[str, Any]:
        """Reconcile one queued report against its exact SDK outbound row."""

        job = await self._store.get_report_job(job_id)
        if not job or str(job.get("status") or "") != "completed":
            return {"status": "not_ready", "job_id": int(job_id)}
        if str(job.get("delivery_status") or "") == "sent":
            return {"status": "sent", "job_id": int(job_id)}
        if str(job.get("delivery_status") or "") != "queued":
            return {
                "status": str(job.get("delivery_status") or "pending"),
                "job_id": int(job_id),
            }
        tenant_id = str(
            job.get("tenant_id")
            or getattr(
                self._store.settings,
                "wxbot_default_tenant_id",
                "default",
            )
            or "default"
        )
        session_id = str(job.get("session_id") or "")
        if not await self._scope_execution_allowed(tenant_id, session_id):
            return {"status": "queued", "job_id": int(job_id)}
        delivery_attempt = int(job.get("delivery_attempt") or 0)
        raw_row_id = job.get("sdk_outbound_id")
        try:
            if isinstance(raw_row_id, bool):
                raise ValueError
            sdk_row_id = int(raw_row_id)
            if sdk_row_id <= 0:
                raise ValueError
        except (TypeError, ValueError):
            error = "queued report is missing a valid SDK outbound id"
            updated = await self._store.mark_report_delivery_terminal(
                int(job_id),
                delivery_attempt=delivery_attempt,
                sdk_outbound_id=0,
                status="indeterminate",
                error=error,
            )
            return {
                "status": "indeterminate" if updated else "attempt_lost",
                "job_id": int(job_id),
                "error": error,
            }

        try:
            sdk_row, sdk_row_source = await self._sdk_outbound_row(sdk_row_id)
        except HTTPException as exc:
            if exc.status_code == 404:
                error = f"SDK outbound row {sdk_row_id} was not found"
                updated = await self._store.mark_report_delivery_terminal(
                    int(job_id),
                    delivery_attempt=delivery_attempt,
                    sdk_outbound_id=sdk_row_id,
                    status="indeterminate",
                    error=error,
                )
                return {
                    "status": "indeterminate" if updated else "attempt_lost",
                    "job_id": int(job_id),
                    "sdk_outbound_id": sdk_row_id,
                    "error": error,
                }
            error = f"SDK delivery check deferred: HTTP {exc.status_code}"
            await self._store.touch_report_delivery_check(
                int(job_id),
                delivery_attempt=delivery_attempt,
                sdk_outbound_id=sdk_row_id,
                error=error,
            )
            return {
                "status": "queued",
                "job_id": int(job_id),
                "sdk_outbound_id": sdk_row_id,
                "error": error,
            }
        except Exception as exc:
            error = f"SDK delivery check deferred: {exc.__class__.__name__}"
            await self._store.touch_report_delivery_check(
                int(job_id),
                delivery_attempt=delivery_attempt,
                sdk_outbound_id=sdk_row_id,
                error=error,
            )
            logger.warning(
                "wxbot.report_delivery_check_deferred",
                job_id=job_id,
                sdk_outbound_id=sdk_row_id,
                error_class=exc.__class__.__name__,
            )
            return {
                "status": "queued",
                "job_id": int(job_id),
                "sdk_outbound_id": sdk_row_id,
                "error": error,
            }

        expected_command_id = f"wxbot-report:{int(job_id)}"
        mismatches: list[str] = []
        if int(sdk_row.get("id") or 0) != sdk_row_id:
            mismatches.append("id")
        if str(sdk_row.get("session_id") or "").strip() != session_id:
            mismatches.append("session_id")
        command_id = str(
            sdk_row.get("command_id") or sdk_row.get("idempotency_key") or ""
        ).strip()
        if sdk_row_source == "detail":
            if command_id != expected_command_id:
                mismatches.append("command_id")
        else:
            # List-only SDK rows do not retain our Idempotency-Key.  Fence the
            # acknowledgement with the exact queue id, target session and the
            # normalized report body returned by the SDK instead.
            expected_text = normalize_report_send_text(
                str(job.get("report_type") or "daily"),
                str(job.get("result_text") or ""),
                footer=str(
                    getattr(
                        self._store.settings,
                        "wxbot_daily_report_footer",
                        "",
                    )
                    or ""
                ),
            )
            actual_text = str(
                sdk_row.get("reply_text") or sdk_row.get("text") or ""
            )
            if command_id and command_id != expected_command_id:
                mismatches.append("command_id")
            if actual_text != expected_text:
                mismatches.append("text")
        if mismatches:
            error = f"SDK outbound identity mismatch: {','.join(mismatches)}"
            updated = await self._store.mark_report_delivery_terminal(
                int(job_id),
                delivery_attempt=delivery_attempt,
                sdk_outbound_id=sdk_row_id,
                status="indeterminate",
                error=error,
            )
            return {
                "status": "indeterminate" if updated else "attempt_lost",
                "job_id": int(job_id),
                "sdk_outbound_id": sdk_row_id,
                "error": error,
            }

        sdk_status = str(sdk_row.get("status") or "").strip().lower()
        if sdk_status in {"pending", "running", "sending"}:
            await self._store.touch_report_delivery_check(
                int(job_id),
                delivery_attempt=delivery_attempt,
                sdk_outbound_id=sdk_row_id,
            )
            return {
                "status": "queued",
                "sdk_status": sdk_status,
                "job_id": int(job_id),
                "sdk_outbound_id": sdk_row_id,
            }
        if sdk_status == "sent":
            updated = await self._store.mark_report_delivery_terminal(
                int(job_id),
                delivery_attempt=delivery_attempt,
                sdk_outbound_id=sdk_row_id,
                status="sent",
            )
            return {
                "status": "sent" if updated else "attempt_lost",
                "sdk_status": sdk_status,
                "job_id": int(job_id),
                "sdk_outbound_id": sdk_row_id,
            }

        sdk_error = str(sdk_row.get("error") or "").strip()
        if sdk_status in {"uncertain", "failed", "cleared"}:
            error = f"SDK delivery {sdk_status}"
            if sdk_error:
                error = f"{error}: {sdk_error}"
        else:
            error = f"unexpected SDK delivery status: {sdk_status or 'missing'}"
        updated = await self._store.mark_report_delivery_terminal(
            int(job_id),
            delivery_attempt=delivery_attempt,
            sdk_outbound_id=sdk_row_id,
            status="indeterminate",
            error=error,
        )
        logger.warning(
            "wxbot.report_delivery_indeterminate",
            job_id=job_id,
            sdk_outbound_id=sdk_row_id,
            sdk_status=sdk_status,
            error=sdk_error,
        )
        return {
            "status": "indeterminate" if updated else "attempt_lost",
            "sdk_status": sdk_status,
            "job_id": int(job_id),
            "sdk_outbound_id": sdk_row_id,
            "error": error,
        }

    async def fetch_report_messages_payload(
        self,
        session_id: str,
        *,
        session_name: str,
        report_type: str,
        date: str = "",
        year_month: str = "",
    ) -> dict[str, Any]:
        payload = await self.sdk_request(
            "GET",
            f"/ext/reports/messages/{session_id}",
            params={
                "report_type": report_type,
                "session_name": session_name,
                "date": date,
                "year_month": year_month,
            },
        )
        if not isinstance(payload, dict):
            raise RuntimeError("invalid report messages payload")
        return payload

    async def _call_llm(self, *, trace_id: str, system: str, user: str, max_tokens: int) -> str:
        _ = max_tokens
        timeout = float(getattr(self._store.settings, "wxbot_report_stage_timeout_seconds", 240.0) or 240.0)
        backend = str(getattr(self._store.settings, "wxbot_report_llm_backend", "http") or "http")
        from plugins.local_agent.complete import complete_chat, resolve_local_backend

        if resolve_local_backend(backend):
            result = await complete_chat(
                self._store.settings,
                backend=backend,
                system=system,
                user=user,
                timeout_seconds=timeout,
            )
            self._last_llm_metadata = {
                "provider": "local_agent",
                "model": result.model,
                "api_mode": result.backend,
                "trace_id": trace_id,
            }
            return result.content
        llm_service = getattr(self._container, "llm_service", None)
        if llm_service is None:
            raise RuntimeError("LLM service not available")
        request = ChatRequest(
            tenant_id=str(getattr(self._store.settings, "wxbot_default_tenant_id", "default") or "default"),
            trace_id=trace_id,
            model_tier="tier-2",
            messages=[ChatMessage(role=Role.USER, content=user)],
            system=system,
            max_tokens=max_tokens,
            temperature=0.3,
            metadata=report_llm_metadata(),
        )
        response = await wait_for_llm_activity(
            llm_service.chat(request),
            timeout=timeout,
        )
        self._last_llm_metadata = _report_model_metadata(self._container, response)
        return str(response.content or "").strip()

    async def run_report_job(self, job_id: int) -> None:
        job = await self._store.get_report_job(job_id)
        if not job:
            return
        tenant_id = str(
            job.get("tenant_id")
            or getattr(
                self._store.settings,
                "wxbot_default_tenant_id",
                "default",
            )
            or "default"
        )
        session_id = str(job.get("session_id") or "")
        if not await self._scope_execution_allowed(tenant_id, session_id):
            return
        run_attempt = await self._store.try_start_report_job(job_id)
        if run_attempt is None:
            return
        run_attempt = int(run_attempt)
        job = await self._store.get_report_job(job_id)
        if not job:
            return
        report_type = str(job.get("report_type") or "daily")
        period_key = str(job.get("period_key") or "")
        try:
            if not await self._scope_allowed_or_defer_report(
                job_id=job_id,
                run_attempt=run_attempt,
                tenant_id=tenant_id,
                session_id=session_id,
            ):
                return
            if report_type in {"weekly", "monthly"}:
                await self._complete_rollup_report(job, run_attempt=run_attempt)
                return
            payload = await self.fetch_report_messages_payload(
                session_id,
                session_name=str(job.get("session_name") or job["session_id"]),
                report_type=report_type,
                date=period_key if report_type == "daily" else "",
                year_month=period_key if report_type == "monthly" else "",
            )
            if not await self._scope_allowed_or_defer_report(
                job_id=job_id,
                run_attempt=run_attempt,
                tenant_id=tenant_id,
                session_id=session_id,
            ):
                return
            raw_messages = payload.get("messages") or []
            if not isinstance(raw_messages, list):
                raise RuntimeError("report messages payload missing messages list")
            messages = _report_non_self_messages([item for item in raw_messages if isinstance(item, dict)])
            lines = report_message_lines(messages)
            chunk_max_chars = report_chunk_max_chars(self._store.settings)
            chunks = chunk_report_lines(lines, max_chars=chunk_max_chars)
            msg_count = len(lines)
            if not chunks:
                report_text = _build_daily_report_text(
                    str(job.get("session_name") or job["session_id"]),
                    str(payload.get("period") or period_key),
                    messages,
                    tz_name=str(getattr(self._store.settings, "timezone", _DEFAULT_TZ) or _DEFAULT_TZ),
                    footer=str(
                        getattr(
                            self._store.settings,
                            "wxbot_daily_report_footer",
                            "",
                        )
                        or ""
                    ),
                )
                report_payload = {
                    "session_id": job["session_id"],
                    "session_name": job.get("session_name") or job["session_id"],
                    "report_type": report_type,
                    "period": payload.get("period") or period_key,
                    "count": msg_count,
                    "source_mode": "daily_raw",
                    "source_job_ids": [],
                    "coverage": {"period_start": period_key, "period_end": period_key, "source_count": msg_count},
                    "missing_periods": [],
                    "skipped_periods": [],
                    "chunk_count": 0,
                    "chunk_max_chars": chunk_max_chars,
                    "cached": False,
                }
                if not await self._scope_allowed_or_defer_report(
                    job_id=job_id,
                    run_attempt=run_attempt,
                    tenant_id=tenant_id,
                    session_id=session_id,
                ):
                    return
                await self._update_report_job_for_attempt(
                    job_id,
                    run_attempt,
                    status="completed",
                    current_stage="completed",
                    msg_count=msg_count,
                    result_text=report_text,
                    report_payload=report_payload,
                    error="",
                )
                return

            partials: list[str] = []
            for index, chunk in enumerate(chunks, start=1):
                if not await self._scope_allowed_or_defer_report(
                    job_id=job_id,
                    run_attempt=run_attempt,
                    tenant_id=tenant_id,
                    session_id=session_id,
                ):
                    return
                stage_payload = dict(job.get("report_payload") or {})
                stage_payload.update(
                    {
                        "chunk_count": len(chunks),
                        "chunk_index": index,
                        "chunk_char_len": len(chunk),
                        "chunk_max_chars": chunk_max_chars,
                    }
                )
                if not await self._update_report_job_for_attempt(
                    job_id,
                    run_attempt,
                    status="running",
                    current_stage=f"summarize_chunk_{index}",
                    msg_count=msg_count,
                    report_payload=stage_payload,
                    error="",
                ):
                    return
                logger.info(
                    "wxbot.report_chunk_summarize",
                    job_id=job_id,
                    chunk_count=len(chunks),
                    chunk_index=index,
                    chunk_char_len=len(chunk),
                )
                partial = await self._call_llm(
                    trace_id=f"wxbot_report_{job_id}_chunk_{index}",
                    system="你擅长整理群聊阶段摘要。只输出中文 Markdown 正文，不要编造成员未说过的话。",
                    user=(
                        f"请整理下面这段{('日报' if report_type == 'daily' else '月报')}原始聊天记录片段。\n"
                        "要求：\n"
                        "1. 只保留真实讨论内容、进展、结论、待办和情绪变化。\n"
                        "2. 忽略机器人/系统回执。\n"
                        "3. 用简短项目符号输出，不要写套话。\n"
                        "4. 不要编造成员未说过的话。\n\n"
                        f"原始记录：\n{chunk}"
                    ),
                    max_tokens=1400,
                )
                if not await self._scope_allowed_or_defer_report(
                    job_id=job_id,
                    run_attempt=run_attempt,
                    tenant_id=tenant_id,
                    session_id=session_id,
                ):
                    return
                partials.append(partial)

            if not await self._scope_allowed_or_defer_report(
                job_id=job_id,
                run_attempt=run_attempt,
                tenant_id=tenant_id,
                session_id=session_id,
            ):
                return
            if not await self._update_report_job_for_attempt(
                job_id,
                run_attempt,
                status="running",
                current_stage="finalize",
                msg_count=msg_count,
                error="",
            ):
                return
            report_period = str(payload.get("period") or period_key)
            topics_text = ""
            summary_text = ""
            final_daily = await self._call_llm(
                trace_id=f"wxbot_report_{job_id}_final",
                system="你是微信群日报整理助手。输出必须简洁、中文、可直接拼进日报模板。",
                user=(
                    "请基于下面这些分块摘要，产出旧 wx-bot 风格的日报内容补充。\n"
                    "输出格式固定为：\n"
                    "今日话题：\n"
                    "- 主题 1：...\n"
                    "- 主题 2：...\n"
                    "- 主题 3：...\n"
                    "一句话总结：\n"
                    "...\n\n"
                    "要求：\n"
                    "1. 只保留真实讨论内容，不要写空洞套话。\n"
                    "2. 今日话题控制在 3~5 条。\n"
                    "3. 一句话总结不超过 30 字。\n"
                    "4. 不要输出标题之外的解释。\n\n"
                    "分块摘要：\n" + "\n\n".join(partials)
                ),
                max_tokens=1200,
            )
            if not await self._scope_allowed_or_defer_report(
                job_id=job_id,
                run_attempt=run_attempt,
                tenant_id=tenant_id,
                session_id=session_id,
            ):
                return
            topics_text, summary_text = _split_sectioned_text(final_daily, "今日话题：")
            final_report = _build_daily_report_text(
                str(job.get("session_name") or job["session_id"]),
                report_period,
                messages,
                topics_text=topics_text,
                summary_text=summary_text,
                tz_name=str(getattr(self._store.settings, "timezone", _DEFAULT_TZ) or _DEFAULT_TZ),
                footer=str(
                    getattr(
                        self._store.settings,
                        "wxbot_daily_report_footer",
                        "",
                    )
                    or ""
                ),
            )
            report_payload = {
                "session_id": job["session_id"],
                "session_name": job.get("session_name") or job["session_id"],
                "report_type": report_type,
                "period": payload.get("period") or period_key,
                "count": msg_count,
                "source_mode": "daily_raw",
                "source_job_ids": [],
                "coverage": {"period_start": period_key, "period_end": period_key, "source_count": msg_count},
                "missing_periods": [],
                "skipped_periods": [],
                "chunk_count": len(chunks),
                "chunk_max_chars": chunk_max_chars,
                "llm": dict(self._last_llm_metadata),
                "cached": False,
            }
            if not await self._scope_allowed_or_defer_report(
                job_id=job_id,
                run_attempt=run_attempt,
                tenant_id=tenant_id,
                session_id=session_id,
            ):
                return
            await self._update_report_job_for_attempt(
                job_id,
                run_attempt,
                status="completed",
                current_stage="completed",
                msg_count=msg_count,
                result_text=final_report,
                report_payload=report_payload,
                error="",
            )
        except asyncio.CancelledError:
            await self._store.update_report_job(
                job_id,
                status="failed",
                current_stage="cancelled",
                error="report job cancelled during shutdown",
                expected_run_attempt=run_attempt,
                expected_status="running",
            )
            raise
        except Exception as exc:
            current = await self._store.get_report_job(job_id)
            current_stage = str((current or {}).get("current_stage") or "unknown")
            error = str(exc).strip() or "report generation failed"
            transient = is_transient_report_error(exc)
            backoff_seconds = float(
                getattr(self._store.settings, "wxbot_report_transient_backoff_seconds", _REPORT_TRANSIENT_BACKOFF_SECONDS)
                or _REPORT_TRANSIENT_BACKOFF_SECONDS
            )
            failure_payload = report_failure_payload(
                dict((current or {}).get("report_payload") or {}),
                error=error,
                current_stage=current_stage,
                transient=transient,
                backoff_seconds=backoff_seconds,
            )
            updated = await self._store.update_report_job(
                job_id,
                status="failed",
                current_stage=current_stage,
                msg_count=int((current or {}).get("msg_count") or 0),
                report_payload=failure_payload,
                error=error,
                expected_run_attempt=run_attempt,
                expected_status="running",
            )
            if not updated:
                return
            logger.warning(
                "wxbot.report_job_failed",
                job_id=job_id,
                error=error,
                transient=transient,
                retry_after=failure_payload.get("retry_after"),
            )

    async def send_report_job(self, job_id: int) -> bool:
        job = await self._store.get_report_job(job_id)
        if not job or str(job.get("status") or "") != "completed":
            return False
        delivery_status = str(job.get("delivery_status") or "pending")
        if delivery_status == "sent":
            return True
        if delivery_status == "queued":
            result = await self.reconcile_report_delivery(job_id)
            return str(result.get("status") or "") == "sent"
        if delivery_status in {"sending", "indeterminate"}:
            return False
        tenant_id = str(
            job.get("tenant_id")
            or getattr(
                self._store.settings,
                "wxbot_default_tenant_id",
                "default",
            )
            or "default"
        )
        session_id = str(job.get("session_id") or "")
        if not await self._scope_execution_allowed(tenant_id, session_id):
            return False
        try:
            canonical_session_name = await self.canonical_group_session_name(
                session_id,
                fallback=str(job.get("session_name") or session_id),
            )
        except Exception as exc:
            logger.warning(
                "wxbot.report_delivery_roster_unavailable",
                job_id=job_id,
                session_id=session_id,
                error_class=exc.__class__.__name__,
            )
            return False
        delivery_attempt = await self._store.try_start_report_delivery(job_id)
        if delivery_attempt is None:
            return False
        delivery_attempt = int(delivery_attempt)
        if not await self._scope_execution_allowed(tenant_id, session_id):
            await self._store.release_report_delivery(
                job_id,
                delivery_attempt=delivery_attempt,
                reason="scope_execution_denied",
            )
            return False
        report_type = str(job.get("report_type") or "daily")
        text = normalize_report_send_text(
            report_type,
            str(job.get("result_text") or ""),
            footer=str(
                getattr(
                    self._store.settings,
                    "wxbot_daily_report_footer",
                    "",
                )
                or ""
            ),
        )
        # A report subscription is an explicit, cadence-limited and
        # idempotent delivery obligation. It must not share the conversational
        # speech budget: managed channels persist report delivery under the
        # external group ID while human observations use the canonical runtime
        # ID, which can otherwise make the hard consecutive-bot guard permanent.
        reservation = None
        if session_id.endswith("@chatroom") and self._speech_ledger is not None:
            reservation = await self._speech_ledger.reserve(
                tenant_id=tenant_id,
                session_id=session_id,
                idempotency_key=f"wxbot-report:{job_id}",
                output_kind="report",
                speech_class="required_delivery",
                text=text,
                metadata={
                    "job_id": job_id,
                    "report_type": report_type,
                    "period_key": str(job.get("period_key") or ""),
                },
            )
            if not reservation.allowed:
                reason = f"speech_budget:{reservation.reason}"
                if not await self._store.mark_report_delivery_failed(
                    job_id,
                    reason,
                    delivery_attempt=delivery_attempt,
                ):
                    return False
                logger.error(
                    "wxbot.report_required_delivery_denied",
                    job_id=job_id,
                    reason=reservation.reason,
                )
                return False
        if not await self._scope_execution_allowed(tenant_id, session_id):
            if reservation is not None and self._speech_ledger is not None:
                await self._speech_ledger.release(
                    reservation,
                    reason="scope_execution_denied",
                )
            await self._store.release_report_delivery(
                job_id,
                delivery_attempt=delivery_attempt,
                reason="scope_execution_denied",
            )
            return False
        try:
            sdk_result = await self.sdk_request(
                "POST",
                "/send",
                json_body={
                    "session_id": str(job["session_id"]),
                    "session_name": canonical_session_name,
                    "sender_name": "",
                    "text": text,
                    "msg_type": "text",
                },
                request_headers={"Idempotency-Key": f"wxbot-report:{job_id}"},
            )
            sdk_row_id = self.sdk_outbound_id(sdk_result)
        except Exception as exc:
            if reservation is not None and self._speech_ledger is not None:
                await self._speech_ledger.release(
                    reservation,
                    reason="report_delivery_failed",
                )
            updated = await self._store.mark_report_delivery_failed(
                job_id,
                f"sdk request failed: {exc.__class__.__name__}",
                delivery_attempt=delivery_attempt,
            )
            if not updated:
                return False
            logger.warning(
                "wxbot.report_delivery_failed",
                job_id=job_id,
                error_class=exc.__class__.__name__,
            )
            raise
        queued = await self._store.mark_report_delivery_queued(
            job_id,
            delivery_attempt=delivery_attempt,
            sdk_outbound_id=sdk_row_id,
        )
        if not queued:
            logger.warning(
                "wxbot.report_delivery_queue_checkpoint_lost",
                job_id=job_id,
                sdk_outbound_id=sdk_row_id,
                delivery_attempt=delivery_attempt,
            )
            return False
        if reservation is not None and self._speech_ledger is not None:
            await self._speech_ledger.commit(
                reservation,
                provider_message_id=str(sdk_row_id),
            )
        result = await self.reconcile_report_delivery(job_id)
        return str(result.get("status") or "") == "sent"
