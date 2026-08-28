"""Durable local-agent jobs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from app.common.logging import get_logger
from app.infra.db import get_engine
from app.infra.runtime_schema import verify_runtime_schema

logger = get_logger(__name__)

JOB_TABLE = "plugin_local_agent_job"
JOB_STATUSES = (
    "queued",
    "submitted",
    "running",
    "succeeded",
    "failed",
)
TERMINAL_STATUSES = frozenset({"succeeded", "failed"})


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def _json_dumps(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    raw = str(value or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


@dataclass
class LocalAgentJob:
    job_id: str
    backend: str
    status: str
    prompt: str
    tenant_id: str = ""
    channel: str = ""
    session_id: str = ""
    user_id: str = ""
    adapter_id: str = ""
    connection_id: str = ""
    request_id: str = ""
    trace_id: str = ""
    original_message_id: str = ""
    sidecar_task_id: str = ""
    result_text: str = ""
    error_code: str = ""
    error_message: str = ""
    callback_target: dict[str, Any] | None = None
    source_message: dict[str, Any] | None = None
    callback_sent: bool = False
    callback_error: str = ""
    locked_by: str = ""
    created_at: str = ""
    updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "backend": self.backend,
            "status": self.status,
            "prompt": self.prompt,
            "tenant_id": self.tenant_id,
            "channel": self.channel,
            "session_id": self.session_id,
            "user_id": self.user_id,
            "adapter_id": self.adapter_id,
            "connection_id": self.connection_id,
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "original_message_id": self.original_message_id,
            "sidecar_task_id": self.sidecar_task_id,
            "result_text": self.result_text,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "callback_target": dict(self.callback_target or {}),
            "source_message": dict(self.source_message or {}),
            "callback_sent": self.callback_sent,
            "callback_error": self.callback_error,
            "locked_by": self.locked_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _row_to_job(row: dict[str, Any]) -> LocalAgentJob:
    return LocalAgentJob(
        job_id=str(row.get("job_id") or ""),
        backend=str(row.get("backend") or ""),
        status=str(row.get("status") or ""),
        prompt=str(row.get("prompt") or ""),
        tenant_id=str(row.get("tenant_id") or ""),
        channel=str(row.get("channel") or ""),
        session_id=str(row.get("session_id") or ""),
        user_id=str(row.get("user_id") or ""),
        adapter_id=str(row.get("adapter_id") or ""),
        connection_id=str(row.get("connection_id") or ""),
        request_id=str(row.get("request_id") or ""),
        trace_id=str(row.get("trace_id") or ""),
        original_message_id=str(row.get("original_message_id") or ""),
        sidecar_task_id=str(row.get("sidecar_task_id") or ""),
        result_text=str(row.get("result_text") or ""),
        error_code=str(row.get("error_code") or ""),
        error_message=str(row.get("error_message") or ""),
        callback_target=_json_loads(row.get("callback_target_json")),
        source_message=_json_loads(row.get("source_message_json")),
        callback_sent=bool(row.get("callback_sent")),
        callback_error=str(row.get("callback_error") or ""),
        locked_by=str(row.get("locked_by") or ""),
        created_at=_iso(row.get("created_at")) if isinstance(row.get("created_at"), datetime) else str(row.get("created_at") or ""),
        updated_at=_iso(row.get("updated_at")) if isinstance(row.get("updated_at"), datetime) else str(row.get("updated_at") or ""),
    )


class LocalAgentStore:
    def __init__(self, settings: Any) -> None:
        self.settings = settings

    async def ensure_tables(self) -> None:
        await verify_runtime_schema(get_engine(), component="local_agent store")
        logger.info("local_agent.schema_verified")

    async def create_job(
        self,
        *,
        backend: str,
        prompt: str,
        tenant_id: str,
        channel: str,
        session_id: str,
        user_id: str,
        adapter_id: str = "",
        connection_id: str = "",
        request_id: str = "",
        trace_id: str = "",
        original_message_id: str = "",
        callback_target: dict[str, Any] | None = None,
        source_message: dict[str, Any] | None = None,
        job_id: str = "",
    ) -> LocalAgentJob:
        now = _now()
        record = LocalAgentJob(
            job_id=str(job_id or uuid4().hex),
            backend=str(backend or "").strip().lower(),
            status="queued",
            prompt=str(prompt or ""),
            tenant_id=str(tenant_id or ""),
            channel=str(channel or ""),
            session_id=str(session_id or ""),
            user_id=str(user_id or ""),
            adapter_id=str(adapter_id or ""),
            connection_id=str(connection_id or ""),
            request_id=str(request_id or ""),
            trace_id=str(trace_id or ""),
            original_message_id=str(original_message_id or ""),
            callback_target=dict(callback_target or {}),
            source_message=dict(source_message or {}),
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
        )
        async with get_engine().begin() as conn:
            await conn.execute(
                text(
                    f"""
                    INSERT INTO {JOB_TABLE} (
                        job_id, backend, status, prompt, tenant_id, channel,
                        session_id, user_id, adapter_id, connection_id,
                        request_id, trace_id, original_message_id,
                        sidecar_task_id, result_text, error_code, error_message,
                        callback_target_json, source_message_json, callback_sent,
                        callback_error, locked_by, locked_until, created_at, updated_at
                    ) VALUES (
                        :job_id, :backend, :status, :prompt, :tenant_id, :channel,
                        :session_id, :user_id, :adapter_id, :connection_id,
                        :request_id, :trace_id, :original_message_id,
                        '', '', '', '',
                        :callback_target_json, :source_message_json, FALSE,
                        '', '', NULL, :created_at, :updated_at
                    )
                    """
                ),
                {
                    "job_id": record.job_id,
                    "backend": record.backend,
                    "status": record.status,
                    "prompt": record.prompt,
                    "tenant_id": record.tenant_id,
                    "channel": record.channel,
                    "session_id": record.session_id,
                    "user_id": record.user_id,
                    "adapter_id": record.adapter_id,
                    "connection_id": record.connection_id,
                    "request_id": record.request_id,
                    "trace_id": record.trace_id,
                    "original_message_id": record.original_message_id,
                    "callback_target_json": _json_dumps(record.callback_target),
                    "source_message_json": _json_dumps(record.source_message),
                    "created_at": now,
                    "updated_at": now,
                },
            )
        return record

    async def get_job(self, job_id: str) -> LocalAgentJob | None:
        async with get_engine().connect() as conn:
            result = await conn.execute(
                text(f"SELECT * FROM {JOB_TABLE} WHERE job_id = :job_id"),
                {"job_id": job_id},
            )
            row = result.mappings().first()
        return _row_to_job(dict(row)) if row is not None else None

    async def list_jobs(
        self,
        *,
        tenant_id: str = "",
        status: str = "",
        limit: int = 50,
    ) -> list[LocalAgentJob]:
        clauses = ["TRUE"]
        params: dict[str, Any] = {"limit": max(1, min(int(limit or 50), 200))}
        if tenant_id:
            clauses.append("tenant_id = :tenant_id")
            params["tenant_id"] = tenant_id
        if status:
            clauses.append("status = :status")
            params["status"] = status
        sql = (
            f"SELECT * FROM {JOB_TABLE} WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC LIMIT :limit"
        )
        async with get_engine().connect() as conn:
            result = await conn.execute(text(sql), params)
            rows = result.mappings().all()
        return [_row_to_job(dict(row)) for row in rows]

    async def claim_due_jobs(
        self,
        *,
        limit: int,
        lock_ttl_seconds: float,
        worker_id: str,
    ) -> list[LocalAgentJob]:
        now = _now()
        lock_until = now + timedelta(seconds=max(5.0, float(lock_ttl_seconds)))
        claimed: list[LocalAgentJob] = []
        async with get_engine().begin() as conn:
            result = await conn.execute(
                text(
                    f"""
                    SELECT job_id FROM {JOB_TABLE}
                    WHERE status IN ('queued', 'submitted', 'running')
                      AND (locked_until IS NULL OR locked_until < :now)
                    ORDER BY created_at ASC
                    LIMIT :limit
                    FOR UPDATE SKIP LOCKED
                    """
                ),
                {"now": now, "limit": max(1, int(limit))},
            )
            job_ids = [str(row[0]) for row in result.fetchall()]
            for job_id in job_ids:
                updated = await conn.execute(
                    text(
                        f"""
                        UPDATE {JOB_TABLE}
                        SET locked_by = :worker_id,
                            locked_until = :lock_until,
                            updated_at = :now
                        WHERE job_id = :job_id
                        RETURNING *
                        """
                    ),
                    {
                        "worker_id": worker_id,
                        "lock_until": lock_until,
                        "now": now,
                        "job_id": job_id,
                    },
                )
                row = updated.mappings().first()
                if row is not None:
                    claimed.append(_row_to_job(dict(row)))
        return claimed

    async def mark_submitted(self, job_id: str, sidecar_task_id: str) -> None:
        now = _now()
        async with get_engine().begin() as conn:
            await conn.execute(
                text(
                    f"""
                    UPDATE {JOB_TABLE}
                    SET status = 'submitted',
                        sidecar_task_id = :sidecar_task_id,
                        updated_at = :now
                    WHERE job_id = :job_id
                    """
                ),
                {
                    "job_id": job_id,
                    "sidecar_task_id": sidecar_task_id,
                    "now": now,
                },
            )

    async def mark_running(self, job_id: str) -> None:
        now = _now()
        async with get_engine().begin() as conn:
            await conn.execute(
                text(
                    f"""
                    UPDATE {JOB_TABLE}
                    SET status = 'running', updated_at = :now
                    WHERE job_id = :job_id
                    """
                ),
                {"job_id": job_id, "now": now},
            )

    async def mark_succeeded(self, job_id: str, result_text: str) -> None:
        now = _now()
        async with get_engine().begin() as conn:
            await conn.execute(
                text(
                    f"""
                    UPDATE {JOB_TABLE}
                    SET status = 'succeeded',
                        result_text = :result_text,
                        error_code = '',
                        error_message = '',
                        finished_at = :now,
                        updated_at = :now,
                        locked_until = NULL
                    WHERE job_id = :job_id
                    """
                ),
                {"job_id": job_id, "result_text": result_text, "now": now},
            )

    async def mark_failed(self, job_id: str, error_code: str, error_message: str) -> None:
        now = _now()
        async with get_engine().begin() as conn:
            await conn.execute(
                text(
                    f"""
                    UPDATE {JOB_TABLE}
                    SET status = 'failed',
                        error_code = :error_code,
                        error_message = :error_message,
                        finished_at = :now,
                        updated_at = :now,
                        locked_until = NULL
                    WHERE job_id = :job_id
                    """
                ),
                {
                    "job_id": job_id,
                    "error_code": error_code[:128],
                    "error_message": error_message[:2000],
                    "now": now,
                },
            )

    async def mark_callback_sent(self, job_id: str) -> None:
        now = _now()
        async with get_engine().begin() as conn:
            await conn.execute(
                text(
                    f"""
                    UPDATE {JOB_TABLE}
                    SET callback_sent = TRUE,
                        callback_error = '',
                        updated_at = :now
                    WHERE job_id = :job_id
                    """
                ),
                {"job_id": job_id, "now": now},
            )

    async def release_lock(self, job_id: str) -> None:
        now = _now()
        async with get_engine().begin() as conn:
            await conn.execute(
                text(
                    f"""
                    UPDATE {JOB_TABLE}
                    SET locked_by = '',
                        locked_until = NULL,
                        updated_at = :now
                    WHERE job_id = :job_id
                    """
                ),
                {"job_id": job_id, "now": now},
            )

    async def mark_callback_error(self, job_id: str, error: str) -> None:
        now = _now()
        async with get_engine().begin() as conn:
            await conn.execute(
                text(
                    f"""
                    UPDATE {JOB_TABLE}
                    SET callback_error = :error, updated_at = :now
                    WHERE job_id = :job_id
                    """
                ),
                {"job_id": job_id, "error": error[:2000], "now": now},
            )
