from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.common.exceptions import UpstreamUnavailable
from app.common.types import ChatResponse
from plugins.wxbot import reports, self_review


class _FixedDateTime:
    @classmethod
    def now(cls, tz=None):
        base = datetime(2026, 4, 22, 14, 0, 0)
        if tz is None:
            return base
        return base.replace(tzinfo=tz)


async def _allow_scope(_tenant_id: str, _session_id: str) -> bool:
    return True


class _MutableScopeGate:
    def __init__(self) -> None:
        self.allowed: object = True
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, tenant_id: str, session_id: str) -> object:
        self.calls.append((tenant_id, session_id))
        return self.allowed


class _CaptureLlm:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.requests = []
        self.error = error

    async def chat(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        content = str(request.messages[0].content)
        if "已完成日报" in content:
            return ChatResponse(content="## 本周摘要\n- 汇总日报内容", model="gpt-test")
        if "已完成周报" in content:
            return ChatResponse(content="## 本月摘要\n- 汇总周报内容", model="gpt-test")
        return ChatResponse(content="今日话题：\n- 修复报告链路\n一句话总结：\n报告更稳", model="gpt-test")


class _ScopeDisablingLlm(_CaptureLlm):
    def __init__(self, gate: _MutableScopeGate) -> None:
        super().__init__()
        self.gate = gate

    async def chat(self, request):
        response = await super().chat(request)
        self.gate.allowed = False
        return response


class _ReportStore:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(
            wxbot_default_tenant_id="default",
            wxbot_report_stage_timeout_seconds=5.0,
            wxbot_report_max_chars_per_chunk=reports._REPORT_MAX_CHARS_PER_CHUNK,
            wxbot_report_transient_backoff_seconds=900.0,
            wxbot_daily_report_footer="",
        )
        self.jobs = {
            1: {
                "id": 1,
                "tenant_id": "default",
                "session_id": "room@chatroom",
                "session_name": "测试群",
                "report_type": "daily",
                "period_key": "2026-04-21",
                "period_label": "2026-04-21",
                "status": "pending",
                "current_stage": "queued",
                "msg_count": 0,
                "result_text": "",
                "report_payload": {},
                "run_attempt": 0,
                "delivery_status": "pending",
                "delivery_attempt": 0,
                "sdk_outbound_id": None,
                "error": "",
            }
        }
        self.completed_queries: list[dict[str, str]] = []

    async def try_start_report_job(self, job_id: int) -> int | None:
        job = self.jobs[job_id]
        if job["status"] not in {"pending", "failed"}:
            return None
        job["status"] = "running"
        job["run_attempt"] = int(job.get("run_attempt") or 0) + 1
        job["current_stage"] = "collect_messages"
        job["error"] = ""
        return int(job["run_attempt"])

    async def get_report_job(self, job_id: int) -> dict[str, object] | None:
        job = self.jobs.get(job_id)
        return dict(job) if job else None

    async def try_start_report_delivery(self, job_id: int) -> int | None:
        job = self.jobs[job_id]
        if str(job.get("delivery_status") or "pending") not in {"pending", "failed"}:
            return None
        job["delivery_status"] = "sending"
        job["delivery_attempt"] = int(job.get("delivery_attempt") or 0) + 1
        job["delivery_error"] = ""
        return int(job["delivery_attempt"])

    async def mark_report_delivery_sent(
        self,
        job_id: int,
        *,
        delivery_attempt: int,
    ) -> bool:
        job = self.jobs[job_id]
        if (
            job.get("delivery_status") != "sending"
            or int(job.get("delivery_attempt") or 0) != delivery_attempt
        ):
            return False
        job["delivery_status"] = "sent"
        job["delivery_error"] = ""
        return True

    async def mark_report_delivery_queued(
        self,
        job_id: int,
        *,
        delivery_attempt: int,
        sdk_outbound_id: int,
    ) -> bool:
        job = self.jobs[job_id]
        if (
            job.get("delivery_status") != "sending"
            or int(job.get("delivery_attempt") or 0) != delivery_attempt
        ):
            return False
        job["delivery_status"] = "queued"
        job["sdk_outbound_id"] = int(sdk_outbound_id)
        job["delivery_error"] = ""
        return True

    async def touch_report_delivery_check(
        self,
        job_id: int,
        *,
        delivery_attempt: int,
        sdk_outbound_id: int,
        error: str = "",
    ) -> bool:
        job = self.jobs[job_id]
        if (
            job.get("delivery_status") != "queued"
            or int(job.get("delivery_attempt") or 0) != delivery_attempt
            or int(job.get("sdk_outbound_id") or 0) != sdk_outbound_id
        ):
            return False
        job["delivery_error"] = error
        return True

    async def mark_report_delivery_terminal(
        self,
        job_id: int,
        *,
        delivery_attempt: int,
        sdk_outbound_id: int,
        status: str,
        error: str = "",
    ) -> bool:
        job = self.jobs[job_id]
        if (
            job.get("delivery_status") != "queued"
            or int(job.get("delivery_attempt") or 0) != delivery_attempt
            or int(job.get("sdk_outbound_id") or 0) != sdk_outbound_id
        ):
            return False
        job["delivery_status"] = status
        job["delivery_error"] = error
        return True

    async def mark_report_delivery_failed(
        self,
        job_id: int,
        error: str,
        *,
        delivery_attempt: int,
    ) -> bool:
        job = self.jobs[job_id]
        if (
            job.get("delivery_status") != "sending"
            or int(job.get("delivery_attempt") or 0) != delivery_attempt
        ):
            return False
        job["delivery_status"] = "failed"
        job["delivery_error"] = error
        return True

    async def release_report_delivery(
        self,
        job_id: int,
        *,
        delivery_attempt: int,
        reason: str,
    ) -> bool:
        job = self.jobs[job_id]
        if (
            job.get("delivery_status") != "sending"
            or int(job.get("delivery_attempt") or 0) != delivery_attempt
        ):
            return False
        job["delivery_status"] = "pending"
        job["delivery_error"] = reason
        return True

    async def update_report_job(
        self,
        job_id: int,
        *,
        status: str,
        current_stage: str,
        msg_count: int | None = None,
        result_text: str | None = None,
        report_payload: dict[str, object] | None = None,
        error: str | None = None,
        expected_run_attempt: int | None = None,
        expected_status: str | None = None,
    ) -> bool:
        job = self.jobs[job_id]
        if expected_run_attempt is not None and int(job.get("run_attempt") or 0) != expected_run_attempt:
            return False
        if expected_status is not None and job.get("status") != expected_status:
            return False
        job["status"] = status
        job["current_stage"] = current_stage
        if msg_count is not None:
            job["msg_count"] = msg_count
        if result_text is not None:
            job["result_text"] = result_text
        if report_payload is not None:
            job["report_payload"] = report_payload
        if error is not None:
            job["error"] = error
        return True

    async def list_completed_report_jobs_for_period_range(
        self,
        *,
        tenant_id: str,
        session_id: str,
        report_type: str,
        period_start: str,
        period_end: str,
    ) -> list[dict[str, object]]:
        self.completed_queries.append(
            {
                "tenant_id": tenant_id,
                "session_id": session_id,
                "report_type": report_type,
                "period_start": period_start,
                "period_end": period_end,
            }
        )
        rows = [
            dict(item)
            for item in self.jobs.values()
            if item["tenant_id"] == tenant_id
            and item["session_id"] == session_id
            and item["report_type"] == report_type
            and item["status"] == "completed"
            and period_start <= str(item["period_key"]) <= period_end
            and str(item.get("result_text") or "").strip()
        ]
        return sorted(rows, key=lambda item: (str(item["period_key"]), int(item["id"])))


class _ReportService(reports.WxbotReportService):
    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("scope_execution_allowed", _allow_scope)
        super().__init__(*args, **kwargs)
        self.fetch_calls = 0

    async def fetch_report_messages_payload(self, *_args, **_kwargs):
        self.fetch_calls += 1
        text = "x" * 6000
        return {
            "period": "2026-04-21",
            "messages": [
                {
                    "sender_wxid": "wxid_a",
                    "sender_name": "Alice",
                    "text": text,
                    "timestamp": "2026-04-21 09:00:00",
                }
            ],
        }


class _CaptureBridge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object] | None, dict[str, object] | None]] = []
        self.request_headers: list[dict[str, str] | None] = []
        self.outbound_status = "sent"
        self.outbound_error = ""
        self.detail_error_status = 0
        self.list_reply_text = "日报正文"

    async def sdk_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, object] | None = None,
        json_body: dict[str, object] | None = None,
        request_headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        self.calls.append((method, path, params, json_body))
        if request_headers is not None:
            self.request_headers.append(request_headers)
        if path == "/ext/roster/groups":
            return {
                "sessions": [
                    {
                        "session_id": "room@chatroom",
                        "session_name": "测试群",
                    }
                ]
            }
        if path == "/send":
            return {"queued": True, "id": 1}
        if path == "/queue/messages/1":
            if self.detail_error_status:
                from fastapi import HTTPException

                raise HTTPException(self.detail_error_status, "detail unavailable")
            return {
                "id": 1,
                "session_id": "room@chatroom",
                "session_name": "测试群",
                "command_id": "wxbot-report:1",
                "status": self.outbound_status,
                "error": self.outbound_error,
            }
        if path == "/queue/messages":
            return {
                "count": 1,
                "items": [
                    {
                        "id": 1,
                        "session_id": "room@chatroom",
                        "session_name": "测试群",
                        "idempotency_key": "",
                        "reply_text": self.list_reply_text,
                        "status": self.outbound_status,
                        "error": self.outbound_error,
                    }
                ],
            }
        return {"ok": True}


