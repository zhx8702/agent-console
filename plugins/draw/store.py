from __future__ import annotations

import base64
import binascii
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from os import replace
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlparse
from uuid import uuid4

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.admin.mutation_ledger import (
    MutationAudit,
    MutationChange,
    MutationIdentity,
    MutationOutcome,
    run_idempotent_mutation,
)
from app.billing.catalog import DRAW_QUALITY_COSTS as DRAW_QUALITY_COSTS
from app.common.image_preview import (
    fetch_image_once,
    preview_first_urls,
    wait_for_image,
)
from app.common.logging import get_logger
from app.common.safe_url import (
    OutboundURLPolicy,
    configure_http_client,
    normalize_origin,
    safe_get,
)
from app.common.wxbot_auth import wxbot_sdk_headers
from app.egress.safe_http import safe_http_request
from app.infra.db import get_engine
from app.infra.runtime_schema import (
    verify_runtime_schema,
)

logger = get_logger(__name__)

DRAW_DEFAULT_QUALITY = "low"
DRAW_QUALITY_VALUES = ("low", "medium", "high")
DRAW_QUALITY_SIZES = {
    "low": "1024x1024",
    "medium": "2048x2048",
    "high": "3840x2160",
}
DRAW_MAX_REMOTE_IMAGE_BYTES = 20 * 1024 * 1024
DRAW_MAX_UPSTREAM_RESPONSE_BYTES = 32 * 1024 * 1024
DRAW_UPSTREAM_RESPONSE_CONTENT_TYPES = (
    "application/json",
    "application/problem+json",
    "image/",
    "text/event-stream",
    "text/plain",
)
DRAW_QUALITY_ERROR_TEXT = "quality 只能是 low、medium、high"
DRAW_TASK_TABLE = "plugin_draw_task"
DRAW_TASK_INTERRUPTED_ERROR_CODE = "TASK_INTERRUPTED"
DRAW_TASK_INTERRUPTED_ERROR_MESSAGE = "任务中断，请重试"
_DRAW_TASK_CALLBACK_CLAIM = "__draw_callback_in_progress__"
_DRAW_TASK_CALLBACK_CLAIM_TIMEOUT_SECONDS = 300.0
_DRAW_TASK_SCOPE_DEFER = "__draw_scope_deferred__"
_DRAW_TASK_SCOPE_DEFER_SECONDS = 30.0
DRAW_TASK_STATUSES = (
    "queued",
    "running",
    "completed",
    "failed",
    "interrupted",
)
_ACTIVE_ADMIN_MUTATION_CONNECTION: ContextVar[AsyncConnection | None] = ContextVar(
    "draw_admin_mutation_connection",
    default=None,
)


def normalize_draw_quality(value: object = DRAW_DEFAULT_QUALITY) -> str:
    quality = str(value or DRAW_DEFAULT_QUALITY).strip().strip("'\"“”‘’").lower()
    if quality not in DRAW_QUALITY_VALUES:
        raise ValueError(DRAW_QUALITY_ERROR_TEXT)
    return quality


class DrawError(Exception):
    """Base error for draw plugin failures."""


class DrawConfigError(DrawError):
    """Raised when required draw configuration is missing or invalid."""


class DrawApiError(DrawError):
    """Raised when the remote draw API cannot produce an image."""


@dataclass
class DrawImageRecord:
    image_id: str
    prompt: str
    local_path: str
    file_name: str
    media_type: str
    public_path: str
    source_url: str = ""
    source_image_id: str = ""
    created_at: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "image_id": self.image_id,
            "prompt": self.prompt,
            "local_path": self.local_path,
            "file_name": self.file_name,
            "media_type": self.media_type,
            "public_path": self.public_path,
            "source_url": self.source_url,
            "source_image_id": self.source_image_id,
            "created_at": self.created_at,
        }


@dataclass
class DrawResult:
    image_id: str
    prompt: str
    local_path: str
    file_name: str
    media_type: str
    public_path: str
    source_url: str = ""
    source_image_id: str = ""


@dataclass
class DrawInputImage:
    content: bytes
    file_name: str
    media_type: str
    source_image_id: str
    source_url: str = ""


@dataclass
class DrawTaskCreate:
    task_id: str = ""
    request_id: str = ""
    trace_id: str = ""
    command_type: str = ""
    status: str = "queued"
    tenant_id: str = ""
    channel: str = ""
    source_key: str = ""
    chat_id: str = ""
    session_id: str = ""
    group_id: str = ""
    user_id: str = ""
    requester: str = ""
    requester_display_name: str = ""
    original_message_id: str = ""
    callback_target: dict[str, Any] | None = None
    callback_reply_to_message_id: str = ""
    source_message: dict[str, Any] | None = None
    prompt: str = ""
    quality: str = DRAW_DEFAULT_QUALITY
    size: str = ""
    source_image: dict[str, Any] | None = None
    retry_count: int = 0
    next_run_at: str = ""
    created_at: str = ""


@dataclass
class DrawTaskRecord:
    task_id: str
    request_id: str
    trace_id: str
    command_type: str
    status: str
    tenant_id: str = ""
    channel: str = ""
    source_key: str = ""
    chat_id: str = ""
    session_id: str = ""
    group_id: str = ""
    user_id: str = ""
    requester: str = ""
    requester_display_name: str = ""
    original_message_id: str = ""
    callback_target: dict[str, Any] | None = None
    callback_reply_to_message_id: str = ""
    source_message: dict[str, Any] | None = None
    prompt: str = ""
    quality: str = DRAW_DEFAULT_QUALITY
    size: str = ""
    source_image: dict[str, Any] | None = None
    result_image_id: str = ""
    result_local_path: str = ""
    result_file_name: str = ""
    result_media_type: str = ""
    result_public_path: str = ""
    result_source_url: str = ""
    error_code: str = ""
    error_message: str = ""
    retry_count: int = 0
    next_run_at: str = ""
    locked_until: str = ""
    locked_by: str = ""
    callback_sent: bool = False
    callback_error: str = ""
    created_at: str = ""
    updated_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    heartbeat_at: str = ""

    def as_dict(self, *, include_prompt: bool = True) -> dict[str, Any]:
        payload = {
            "task_id": self.task_id,
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "command_type": self.command_type,
            "status": self.status,
            "tenant_id": self.tenant_id,
            "channel": self.channel,
            "source_key": self.source_key,
            "chat_id": self.chat_id,
            "session_id": self.session_id,
            "group_id": self.group_id,
            "user_id": self.user_id,
            "requester": self.requester,
            "requester_display_name": self.requester_display_name,
            "original_message_id": self.original_message_id,
            "callback_target": dict(self.callback_target or {}),
            "callback_reply_to_message_id": self.callback_reply_to_message_id,
            "source_message": dict(self.source_message or {}),
            "quality": self.quality,
            "size": self.size,
            "source_image": dict(self.source_image or {}),
            "result_image_id": self.result_image_id,
            "result_local_path": self.result_local_path,
            "result_file_name": self.result_file_name,
            "result_media_type": self.result_media_type,
            "result_public_path": self.result_public_path,
            "result_source_url": self.result_source_url,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "retry_count": self.retry_count,
            "next_run_at": self.next_run_at,
            "locked_until": self.locked_until,
            "locked_by": self.locked_by,
            "callback_sent": self.callback_sent,
            "callback_error": self.callback_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "heartbeat_at": self.heartbeat_at,
        }
        if include_prompt:
            payload["prompt"] = self.prompt
        return payload


@dataclass
class DrawEndpoint:
    name: str
    client_name: str
    api_url: str
    api_key: str
    model: str
    timeout: float
    key_header: str
    key_prefix: str
    prompt_field: str
    model_field: str
    response_format: str
    extra_body_raw: str


def _draw_endpoint_policy(url: str, *, timeout: float) -> OutboundURLPolicy:
    """Bind a configured Draw service to one exact trusted origin."""

    parsed = urlparse(str(url or "").strip())
    hostname = str(parsed.hostname or "").strip().lower().rstrip(".")
    origin = normalize_origin(url)
    return OutboundURLPolicy(
        allowed_hosts=frozenset({hostname}) if hostname else frozenset(),
        allowed_private_origins=frozenset({origin}) if origin else frozenset(),
        max_redirects=0,
        max_response_bytes=DRAW_MAX_UPSTREAM_RESPONSE_BYTES,
        timeout_seconds=max(0.1, float(timeout)),
        allowed_response_content_types=DRAW_UPSTREAM_RESPONSE_CONTENT_TYPES,
    )


def _encode_multipart(
    data: dict[str, Any],
    files: dict[str, tuple[str, bytes, str]],
) -> tuple[bytes, str]:
    """Encode the in-memory image once before the pinned POST is built."""

    request = httpx.Request("POST", "https://draw.invalid/", data=data, files=files)
    body = request.read()
    content_type = str(request.headers.get("content-type") or "")
    if not content_type.startswith("multipart/form-data;"):
        raise DrawApiError("无法构造重绘请求")
    return body, content_type


