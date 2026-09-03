"""Persistence for speaker portraits and generation jobs."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from app.common.logging import get_logger
from app.infra.db import get_engine
from app.infra.runtime_schema import verify_runtime_schema

logger = get_logger(__name__)

PORTRAIT_TABLE = "plugin_speaker_portraits"
REVISION_TABLE = "plugin_speaker_portrait_revisions"
JOB_TABLE = "plugin_speaker_portrait_jobs"


def _now() -> datetime:
    return datetime.now(UTC)


def _dumps(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"))


def _loads(value: Any) -> dict[str, Any]:
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


def _row(row: Any) -> dict[str, Any]:
    return dict(row._mapping) if hasattr(row, "_mapping") else dict(row)


class SpeakerPortraitStore:
    def __init__(self, settings: Any) -> None:
        self.settings = settings

    async def ensure_tables(self) -> None:
        await verify_runtime_schema(get_engine(), component="speaker_portrait store")
        logger.info("speaker_portrait.schema_verified")

    async def create_job(
        self,
        *,
        tenant_id: str,
        session_id: str,
        session_name: str,
        speaker_id: str,
        speaker_name: str,
        connection_id: str = "",
        external_session_id: str = "",
        days_limit: int = 90,
        max_messages: int = 4000,
        mode: str = "full",
        since_timestamp: str = "",
        portrait_id: int | None = None,
        claimed_pending_messages: int | None = None,
    ) -> dict[str, Any]:
        normalized_mode = "incremental" if str(mode or "").strip().lower() == "incremental" else "full"
        async with get_engine().begin() as conn:
            portrait_result = await conn.execute(
                text(
                    f"""
                    SELECT id, session_id, pending_messages, last_distilled_message_at
                    FROM {PORTRAIT_TABLE}
                    WHERE tenant_id = :tenant_id AND channel = 'wechat'
                      AND source_key = 'wxbot' AND speaker_id = :speaker_id
                    FOR UPDATE
                    """
                ),
                {"tenant_id": tenant_id, "speaker_id": speaker_id},
            )
            portrait = portrait_result.mappings().first()
            resolved_portrait_id = int(portrait_id) if portrait_id is not None else None
            if resolved_portrait_id is None and portrait is not None:
                resolved_portrait_id = int(portrait["id"])
            resolved_pending = claimed_pending_messages
            if resolved_pending is None:
                resolved_pending = int(portrait["pending_messages"] or 0) if portrait is not None else 0
            resolved_since = str(since_timestamp or "").strip()
            if normalized_mode == "incremental" and not resolved_since and portrait is not None:
                resolved_since = str(portrait["last_distilled_message_at"] or "").strip()
            resolved_external_session_id = str(external_session_id or "").strip()
            if (
                (not resolved_external_session_id or resolved_external_session_id.startswith("cx1:"))
                and portrait is not None
            ):
                stored_session_id = str(portrait["session_id"] or "").strip()
                if stored_session_id and not stored_session_id.startswith("cx1:"):
                    resolved_external_session_id = stored_session_id
            if resolved_portrait_id is not None:
                active_result = await conn.execute(
                    text(
                        f"""
                        SELECT * FROM {JOB_TABLE}
                        WHERE portrait_id = :portrait_id
                          AND status IN ('queued', 'running')
                        ORDER BY id
                        LIMIT 1
                        """
                    ),
                    {"portrait_id": resolved_portrait_id},
                )
                active = active_result.mappings().first()
                if active is not None:
                    return dict(active)
            result = await conn.execute(
                text(
                    f"""
                    INSERT INTO {JOB_TABLE} (
                        tenant_id, session_id, session_name, speaker_id, speaker_name,
                        connection_id, external_session_id, status, days_limit, max_messages,
                        mode, since_timestamp, portrait_id, claimed_pending_messages,
                        created_at, updated_at
                    ) VALUES (
                        :tenant_id, :session_id, :session_name, :speaker_id, :speaker_name,
                        :connection_id, :external_session_id, 'queued', :days_limit, :max_messages,
                        :mode, :since_timestamp, :portrait_id, :claimed_pending_messages,
                        :now, :now
                    )
                    RETURNING id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "session_id": session_id,
                    "session_name": session_name,
                    "speaker_id": speaker_id,
                    "speaker_name": speaker_name,
                    "connection_id": connection_id,
                    "external_session_id": resolved_external_session_id,
                    "days_limit": days_limit,
                    "max_messages": max_messages,
                    "mode": normalized_mode,
                    "since_timestamp": resolved_since[:64],
                    "portrait_id": resolved_portrait_id,
                    "claimed_pending_messages": max(0, int(resolved_pending or 0)),
                    "now": _now(),
                },
            )
            job_id = int(result.scalar_one())
        job = await self.get_job(job_id)
        return job or {"id": job_id, "status": "queued"}

    async def get_job(self, job_id: int) -> dict[str, Any] | None:
        async with get_engine().connect() as conn:
            result = await conn.execute(
                text(f"SELECT * FROM {JOB_TABLE} WHERE id = :id"),
                {"id": job_id},
            )
            row = result.mappings().first()
        return dict(row) if row else None

    async def list_jobs(
        self,
        *,
        tenant_id: str,
        session_id: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        sql = f"SELECT * FROM {JOB_TABLE} WHERE tenant_id = :tenant_id"
        params: dict[str, Any] = {"tenant_id": tenant_id, "limit": max(1, min(limit, 200))}
        if session_id:
            sql += " AND session_id = :session_id"
            params["session_id"] = session_id
        sql += " ORDER BY id DESC LIMIT :limit"
        async with get_engine().connect() as conn:
            result = await conn.execute(text(sql), params)
            return [dict(row) for row in result.mappings().all()]

    async def claim_next_job(self, *, claim_owner: str, lease_seconds: float) -> dict[str, Any] | None:
        until = _now() + timedelta(seconds=max(30.0, lease_seconds))
        async with get_engine().begin() as conn:
            result = await conn.execute(
                text(
                    f"""
                    SELECT id FROM {JOB_TABLE}
                    WHERE status = 'queued'
                      AND (locked_until IS NULL OR locked_until < :now)
                    ORDER BY id
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                    """
                ),
                {"now": _now()},
            )
            row = result.first()
            if row is None:
                return None
            job_id = int(row[0])
            await conn.execute(
                text(
                    f"""
                    UPDATE {JOB_TABLE}
                    SET status = 'running', locked_by = :owner, locked_until = :until,
                        started_at = :now, updated_at = :now
                    WHERE id = :id
                    """
                ),
                {"owner": claim_owner, "until": until, "now": _now(), "id": job_id},
            )
        return await self.get_job(job_id)

    async def complete_empty_job(self, job_id: int) -> None:
        async with get_engine().begin() as conn:
            await conn.execute(
                text(
                    f"""
                    UPDATE {JOB_TABLE}
                    SET status = 'completed', error = '', locked_by = '', locked_until = NULL,
                        finished_at = :now, updated_at = :now
                    WHERE id = :id
                    """
                ),
                {"now": _now(), "id": job_id},
            )

    async def fail_job(self, job_id: int, error: str) -> None:
        async with get_engine().begin() as conn:
            await conn.execute(
                text(
                    f"""
                    UPDATE {JOB_TABLE}
                    SET status = 'failed', error = :error, locked_by = '', locked_until = NULL,
                        finished_at = :now, updated_at = :now
                    WHERE id = :id
                    """
                ),
                {"error": str(error or "")[:2000], "now": _now(), "id": job_id},
            )

    async def complete_job(
        self,
        job_id: int,
        *,
        portrait: dict[str, Any],
        evidence: dict[str, Any],
        speaker_id: str,
        speaker_name: str,
        tenant_id: str,
        session_id: str,
        channel: str = "wechat",
        source_key: str = "wxbot",
        message_count: int = 0,
        last_message_at: str = "",
        mode: str = "full",
    ) -> dict[str, Any]:
        now = _now()
        async with get_engine().begin() as conn:
            job_state = await conn.execute(
                text(
                    f"""
                    SELECT claimed_pending_messages
                    FROM {JOB_TABLE}
                    WHERE id = :job_id
                    FOR UPDATE
                    """
                ),
                {"job_id": job_id},
            )
            job_row = job_state.mappings().first()
            claimed_pending_messages = (
                max(0, int(job_row["claimed_pending_messages"] or 0))
                if job_row is not None
                else 0
            )
            existing = await conn.execute(
                text(
                    f"""
                    SELECT id FROM {PORTRAIT_TABLE}
                    WHERE tenant_id = :tenant_id AND channel = :channel
                      AND source_key = :source_key AND speaker_id = :speaker_id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "channel": channel,
                    "source_key": source_key,
                    "speaker_id": speaker_id,
                },
            )
            portrait_id = existing.scalar_one_or_none()
            if portrait_id is None:
                inserted = await conn.execute(
                    text(
                        f"""
                        INSERT INTO {PORTRAIT_TABLE} (
                            tenant_id, channel, source_key, speaker_id, display_name,
                            session_id, status, created_at, updated_at
                        ) VALUES (
                            :tenant_id, :channel, :source_key, :speaker_id, :display_name,
                            :session_id, 'ready', :now, :now
                        )
                        RETURNING id
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "channel": channel,
                        "source_key": source_key,
                        "speaker_id": speaker_id,
                        "display_name": speaker_name,
                        "session_id": session_id,
                        "now": now,
                    },
                )
                portrait_id = int(inserted.scalar_one())
            else:
                portrait_id = int(portrait_id)
                await conn.execute(
                    text(
                        f"""
                        UPDATE {PORTRAIT_TABLE}
                        SET display_name = :display_name,
                            session_id = CASE
                                WHEN :session_id <> '' AND :session_id NOT LIKE 'cx1:%'
                                    THEN :session_id
                                ELSE session_id
                            END,
                            status = 'ready', updated_at = :now
                        WHERE id = :id
                        """
                    ),
                    {
                        "display_name": speaker_name,
                        "session_id": session_id,
                        "now": now,
                        "id": portrait_id,
                    },
                )
            revision = await conn.execute(
                text(
                    f"""
                    INSERT INTO {REVISION_TABLE} (
                        portrait_id, schema_version, portrait_json, evidence_json,
                        source, job_id, created_at
                    ) VALUES (
                        :portrait_id, 1, :portrait_json, :evidence_json,
                        'local_cli', :job_id, :now
                    )
                    RETURNING id
                    """
                ),
                {
                    "portrait_id": portrait_id,
                    "portrait_json": _dumps(portrait),
                    "evidence_json": _dumps(evidence),
                    "job_id": job_id,
                    "now": now,
                },
            )
            revision_id = int(revision.scalar_one())
            await conn.execute(
                text(
                    f"""
                    UPDATE {PORTRAIT_TABLE}
                    SET current_revision_id = :revision_id,
                        last_message_at = CASE
                            WHEN :last_message_at <> '' AND (
                                last_message_at = '' OR :last_message_at > last_message_at
                            ) THEN :last_message_at
                            ELSE last_message_at
                        END,
                        last_distilled_message_at = CASE
                            WHEN :last_message_at <> '' AND (
                                last_distilled_message_at = ''
                                OR :last_message_at > last_distilled_message_at
                            ) THEN :last_message_at
                            ELSE last_distilled_message_at
                        END,
                        pending_messages = GREATEST(
                            pending_messages - :claimed_pending_messages,
                            0
                        ),
                        last_full_at = CASE
                            WHEN :mode = 'full' THEN :now
                            ELSE last_full_at
                        END,
                        updated_at = :now
                    WHERE id = :id
                    """
                ),
                {
                    "revision_id": revision_id,
                    "last_message_at": str(last_message_at or "")[:64],
                    "mode": "incremental" if str(mode or "") == "incremental" else "full",
                    "claimed_pending_messages": claimed_pending_messages,
                    "now": now,
                    "id": portrait_id,
                },
            )
            await conn.execute(
                text(
                    f"""
                    UPDATE {JOB_TABLE}
                    SET status = 'completed', error = '', message_count = :message_count,
                        portrait_id = :portrait_id, revision_id = :revision_id,
                        locked_by = '', locked_until = NULL,
                        finished_at = :now, updated_at = :now
                    WHERE id = :job_id
                    """
                ),
                {
                    "message_count": message_count,
                    "portrait_id": portrait_id,
                    "revision_id": revision_id,
                    "now": now,
                    "job_id": job_id,
                },
            )
        return {
            "portrait_id": portrait_id,
            "revision_id": revision_id,
        }

    async def get_portrait(
        self,
        *,
        tenant_id: str,
        speaker_id: str,
        channel: str = "wechat",
        source_key: str = "wxbot",
    ) -> dict[str, Any] | None:
        async with get_engine().connect() as conn:
            result = await conn.execute(
                text(
                    f"""
                    SELECT p.*, r.portrait_json, r.evidence_json, r.created_at AS revision_created_at
                    FROM {PORTRAIT_TABLE} p
                    LEFT JOIN {REVISION_TABLE} r ON r.id = p.current_revision_id
                    WHERE p.tenant_id = :tenant_id AND p.channel = :channel
                      AND p.source_key = :source_key AND p.speaker_id = :speaker_id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "channel": channel,
                    "source_key": source_key,
                    "speaker_id": speaker_id,
                },
            )
            row = result.mappings().first()
        if not row:
            return None
        payload = dict(row)
        payload["portrait"] = _loads(payload.pop("portrait_json", {}))
        payload["evidence"] = _loads(payload.pop("evidence_json", {}))
        return payload

    async def list_revision_portraits(self, portrait_id: int) -> list[dict[str, Any]]:
        async with get_engine().connect() as conn:
            result = await conn.execute(
                text(
                    f"""
                    SELECT portrait_json FROM {REVISION_TABLE}
                    WHERE portrait_id = :id
                    ORDER BY id ASC
                    """
                ),
                {"id": int(portrait_id)},
            )
            rows = result.mappings().all()
        portraits: list[dict[str, Any]] = []
        for row in rows:
            payload = _loads(row.get("portrait_json"))
            if payload:
                portraits.append(payload)
        return portraits

    async def list_portraits(
        self,
        *,
        tenant_id: str,
        session_id: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        sql = f"SELECT * FROM {PORTRAIT_TABLE} WHERE tenant_id = :tenant_id"
        params: dict[str, Any] = {"tenant_id": tenant_id, "limit": max(1, min(limit, 200))}
        if session_id:
            sql += " AND session_id = :session_id"
            params["session_id"] = session_id
        sql += " ORDER BY updated_at DESC LIMIT :limit"
        async with get_engine().connect() as conn:
            result = await conn.execute(text(sql), params)
            return [dict(row) for row in result.mappings().all()]

    async def note_speaker_message(
        self,
        *,
        tenant_id: str,
        speaker_id: str,
        speaker_name: str = "",
        session_id: str = "",
        timestamp: str = "",
        channel: str = "wechat",
        source_key: str = "wxbot",
    ) -> bool:
        """Count a new utterance for an existing portrait. No-op if none exists."""

        async with get_engine().begin() as conn:
            result = await conn.execute(
                text(
                    f"""
                    UPDATE {PORTRAIT_TABLE}
                    SET pending_messages = pending_messages + 1,
                        display_name = CASE
                            WHEN :speaker_name <> '' THEN :speaker_name
                            ELSE display_name
                        END,
                        session_id = CASE
                            WHEN :session_id <> '' AND :session_id NOT LIKE 'cx1:%'
                                THEN :session_id
                            ELSE session_id
                        END,
                        last_message_at = CASE
                            WHEN :timestamp <> '' AND (
                                last_message_at = '' OR :timestamp > last_message_at
                            ) THEN :timestamp
                            ELSE last_message_at
                        END
                    WHERE tenant_id = :tenant_id AND channel = :channel
                      AND source_key = :source_key AND speaker_id = :speaker_id
                      AND hot_update_enabled IS TRUE
                    """
                ),
                {
                    "speaker_name": str(speaker_name or "")[:256],
                    "session_id": str(session_id or "")[:256],
                    "timestamp": str(timestamp or "")[:64],
                    "tenant_id": tenant_id,
                    "channel": channel,
                    "source_key": source_key,
                    "speaker_id": speaker_id,
                },
            )
            return int(result.rowcount or 0) > 0

    async def due_hot_updates(
        self,
        *,
        min_messages: int,
        min_seconds: float,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        cutoff = _now() - timedelta(seconds=max(60.0, min_seconds))
        async with get_engine().connect() as conn:
            result = await conn.execute(
                text(
                    f"""
                    SELECT p.*
                    FROM {PORTRAIT_TABLE} p
                    WHERE p.hot_update_enabled IS TRUE
                      AND p.pending_messages > 0
                      AND (
                        p.pending_messages >= :min_messages
                        OR p.updated_at <= :cutoff
                      )
                      AND NOT EXISTS (
                        SELECT 1 FROM {JOB_TABLE} j
                        WHERE j.portrait_id = p.id
                          AND (
                            j.status IN ('queued', 'running')
                            OR (
                              j.mode = 'incremental'
                              AND j.created_at > :cutoff
                            )
                          )
                      )
                    ORDER BY p.pending_messages DESC, p.updated_at ASC
                    LIMIT :limit
                    """
                ),
                {
                    "min_messages": max(1, int(min_messages)),
                    "cutoff": cutoff,
                    "limit": max(1, min(limit, 20)),
                },
            )
            return [dict(row) for row in result.mappings().all()]

    async def clear_pending(self, portrait_id: int) -> None:
        async with get_engine().begin() as conn:
            await conn.execute(
                text(
                    f"""
                    UPDATE {PORTRAIT_TABLE}
                    SET pending_messages = 0, updated_at = :now
                    WHERE id = :id
                    """
                ),
                {"now": _now(), "id": portrait_id},
            )