class _CaptureSpeechLedger:
    def __init__(self) -> None:
        self.reservations: list[dict[str, object]] = []
        self.commits: list[tuple[object, str]] = []

    async def reserve(self, **kwargs: object) -> SimpleNamespace:
        self.reservations.append(dict(kwargs))
        return SimpleNamespace(
            allowed=True,
            reason="obligation_bypass",
            idempotency_key=str(kwargs.get("idempotency_key") or ""),
            output_kind=str(kwargs.get("output_kind") or ""),
            speech_class=str(kwargs.get("speech_class") or ""),
        )

    async def commit(
        self,
        reservation: object,
        *,
        provider_message_id: str = "",
    ) -> None:
        self.commits.append((reservation, provider_message_id))

    async def release(self, _reservation: object, *, reason: str = "") -> None:
        _ = reason


class _SelfReviewStore:
    def __init__(
        self,
        *,
        auto_create_kb_doc: object = False,
        requested_auto_create_kb_doc: object | None = None,
    ) -> None:
        self.settings = SimpleNamespace(
            wxbot_default_tenant_id="default",
            wxbot_report_stage_timeout_seconds=5.0,
        )
        self.subscription = {
            "tenant_id": "default",
            "session_id": "room@chatroom",
            "session_name": "测试群",
            "focus_mode": "bot_interactions",
            "auto_create_kb_doc": auto_create_kb_doc,
        }
        self.jobs = {
            1: {
                "id": 1,
                "tenant_id": "default",
                "session_id": "room@chatroom",
                "session_name": "测试群",
                "period_key": "2026-04-21",
                "period_label": "2026-04-21",
                "status": "pending",
                "current_stage": "queued",
                "msg_count": 0,
                "result_text": "",
                "review_payload": (
                    {"requested_auto_create_kb_doc": requested_auto_create_kb_doc}
                    if requested_auto_create_kb_doc is not None
                    else {}
                ),
                "run_attempt": 0,
                "kb_doc_id": None,
                "kb_doc_title": "",
                "error": "",
            }
        }

    async def try_start_self_review_job(self, job_id: int) -> int | None:
        job = self.jobs[job_id]
        if job["status"] not in {"pending", "failed"}:
            return None
        job["status"] = "running"
        job["run_attempt"] = int(job.get("run_attempt") or 0) + 1
        job["current_stage"] = "collect_messages"
        job["error"] = ""
        return int(job["run_attempt"])

    async def get_self_review_job(self, job_id: int) -> dict[str, object] | None:
        job = self.jobs.get(job_id)
        return dict(job) if job else None

    async def get_self_review_subscription(self, tenant_id: str, session_id: str) -> dict[str, object] | None:
        assert tenant_id == "default"
        assert session_id == "room@chatroom"
        return dict(self.subscription)

    async def update_self_review_job(
        self,
        job_id: int,
        *,
        status: str,
        current_stage: str,
        msg_count: int | None = None,
        result_text: str | None = None,
        review_payload: dict[str, object] | None = None,
        kb_doc_id: int | None = None,
        kb_doc_title: str | None = None,
        error: str | None = None,
        expected_run_attempt: int | None = None,
        expected_status: str | None = None,
    ) -> bool:
        job = self.jobs[job_id]
        if expected_run_attempt is not None and int(job.get("run_attempt") or 0) != expected_run_attempt:
            return False
        if expected_status is not None and job.get("status") != expected_status:
            return False
        job["status"] = status
        job["current_stage"] = current_stage
        if msg_count is not None:
            job["msg_count"] = msg_count
        if result_text is not None:
            job["result_text"] = result_text
        if review_payload is not None:
            job["review_payload"] = review_payload
        if kb_doc_id is not None:
            job["kb_doc_id"] = kb_doc_id
        if kb_doc_title is not None:
            job["kb_doc_title"] = kb_doc_title
        if error is not None:
            job["error"] = error
        return True