class DrawStore:
    def __init__(
        self,
        settings: Any,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.settings = settings
        self._session_factory = session_factory
        self._client: httpx.AsyncClient | None = None
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._storage_dir = self._resolve_storage_dir(
            str(getattr(settings, "draw_storage_dir", "/mnt/c/Users/Public/cs-system-draw") or "")
        )
        self._index_path = self._storage_dir / "images.json"

    async def initialize(self) -> None:
        await self.ensure_task_table()
        try:
            self._ensure_storage_dir()
            self._sync_index_with_files()
        except (DrawConfigError, OSError):
            logger.warning("draw.storage_init_failed", storage_dir=str(self._storage_dir))

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        for name, client in list(self._clients.items()):
            if name == "primary" and client is self._client:
                continue
            await client.aclose()
        self._clients.clear()

    def resolve_file(self, file_name: str) -> Path | None:
        candidate = (self._storage_dir / file_name).resolve()
        try:
            candidate.relative_to(self._storage_dir)
        except ValueError:
            return None
        if not candidate.is_file():
            return None
        return candidate

    def resolve_image_id(self, image_id: str) -> DrawImageRecord | None:
        image_id = str(image_id or "").strip()
        if not image_id:
            return None
        self._ensure_storage_dir()
        records = self._read_index()
        record = records.get(image_id)
        if record is None:
            self._sync_index_with_files()
            records = self._read_index()
            record = records.get(image_id)
        if record is None:
            return None
        if self.resolve_file(record.file_name) is None:
            return None
        return record

    def list_images(self, *, limit: int = 50) -> list[DrawImageRecord]:
        self._ensure_storage_dir()
        self._sync_index_with_files()
        records = list(self._read_index().values())
        records.sort(key=lambda item: item.created_at or item.file_name, reverse=True)
        return records[: max(1, min(int(limit or 50), 200))]

    async def ensure_task_table(self) -> None:
        await verify_runtime_schema(get_engine(), component="draw store")
        logger.info("draw.schema_verified")

    async def run_admin_mutation(
        self,
        *,
        identity: MutationIdentity,
        audit: MutationAudit,
        mutate: Callable[[], Awaitable[MutationChange]],
    ) -> MutationOutcome:
        """Run one manual recovery/send together with its durable ledger."""

        async with get_engine().begin() as conn:
            token = _ACTIVE_ADMIN_MUTATION_CONNECTION.set(conn)
            try:
                return await run_idempotent_mutation(
                    conn,
                    identity=identity,
                    audit=audit,
                    mutate=mutate,
                )
            finally:
                _ACTIVE_ADMIN_MUTATION_CONNECTION.reset(token)

    async def create_draw_task(self, task: DrawTaskCreate) -> DrawTaskRecord:
        task_id = str(task.task_id or "").strip() or f"drawtask_{uuid4().hex}"
        status = _normalize_task_status(task.status)
        params = {
            "task_id": task_id,
            "request_id": str(task.request_id or ""),
            "trace_id": str(task.trace_id or ""),
            "command_type": _normalize_task_command_type(task.command_type),
            "status": status,
            "tenant_id": str(task.tenant_id or ""),
            "channel": str(task.channel or ""),
            "source_key": str(task.source_key or ""),
            "chat_id": str(task.chat_id or ""),
            "session_id": str(task.session_id or ""),
            "group_id": str(task.group_id or ""),
            "user_id": str(task.user_id or ""),
            "requester": str(task.requester or ""),
            "requester_display_name": str(task.requester_display_name or ""),
            "original_message_id": str(task.original_message_id or ""),
            "callback_target_json": _json_dumps(task.callback_target or {}),
            "callback_reply_to_message_id": str(task.callback_reply_to_message_id or ""),
            "source_message_json": _json_dumps(task.source_message or {}),
            "prompt": str(task.prompt or ""),
            "quality": normalize_draw_quality(task.quality),
            "size": str(task.size or ""),
            "source_image_json": _json_dumps(task.source_image or {}),
            "retry_count": max(0, int(task.retry_count or 0)),
        }
        async with self._db() as db:
            dialect = _dialect_name(db)
            now = _timestamp_param(task.created_at or _utc_now(), dialect=dialect)
            next_run_at = _timestamp_param(task.next_run_at, dialect=dialect) if task.next_run_at else now
            params.update(
                {
                    "created_at": now,
                    "updated_at": now,
                    "next_run_at": next_run_at,
                    "heartbeat_at": now if status == "running" else None,
                }
            )
            await db.execute(text(_insert_draw_task_sql(dialect)), params)
        record = await self.get_draw_task(task_id)
        if record is None:
            raise DrawError(f"draw task was not persisted: {task_id}")
        return record

    async def get_draw_task(self, task_id: str) -> DrawTaskRecord | None:
        task_id = str(task_id or "").strip()
        if not task_id:
            return None
        async with self._db() as db:
            row = await _fetch_draw_task_row(db, task_id)
        return _draw_task_record_from_row(row) if row is not None else None

    async def list_draw_tasks(
        self,
        *,
        limit: int = 50,
        tenant_id: str = "",
        status: str = "",
    ) -> list[DrawTaskRecord]:
        safe_limit = max(1, min(int(limit or 50), 200))
        filters: list[str] = []
        params: dict[str, Any] = {"limit": safe_limit}
        if str(tenant_id or "").strip():
            filters.append("tenant_id = :tenant_id")
            params["tenant_id"] = str(tenant_id or "").strip()
        if str(status or "").strip():
            filters.append("status = :status")
            params["status"] = _normalize_task_status(status)
        where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
        async with self._db() as db:
            result = await db.execute(
                text(
                    f"""
                    SELECT *
                    FROM {DRAW_TASK_TABLE}
                    {where_sql}
                    ORDER BY created_at DESC
                    LIMIT :limit
                    """
                ),
                params,
            )
            return [
                _draw_task_record_from_row(dict(row))
                for row in result.mappings().all()
            ]

    async def list_stale_draw_tasks(
        self,
        *,
        stale_seconds: float,
        limit: int = 50,
    ) -> list[DrawTaskRecord]:
        safe_limit = max(1, min(int(limit or 50), 200))
        async with self._db() as db:
            cutoff = _stale_cutoff(stale_seconds, dialect=_dialect_name(db))
            result = await db.execute(
                text(
                    f"""
                    SELECT *
                    FROM {DRAW_TASK_TABLE}
                    WHERE (
                        (
                            (
                                status = 'queued'
                                AND (next_run_at IS NULL OR next_run_at <= :now)
                            )
                            OR status = 'running'
                        )
                        AND COALESCE(heartbeat_at, updated_at) < :cutoff
                        OR (
                            status IN ('interrupted', 'failed')
                            AND error_code = :error_code
                        )
                        OR (
                            status = 'completed'
                            AND COALESCE(callback_error, '') <> ''
                        )
                      )
                      AND callback_sent = :callback_sent
                      AND (
                        COALESCE(callback_error, '') NOT IN (
                            :callback_claim,
                            :scope_defer
                        )
                        OR (
                            callback_error = :callback_claim
                            AND updated_at < :claim_cutoff
                        )
                        OR (
                            callback_error = :scope_defer
                            AND updated_at < :scope_defer_cutoff
                        )
                      )
                    ORDER BY COALESCE(heartbeat_at, updated_at) ASC, created_at ASC
                    LIMIT :limit
                    """
                ),
                {
                    "callback_sent": False,
                    "callback_claim": _DRAW_TASK_CALLBACK_CLAIM,
                    "scope_defer": _DRAW_TASK_SCOPE_DEFER,
                    "now": _timestamp_param(
                        _utc_now(),
                        dialect=_dialect_name(db),
                    ),
                    "cutoff": cutoff,
                    "claim_cutoff": _stale_cutoff(
                        _DRAW_TASK_CALLBACK_CLAIM_TIMEOUT_SECONDS,
                        dialect=_dialect_name(db),
                    ),
                    "scope_defer_cutoff": _stale_cutoff(
                        _DRAW_TASK_SCOPE_DEFER_SECONDS,
                        dialect=_dialect_name(db),
                    ),
                    "error_code": DRAW_TASK_INTERRUPTED_ERROR_CODE,
                    "limit": safe_limit,
                },
            )
            return [
                _draw_task_record_from_row(dict(row))
                for row in result.mappings().all()
            ]

    async def list_stale_tasks(
        self,
        *,
        stale_seconds: float,
        limit: int = 50,
    ) -> list[DrawTaskRecord]:
        return await self.list_stale_draw_tasks(stale_seconds=stale_seconds, limit=limit)

    async def recover_stale_tasks(
        self,
        *,
        stale_seconds: float,
        limit: int = 50,
        status: str = "interrupted",
        error_code: str = DRAW_TASK_INTERRUPTED_ERROR_CODE,
        error_message: str = DRAW_TASK_INTERRUPTED_ERROR_MESSAGE,
    ) -> list[DrawTaskRecord]:
        normalized_status = _normalize_task_status(status)
        candidates = await self.list_stale_draw_tasks(stale_seconds=stale_seconds, limit=limit)
        if not candidates:
            return []

        recovered: list[DrawTaskRecord] = []
        async with self._db() as db:
            dialect = _dialect_name(db)
            cutoff = _stale_cutoff(stale_seconds, dialect=dialect)
            now = _timestamp_param(_utc_now(), dialect=dialect)
            for task in candidates:
                if task.status in {"queued", "running"}:
                    result = await db.execute(
                        text(
                            f"""
                            UPDATE {DRAW_TASK_TABLE}
                            SET status = :status,
                                error_code = :error_code,
                                error_message = :error_message,
                                finished_at = :now,
                                heartbeat_at = :now,
                                updated_at = :now
                            WHERE task_id = :task_id
                              AND status IN ('queued', 'running')
                              AND callback_sent = :callback_sent
                              AND COALESCE(heartbeat_at, updated_at) < :cutoff
                            """
                        ),
                        {
                            "task_id": task.task_id,
                            "status": normalized_status,
                            "error_code": str(error_code or "")[:128],
                            "error_message": str(error_message or "")[:1000],
                            "callback_sent": False,
                            "cutoff": cutoff,
                            "now": now,
                        },
                    )
                    if not getattr(result, "rowcount", 0):
                        continue
                elif task.status not in {"interrupted", "failed"} or task.error_code != error_code:
                    continue

                row = await _fetch_draw_task_row(db, task.task_id)
                if row is None:
                    continue
                record = _draw_task_record_from_row(row)
                if (
                    record.callback_sent
                    or (
                        record.callback_error == _DRAW_TASK_CALLBACK_CLAIM
                        and not _is_stale_timestamp(
                            record.updated_at,
                            seconds=_DRAW_TASK_CALLBACK_CLAIM_TIMEOUT_SECONDS,
                        )
                    )
                    or record.error_code != error_code
                ):
                    continue
                recovered.append(record)
        return recovered

    async def recover_stale_draw_task(
        self,
        task_id: str,
        *,
        stale_seconds: float,
        status: str = "interrupted",
        error_code: str = DRAW_TASK_INTERRUPTED_ERROR_CODE,
        error_message: str = DRAW_TASK_INTERRUPTED_ERROR_MESSAGE,
    ) -> DrawTaskRecord | None:
        """Recover one pre-authorized stale task using an atomic stale fence."""

        task_id = str(task_id or "").strip()
        if not task_id:
            return None
        normalized_status = _normalize_task_status(status)
        async with self._db() as db:
            dialect = _dialect_name(db)
            cutoff = _stale_cutoff(stale_seconds, dialect=dialect)
            now = _timestamp_param(_utc_now(), dialect=dialect)
            result = await db.execute(
                text(
                    f"""
                    UPDATE {DRAW_TASK_TABLE}
                    SET status = CASE
                            WHEN status IN ('queued', 'running') THEN :status
                            ELSE status
                        END,
                        error_code = CASE
                            WHEN status IN ('queued', 'running') THEN :error_code
                            ELSE error_code
                        END,
                        error_message = CASE
                            WHEN status IN ('queued', 'running') THEN :error_message
                            ELSE error_message
                        END,
                        finished_at = CASE
                            WHEN status IN ('queued', 'running') THEN :now
                            ELSE finished_at
                        END,
                        heartbeat_at = CASE
                            WHEN status IN ('queued', 'running') THEN :now
                            ELSE heartbeat_at
                        END,
                        callback_error = CASE
                            WHEN callback_error = :scope_defer THEN ''
                            ELSE callback_error
                        END,
                        updated_at = :now
                    WHERE task_id = :task_id
                      AND callback_sent = :callback_sent
                      AND (
                        (
                          status IN ('queued', 'running')
                          AND COALESCE(heartbeat_at, updated_at) < :cutoff
                        )
                        OR (
                          status IN ('interrupted', 'failed')
                          AND error_code = :error_code
                        )
                        OR (
                          status = 'completed'
                          AND COALESCE(callback_error, '') <> ''
                        )
                      )
                    """
                ),
                {
                    "task_id": task_id,
                    "status": normalized_status,
                    "error_code": str(error_code or "")[:128],
                    "error_message": str(error_message or "")[:1000],
                    "callback_sent": False,
                    "scope_defer": _DRAW_TASK_SCOPE_DEFER,
                    "cutoff": cutoff,
                    "now": now,
                },
            )
            if not getattr(result, "rowcount", 0):
                return None
            row = await _fetch_draw_task_row(db, task_id)
        return _draw_task_record_from_row(row) if row is not None else None

    async def defer_stale_draw_task(
        self,
        task_id: str,
        *,
        stale_seconds: float,
        defer_seconds: float = _DRAW_TASK_SCOPE_DEFER_SECONDS,
    ) -> bool:
        """Back off a scope-denied stale task without completing or failing it."""

        task_id = str(task_id or "").strip()
        if not task_id:
            return False
        async with self._db() as db:
            dialect = _dialect_name(db)
            now_dt = _utc_now()
            now = _timestamp_param(now_dt, dialect=dialect)
            next_run_at = _timestamp_param(
                now_dt + timedelta(seconds=max(1.0, float(defer_seconds or 30.0))),
                dialect=dialect,
            )
            result = await db.execute(
                text(
                    f"""
                    UPDATE {DRAW_TASK_TABLE}
                    SET status = CASE
                            WHEN status IN ('queued', 'running') THEN 'queued'
                            ELSE status
                        END,
                        next_run_at = CASE
                            WHEN status IN ('queued', 'running') THEN :next_run_at
                            ELSE next_run_at
                        END,
                        locked_until = CASE
                            WHEN status IN ('queued', 'running') THEN NULL
                            ELSE locked_until
                        END,
                        locked_by = CASE
                            WHEN status IN ('queued', 'running') THEN ''
                            ELSE locked_by
                        END,
                        callback_error = CASE
                            WHEN status IN ('completed', 'interrupted', 'failed')
                                THEN :scope_defer
                            ELSE callback_error
                        END,
                        updated_at = :now
                    WHERE task_id = :task_id
                      AND callback_sent = :callback_sent
                      AND (
                        (
                          status IN ('queued', 'running')
                          AND COALESCE(heartbeat_at, updated_at) < :cutoff
                        )
                        OR (
                          status IN ('interrupted', 'failed')
                          AND error_code = :error_code
                        )
                        OR (
                          status = 'completed'
                          AND COALESCE(callback_error, '') <> ''
                        )
                      )
                    """
                ),
                {
                    "task_id": task_id,
                    "callback_sent": False,
                    "cutoff": _stale_cutoff(stale_seconds, dialect=dialect),
                    "error_code": DRAW_TASK_INTERRUPTED_ERROR_CODE,
                    "scope_defer": _DRAW_TASK_SCOPE_DEFER,
                    "next_run_at": next_run_at,
                    "now": now,
                },
            )
        return bool(getattr(result, "rowcount", 0))

    async def mark_draw_task_running(self, task_id: str) -> DrawTaskRecord | None:
        return await self._update_task_status(
            task_id,
            status="running",
            started_at=_utc_now_iso(),
            heartbeat_at=_utc_now_iso(),
        )

    async def claim_draw_task_for_execution(
        self,
        task_id: str,
        *,
        worker_id: str = "",
        lock_ttl_seconds: float = 900.0,
    ) -> DrawTaskRecord | None:
        task_id = str(task_id or "").strip()
        if not task_id:
            return None
        safe_ttl = max(1.0, float(lock_ttl_seconds or 900.0))
        worker_id = str(worker_id or "draw-task-runner")[:128]
        async with self._db() as db:
            dialect = _dialect_name(db)
            now_dt = _utc_now()
            now = _timestamp_param(now_dt, dialect=dialect)
            locked_until = _timestamp_param(now_dt + timedelta(seconds=safe_ttl), dialect=dialect)
            result = await db.execute(
                text(
                    f"""
                    UPDATE {DRAW_TASK_TABLE}
                    SET status = 'running',
                        locked_until = :locked_until,
                        locked_by = :worker_id,
                        started_at = COALESCE(started_at, :now),
                        heartbeat_at = :now,
                        updated_at = :now
                    WHERE task_id = :task_id
                      AND status IN ('queued', 'running')
                      AND callback_sent = :callback_sent
                      AND (
                        (
                          status = 'queued'
                          AND (locked_until IS NULL OR locked_until <= :now OR COALESCE(locked_by, '') = '')
                        )
                        OR COALESCE(locked_by, '') = :worker_id
                        OR locked_until IS NULL
                        OR locked_until <= :now
                      )
                    """
                ),
                {
                    "task_id": task_id,
                    "callback_sent": False,
                    "now": now,
                    "locked_until": locked_until,
                    "worker_id": worker_id,
                },
            )
            if not getattr(result, "rowcount", 0):
                row = await _fetch_draw_task_row(db, task_id)
                return _draw_task_record_from_row(row) if row is not None else None
            row = await _fetch_draw_task_row(db, task_id)
        return _draw_task_record_from_row(row) if row is not None else None

    async def heartbeat_draw_task(self, task_id: str) -> None:
        task_id = str(task_id or "").strip()
        if not task_id:
            return
        async with self._db() as db:
            now = _timestamp_param(_utc_now(), dialect=_dialect_name(db))
            await db.execute(
                text(
                    f"""
                    UPDATE {DRAW_TASK_TABLE}
                    SET heartbeat_at = :now, updated_at = :now
                    WHERE task_id = :task_id
                      AND status NOT IN ('completed', 'failed', 'interrupted')
                    """
                ),
                {"task_id": task_id, "now": now},
            )

    async def complete_draw_task(
        self,
        task_id: str,
        result: DrawResult | dict[str, Any],
    ) -> DrawTaskRecord | None:
        task_id = str(task_id or "").strip()
        if not task_id:
            return None
        result_payload = result if isinstance(result, dict) else result.__dict__
        params: dict[str, object] = {
            "task_id": task_id,
            "status": "completed",
            "result_image_id": str(result_payload.get("image_id") or ""),
            "result_local_path": str(result_payload.get("local_path") or ""),
            "result_file_name": str(result_payload.get("file_name") or ""),
            "result_media_type": str(result_payload.get("media_type") or ""),
            "result_public_path": str(result_payload.get("public_path") or ""),
            "result_source_url": str(result_payload.get("source_url") or ""),
        }
        async with self._db() as db:
            params["now"] = _timestamp_param(_utc_now(), dialect=_dialect_name(db))
            await db.execute(
                text(
                    f"""
                    UPDATE {DRAW_TASK_TABLE}
                    SET status = :status,
                        result_image_id = :result_image_id,
                        result_local_path = :result_local_path,
                        result_file_name = :result_file_name,
                        result_media_type = :result_media_type,
                        result_public_path = :result_public_path,
                        result_source_url = :result_source_url,
                        error_code = '',
                        error_message = '',
                        locked_until = NULL,
                        locked_by = '',
                        finished_at = :now,
                        heartbeat_at = :now,
                        updated_at = :now
                    WHERE task_id = :task_id
                      AND status NOT IN ('completed', 'failed', 'interrupted')
                    """
                ),
                params,
            )
        return await self.get_draw_task(task_id)

    async def fail_draw_task(
        self,
        task_id: str,
        *,
        status: str = "failed",
        error_code: str = "",
        error_message: str = "",
    ) -> DrawTaskRecord | None:
        return await self._update_task_status(
            task_id,
            status=status,
            error_code=error_code,
            error_message=error_message,
            finished_at=_utc_now_iso(),
            heartbeat_at=_utc_now_iso(),
        )

    async def claim_draw_task_callback(self, task_id: str) -> bool:
        task_id = str(task_id or "").strip()
        if not task_id:
            return True
        async with self._db() as db:
            dialect = _dialect_name(db)
            now = _timestamp_param(_utc_now(), dialect=dialect)
            result = await db.execute(
                text(
                    f"""
                    UPDATE {DRAW_TASK_TABLE}
                    SET callback_error = :callback_claim,
                        updated_at = :now
                    WHERE task_id = :task_id
                      AND callback_sent = :callback_sent
                      AND (
                        COALESCE(callback_error, '') <> :callback_claim
                        OR updated_at < :claim_cutoff
                      )
                    """
                ),
                {
                    "task_id": task_id,
                    "callback_sent": False,
                    "callback_claim": _DRAW_TASK_CALLBACK_CLAIM,
                    "claim_cutoff": _stale_cutoff(
                        _DRAW_TASK_CALLBACK_CLAIM_TIMEOUT_SECONDS,
                        dialect=dialect,
                    ),
                    "now": now,
                },
            )
            if getattr(result, "rowcount", 0):
                return True
            row = await _fetch_draw_task_row(db, task_id)
        if row is None:
            return True
        return False

    async def mark_draw_task_callback_sent(
        self,
        task_id: str,
        *,
        callback_error: str = "",
    ) -> None:
        task_id = str(task_id or "").strip()
        if not task_id:
            return
        async with self._db() as db:
            now = _timestamp_param(_utc_now(), dialect=_dialect_name(db))
            await db.execute(
                text(
                    f"""
                    UPDATE {DRAW_TASK_TABLE}
                    SET callback_sent = :callback_sent,
                        callback_error = :callback_error,
                        updated_at = :now
                    WHERE task_id = :task_id
                    """
                ),
                {
                    "task_id": task_id,
                    "callback_sent": True,
                    "callback_error": str(callback_error or "")[:1000],
                    "now": now,
                },
            )

    async def release_draw_task_callback_claim(
        self,
        task_id: str,
        *,
        reason: str = "scope_execution_denied",
    ) -> bool:
        """Release only the callback claim currently owned by this attempt."""

        task_id = str(task_id or "").strip()
        if not task_id:
            return False
        async with self._db() as db:
            now = _timestamp_param(_utc_now(), dialect=_dialect_name(db))
            result = await db.execute(
                text(
                    f"""
                    UPDATE {DRAW_TASK_TABLE}
                    SET callback_error = :reason,
                        updated_at = :now
                    WHERE task_id = :task_id
                      AND callback_sent = :callback_sent
                      AND callback_error = :callback_claim
                    """
                ),
                {
                    "task_id": task_id,
                    "callback_sent": False,
                    "callback_claim": _DRAW_TASK_CALLBACK_CLAIM,
                    "reason": str(reason or "scope_execution_denied")[:1000],
                    "now": now,
                },
            )
        return bool(getattr(result, "rowcount", 0))

    async def mark_draw_task_callback_error(
        self,
        task_id: str,
        *,
        callback_error: str,
        force: bool = False,
    ) -> None:
        task_id = str(task_id or "").strip()
        if not task_id:
            return
        async with self._db() as db:
            now = _timestamp_param(_utc_now(), dialect=_dialect_name(db))
            callback_filter = "" if force else "AND callback_sent = :callback_sent"
            await db.execute(
                text(
                    f"""
                    UPDATE {DRAW_TASK_TABLE}
                    SET callback_error = :callback_error,
                        updated_at = :now
                    WHERE task_id = :task_id
                      {callback_filter}
                    """
                ),
                {
                    "task_id": task_id,
                    "callback_error": str(callback_error or "")[:1000],
                    "callback_sent": False,
                    "now": now,
                },
            )

    async def create_retry_draw_task(
        self,
        parent: DrawTaskRecord,
        *,
        retry_count: int,
        next_run_at: str = "",
    ) -> DrawTaskRecord:
        retry_count = max(0, int(retry_count or 0))
        retry_task = DrawTaskCreate(
            request_id=parent.request_id,
            trace_id=f"{parent.trace_id}:retry{retry_count}" if parent.trace_id else "",
            command_type=parent.command_type,
            status="queued",
            tenant_id=parent.tenant_id,
            channel=parent.channel,
            source_key=parent.source_key,
            chat_id=parent.chat_id,
            session_id=parent.session_id,
            group_id=parent.group_id,
            user_id=parent.user_id,
            requester=parent.requester,
            requester_display_name=parent.requester_display_name,
            original_message_id=parent.original_message_id,
            callback_target=dict(parent.callback_target or {}),
            callback_reply_to_message_id=parent.callback_reply_to_message_id,
            source_message={
                **dict(parent.source_message or {}),
                "draw_retry_parent_task_id": parent.task_id,
                "draw_retry_count": retry_count,
            },
            prompt=parent.prompt,
            quality=parent.quality,
            size=parent.size,
            source_image={
                **dict(parent.source_image or {}),
                "retry_parent_task_id": parent.task_id,
            },
            retry_count=retry_count,
            next_run_at=next_run_at,
        )
        return await self.create_draw_task(retry_task)

    async def reserve_draw_task_retry(
        self,
        task_id: str,
        *,
        max_retries: int,
    ) -> DrawTaskRecord | None:
        task_id = str(task_id or "").strip()
        if not task_id:
            return None
        max_retries = max(0, int(max_retries or 0))
        async with self._db() as db:
            now = _timestamp_param(_utc_now(), dialect=_dialect_name(db))
            result = await db.execute(
                text(
                    f"""
                    UPDATE {DRAW_TASK_TABLE}
                    SET retry_count = retry_count + 1,
                        updated_at = :now
                    WHERE task_id = :task_id
                      AND status IN ('failed', 'interrupted')
                      AND retry_count < :max_retries
                    """
                ),
                {
                    "task_id": task_id,
                    "max_retries": max_retries,
                    "now": now,
                },
            )
            if not getattr(result, "rowcount", 0):
                return None
            row = await _fetch_draw_task_row(db, task_id)
        return _draw_task_record_from_row(row) if row is not None else None

    async def claim_due_draw_tasks(
        self,
        *,
        limit: int = 5,
        lock_ttl_seconds: float = 900.0,
        worker_id: str = "",
    ) -> list[DrawTaskRecord]:
        safe_limit = max(1, min(int(limit or 5), 50))
        safe_ttl = max(1.0, float(lock_ttl_seconds or 900.0))
        worker_id = str(worker_id or "draw-task-worker")[:128]
        claimed: list[DrawTaskRecord] = []
        async with self._db() as db:
            dialect = _dialect_name(db)
            now_dt = _utc_now()
            now = _timestamp_param(now_dt, dialect=dialect)
            locked_until = _timestamp_param(now_dt + timedelta(seconds=safe_ttl), dialect=dialect)
            result = await db.execute(
                text(
                    f"""
                    SELECT *
                    FROM {DRAW_TASK_TABLE}
                    WHERE callback_sent = :callback_sent
                      AND (
                        (
                          status = 'queued'
                          AND (next_run_at IS NULL OR next_run_at <= :now)
                          AND (locked_until IS NULL OR locked_until <= :now OR COALESCE(locked_by, '') = '')
                        )
                        OR (
                          status = 'running'
                          AND locked_until IS NOT NULL
                          AND locked_until <= :now
                        )
                      )
                    ORDER BY COALESCE(next_run_at, created_at) ASC, created_at ASC
                    LIMIT :limit
                    """
                ),
                {"callback_sent": False, "now": now, "limit": safe_limit},
            )
            candidates = [_draw_task_record_from_row(dict(row)) for row in result.mappings().all()]
            for task in candidates:
                update = await db.execute(
                    text(
                        f"""
                        UPDATE {DRAW_TASK_TABLE}
                        SET status = 'running',
                            locked_until = :locked_until,
                            locked_by = :worker_id,
                            started_at = COALESCE(started_at, :now),
                            heartbeat_at = :now,
                            updated_at = :now
                        WHERE task_id = :task_id
                          AND callback_sent = :callback_sent
                          AND (
                            (
                              status = 'queued'
                              AND (next_run_at IS NULL OR next_run_at <= :now)
                              AND (locked_until IS NULL OR locked_until <= :now OR COALESCE(locked_by, '') = '')
                            )
                            OR (
                              status = 'running'
                              AND locked_until IS NOT NULL
                              AND locked_until <= :now
                            )
                          )
                        """
                    ),
                    {
                        "task_id": task.task_id,
                        "callback_sent": False,
                        "now": now,
                        "locked_until": locked_until,
                        "worker_id": worker_id,
                    },
                )
                if not getattr(update, "rowcount", 0):
                    continue
                row = await _fetch_draw_task_row(db, task.task_id)
                if row is not None:
                    claimed.append(_draw_task_record_from_row(row))
        return claimed

    async def defer_draw_task_claim(
        self,
        task_id: str,
        *,
        worker_id: str,
        defer_seconds: float = _DRAW_TASK_SCOPE_DEFER_SECONDS,
    ) -> bool:
        """Release a queue claim denied by the current tenant/session policy."""

        task_id = str(task_id or "").strip()
        worker_id = str(worker_id or "").strip()[:128]
        if not task_id or not worker_id:
            return False
        async with self._db() as db:
            dialect = _dialect_name(db)
            now_dt = _utc_now()
            result = await db.execute(
                text(
                    f"""
                    UPDATE {DRAW_TASK_TABLE}
                    SET status = 'queued',
                        next_run_at = :next_run_at,
                        locked_until = NULL,
                        locked_by = '',
                        heartbeat_at = :now,
                        updated_at = :now
                    WHERE task_id = :task_id
                      AND status = 'running'
                      AND locked_by = :worker_id
                      AND callback_sent = :callback_sent
                    """
                ),
                {
                    "task_id": task_id,
                    "worker_id": worker_id,
                    "callback_sent": False,
                    "next_run_at": _timestamp_param(
                        now_dt
                        + timedelta(
                            seconds=max(1.0, float(defer_seconds or 30.0))
                        ),
                        dialect=dialect,
                    ),
                    "now": _timestamp_param(now_dt, dialect=dialect),
                },
            )
        return bool(getattr(result, "rowcount", 0))

    async def _update_task_status(
        self,
        task_id: str,
        *,
        status: str,
        error_code: str = "",
        error_message: str = "",
        started_at: str | None = None,
        finished_at: str | None = None,
        heartbeat_at: str | None = None,
    ) -> DrawTaskRecord | None:
        task_id = str(task_id or "").strip()
        if not task_id:
            return None
        normalized_status = _normalize_task_status(status)
        assignments = [
            "status = :status",
            "error_code = :error_code",
            "error_message = :error_message",
            "updated_at = :now",
        ]
        params: dict[str, Any] = {
            "task_id": task_id,
            "status": normalized_status,
            "error_code": str(error_code or "")[:128],
            "error_message": str(error_message or "")[:1000],
        }
        async with self._db() as db:
            dialect = _dialect_name(db)
            params["now"] = _timestamp_param(_utc_now(), dialect=dialect)
            if started_at is not None:
                assignments.append("started_at = COALESCE(started_at, :started_at)")
                params["started_at"] = _timestamp_param(started_at, dialect=dialect)
            if finished_at is not None:
                assignments.append("finished_at = :finished_at")
                params["finished_at"] = _timestamp_param(finished_at, dialect=dialect)
                assignments.append("locked_until = NULL")
                assignments.append("locked_by = ''")
            if heartbeat_at is not None:
                assignments.append("heartbeat_at = :heartbeat_at")
                params["heartbeat_at"] = _timestamp_param(heartbeat_at, dialect=dialect)
            await db.execute(
                text(
                    f"""
                    UPDATE {DRAW_TASK_TABLE}
                    SET {', '.join(assignments)}
                    WHERE task_id = :task_id
                      AND status NOT IN ('completed', 'failed', 'interrupted')
                    """
                ),
                params,
            )
        return await self.get_draw_task(task_id)

    @asynccontextmanager
    async def _db(self) -> AsyncIterator[AsyncSession]:
        active_connection = _ACTIVE_ADMIN_MUTATION_CONNECTION.get()
        if active_connection is not None:
            async with AsyncSession(
                bind=active_connection,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            ) as db:
                try:
                    yield db
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise
            return
        factory = self._session_factory
        if factory is None:
            from app.infra.db import get_session_factory

            factory = get_session_factory()
        async with factory() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def generate_image(
        self,
        prompt: str,
        *,
        trace_id: str,
        quality: str = DRAW_DEFAULT_QUALITY,
    ) -> DrawResult:
        prompt = str(prompt or "").strip()
        if not prompt:
            raise DrawApiError("提示词不能为空")
        quality = normalize_draw_quality(quality)

        self._ensure_storage_dir()
        endpoints = self._resolve_endpoints()
        if not endpoints:
            raise DrawConfigError("未配置 DRAW_API_URL")

        errors: list[str] = []
        for endpoint in endpoints:
            try:
                return await self._generate_from_endpoint(
                    endpoint,
                    prompt,
                    trace_id=trace_id,
                    quality=quality,
                )
            except DrawError as exc:
                errors.append(f"{endpoint.name}: {exc}")
                logger.warning(
                    "draw.endpoint_failed",
                    trace_id=trace_id,
                    endpoint=endpoint.name,
                    error=str(exc),
                )
                continue

        raise DrawApiError("; ".join(errors))

    async def edit_image(
        self,
        image_id: str,
        prompt: str,
        *,
        trace_id: str,
        quality: str = DRAW_DEFAULT_QUALITY,
    ) -> DrawResult:
        prompt = str(prompt or "").strip()
        if not prompt:
            raise DrawApiError("提示词不能为空")
        quality = normalize_draw_quality(quality)

        source = self.resolve_image_id(image_id)
        if source is None:
            raise DrawApiError("找不到这个图片 ID")

        endpoints = self._resolve_endpoints()
        if not endpoints:
            raise DrawConfigError("未配置 DRAW_API_URL")

        errors: list[str] = []
        for endpoint in endpoints:
            try:
                return await self._edit_from_endpoint(
                    endpoint,
                    source,
                    prompt,
                    trace_id=trace_id,
                    quality=quality,
                )
            except DrawError as exc:
                errors.append(f"{endpoint.name}: {exc}")
                logger.warning(
                    "draw.edit_endpoint_failed",
                    trace_id=trace_id,
                    endpoint=endpoint.name,
                    image_id=image_id,
                    error=str(exc),
                )
                continue

        raise DrawApiError("; ".join(errors))

    async def edit_reference_image(
        self,
        *,
        image_url: str = "",
        image_path: str = "",
        prompt: str,
        trace_id: str,
        quality: str = DRAW_DEFAULT_QUALITY,
        source_label: str = "reference",
    ) -> DrawResult:
        prompt = str(prompt or "").strip()
        if not prompt:
            raise DrawApiError("提示词不能为空")
        quality = normalize_draw_quality(quality)

        self._ensure_storage_dir()
        endpoints = self._resolve_endpoints()
        if not endpoints:
            raise DrawConfigError("未配置 DRAW_API_URL")

        errors: list[str] = []
        for endpoint in endpoints:
            try:
                client = await self._get_direct_client(endpoint.client_name, endpoint.timeout)
                configure_http_client(
                    client,
                    allowed_private_origins=[
                        endpoint.api_url,
                        str(getattr(self.settings, "wxbot_sdk_url", "") or ""),
                    ],
                    origin_headers={
                        str(getattr(self.settings, "wxbot_sdk_url", "") or ""):
                            wxbot_sdk_headers(self.settings)
                    },
                )
                source = await self._load_reference_image(
                    image_url=image_url,
                    image_path=image_path,
                    source_label=source_label,
                    client=client,
                )
                return await self._edit_from_input_image(
                    endpoint,
                    source,
                    prompt,
                    trace_id=trace_id,
                    quality=quality,
                )
            except DrawError as exc:
                errors.append(f"{endpoint.name}: {exc}")
                logger.warning(
                    "draw.reference_edit_endpoint_failed",
                    trace_id=trace_id,
                    endpoint=endpoint.name,
                    source_label=source_label,
                    error=str(exc),
                )
                continue

        raise DrawApiError("; ".join(errors))

    def _resolve_endpoints(self) -> list[DrawEndpoint]:
        endpoints: list[DrawEndpoint] = []
        for prefix, name, client_name in (
            ("draw_api", "primary", "primary"),
            ("draw_fallback_api", "fallback", "fallback"),
        ):
            endpoint = self._build_endpoint(prefix, name=name, client_name=client_name)
            if endpoint is not None:
                endpoints.append(endpoint)
        return endpoints

    def _build_endpoint(self, prefix: str, *, name: str, client_name: str) -> DrawEndpoint | None:
        api_url = self._normalize_generation_api_url(
            str(getattr(self.settings, f"{prefix}_url", "") or "").strip()
        )
        if not api_url:
            return None
        return DrawEndpoint(
            name=name,
            client_name=client_name,
            api_url=api_url,
            api_key=str(getattr(self.settings, f"{prefix}_key", "") or "").strip(),
            model=str(getattr(self.settings, f"{prefix}_model", "") or "").strip(),
            timeout=float(getattr(self.settings, f"{prefix}_timeout_seconds", 60.0) or 60.0),
            key_header=str(getattr(self.settings, f"{prefix}_key_header", "Authorization") or "Authorization").strip(),
            key_prefix=str(getattr(self.settings, f"{prefix}_key_prefix", "Bearer ") or ""),
            prompt_field=str(getattr(self.settings, f"{prefix}_prompt_field", "prompt") or "prompt").strip(),
            model_field=str(getattr(self.settings, f"{prefix}_model_field", "model") or "model").strip(),
            response_format=str(getattr(self.settings, f"{prefix}_response_format", "") or "").strip(),
            extra_body_raw=str(getattr(self.settings, f"{prefix}_extra_body", "") or "").strip(),
        )

    def _normalize_generation_api_url(self, api_url: str) -> str:
        api_url = str(api_url or "").strip()
        if not api_url:
            return ""
        stripped = api_url.rstrip("/")
        lower = stripped.lower()
        if lower.endswith(("/images/generations", "/image/generations")):
            return stripped
        parsed = urlparse(stripped)
        if parsed.scheme in {"http", "https"} and parsed.path.rstrip("/") in {"", "/v1"}:
            suffix = "images/generations" if parsed.path.rstrip("/") == "/v1" else "v1/images/generations"
            return f"{stripped}/{suffix}"
        return stripped

    async def _generate_from_endpoint(
        self,
        endpoint: DrawEndpoint,
        prompt: str,
        *,
        trace_id: str,
        quality: str,
    ) -> DrawResult:
        payload = self._build_payload(prompt, endpoint, quality=quality)
        headers = self._build_headers(endpoint)
        client = await self._get_client(endpoint.client_name, endpoint.timeout)
        configure_http_client(
            client,
            allowed_private_origins=[endpoint.api_url],
            origin_headers={endpoint.api_url: headers},
        )

        try:
            response = await safe_http_request(
                client,
                "POST",
                endpoint.api_url,
                json=payload,
                headers=headers,
                policy=_draw_endpoint_policy(endpoint.api_url, timeout=endpoint.timeout),
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise DrawApiError(
                f"绘图接口返回 {exc.response.status_code}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise DrawApiError("绘图接口请求超时") from exc
        except httpx.HTTPError as exc:
            raise DrawApiError("绘图接口请求失败") from exc

        file_bytes, media_type, source_url = await self._extract_image_payload(
            response,
            client=client,
            headers=headers,
            api_url=endpoint.api_url,
        )
        saved = self._save_image(
            file_bytes,
            media_type=media_type,
            trace_id=trace_id,
            prompt=prompt,
            source_url=source_url,
        )
        logger.info(
            "draw.image_generated",
            trace_id=trace_id,
            endpoint=endpoint.name,
            quality=quality,
            image_id=saved.image_id,
            file_name=saved.path.name,
            media_type=media_type,
        )
        return DrawResult(
            image_id=saved.image_id,
            prompt=prompt,
            local_path=str(saved.path),
            file_name=saved.path.name,
            media_type=media_type,
            public_path=saved.public_path,
            source_url=source_url,
        )

    async def _edit_from_endpoint(
        self,
        endpoint: DrawEndpoint,
        source: DrawImageRecord,
        prompt: str,
        *,
        trace_id: str,
        quality: str,
    ) -> DrawResult:
        image_path = self.resolve_file(source.file_name)
        if image_path is None:
            raise DrawApiError("原图文件不存在")

        try:
            image_bytes = image_path.read_bytes()
        except OSError as exc:
            raise DrawApiError("读取原图失败") from exc

        return await self._edit_from_input_image(
            endpoint,
            DrawInputImage(
                content=image_bytes,
                file_name=source.file_name,
                media_type=source.media_type or "image/png",
                source_image_id=source.image_id,
            ),
            prompt,
            trace_id=trace_id,
            quality=quality,
        )

    async def _edit_from_input_image(
        self,
        endpoint: DrawEndpoint,
        source: DrawInputImage,
        prompt: str,
        *,
        trace_id: str,
        quality: str,
    ) -> DrawResult:
        edit_url = self._resolve_edit_api_url(endpoint)
        data = self._build_payload(prompt, endpoint, quality=quality)
        headers = self._build_headers(endpoint)
        headers.pop("Content-Type", None)
        client = await self._get_client(endpoint.client_name, endpoint.timeout)
        configure_http_client(
            client,
            allowed_private_origins=[endpoint.api_url, edit_url],
            origin_headers={
                endpoint.api_url: headers,
                edit_url: headers,
            },
        )

        files = {
            "image": (
                source.file_name or "reference.png",
                source.content,
                source.media_type or "image/png",
            )
        }
        multipart_body, multipart_content_type = _encode_multipart(data, files)
        request_headers = {
            **headers,
            "Content-Type": multipart_content_type,
        }
        try:
            response = await safe_http_request(
                client,
                "POST",
                edit_url,
                content=multipart_body,
                headers=request_headers,
                policy=_draw_endpoint_policy(edit_url, timeout=endpoint.timeout),
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise DrawApiError(
                f"重绘接口返回 {exc.response.status_code}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise DrawApiError("重绘接口请求超时") from exc
        except httpx.HTTPError as exc:
            raise DrawApiError("重绘接口请求失败") from exc

        file_bytes, media_type, source_url = await self._extract_image_payload(
            response,
            client=client,
            headers=headers,
            api_url=edit_url,
        )
        saved = self._save_image(
            file_bytes,
            media_type=media_type,
            trace_id=trace_id,
            prompt=prompt,
            source_url=source_url,
            source_image_id=source.source_image_id,
        )
        logger.info(
            "draw.image_edited",
            trace_id=trace_id,
            endpoint=endpoint.name,
            quality=quality,
            image_id=saved.image_id,
            source_image_id=source.source_image_id,
            file_name=saved.path.name,
            media_type=media_type,
        )
        return DrawResult(
            image_id=saved.image_id,
            prompt=prompt,
            local_path=str(saved.path),
            file_name=saved.path.name,
            media_type=media_type,
            public_path=saved.public_path,
            source_url=source_url,
            source_image_id=source.source_image_id,
        )

    async def _load_reference_image(
        self,
        *,
        image_url: str,
        image_path: str,
        source_label: str,
        client: httpx.AsyncClient,
    ) -> DrawInputImage:
        image_url = str(image_url or "").strip()
        image_path = str(image_path or "").strip()
        source_id = str(source_label or "").strip() or "reference"

        if image_url.startswith("data:image/"):
            media_type, content = self._decode_data_image(image_url)
            return DrawInputImage(
                content=content,
                file_name=f"reference{self._extension_for(media_type, content)}",
                media_type=media_type,
                source_image_id=source_id,
                source_url=image_url,
            )

        if image_url:
            parsed = urlparse(image_url)
            if parsed.scheme in {"http", "https"}:
                last_error: Exception | None = None
                urls = preview_first_urls(image_url)
                wait_seconds = float(
                    getattr(self.settings, "wxbot_preview_wait_seconds", 8.0) or 0.0
                )
                poll_interval = float(
                    getattr(self.settings, "wxbot_preview_poll_interval_seconds", 0.7) or 0.7
                )
                for index, candidate_url in enumerate(urls):
                    try:
                        if index == 0 and len(urls) > 1:
                            fetched = await wait_for_image(
                                client,
                                candidate_url,
                                wait_seconds=wait_seconds,
                                poll_interval_seconds=poll_interval,
                                max_bytes=DRAW_MAX_REMOTE_IMAGE_BYTES,
                            )
                        else:
                            fetched = await fetch_image_once(
                                client,
                                candidate_url,
                                max_bytes=DRAW_MAX_REMOTE_IMAGE_BYTES,
                            )
                        file_name = self._reference_file_name(
                            fetched.url,
                            fetched.media_type,
                            fetched.content,
                        )
                        return DrawInputImage(
                            content=fetched.content,
                            file_name=file_name,
                            media_type=fetched.media_type,
                            source_image_id=source_id,
                            source_url=fetched.url,
                        )
                    except httpx.HTTPError as exc:
                        last_error = exc
                        logger.warning(
                            "draw.reference_image_fetch_failed",
                            source_label=source_id,
                            error_class=exc.__class__.__name__,
                        )
                        continue
                if not image_path and last_error is not None:
                    raise DrawApiError("读取引用图片失败") from last_error
            if not image_path:
                image_path = image_url

        if image_path:
            path = Path(image_path).expanduser().resolve()
            if not path.is_relative_to(self._storage_dir):
                raise DrawApiError("引用图片路径必须来自受控媒体缓存")
            if path.is_file():
                try:
                    content = path.read_bytes()
                except OSError as exc:
                    raise DrawApiError("读取引用图片失败") from exc
                media_type = self._media_type_for_extension(path.suffix)
                if not media_type.startswith("image/"):
                    media_type = "image/png"
                return DrawInputImage(
                    content=content,
                    file_name=path.name or f"reference{self._extension_for(media_type, content)}",
                    media_type=media_type,
                    source_image_id=source_id,
                    source_url="",
                )

        raise DrawApiError("引用图片不可读取")

    async def cache_reference_image(
        self,
        *,
        image_url: str = "",
        image_path: str = "",
        source_label: str = "reference",
        trace_id: str = "",
    ) -> DrawInputImage:
        self._ensure_storage_dir()
        client = await self._get_client("reference", 20.0)
        configure_http_client(
            client,
            allowed_private_origins=[
                str(getattr(self.settings, "wxbot_sdk_url", "") or ""),
            ],
            origin_headers={
                str(getattr(self.settings, "wxbot_sdk_url", "") or ""):
                    wxbot_sdk_headers(self.settings)
            },
        )
        source = await self._load_reference_image(
            image_url=image_url,
            image_path=image_path,
            source_label=source_label,
            client=client,
        )
        extension = self._extension_for(source.media_type, source.content)
        safe_label = "".join(ch for ch in source.source_image_id if ch.isalnum())[-24:] or "reference"
        safe_trace = "".join(ch for ch in trace_id if ch.isalnum())[-16:] or "draw"
        file_path = self._storage_dir / f"reference-{safe_label}-{safe_trace}-{uuid4().hex[:8]}{extension}"
        try:
            file_path.write_bytes(source.content)
        except OSError as exc:
            raise DrawApiError("缓存引用图片失败") from exc
        return DrawInputImage(
            content=source.content,
            file_name=str(file_path),
            media_type=source.media_type,
            source_image_id=source.source_image_id,
            source_url=source.source_url,
        )

    async def _get_client(self, client_name: str, timeout: float) -> httpx.AsyncClient:
        if client_name == "primary" and self._client is not None:
            return self._client
        existing = self._clients.get(client_name)
        if existing is not None:
            return existing
        client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )
        self._clients[client_name] = client
        if client_name == "primary":
            self._client = client
        return client

    async def _get_direct_client(self, client_name: str, timeout: float) -> httpx.AsyncClient:
        if client_name == "primary" and self._client is not None and "primary" not in self._clients:
            return self._client
        direct_name = f"{client_name}:direct"
        existing = self._clients.get(direct_name)
        if existing is not None:
            return existing
        client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )
        self._clients[direct_name] = client
        return client

    def _resolve_storage_dir(self, raw_path: str) -> Path:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = Path(self.settings.project_root) / candidate
        return candidate.resolve()

    def _ensure_storage_dir(self) -> None:
        try:
            self._storage_dir.mkdir(parents=True, exist_ok=True)
            probe_path = self._storage_dir / f".write-test-{uuid4().hex}"
            probe_path.write_text("", encoding="utf-8")
            probe_path.unlink(missing_ok=True)
        except OSError as exc:
            raise DrawConfigError(
                "DRAW_STORAGE_DIR 不可写, 请配置到 Windows/WSL 共享目录, 例如 "
                "/mnt/c/Users/Public/cs-system-draw"
            ) from exc

    def _build_payload(
        self,
        prompt: str,
        endpoint: DrawEndpoint,
        *,
        quality: str = DRAW_DEFAULT_QUALITY,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        prompt_field = endpoint.prompt_field
        model_field = endpoint.model_field
        response_format = endpoint.response_format
        extra_body_raw = endpoint.extra_body_raw
        model = endpoint.model

        if extra_body_raw:
            try:
                extra_body = json.loads(extra_body_raw)
            except json.JSONDecodeError as exc:
                raise DrawConfigError("DRAW_API_EXTRA_BODY 不是合法 JSON") from exc
            if not isinstance(extra_body, dict):
                raise DrawConfigError("DRAW_API_EXTRA_BODY 必须是 JSON object")
            payload.update(extra_body)

        payload[prompt_field] = prompt
        if model:
            payload[model_field] = model
        normalized_quality = normalize_draw_quality(quality)
        payload["quality"] = normalized_quality
        payload["size"] = DRAW_QUALITY_SIZES[normalized_quality]
        if response_format:
            payload.setdefault("response_format", response_format)
        return payload

    def _resolve_edit_api_url(self, endpoint: DrawEndpoint) -> str:
        setting_name = "draw_api_edit_url"
        if endpoint.name == "fallback":
            setting_name = "draw_fallback_api_edit_url"
        configured = str(getattr(self.settings, setting_name, "") or "").strip()
        if configured:
            return self._normalize_edit_api_url(configured)
        api_url = endpoint.api_url.rstrip("/")
        for suffix in ("/images/generations", "/image/generations"):
            if api_url.endswith(suffix):
                return f"{api_url[: -len(suffix)]}/images/edits"
        return api_url

    def _normalize_edit_api_url(self, api_url: str) -> str:
        api_url = str(api_url or "").strip()
        if not api_url:
            return ""
        stripped = api_url.rstrip("/")
        lower = stripped.lower()
        if lower.endswith(("/images/edits", "/image/edits")):
            return stripped
        parsed = urlparse(stripped)
        if parsed.scheme in {"http", "https"} and parsed.path.rstrip("/") in {"", "/v1"}:
            suffix = "images/edits" if parsed.path.rstrip("/") == "/v1" else "v1/images/edits"
            return f"{stripped}/{suffix}"
        return stripped

    def _build_headers(self, endpoint: DrawEndpoint) -> dict[str, str]:
        headers = {
            "Accept": "application/json, image/*, text/event-stream",
        }
        api_key = endpoint.api_key
        header_name = endpoint.key_header
        key_prefix = endpoint.key_prefix
        if api_key and header_name:
            headers[header_name] = f"{key_prefix}{api_key}" if key_prefix else api_key
        return headers

    async def _extract_image_payload(
        self,
        response: httpx.Response,
        *,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        api_url: str,
    ) -> tuple[bytes, str, str]:
        content_type = str(response.headers.get("content-type") or "").lower()
        if content_type.startswith("image/"):
            return response.content, content_type.split(";", 1)[0], ""

        try:
            payload = self._decode_json_payload(response)
        except ValueError as exc:
            raise DrawApiError("绘图接口未返回可解析的 JSON 或图片二进制") from exc

        image_b64 = self._find_first_str(
            payload,
            ("b64_json", "image_base64", "base64", "b64"),
        )
        if image_b64:
            media_type = self._find_first_str(
                payload,
                ("media_type", "mime_type", "content_type"),
            ) or "image/png"
            return self._decode_base64_image(image_b64), media_type, ""

        image_url = self._find_first_str(payload, ("url", "image_url"))
        if image_url:
            return await self._download_image(
                client,
                self._resolve_image_url(api_url, image_url),
                headers=headers,
            )

        raise DrawApiError("绘图接口响应中未找到图片数据")

    def _decode_json_payload(self, response: httpx.Response) -> Any:
        content_type = str(response.headers.get("content-type") or "").lower()
        text = response.text
        if "text/event-stream" not in content_type and not text.lstrip().startswith("data:"):
            return response.json()

        last_payload: Any = None
        image_payload: Any = None
        data_lines: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                decoded = self._decode_sse_data_lines(data_lines)
                if decoded is not None:
                    last_payload = decoded
                    if self._payload_contains_image(decoded):
                        image_payload = decoded
                data_lines = []
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
        decoded = self._decode_sse_data_lines(data_lines)
        if decoded is not None:
            last_payload = decoded
            if self._payload_contains_image(decoded):
                image_payload = decoded
        if image_payload is not None:
            return image_payload
        if last_payload is None:
            raise ValueError("SSE response did not contain JSON data")
        return last_payload

    def _payload_contains_image(self, payload: Any) -> bool:
        return bool(
            self._find_first_str(payload, ("b64_json", "image_base64", "base64", "b64"))
            or self._find_first_str(payload, ("url", "image_url"))
        )

    def _decode_sse_data_lines(self, data_lines: list[str]) -> Any:
        if not data_lines:
            return None
        data = "\n".join(line for line in data_lines if line)
        if not data or data == "[DONE]":
            return None
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return None

    async def _download_image(
        self,
        client: httpx.AsyncClient,
        image_url: str,
        *,
        headers: dict[str, str],
    ) -> tuple[bytes, str, str]:
        accept_header = str(headers.get("Accept") or "image/*,*/*")
        try:
            response = await safe_get(
                client,
                image_url,
                headers={"Accept": accept_header},
                max_response_bytes=DRAW_MAX_REMOTE_IMAGE_BYTES,
                allowed_content_types=("image/",),
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise DrawApiError("下载图片失败") from exc
        content_type = str(response.headers.get("content-type") or "image/png").split(";", 1)[0]
        if not content_type.startswith("image/"):
            raise DrawApiError("图片下载地址未返回图片内容")
        if len(response.content) > DRAW_MAX_REMOTE_IMAGE_BYTES:
            raise DrawApiError("下载图片超过大小限制")
        return response.content, content_type, image_url

    def _resolve_image_url(self, api_url: str, image_url: str) -> str:
        image_url = str(image_url or "").strip()
        if not image_url:
            return ""
        if image_url.startswith(("http://", "https://")):
            return image_url
        return urljoin(api_url, image_url)

    def _find_first_str(self, payload: Any, field_names: tuple[str, ...]) -> str:
        if isinstance(payload, dict):
            for field_name in field_names:
                value = payload.get(field_name)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for value in payload.values():
                found = self._find_first_str(value, field_names)
                if found:
                    return found
        elif isinstance(payload, list):
            for value in payload:
                found = self._find_first_str(value, field_names)
                if found:
                    return found
        return ""

    def _decode_base64_image(self, encoded: str) -> bytes:
        body = encoded.strip()
        if body.startswith("data:") and "," in body:
            body = body.split(",", 1)[1]
        try:
            return base64.b64decode(body, validate=True)
        except binascii.Error as exc:
            raise DrawApiError("接口返回的 base64 图片数据无效") from exc

    def _decode_data_image(self, image_url: str) -> tuple[str, bytes]:
        header, _, body = str(image_url or "").partition(",")
        if not body:
            raise DrawApiError("引用图片 data URL 无效")
        media_type = header.removeprefix("data:").split(";", 1)[0] or "image/png"
        return media_type, self._decode_base64_image(body)

    def _reference_file_name(self, image_url: str, media_type: str, content: bytes) -> str:
        parsed = urlparse(image_url)
        name = Path(unquote(parsed.path or "")).name
        if name and "." in name:
            return name
        return f"reference{self._extension_for(media_type, content)}"

    @dataclass
    class _SavedImage:
        image_id: str
        path: Path
        public_path: str

    def _save_image(
        self,
        content: bytes,
        *,
        media_type: str,
        trace_id: str,
        prompt: str,
        source_url: str = "",
        source_image_id: str = "",
    ) -> _SavedImage:
        if not content:
            raise DrawApiError("接口返回了空图片内容")
        extension = self._extension_for(media_type, content)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        safe_trace = "".join(ch for ch in trace_id if ch.isalnum())[-16:] or "draw"
        image_id = f"img_{timestamp}_{uuid4().hex[:8]}"
        file_path = self._storage_dir / f"{image_id}-{safe_trace}{extension}"
        file_path.write_bytes(content)
        public_path = f"/plugins/draw/files/{quote(file_path.name)}"
        self._upsert_record(
            DrawImageRecord(
                image_id=image_id,
                prompt=prompt,
                local_path=str(file_path),
                file_name=file_path.name,
                media_type=media_type,
                public_path=public_path,
                source_url=source_url,
                source_image_id=source_image_id,
                created_at=datetime.now(UTC).isoformat(),
            )
        )
        return self._SavedImage(image_id=image_id, path=file_path, public_path=public_path)

    def _read_index(self) -> dict[str, DrawImageRecord]:
        try:
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError):
            logger.warning("draw.index_read_failed", index_path=str(self._index_path))
            return {}
        items = raw.get("images") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return {}
        records: dict[str, DrawImageRecord] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            image_id = str(item.get("image_id") or "").strip()
            file_name = str(item.get("file_name") or "").strip()
            if not image_id or not file_name:
                continue
            records[image_id] = DrawImageRecord(
                image_id=image_id,
                prompt=str(item.get("prompt") or ""),
                local_path=str(item.get("local_path") or str(self._storage_dir / file_name)),
                file_name=file_name,
                media_type=str(item.get("media_type") or "image/png"),
                public_path=str(item.get("public_path") or f"/plugins/draw/files/{quote(file_name)}"),
                source_url=str(item.get("source_url") or ""),
                source_image_id=str(item.get("source_image_id") or ""),
                created_at=str(item.get("created_at") or ""),
            )
        return records

    def _write_index(self, records: dict[str, DrawImageRecord]) -> None:
        items = [record.as_dict() for record in records.values()]
        items.sort(key=lambda item: str(item.get("created_at") or item.get("file_name") or ""))
        tmp_path = self._index_path.with_suffix(".json.tmp")
        payload = json.dumps({"images": items}, ensure_ascii=False, indent=2)
        tmp_path.write_text(f"{payload}\n", encoding="utf-8")
        replace(tmp_path, self._index_path)

    def _upsert_record(self, record: DrawImageRecord) -> None:
        records = self._read_index()
        records[record.image_id] = record
        self._write_index(records)

    def _sync_index_with_files(self) -> None:
        records = self._read_index()
        changed = False
        known_files = {record.file_name for record in records.values()}
        for path in self._storage_dir.iterdir():
            if not path.is_file() or path.name == self._index_path.name:
                continue
            if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".img"}:
                continue
            if path.name in known_files:
                continue
            image_id = path.stem
            records[image_id] = DrawImageRecord(
                image_id=image_id,
                prompt="",
                local_path=str(path),
                file_name=path.name,
                media_type=self._media_type_for_extension(path.suffix),
                public_path=f"/plugins/draw/files/{quote(path.name)}",
                created_at=datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
            )
            changed = True
        if changed:
            self._write_index(records)

    def _extension_for(self, media_type: str, content: bytes) -> str:
        media_type = (media_type or "").lower()
        if "png" in media_type or content.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if "jpeg" in media_type or "jpg" in media_type or content.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if "webp" in media_type or (content.startswith(b"RIFF") and content[8:12] == b"WEBP"):
            return ".webp"
        if "gif" in media_type or content.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        return ".img"

    def _media_type_for_extension(self, extension: str) -> str:
        extension = extension.lower()
        if extension == ".png":
            return "image/png"
        if extension in {".jpg", ".jpeg"}:
            return "image/jpeg"
        if extension == ".webp":
            return "image/webp"
        if extension == ".gif":
            return "image/gif"
        return "application/octet-stream"


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp_param(value: datetime | str, *, dialect: str) -> datetime | str:
    if dialect != "postgresql":
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value or "")
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value or ""))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _stale_cutoff(stale_seconds: float, *, dialect: str) -> datetime | str:
    try:
        seconds = float(stale_seconds)
    except (TypeError, ValueError):
        seconds = 3600.0
    seconds = max(1.0, seconds)
    cutoff = datetime.now(UTC) - timedelta(seconds=seconds)
    if dialect == "postgresql":
        return cutoff
    return cutoff.isoformat()


