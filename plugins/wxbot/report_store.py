"""Report and self-review persistence mixin for :mod:`plugins.wxbot.store`."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from app.infra.db import get_engine


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    cleaned = str(value).strip().lower()
    if cleaned in {"1", "true", "yes", "y", "on"}:
        return True
    if cleaned in {"0", "false", "no", "n", "off", ""}:
        return False
    return default


async def _exec(sql: str, params: dict | None = None) -> list[dict]:
    engine = get_engine()
    async with engine.begin() as conn:
        result = await conn.execute(text(sql), params or {})
        if result.returns_rows:
            return [dict(row._mapping) for row in result.fetchall()]
        return []


class WxbotReportStoreMixin:
    def _report_job_stale_seconds(self, stale_seconds: float | None) -> float:
        if stale_seconds is None:
            try:
                stage_timeout = float(
                    getattr(
                        getattr(self, "settings", None),
                        "wxbot_report_stage_timeout_seconds",
                        240.0,
                    )
                    or 240.0
                )
            except (TypeError, ValueError):
                stage_timeout = 240.0
            stale_seconds = max(3600.0, stage_timeout * 10.0)
        try:
            return max(60.0, float(stale_seconds))
        except (TypeError, ValueError):
            return 3600.0

    @staticmethod
    def _hydrate_report_job(row: dict | None) -> dict | None:
        if not row:
            return None
        item = dict(row)
        raw = str(item.get("report_json") or "")
        try:
            item["report_payload"] = json.loads(raw) if raw else {}
        except Exception:
            item["report_payload"] = {}
        item["run_attempt"] = int(item.get("run_attempt") or 0)
        item["delivery_attempt"] = int(item.get("delivery_attempt") or 0)
        item["sdk_outbound_id"] = (
            int(item["sdk_outbound_id"])
            if item.get("sdk_outbound_id") is not None
            else None
        )
        return item

    @staticmethod
    def _hydrate_report_subscription(row: dict | None) -> dict | None:
        if not row:
            return None
        item = dict(row)
        item["daily_enabled"] = bool(item.get("daily_enabled"))
        item["weekly_enabled"] = _coerce_bool(item.get("weekly_enabled"), default=True)
        item["monthly_enabled"] = bool(item.get("monthly_enabled"))
        item["daily_hour"] = int(item.get("daily_hour") or 9)
        item["weekly_day"] = int(item.get("weekly_day") or 1)
        item["weekly_hour"] = int(item.get("weekly_hour") or 9)
        item["monthly_day"] = int(item.get("monthly_day") or 1)
        item["tz"] = str(item.get("tz") or "Asia/Shanghai")
        return item

    async def list_report_subscriptions(self, tenant_id: str) -> list[dict]:
        rows = await _exec(
            "SELECT * FROM plugin_wxbot_report_subscriptions "
            "WHERE tenant_id = :tid ORDER BY session_name, session_id",
            {"tid": tenant_id},
        )
        return [
            item
            for item in (self._hydrate_report_subscription(row) for row in rows)
            if item is not None
        ]

    async def get_report_subscription(self, tenant_id: str, session_id: str) -> dict | None:
        rows = await _exec(
            "SELECT * FROM plugin_wxbot_report_subscriptions "
            "WHERE tenant_id = :tid AND session_id = :sid LIMIT 1",
            {"tid": tenant_id, "sid": session_id},
        )
        return self._hydrate_report_subscription(rows[0]) if rows else None

    async def upsert_report_subscription(
        self,
        tenant_id: str,
        *,
        session_id: str,
        session_name: str,
        daily_enabled: bool = False,
        weekly_enabled: bool = True,
        monthly_enabled: bool = False,
        daily_hour: int = 9,
        weekly_day: int = 1,
        weekly_hour: int = 9,
        monthly_day: int = 1,
        tz: str = "Asia/Shanghai",
    ) -> dict:
        rows = await _exec(
            "INSERT INTO plugin_wxbot_report_subscriptions "
            "(tenant_id, session_id, session_name, daily_enabled, weekly_enabled, monthly_enabled, "
            "daily_hour, weekly_day, weekly_hour, monthly_day, tz, updated_at) "
            "VALUES (:tid, :sid, :sname, :daily_enabled, :weekly_enabled, :monthly_enabled, "
            ":daily_hour, :weekly_day, :weekly_hour, :monthly_day, :tz, NOW()) "
            "ON CONFLICT (tenant_id, session_id) DO UPDATE SET "
            "session_name = EXCLUDED.session_name, "
            "daily_enabled = EXCLUDED.daily_enabled, "
            "weekly_enabled = EXCLUDED.weekly_enabled, "
            "monthly_enabled = EXCLUDED.monthly_enabled, "
            "daily_hour = EXCLUDED.daily_hour, "
            "weekly_day = EXCLUDED.weekly_day, "
            "weekly_hour = EXCLUDED.weekly_hour, "
            "monthly_day = EXCLUDED.monthly_day, "
            "tz = EXCLUDED.tz, "
            "updated_at = EXCLUDED.updated_at "
            "RETURNING *",
            {
                "tid": tenant_id,
                "sid": session_id,
                "sname": session_name,
                "daily_enabled": bool(daily_enabled),
                "weekly_enabled": _coerce_bool(weekly_enabled, default=True),
                "monthly_enabled": bool(monthly_enabled),
                "daily_hour": int(daily_hour),
                "weekly_day": int(weekly_day),
                "weekly_hour": int(weekly_hour),
                "monthly_day": int(monthly_day),
                "tz": str(tz or "Asia/Shanghai").strip() or "Asia/Shanghai",
            },
        )
        return self._hydrate_report_subscription(rows[0])  # type: ignore[arg-type]

    async def delete_report_subscription(self, tenant_id: str, session_id: str) -> bool:
        rows = await _exec(
            "DELETE FROM plugin_wxbot_report_subscriptions "
            "WHERE tenant_id = :tid AND session_id = :sid RETURNING session_id",
            {"tid": tenant_id, "sid": session_id},
        )
        return bool(rows)

    async def list_enabled_report_subscriptions(self, tenant_id: str) -> list[dict]:
        rows = await _exec(
            "SELECT * FROM plugin_wxbot_report_subscriptions "
            "WHERE tenant_id = :tid AND (daily_enabled = TRUE OR weekly_enabled = TRUE OR monthly_enabled = TRUE) "
            "ORDER BY updated_at DESC, session_name, session_id",
            {"tid": tenant_id},
        )
        return [
            item
            for item in (self._hydrate_report_subscription(row) for row in rows)
            if item is not None
        ]

    async def get_or_create_report_job(
        self,
        *,
        tenant_id: str,
        session_id: str,
        session_name: str,
        report_type: str,
        period_key: str,
        period_label: str,
    ) -> dict:
        rows = await _exec(
            "INSERT INTO plugin_wxbot_report_jobs "
            "(tenant_id, session_id, session_name, report_type, period_key, period_label) "
            "VALUES (:tid, :sid, :sname, :rtype, :pkey, :plabel) "
            "ON CONFLICT (tenant_id, session_id, report_type, period_key) "
            "DO UPDATE SET session_name = EXCLUDED.session_name, period_label = EXCLUDED.period_label "
            "RETURNING *",
            {
                "tid": tenant_id,
                "sid": session_id,
                "sname": session_name or "",
                "rtype": report_type,
                "pkey": period_key,
                "plabel": period_label,
            },
        )
        return self._hydrate_report_job(rows[0])  # type: ignore[arg-type]

    async def get_report_job(self, job_id: int) -> dict | None:
        rows = await _exec(
            "SELECT * FROM plugin_wxbot_report_jobs WHERE id = :id",
            {"id": job_id},
        )
        return self._hydrate_report_job(rows[0]) if rows else None

    async def list_completed_report_jobs_for_period_range(
        self,
        *,
        tenant_id: str,
        session_id: str,
        report_type: str,
        period_start: str,
        period_end: str,
    ) -> list[dict]:
        rows = await _exec(
            "SELECT * FROM plugin_wxbot_report_jobs "
            "WHERE tenant_id = :tid AND session_id = :sid AND report_type = :rtype "
            "AND status = 'completed' AND period_key >= :pstart AND period_key <= :pend "
            "ORDER BY period_key ASC, id ASC",
            {
                "tid": tenant_id,
                "sid": session_id,
                "rtype": report_type,
                "pstart": period_start,
                "pend": period_end,
            },
        )
        return [
            item
            for item in (self._hydrate_report_job(row) for row in rows)
            if item is not None and str(item.get("result_text") or "").strip()
        ]

    async def get_report_job_by_scope(
        self,
        *,
        tenant_id: str,
        session_id: str,
        report_type: str,
        period_key: str,
    ) -> dict | None:
        rows = await _exec(
            "SELECT * FROM plugin_wxbot_report_jobs "
            "WHERE tenant_id = :tid AND session_id = :sid AND report_type = :rtype AND period_key = :pkey "
            "LIMIT 1",
            {
                "tid": tenant_id,
                "sid": session_id,
                "rtype": report_type,
                "pkey": period_key,
            },
        )
        return self._hydrate_report_job(rows[0]) if rows else None

    async def update_report_job(
        self,
        job_id: int,
        *,
        status: str,
        current_stage: str,
        msg_count: int | None = None,
        result_text: str | None = None,
        report_payload: dict[str, Any] | None = None,
        error: str | None = None,
        expected_run_attempt: int | None = None,
        expected_status: str | None = None,
    ) -> bool:
        completed = "NOW()" if status in ("completed", "failed") else "NULL"
        conditions = ["id = :id"]
        params: dict[str, Any] = {
            "id": job_id,
            "status": status,
            "current_stage": current_stage,
            "msg_count": msg_count,
            "result_text": result_text,
            "report_json": json.dumps(report_payload, ensure_ascii=False)
            if report_payload is not None
            else None,
            "error": error,
        }
        if expected_run_attempt is not None:
            conditions.append("run_attempt = :expected_run_attempt")
            params["expected_run_attempt"] = int(expected_run_attempt)
            expected_status = expected_status or "running"
        if expected_status is not None:
            conditions.append("status = :expected_status")
            params["expected_status"] = expected_status
        rows = await _exec(
            f"UPDATE plugin_wxbot_report_jobs SET "
            f"status = :status, current_stage = :current_stage, "
            f"msg_count = COALESCE(:msg_count, msg_count), "
            f"result_text = COALESCE(:result_text, result_text), "
            f"report_json = COALESCE(:report_json, report_json), "
            f"error = COALESCE(:error, error), updated_at = NOW(), completed_at = {completed} "
            f"WHERE {' AND '.join(conditions)} RETURNING id",
            params,
        )
        return bool(rows)

    async def try_start_report_job(
        self,
        job_id: int,
        *,
        stale_seconds: float | None = None,
    ) -> int | None:
        stale_seconds = self._report_job_stale_seconds(stale_seconds)
        rows = await _exec(
            "UPDATE plugin_wxbot_report_jobs "
            "SET status = 'running', current_stage = 'collect_messages', error = '', "
            "run_attempt = run_attempt + 1, updated_at = NOW(), completed_at = NULL "
            "WHERE id = :id AND (status IN ('pending', 'failed') OR ("
            "status = 'running' AND updated_at < NOW() - (:stale_seconds * INTERVAL '1 second')"
            ")) RETURNING run_attempt",
            {"id": job_id, "stale_seconds": stale_seconds},
        )
        return int(rows[0]["run_attempt"]) if rows else None

    async def try_start_report_delivery(
        self,
        job_id: int,
        *,
        stale_seconds: float | None = None,
    ) -> int | None:
        stale_seconds = self._report_job_stale_seconds(stale_seconds)
        expired = await _exec(
            "UPDATE plugin_wxbot_report_jobs "
            "SET delivery_status = 'indeterminate', "
            "delivery_error = 'delivery lease expired with unknown SDK outcome', updated_at = NOW() "
            "WHERE id = :id AND delivery_status = 'sending' "
            "AND updated_at < NOW() - (:stale_seconds * INTERVAL '1 second') "
            "RETURNING id",
            {"id": job_id, "stale_seconds": stale_seconds},
        )
        if expired:
            return None
        rows = await _exec(
            "UPDATE plugin_wxbot_report_jobs "
            "SET delivery_status = 'sending', delivery_error = '', "
            "delivery_attempt = delivery_attempt + 1, sdk_outbound_id = NULL, "
            "delivery_queued_at = NULL, delivery_checked_at = NULL, "
            "delivered_at = NULL, updated_at = NOW() "
            "WHERE id = :id AND delivery_status IN ('pending', 'failed') "
            "RETURNING delivery_attempt",
            {"id": job_id},
        )
        return int(rows[0]["delivery_attempt"]) if rows else None

    async def mark_report_delivery_queued(
        self,
        job_id: int,
        *,
        delivery_attempt: int,
        sdk_outbound_id: int,
    ) -> bool:
        """Record the durable SDK queue row before waiting for its outcome."""

        rows = await _exec(
            "UPDATE plugin_wxbot_report_jobs "
            "SET delivery_status = 'queued', sdk_outbound_id = :sdk_outbound_id, "
            "delivery_error = '', delivery_queued_at = NOW(), "
            "delivery_checked_at = NULL, delivered_at = NULL, updated_at = NOW() "
            "WHERE id = :id AND delivery_status = 'sending' "
            "AND delivery_attempt = :delivery_attempt RETURNING id",
            {
                "id": job_id,
                "delivery_attempt": int(delivery_attempt),
                "sdk_outbound_id": int(sdk_outbound_id),
            },
        )
        return bool(rows)

    async def touch_report_delivery_check(
        self,
        job_id: int,
        *,
        delivery_attempt: int,
        sdk_outbound_id: int,
        error: str = "",
    ) -> bool:
        """Record a non-terminal SDK status check using both fencing ids."""

        rows = await _exec(
            "UPDATE plugin_wxbot_report_jobs "
            "SET delivery_checked_at = NOW(), delivery_error = :error, "
            "updated_at = NOW() "
            "WHERE id = :id AND delivery_status = 'queued' "
            "AND delivery_attempt = :delivery_attempt "
            "AND sdk_outbound_id = :sdk_outbound_id RETURNING id",
            {
                "id": job_id,
                "delivery_attempt": int(delivery_attempt),
                "sdk_outbound_id": int(sdk_outbound_id),
                "error": str(error or "")[:500],
            },
        )
        return bool(rows)

    async def mark_report_delivery_terminal(
        self,
        job_id: int,
        *,
        delivery_attempt: int,
        sdk_outbound_id: int,
        status: str,
        error: str = "",
    ) -> bool:
        """CAS a queued SDK delivery to a supported terminal state."""

        terminal_status = str(status or "").strip().lower()
        if terminal_status not in {"sent", "indeterminate"}:
            raise ValueError("report delivery terminal status must be sent or indeterminate")
        rows = await _exec(
            "UPDATE plugin_wxbot_report_jobs "
            "SET delivery_status = :status, delivery_error = :error, "
            "delivery_checked_at = NOW(), "
            "delivered_at = CASE WHEN :is_sent THEN NOW() ELSE NULL END, "
            "updated_at = NOW() "
            "WHERE id = :id AND delivery_status = 'queued' "
            "AND delivery_attempt = :delivery_attempt "
            "AND sdk_outbound_id = :sdk_outbound_id RETURNING id",
            {
                "id": job_id,
                "delivery_attempt": int(delivery_attempt),
                "sdk_outbound_id": int(sdk_outbound_id),
                "status": terminal_status,
                "is_sent": terminal_status == "sent",
                "error": str(error or "")[:500],
            },
        )
        return bool(rows)

    async def list_report_deliveries_to_reconcile(
        self,
        tenant_id: str,
        *,
        limit: int = 100,
    ) -> list[dict]:
        rows = await _exec(
            "SELECT * FROM plugin_wxbot_report_jobs "
            "WHERE tenant_id = :tid AND delivery_status = 'queued' "
            "AND sdk_outbound_id IS NOT NULL "
            "ORDER BY delivery_checked_at ASC NULLS FIRST, "
            "delivery_queued_at ASC NULLS FIRST, id ASC LIMIT :limit",
            {"tid": tenant_id, "limit": max(1, min(int(limit), 1000))},
        )
        return [
            item
            for item in (self._hydrate_report_job(row) for row in rows)
            if item is not None
        ]

    async def mark_report_delivery_sent(self, job_id: int, *, delivery_attempt: int) -> bool:
        rows = await _exec(
            "UPDATE plugin_wxbot_report_jobs "
            "SET delivery_status = 'sent', delivery_error = '', delivered_at = NOW(), updated_at = NOW() "
            "WHERE id = :id AND delivery_status = 'sending' "
            "AND delivery_attempt = :delivery_attempt RETURNING id",
            {"id": job_id, "delivery_attempt": int(delivery_attempt)},
        )
        return bool(rows)

    async def release_report_delivery(
        self,
        job_id: int,
        *,
        delivery_attempt: int,
        reason: str,
    ) -> bool:
        """Return a claimed delivery to pending before any external send occurs."""

        rows = await _exec(
            "UPDATE plugin_wxbot_report_jobs "
            "SET delivery_status = 'pending', delivery_error = :reason, updated_at = NOW() "
            "WHERE id = :id AND delivery_status = 'sending' "
            "AND delivery_attempt = :delivery_attempt RETURNING id",
            {
                "id": job_id,
                "reason": str(reason or "")[:500],
                "delivery_attempt": int(delivery_attempt),
            },
        )
        return bool(rows)

    async def mark_report_delivery_failed(
        self,
        job_id: int,
        error: str,
        *,
        delivery_attempt: int,
    ) -> bool:
        rows = await _exec(
            "UPDATE plugin_wxbot_report_jobs "
            "SET delivery_status = 'failed', delivery_error = :error, updated_at = NOW() "
            "WHERE id = :id AND delivery_status = 'sending' "
            "AND delivery_attempt = :delivery_attempt RETURNING id",
            {
                "id": job_id,
                "error": str(error or "")[:500],
                "delivery_attempt": int(delivery_attempt),
            },
        )
        return bool(rows)

    async def mark_report_delivery_indeterminate(
        self,
        job_id: int,
        error: str,
        *,
        delivery_attempt: int,
    ) -> bool:
        """Fence a delivery whose SDK outcome cannot be proven without resending."""

        rows = await _exec(
            "UPDATE plugin_wxbot_report_jobs "
            "SET delivery_status = 'indeterminate', delivery_error = :error, updated_at = NOW() "
            "WHERE id = :id AND delivery_status = 'sending' "
            "AND delivery_attempt = :delivery_attempt RETURNING id",
            {
                "id": job_id,
                "error": str(error or "")[:500],
                "delivery_attempt": int(delivery_attempt),
            },
        )
        return bool(rows)

    async def fail_stale_report_jobs(
        self,
        *,
        stale_seconds: float | None = None,
    ) -> None:
        stale_seconds = self._report_job_stale_seconds(stale_seconds)
        params = {"stale_seconds": stale_seconds}
        await _exec(
            "UPDATE plugin_wxbot_report_jobs "
            "SET status = 'failed', error = 'job interrupted by service restart', updated_at = NOW(), completed_at = NOW() "
            "WHERE status = 'running' "
            "AND updated_at < NOW() - (:stale_seconds * INTERVAL '1 second')",
            params,
        )
        await _exec(
            "UPDATE plugin_wxbot_report_jobs "
            "SET delivery_status = 'indeterminate', "
            "delivery_error = 'delivery interrupted by service restart; SDK outcome unknown', updated_at = NOW() "
            "WHERE delivery_status = 'sending' "
            "AND updated_at < NOW() - (:stale_seconds * INTERVAL '1 second')",
            params,
        )

    # ── Self review jobs / cache ──

    @staticmethod
    def _hydrate_self_review_subscription(row: dict | None) -> dict | None:
        if not row:
            return None
        item = dict(row)
        item["enabled"] = _coerce_bool(item.get("enabled"))
        item["daily_hour"] = int(item.get("daily_hour") or 23)
        item["tz"] = str(item.get("tz") or "Asia/Shanghai")
        item["focus_mode"] = str(item.get("focus_mode") or "bot_interactions")
        item["auto_create_kb_doc"] = _coerce_bool(item.get("auto_create_kb_doc"), default=True)
        return item

    @staticmethod
    def _hydrate_self_review_job(row: dict | None) -> dict | None:
        if not row:
            return None
        item = dict(row)
        raw = str(item.get("review_json") or "")
        try:
            item["review_payload"] = json.loads(raw) if raw else {}
        except Exception:
            item["review_payload"] = {}
        item["run_attempt"] = int(item.get("run_attempt") or 0)
        item["kb_doc_id"] = int(item["kb_doc_id"]) if item.get("kb_doc_id") is not None else None
        return item

    async def list_self_review_subscriptions(self, tenant_id: str) -> list[dict]:
        rows = await _exec(
            "SELECT * FROM plugin_wxbot_self_review_subscriptions "
            "WHERE tenant_id = :tid ORDER BY session_name, session_id",
            {"tid": tenant_id},
        )
        return [
            item
            for item in (self._hydrate_self_review_subscription(row) for row in rows)
            if item is not None
        ]

    async def get_self_review_subscription(self, tenant_id: str, session_id: str) -> dict | None:
        rows = await _exec(
            "SELECT * FROM plugin_wxbot_self_review_subscriptions "
            "WHERE tenant_id = :tid AND session_id = :sid LIMIT 1",
            {"tid": tenant_id, "sid": session_id},
        )
        return self._hydrate_self_review_subscription(rows[0]) if rows else None

    async def upsert_self_review_subscription(
        self,
        tenant_id: str,
        *,
        session_id: str,
        session_name: str,
        enabled: bool = False,
        daily_hour: int = 23,
        tz: str = "Asia/Shanghai",
        focus_mode: str = "bot_interactions",
        auto_create_kb_doc: bool = True,
    ) -> dict:
        rows = await _exec(
            "INSERT INTO plugin_wxbot_self_review_subscriptions "
            "(tenant_id, session_id, session_name, enabled, daily_hour, tz, focus_mode, auto_create_kb_doc, updated_at) "
            "VALUES (:tid, :sid, :sname, :enabled, :daily_hour, :tz, :focus_mode, :auto_create_kb_doc, NOW()) "
            "ON CONFLICT (tenant_id, session_id) DO UPDATE SET "
            "session_name = EXCLUDED.session_name, "
            "enabled = EXCLUDED.enabled, "
            "daily_hour = EXCLUDED.daily_hour, "
            "tz = EXCLUDED.tz, "
            "focus_mode = EXCLUDED.focus_mode, "
            "auto_create_kb_doc = EXCLUDED.auto_create_kb_doc, "
            "updated_at = EXCLUDED.updated_at "
            "RETURNING *",
            {
                "tid": tenant_id,
                "sid": session_id,
                "sname": session_name,
                "enabled": _coerce_bool(enabled),
                "daily_hour": int(daily_hour),
                "tz": str(tz or "Asia/Shanghai").strip() or "Asia/Shanghai",
                "focus_mode": str(focus_mode or "bot_interactions").strip() or "bot_interactions",
                "auto_create_kb_doc": _coerce_bool(auto_create_kb_doc, default=True),
            },
        )
        return self._hydrate_self_review_subscription(rows[0])  # type: ignore[arg-type]

    async def delete_self_review_subscription(self, tenant_id: str, session_id: str) -> bool:
        rows = await _exec(
            "DELETE FROM plugin_wxbot_self_review_subscriptions "
            "WHERE tenant_id = :tid AND session_id = :sid RETURNING session_id",
            {"tid": tenant_id, "sid": session_id},
        )
        return bool(rows)

    async def list_enabled_self_review_subscriptions(self, tenant_id: str) -> list[dict]:
        rows = await _exec(
            "SELECT * FROM plugin_wxbot_self_review_subscriptions "
            "WHERE tenant_id = :tid AND enabled = TRUE "
            "ORDER BY updated_at DESC, session_name, session_id",
            {"tid": tenant_id},
        )
        return [
            item
            for item in (self._hydrate_self_review_subscription(row) for row in rows)
            if item is not None
        ]

    async def get_or_create_self_review_job(
        self,
        *,
        tenant_id: str,
        session_id: str,
        session_name: str,
        period_key: str,
        period_label: str,
    ) -> dict:
        rows = await _exec(
            "INSERT INTO plugin_wxbot_self_review_jobs "
            "(tenant_id, session_id, session_name, period_key, period_label) "
            "VALUES (:tid, :sid, :sname, :pkey, :plabel) "
            "ON CONFLICT (tenant_id, session_id, period_key) "
            "DO UPDATE SET session_name = EXCLUDED.session_name, period_label = EXCLUDED.period_label "
            "RETURNING *",
            {
                "tid": tenant_id,
                "sid": session_id,
                "sname": session_name or "",
                "pkey": period_key,
                "plabel": period_label,
            },
        )
        return self._hydrate_self_review_job(rows[0])  # type: ignore[arg-type]

    async def get_self_review_job(self, job_id: int) -> dict | None:
        rows = await _exec(
            "SELECT * FROM plugin_wxbot_self_review_jobs WHERE id = :id",
            {"id": job_id},
        )
        return self._hydrate_self_review_job(rows[0]) if rows else None

    async def get_self_review_job_by_scope(
        self,
        *,
        tenant_id: str,
        session_id: str,
        period_key: str,
    ) -> dict | None:
        rows = await _exec(
            "SELECT * FROM plugin_wxbot_self_review_jobs "
            "WHERE tenant_id = :tid AND session_id = :sid AND period_key = :pkey "
            "LIMIT 1",
            {
                "tid": tenant_id,
                "sid": session_id,
                "pkey": period_key,
            },
        )
        return self._hydrate_self_review_job(rows[0]) if rows else None

    async def list_self_review_jobs(
        self,
        tenant_id: str,
        *,
        session_id: str = "",
        limit: int = 20,
    ) -> list[dict]:
        clauses = ["tenant_id = :tid"]
        params: dict[str, Any] = {
            "tid": tenant_id,
            "lim": max(1, min(int(limit or 20), 200)),
        }
        if session_id.strip():
            clauses.append("session_id = :sid")
            params["sid"] = session_id.strip()
        rows = await _exec(
            "SELECT * FROM plugin_wxbot_self_review_jobs "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC, id DESC LIMIT :lim",
            params,
        )
        return [
            item
            for item in (self._hydrate_self_review_job(row) for row in rows)
            if item is not None
        ]

    async def update_self_review_job(
        self,
        job_id: int,
        *,
        status: str,
        current_stage: str,
        msg_count: int | None = None,
        result_text: str | None = None,
        review_payload: dict[str, Any] | None = None,
        kb_doc_id: int | None = None,
        kb_doc_title: str | None = None,
        error: str | None = None,
        expected_run_attempt: int | None = None,
        expected_status: str | None = None,
    ) -> bool:
        completed = "NOW()" if status in ("completed", "failed") else "NULL"
        conditions = ["id = :id"]
        params: dict[str, Any] = {
            "id": job_id,
            "status": status,
            "current_stage": current_stage,
            "msg_count": msg_count,
            "result_text": result_text,
            "review_json": json.dumps(review_payload, ensure_ascii=False)
            if review_payload is not None
            else None,
            "kb_doc_id": kb_doc_id,
            "kb_doc_title": kb_doc_title,
            "error": error,
        }
        if expected_run_attempt is not None:
            conditions.append("run_attempt = :expected_run_attempt")
            params["expected_run_attempt"] = int(expected_run_attempt)
            expected_status = expected_status or "running"
        if expected_status is not None:
            conditions.append("status = :expected_status")
            params["expected_status"] = expected_status
        rows = await _exec(
            f"UPDATE plugin_wxbot_self_review_jobs SET "
            f"status = :status, current_stage = :current_stage, "
            f"msg_count = COALESCE(:msg_count, msg_count), "
            f"result_text = COALESCE(:result_text, result_text), "
            f"review_json = COALESCE(:review_json, review_json), "
            f"kb_doc_id = COALESCE(:kb_doc_id, kb_doc_id), "
            f"kb_doc_title = COALESCE(:kb_doc_title, kb_doc_title), "
            f"error = COALESCE(:error, error), updated_at = NOW(), completed_at = {completed} "
            f"WHERE {' AND '.join(conditions)} RETURNING id",
            params,
        )
        return bool(rows)

    async def try_start_self_review_job(
        self,
        job_id: int,
        *,
        stale_seconds: float | None = None,
    ) -> int | None:
        stale_seconds = self._report_job_stale_seconds(stale_seconds)
        rows = await _exec(
            "UPDATE plugin_wxbot_self_review_jobs "
            "SET status = 'running', current_stage = 'collect_messages', error = '', "
            "run_attempt = run_attempt + 1, updated_at = NOW(), completed_at = NULL "
            "WHERE id = :id AND (status IN ('pending', 'failed') OR ("
            "status = 'running' AND updated_at < NOW() - (:stale_seconds * INTERVAL '1 second')"
            ")) RETURNING run_attempt",
            {"id": job_id, "stale_seconds": stale_seconds},
        )
        return int(rows[0]["run_attempt"]) if rows else None

    async def fail_stale_self_review_jobs(
        self,
        *,
        stale_seconds: float | None = None,
    ) -> None:
        stale_seconds = self._report_job_stale_seconds(stale_seconds)
        await _exec(
            "UPDATE plugin_wxbot_self_review_jobs "
            "SET status = 'failed', error = 'job interrupted by service restart', updated_at = NOW(), completed_at = NOW() "
            "WHERE status = 'running' "
            "AND updated_at < NOW() - (:stale_seconds * INTERVAL '1 second')",
            {"stale_seconds": stale_seconds},
        )