class _SelfReviewService(self_review.WxbotSelfReviewService):
    def __init__(
        self,
        store: _SelfReviewStore,
        *,
        scope_execution_allowed=_allow_scope,
    ) -> None:
        super().__init__(
            store,
            SimpleNamespace(llm_service=None),
            scope_execution_allowed=scope_execution_allowed,
        )
        self.kb_write_calls: list[dict[str, object]] = []
        self.kb_write_error: Exception | None = None

    async def fetch_messages_payload(self, *_args, **_kwargs):
        return {
            "messages": [
                {
                    "timestamp": "2026-04-21 09:00:00",
                    "sender_wxid": "wxid_user",
                    "sender_name": "Alice",
                    "msg_type": "text",
                    "text": "@机器人 帮我看看订单",
                    "is_self_sent": False,
                },
                {
                    "timestamp": "2026-04-21 09:00:03",
                    "sender_wxid": "wxid_bot",
                    "sender_name": "机器人",
                    "msg_type": "text",
                    "text": "我来查询。",
                    "is_self_sent": True,
                },
            ],
        }

    async def _call_llm(self, **_kwargs) -> str:
        return "## 复盘\n- 机器人有一次有效响应"

    async def _write_kb_document(self, **kwargs) -> int:
        self.kb_write_calls.append(dict(kwargs))
        if self.kb_write_error is not None:
            raise self.kb_write_error
        return 99


class _ScopeDisablingSelfReviewService(_SelfReviewService):
    def __init__(self, store: _SelfReviewStore, gate: _MutableScopeGate) -> None:
        super().__init__(store, scope_execution_allowed=gate)  # type: ignore[arg-type]
        self.gate = gate

    async def _call_llm(self, **_kwargs) -> str:
        self.gate.allowed = False
        return "## 复盘\n- 不应由已禁用的 attempt 完成"