def _is_stale_timestamp(value: str, *, seconds: float) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed < datetime.now(UTC) - timedelta(seconds=max(1.0, float(seconds)))


def _dialect_name(db: AsyncSession) -> str:
    bind = db.get_bind()
    return str(bind.dialect.name)


def _json_dumps(value: Any) -> str:
    return json.dumps(
        value if value is not None else {},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _json_loads(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(decoded, dict):
            return decoded
    return {}


def _normalize_task_status(status: object) -> str:
    value = str(status or "queued").strip().lower()
    if value not in DRAW_TASK_STATUSES:
        raise ValueError(f"invalid draw task status: {value}")
    return value


def _normalize_task_command_type(command_type: object) -> str:
    value = str(command_type or "").strip().lower()
    if "redraw" in value or "重绘" in value:
        return "redraw"
    return "draw"


async def _fetch_draw_task_row(db: AsyncSession, task_id: str) -> dict[str, Any] | None:
    result = await db.execute(
        text(f"SELECT * FROM {DRAW_TASK_TABLE} WHERE task_id = :task_id"),
        {"task_id": task_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


def _row_str(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


def _row_bool(row: dict[str, Any], key: str) -> bool:
    value = row.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    if isinstance(value, str):
        return value.lower() in {"1", "true", "t", "yes"}
    return bool(value)


def _row_int(row: dict[str, Any], key: str) -> int:
    try:
        return int(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _draw_task_record_from_row(row: dict[str, Any]) -> DrawTaskRecord:
    return DrawTaskRecord(
        task_id=_row_str(row, "task_id"),
        request_id=_row_str(row, "request_id"),
        trace_id=_row_str(row, "trace_id"),
        command_type=_row_str(row, "command_type"),
        status=_row_str(row, "status"),
        tenant_id=_row_str(row, "tenant_id"),
        channel=_row_str(row, "channel"),
        source_key=_row_str(row, "source_key"),
        chat_id=_row_str(row, "chat_id"),
        session_id=_row_str(row, "session_id"),
        group_id=_row_str(row, "group_id"),
        user_id=_row_str(row, "user_id"),
        requester=_row_str(row, "requester"),
        requester_display_name=_row_str(row, "requester_display_name"),
        original_message_id=_row_str(row, "original_message_id"),
        callback_target=_json_loads(row.get("callback_target_json")),
        callback_reply_to_message_id=_row_str(row, "callback_reply_to_message_id"),
        source_message=_json_loads(row.get("source_message_json")),
        prompt=_row_str(row, "prompt"),
        quality=_row_str(row, "quality") or DRAW_DEFAULT_QUALITY,
        size=_row_str(row, "size"),
        source_image=_json_loads(row.get("source_image_json")),
        result_image_id=_row_str(row, "result_image_id"),
        result_local_path=_row_str(row, "result_local_path"),
        result_file_name=_row_str(row, "result_file_name"),
        result_media_type=_row_str(row, "result_media_type"),
        result_public_path=_row_str(row, "result_public_path"),
        result_source_url=_row_str(row, "result_source_url"),
        error_code=_row_str(row, "error_code"),
        error_message=_row_str(row, "error_message"),
        retry_count=_row_int(row, "retry_count"),
        next_run_at=_row_str(row, "next_run_at"),
        locked_until=_row_str(row, "locked_until"),
        locked_by=_row_str(row, "locked_by"),
        callback_sent=_row_bool(row, "callback_sent"),
        callback_error=_row_str(row, "callback_error"),
        created_at=_row_str(row, "created_at"),
        updated_at=_row_str(row, "updated_at"),
        started_at=_row_str(row, "started_at"),
        finished_at=_row_str(row, "finished_at"),
        heartbeat_at=_row_str(row, "heartbeat_at"),
    )


def _insert_draw_task_sql(dialect: str) -> str:
    conflict_clause = "ON CONFLICT (task_id) DO NOTHING" if dialect in {"postgresql", "sqlite"} else ""
    return f"""
        INSERT INTO {DRAW_TASK_TABLE} (
            task_id,
            request_id,
            trace_id,
            command_type,
            status,
            tenant_id,
            channel,
            source_key,
            chat_id,
            session_id,
            group_id,
            user_id,
            requester,
            requester_display_name,
            original_message_id,
            callback_target_json,
            callback_reply_to_message_id,
            source_message_json,
            prompt,
            quality,
            size,
            source_image_json,
            retry_count,
            next_run_at,
            created_at,
            updated_at,
            heartbeat_at
        )
        VALUES (
            :task_id,
            :request_id,
            :trace_id,
            :command_type,
            :status,
            :tenant_id,
            :channel,
            :source_key,
            :chat_id,
            :session_id,
            :group_id,
            :user_id,
            :requester,
            :requester_display_name,
            :original_message_id,
            :callback_target_json,
            :callback_reply_to_message_id,
            :source_message_json,
            :prompt,
            :quality,
            :size,
            :source_image_json,
            :retry_count,
            :next_run_at,
            :created_at,
            :updated_at,
            :heartbeat_at
        )
        {conflict_clause}
    """
