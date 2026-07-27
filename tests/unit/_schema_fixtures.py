"""Explicit schemas for isolated SQLite tests.

Application code must never create or mutate database schema at runtime.
These fixtures mirror the Alembic-owned tables only for hermetic unit tests.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from sqlalchemy import text

from app.orchestrator.effect_log import EFFECT_LOG_SCHEMA_REVISION


async def bootstrap_effect_log_schema(
    factory: Any,
    *,
    revision: str = EFFECT_LOG_SCHEMA_REVISION,
) -> None:
    async with factory() as db:
        await db.execute(
            text(
                """
                CREATE TABLE flow_effect_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    trace_id TEXT NOT NULL DEFAULT '',
                    owner TEXT NOT NULL,
                    type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'prepared',
                    dry_run INTEGER NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL DEFAULT '{}',
                    claim_owner TEXT NOT NULL DEFAULT '',
                    lease_expires_at TEXT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    started_at TEXT NULL,
                    completed_at TEXT NULL,
                    failed_at TEXT NULL,
                    CONSTRAINT ck_flow_effect_log_status
                        CHECK (status IN ('prepared', 'running', 'completed', 'failed')),
                    CONSTRAINT ck_flow_effect_log_attempt CHECK (attempt >= 0),
                    CONSTRAINT uq_flow_effect_log_tenant_key_dry
                        UNIQUE (tenant_id, idempotency_key, dry_run)
                )
                """
            )
        )
        await db.execute(
            text(
                """
                CREATE INDEX ix_flow_effect_log_status_lease
                ON flow_effect_log (status, lease_expires_at)
                """
            )
        )
        await db.execute(
            text(
                """
                CREATE INDEX ix_flow_effect_log_tenant_created
                ON flow_effect_log (tenant_id, created_at)
                """
            )
        )
        await db.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(128) PRIMARY KEY)")
        )
        await db.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": revision},
        )
        await db.commit()


def bootstrap_draw_task_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE plugin_draw_task (
            task_id VARCHAR(64) PRIMARY KEY,
            request_id VARCHAR(128) DEFAULT '',
            trace_id VARCHAR(128) DEFAULT '',
            command_type VARCHAR(32) NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'queued',
            tenant_id VARCHAR(64) DEFAULT '',
            channel VARCHAR(32) DEFAULT '',
            source_key TEXT DEFAULT '',
            chat_id VARCHAR(256) DEFAULT '',
            session_id VARCHAR(256) DEFAULT '',
            group_id VARCHAR(256) DEFAULT '',
            user_id VARCHAR(256) DEFAULT '',
            requester VARCHAR(256) DEFAULT '',
            requester_display_name VARCHAR(256) DEFAULT '',
            original_message_id VARCHAR(128) DEFAULT '',
            callback_target_json TEXT DEFAULT '{}',
            callback_reply_to_message_id VARCHAR(128) DEFAULT '',
            source_message_json TEXT DEFAULT '{}',
            prompt TEXT DEFAULT '',
            quality VARCHAR(16) DEFAULT 'low',
            size VARCHAR(32) DEFAULT '',
            source_image_json TEXT DEFAULT '{}',
            result_image_id VARCHAR(128) DEFAULT '',
            result_local_path TEXT DEFAULT '',
            result_file_name TEXT DEFAULT '',
            result_media_type VARCHAR(128) DEFAULT '',
            result_public_path TEXT DEFAULT '',
            result_source_url TEXT DEFAULT '',
            error_code VARCHAR(128) DEFAULT '',
            error_message TEXT DEFAULT '',
            retry_count INTEGER NOT NULL DEFAULT 0,
            next_run_at TEXT,
            locked_until TEXT,
            locked_by VARCHAR(128) DEFAULT '',
            callback_sent INTEGER NOT NULL DEFAULT 0,
            callback_error TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            heartbeat_at TEXT
        );
        CREATE INDEX idx_draw_task_status_heartbeat
            ON plugin_draw_task (status, heartbeat_at, updated_at);
        CREATE INDEX idx_draw_task_tenant_created
            ON plugin_draw_task (tenant_id, created_at DESC);
        CREATE INDEX idx_draw_task_queue_due
            ON plugin_draw_task (status, next_run_at, locked_until);
        """
    )
    connection.commit()