def test_resolve_preview_period_defaults_to_previous_day_and_month(monkeypatch) -> None:
    monkeypatch.setattr(reports, "datetime", _FixedDateTime)

    daily_key, daily_label = reports.resolve_preview_period("daily", tz="Asia/Shanghai")
    monthly_key, monthly_label = reports.resolve_preview_period("monthly", tz="Asia/Shanghai")

    assert (daily_key, daily_label) == ("2026-04-21", "2026-04-21")
    assert (monthly_key, monthly_label) == ("2026-03", "2026-03")


def test_weekly_due_period_monday_generates_previous_monday_sunday() -> None:
    period_key, period_label = reports.resolve_due_period(
        {
            "weekly_enabled": True,
            "weekly_day": 1,
            "weekly_hour": 9,
            "tz": "Asia/Shanghai",
        },
        "weekly",
        now=datetime(2026, 5, 4, 9, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert (period_key, period_label) == ("2026-04-27..2026-05-03", "2026-04-27..2026-05-03")


def test_weekly_due_period_non_due_before_fire_and_wrong_day() -> None:
    sub = {"weekly_enabled": True, "weekly_day": 1, "weekly_hour": 9, "tz": "Asia/Shanghai"}

    assert reports.resolve_due_period(sub, "weekly", now=datetime(2026, 5, 4, 8, 59, tzinfo=ZoneInfo("Asia/Shanghai"))) is None
    assert reports.resolve_due_period(sub, "weekly", now=datetime(2026, 5, 5, 9, 1, tzinfo=ZoneInfo("Asia/Shanghai"))) is None


def test_weekly_due_period_uses_subscription_timezone() -> None:
    due = reports.resolve_due_period(
        {
            "weekly_enabled": True,
            "weekly_day": 1,
            "weekly_hour": 9,
            "tz": "Asia/Shanghai",
        },
        "weekly",
        now=datetime(2026, 5, 4, 1, 1, tzinfo=UTC),
    )

    assert due == ("2026-04-27..2026-05-03", "2026-04-27..2026-05-03")


def test_monthly_due_period_unchanged() -> None:
    due = reports.resolve_due_period(
        {
            "monthly_enabled": True,
            "monthly_day": 1,
            "tz": "Asia/Shanghai",
        },
        "monthly",
        now=datetime(2026, 5, 1, 0, 5, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert due == ("2026-04", "2026-04")


def test_report_scheduler_includes_weekly_jobs() -> None:
    source = Path("plugins/wxbot/plugin.py").read_text(encoding="utf-8")

    assert 'for report_type in ("daily", "weekly", "monthly")' in source


def test_wxbot_schema_migration_has_weekly_subscription_columns() -> None:
    source = Path("migrations/versions/20260718_0016_wxbot_schema.py").read_text(
        encoding="utf-8"
    )

    assert "weekly_enabled" in source
    assert "weekly_day" in source
    assert "weekly_hour" in source


def test_report_llm_metadata_disables_web_search_without_disabling_fallback() -> None:
    metadata = reports.report_llm_metadata({"openai_web_search": True})

    assert metadata["wxbot_report_job"] is True
    assert metadata["openai_web_search"] is False
    assert metadata["web_search"] is False
    assert "disable_openai_fallback" not in metadata


async def test_report_llm_request_disables_web_search_and_allows_openai_fallback() -> None:
    store = _ReportStore()
    llm = _CaptureLlm()
    service = reports.WxbotReportService(store, SimpleNamespace(llm_service=llm))

    result = await service._call_llm(
        trace_id="trace-report",
        system="system",
        user="user",
        max_tokens=100,
    )

    assert result
    metadata = llm.requests[0].metadata
    assert metadata["openai_web_search"] is False
    assert metadata["web_search"] is False
    assert metadata["wxbot_report_job"] is True
    assert "disable_openai_fallback" not in metadata


def test_chunk_report_lines_default_is_12000_and_splits_large_input_more_finely() -> None:
    assert reports._REPORT_MAX_CHARS_PER_CHUNK == 12_000
    lines = [f"{index:04d} " + ("x" * 95) for index in range(300)]

    chunks = reports.chunk_report_lines(lines)

    assert len(chunks) >= 3
    assert all(len(chunk) <= reports._REPORT_MAX_CHARS_PER_CHUNK for chunk in chunks)


def test_chunk_report_lines_splits_single_oversized_line_to_limit() -> None:
    chunks = reports.chunk_report_lines(["x" * 25_000])

    assert len(chunks) == 3
    assert all(len(chunk) <= reports._REPORT_MAX_CHARS_PER_CHUNK for chunk in chunks)


async def test_report_job_transient_llm_failure_records_retry_after_and_preserves_error() -> None:
    store = _ReportStore()
    llm = _CaptureLlm(error=UpstreamUnavailable("openai responses unavailable: 502 Bad Gateway"))
    service = _ReportService(store, SimpleNamespace(llm_service=llm))

    await service.run_report_job(1)

    job = store.jobs[1]
    payload = job["report_payload"]
    assert job["status"] == "failed"
    assert job["current_stage"] == "summarize_chunk_1"
    assert "502 Bad Gateway" in job["error"]
    assert payload["last_error"] == job["error"]
    assert payload["transient_error"] is True
    assert payload["retry_after"]


async def test_report_job_scope_disable_after_llm_defers_without_failure() -> None:
    store = _ReportStore()
    gate = _MutableScopeGate()
    llm = _ScopeDisablingLlm(gate)
    service = _ReportService(
        store,
        SimpleNamespace(llm_service=llm),
        scope_execution_allowed=gate,
    )

    await service.run_report_job(1)

    job = store.jobs[1]
    assert len(llm.requests) == 1
    assert job["status"] == "pending"
    assert job["current_stage"] == "scope_execution_denied"
    assert job["error"] == ""
    assert job["run_attempt"] == 1
    assert "last_failed_at" not in job["report_payload"]


async def test_report_job_missing_scope_gate_fails_closed_before_claim() -> None:
    store = _ReportStore()
    service = reports.WxbotReportService(
        store,
        SimpleNamespace(llm_service=_CaptureLlm()),
    )

    await service.run_report_job(1)

    assert store.jobs[1]["status"] == "pending"
    assert store.jobs[1]["run_attempt"] == 0


async def test_report_job_scope_gate_requires_literal_true() -> None:
    store = _ReportStore()
    gate = _MutableScopeGate()
    gate.allowed = 1
    service = _ReportService(
        store,
        SimpleNamespace(llm_service=_CaptureLlm()),
        scope_execution_allowed=gate,
    )

    await service.run_report_job(1)

    assert store.jobs[1]["status"] == "pending"
    assert store.jobs[1]["run_attempt"] == 0


async def test_weekly_report_uses_completed_daily_jobs_and_does_not_fetch_raw() -> None:
    store = _ReportStore()
    store.jobs[1].update(
        {
            "report_type": "weekly",
            "period_key": "2026-04-20..2026-04-26",
            "period_label": "2026-04-20..2026-04-26",
        }
    )
    for offset, day in enumerate(("2026-04-20", "2026-04-21", "2026-04-23"), start=2):
        store.jobs[offset] = {
            "id": offset,
            "tenant_id": "default",
            "session_id": "room@chatroom",
            "session_name": "测试群",
            "report_type": "daily",
            "period_key": day,
            "period_label": day,
            "status": "completed",
            "current_stage": "completed",
            "msg_count": 1,
            "result_text": f"日报 {day}",
            "report_payload": {},
            "delivery_status": "pending",
            "error": "",
        }
    service = _ReportService(store, SimpleNamespace(llm_service=_CaptureLlm()))

    await service.run_report_job(1)

    job = store.jobs[1]
    payload = job["report_payload"]
    assert service.fetch_calls == 0
    assert job["status"] == "completed"
    assert payload["source_mode"] == "daily_rollup"
    assert payload["source_job_ids"] == [2, 3, 4]
    assert "2026-04-22" in payload["missing_periods"]
    assert "汇总日报内容" in job["result_text"]


async def test_monthly_report_uses_completed_weekly_jobs_and_does_not_fetch_raw() -> None:
    store = _ReportStore()
    store.jobs[1].update(
        {
            "report_type": "monthly",
            "period_key": "2026-04",
            "period_label": "2026-04",
        }
    )
    for offset, week in enumerate(("2026-03-30..2026-04-05", "2026-04-06..2026-04-12"), start=2):
        store.jobs[offset] = {
            "id": offset,
            "tenant_id": "default",
            "session_id": "room@chatroom",
            "session_name": "测试群",
            "report_type": "weekly",
            "period_key": week,
            "period_label": week,
            "status": "completed",
            "current_stage": "completed",
            "msg_count": 1,
            "result_text": f"周报 {week}",
            "report_payload": {},
            "delivery_status": "pending",
            "error": "",
        }
    service = _ReportService(store, SimpleNamespace(llm_service=_CaptureLlm()))

    await service.run_report_job(1)

    job = store.jobs[1]
    payload = job["report_payload"]
    assert service.fetch_calls == 0
    assert job["status"] == "completed"
    assert payload["source_mode"] == "weekly_rollup"
    assert payload["source_job_ids"] == [2, 3]
    assert "2026-04-13..2026-04-19" in payload["missing_periods"]
    assert "汇总周报内容" in job["result_text"]


async def test_rollup_without_lower_reports_completes_with_no_data() -> None:
    store = _ReportStore()
    store.jobs[1].update(
        {
            "report_type": "weekly",
            "period_key": "2026-04-20..2026-04-26",
            "period_label": "2026-04-20..2026-04-26",
        }
    )
    service = _ReportService(store, SimpleNamespace(llm_service=_CaptureLlm()))

    await service.run_report_job(1)

    job = store.jobs[1]
    assert service.fetch_calls == 0
    assert job["status"] == "completed"
    assert job["report_payload"]["no_data"] is True
    assert job["report_payload"]["source_mode"] == "daily_rollup"


def test_failed_transient_report_job_in_backoff_is_deferred_without_clearing_error() -> None:
    error = "openai responses unavailable: 502 Bad Gateway"
    job = {
        "status": "failed",
        "error": error,
        "report_payload": {
            "transient_error": True,
            "retry_after": datetime(2099, 1, 1, tzinfo=UTC).isoformat(),
            "last_error": error,
        },
    }

    assert reports.should_defer_report_job_retry(job) is True
    assert job["error"] == error
    assert job["report_payload"]["last_error"] == error


def test_self_review_preview_period_defaults_to_previous_day(monkeypatch) -> None:
    monkeypatch.setattr(self_review, "datetime", _FixedDateTime)

    period_key, period_label = self_review.resolve_self_review_preview_period(tz="Asia/Shanghai")

    assert (period_key, period_label) == ("2026-04-21", "2026-04-21")


def test_self_review_due_period_targets_previous_day_after_fire_time() -> None:
    period_key, period_label = self_review.resolve_self_review_due_period(
        {
            "enabled": True,
            "daily_hour": 23,
            "tz": "Asia/Shanghai",
        },
        now=datetime(2026, 4, 22, 23, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    assert (period_key, period_label) == ("2026-04-21", "2026-04-21")


async def test_self_review_default_generates_pending_draft_without_kb_write() -> None:
    store = _SelfReviewStore()
    service = _SelfReviewService(store)

    await service.run_self_review_job(1)

    job = store.jobs[1]
    assert job["status"] == "completed"
    assert job["result_text"]
    assert job["review_payload"]["auto_create_kb_doc"] is False
    assert job["review_payload"]["kb_doc_id"] is None
    assert job["review_payload"]["kb_publish_status"] == "pending_review"
    assert service.kb_write_calls == []


async def test_self_review_scope_disable_after_llm_defers_without_failure() -> None:
    store = _SelfReviewStore()
    gate = _MutableScopeGate()
    service = _ScopeDisablingSelfReviewService(store, gate)

    await service.run_self_review_job(1)

    job = store.jobs[1]
    assert job["status"] == "pending"
    assert job["current_stage"] == "scope_execution_denied"
    assert job["error"] == ""
    assert job["run_attempt"] == 1
    assert job["result_text"] == ""


async def test_self_review_legacy_true_flags_never_auto_publish() -> None:
    store = _SelfReviewStore(
        auto_create_kb_doc=True,
        requested_auto_create_kb_doc=True,
    )
    service = _SelfReviewService(store)

    await service.run_self_review_job(1)

    job = store.jobs[1]
    assert job["status"] == "completed"
    assert job["review_payload"]["auto_create_kb_doc"] is False
    assert job["review_payload"]["kb_publish_status"] == "pending_review"
    assert job["kb_doc_id"] is None
    assert service.kb_write_calls == []


async def test_self_review_string_false_skips_kb_write() -> None:
    store = _SelfReviewStore(auto_create_kb_doc="false")
    service = _SelfReviewService(store)

    await service.run_self_review_job(1)

    assert store.jobs[1]["review_payload"]["auto_create_kb_doc"] is False
    assert store.jobs[1]["review_payload"]["kb_publish_status"] == "pending_review"
    assert service.kb_write_calls == []


async def test_self_review_manual_publish_records_review_audit_metadata() -> None:
    store = _SelfReviewStore(auto_create_kb_doc=True)
    service = _SelfReviewService(store)
    await service.run_self_review_job(1)

    result = await service.publish_self_review_job(
        1,
        tenant_id="default",
        actor="admin-user",
        request_id="admin_request_1",
    )

    job = store.jobs[1]
    assert result == {
        "job_id": 1,
        "tenant_id": "default",
        "kb_doc_id": 99,
        "kb_doc_title": "[测试群] 自我迭代复盘 · 2026-04-21",
        "kb_publish_status": "published",
        "idempotent": False,
    }
    assert job["kb_doc_id"] == 99
    assert job["review_payload"]["kb_publish_status"] == "published"
    metadata = service.kb_write_calls[0]["metadata"]
    assert metadata["reviewed"] is True
    assert metadata["reviewed_by"] == "admin-user"
    assert metadata["reviewed_request_id"] == "admin_request_1"
    assert metadata["published_by"] == "admin-user"
    assert metadata["published_request_id"] == "admin_request_1"


async def test_self_review_manual_publish_is_concurrently_idempotent() -> None:
    store = _SelfReviewStore()
    service = _SelfReviewService(store)
    await service.run_self_review_job(1)

    first, second = await asyncio.gather(
        service.publish_self_review_job(
            1,
            tenant_id="default",
            actor="admin",
            request_id="request-a",
        ),
        service.publish_self_review_job(
            1,
            tenant_id="default",
            actor="admin",
            request_id="request-b",
        ),
    )

    assert len(service.kb_write_calls) == 1
    assert {first["kb_doc_id"], second["kb_doc_id"]} == {99}
    assert {first["idempotent"], second["idempotent"]} == {False, True}


async def test_self_review_manual_publish_rejects_cross_tenant_job() -> None:
    store = _SelfReviewStore()
    store.jobs[1]["tenant_id"] = "another-tenant"
    service = _SelfReviewService(store)

    with pytest.raises(self_review.SelfReviewJobNotFound):
        await service.publish_self_review_job(
            1,
            tenant_id="default",
            actor="admin",
            request_id="request-cross-tenant",
        )

    assert service.kb_write_calls == []


async def test_self_review_manual_publish_failure_remains_pending_review() -> None:
    store = _SelfReviewStore()
    service = _SelfReviewService(store)
    await service.run_self_review_job(1)
    service.kb_write_error = RuntimeError("vector index unavailable")

    with pytest.raises(self_review.SelfReviewPublishFailed):
        await service.publish_self_review_job(
            1,
            tenant_id="default",
            actor="admin",
            request_id="request-failed",
        )

    job = store.jobs[1]
    assert job["kb_doc_id"] is None
    assert job["review_payload"]["kb_publish_status"] == "pending_review"
    assert job["review_payload"]["kb_doc_error"] == "vector index unavailable"


def test_build_daily_report_text_matches_old_wxbot_style_sections() -> None:
    report = reports._build_daily_report_text(
        "测试群",
        "2026-04-21",
        [
            {
                "sender_wxid": "wxid_a",
                "sender_name": "Alice",
                "text": "今天先修复 bridge 锁问题",
                "timestamp": "2026/04/21 09:01:00",
            },
            {
                "sender_wxid": "wxid_a",
                "sender_name": "Alice",
                "text": "然后排查 draw 插件",
                "timestamp": "2026/04/21 09:10:00",
            },
            {
                "sender_wxid": "wxid_b",
                "sender_name": "Bob",
                "text": "下午补测试",
                "timestamp": "2026/04/21 15:20:00",
            },
        ],
        topics_text="- Bridge 锁冲突\n- Draw 插件排查",
        summary_text="今天主要在修桥接和插件链路",
        tz_name="Asia/Shanghai",
    )

    assert "[测试群] 日报 · 2026-04-21" in report
    assert "━━━ 活跃概览 ━━━" in report
    assert "━━━ 活跃之星 ━━━" in report
    assert "━━━ 今日话题 ━━━" in report
    assert "━━━ 一句话总结 ━━━" in report
    assert "Alice — 2 条" in report
    assert report.endswith("「今天主要在修桥接和插件链路」")


def test_daily_report_footer_is_opt_in_and_daily_only() -> None:
    footer = "Project: https://example.invalid/project"
    daily_no_data = reports._build_daily_report_text(
        "测试群",
        "2026-04-21",
        [],
        footer=footer,
    )
    monthly_no_data = reports._build_monthly_report_text("测试群", "2026-04", [])
    weekly_rollup = reports._build_rollup_report_text("测试群", "weekly", "2026-04-20..2026-04-26", "## 本周摘要")

    assert daily_no_data.endswith(footer)
    assert footer not in monthly_no_data
    assert footer not in weekly_rollup
    assert not reports._build_daily_report_text(
        "测试群",
        "2026-04-21",
        [],
    ).endswith(footer)


async def test_send_report_job_preserves_text_when_footer_is_not_configured() -> None:
    store = _ReportStore()
    store.jobs[1].update(
        {
            "status": "completed",
            "result_text": "[测试群] 日报 · 2026-04-21\n\n旧缓存正文",
            "delivery_status": "pending",
        }
    )
    bridge = _CaptureBridge()
    service = reports.WxbotReportService(
        store,
        SimpleNamespace(llm_service=None),
        bridge=bridge,
        scope_execution_allowed=_allow_scope,
    )

    sent = await service.send_report_job(1)

    assert sent is True
    assert bridge.request_headers == [{"Idempotency-Key": "wxbot-report:1"}]
    send_call = next(call for call in bridge.calls if call[1] == "/send")
    text = send_call[3]["text"]
    assert text == "[测试群] 日报 · 2026-04-21\n\n旧缓存正文"


async def test_report_subscription_is_a_required_delivery() -> None:
    store = _ReportStore()
    store.jobs[1].update(
        {
            "status": "completed",
            "result_text": "日报正文",
            "delivery_status": "pending",
        }
    )
    bridge = _CaptureBridge()
    ledger = _CaptureSpeechLedger()
    service = reports.WxbotReportService(
        store,
        SimpleNamespace(llm_service=None),
        bridge=bridge,
        speech_ledger=ledger,
        scope_execution_allowed=_allow_scope,
    )

    sent = await service.send_report_job(1)

    assert sent is True
    assert ledger.reservations[0]["output_kind"] == "report"
    assert ledger.reservations[0]["speech_class"] == "required_delivery"
    assert ledger.commits[0][1] == "1"


async def test_send_report_scope_disable_after_claim_releases_without_sdk_call() -> None:
    store = _ReportStore()
    store.jobs[1].update(
        {
            "status": "completed",
            "result_text": "日报正文",
            "delivery_status": "pending",
        }
    )
    bridge = _CaptureBridge()
    decisions = iter((True, False))

    async def scope_gate(_tenant_id: str, _session_id: str) -> bool:
        return next(decisions)

    service = reports.WxbotReportService(
        store,
        SimpleNamespace(llm_service=None),
        bridge=bridge,
        scope_execution_allowed=scope_gate,
    )

    sent = await service.send_report_job(1)

    assert sent is False
    assert store.jobs[1]["delivery_status"] == "pending"
    assert store.jobs[1]["delivery_error"] == "scope_execution_denied"
    assert store.jobs[1]["delivery_attempt"] == 1
    assert [call[1] for call in bridge.calls] == ["/ext/roster/groups"]


async def test_send_report_job_does_not_duplicate_daily_footer() -> None:
    store = _ReportStore()
    footer = "Project: https://example.invalid/project"
    store.settings.wxbot_daily_report_footer = footer
    store.jobs[1].update(
        {
            "status": "completed",
            "result_text": f"[测试群] 日报 · 2026-04-21\n\n正文\n\n{footer}",
            "delivery_status": "pending",
        }
    )
    bridge = _CaptureBridge()
    service = reports.WxbotReportService(
        store,
        SimpleNamespace(llm_service=None),
        bridge=bridge,
        scope_execution_allowed=_allow_scope,
    )

    sent = await service.send_report_job(1)

    assert sent is True
    send_call = next(call for call in bridge.calls if call[1] == "/send")
    text = send_call[3]["text"]
    assert text.endswith(footer)
    assert text.count(footer) == 1


async def test_send_report_job_preserves_weekly_text_without_daily_footer() -> None:
    store = _ReportStore()
    store.jobs[1].update(
        {
            "report_type": "weekly",
            "period_key": "2026-04-20..2026-04-26",
            "period_label": "2026-04-20..2026-04-26",
            "status": "completed",
            "result_text": "[测试群] 周报 · 2026-04-20..2026-04-26\n\n周报正文",
            "delivery_status": "pending",
        }
    )
    bridge = _CaptureBridge()
    service = reports.WxbotReportService(
        store,
        SimpleNamespace(llm_service=None),
        bridge=bridge,
        scope_execution_allowed=_allow_scope,
    )

    sent = await service.send_report_job(1)

    assert sent is True
    send_call = next(call for call in bridge.calls if call[1] == "/send")
    assert send_call[3]["text"] == "[测试群] 周报 · 2026-04-20..2026-04-26\n\n周报正文"


async def test_send_report_job_remains_queued_until_sdk_row_is_sent() -> None:
    store = _ReportStore()
    store.jobs[1].update(
        {
            "status": "completed",
            "result_text": "日报正文",
            "delivery_status": "pending",
        }
    )
    bridge = _CaptureBridge()
    bridge.outbound_status = "running"
    service = reports.WxbotReportService(
        store,
        SimpleNamespace(llm_service=None),
        bridge=bridge,
        scope_execution_allowed=_allow_scope,
    )

    sent = await service.send_report_job(1)

    assert sent is False
    assert store.jobs[1]["delivery_status"] == "queued"
    assert store.jobs[1]["sdk_outbound_id"] == 1


async def test_report_delivery_reconciles_against_list_only_sdk() -> None:
    store = _ReportStore()
    store.jobs[1].update(
        {
            "status": "completed",
            "result_text": "日报正文",
            "delivery_status": "pending",
        }
    )
    bridge = _CaptureBridge()
    bridge.detail_error_status = 502
    service = reports.WxbotReportService(
        store,
        SimpleNamespace(llm_service=None),
        bridge=bridge,
        scope_execution_allowed=_allow_scope,
    )

    sent = await service.send_report_job(1)

    assert sent is True
    assert store.jobs[1]["delivery_status"] == "sent"
    assert [call[1] for call in bridge.calls][-2:] == [
        "/queue/messages/1",
        "/queue/messages",
    ]


async def test_list_only_sdk_row_requires_exact_report_body() -> None:
    store = _ReportStore()
    store.jobs[1].update(
        {
            "status": "completed",
            "result_text": "日报正文",
            "delivery_status": "pending",
        }
    )
    bridge = _CaptureBridge()
    bridge.detail_error_status = 404
    bridge.list_reply_text = "另一条消息"
    service = reports.WxbotReportService(
        store,
        SimpleNamespace(llm_service=None),
        bridge=bridge,
        scope_execution_allowed=_allow_scope,
    )

    sent = await service.send_report_job(1)

    assert sent is False
    assert store.jobs[1]["delivery_status"] == "indeterminate"
    assert "text" in store.jobs[1]["delivery_error"]


async def test_send_report_job_marks_failed_sdk_row_indeterminate() -> None:
    store = _ReportStore()
    store.jobs[1].update(
        {
            "status": "completed",
            "result_text": "日报正文",
            "delivery_status": "pending",
        }
    )
    bridge = _CaptureBridge()
    bridge.outbound_status = "failed"
    bridge.outbound_error = "group transition not confirmed"
    service = reports.WxbotReportService(
        store,
        SimpleNamespace(llm_service=None),
        bridge=bridge,
        scope_execution_allowed=_allow_scope,
    )

    sent = await service.send_report_job(1)

    assert sent is False
    assert store.jobs[1]["delivery_status"] == "indeterminate"
    assert "group transition not confirmed" in store.jobs[1]["delivery_error"]
