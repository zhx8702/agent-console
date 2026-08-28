"""Add local-agent job table.

Revision ID: 0047_local_agent_jobs
Revises: 0046_memory_event_provenance_expiry
Create Date: 2026-08-25
"""

from __future__ import annotations

from alembic import op

revision = "0047_local_agent_jobs"
down_revision = "0046_memory_event_provenance_expiry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_local_agent_job (
            job_id                  VARCHAR(64) PRIMARY KEY,
            backend                 VARCHAR(16) NOT NULL,
            status                  VARCHAR(16) NOT NULL DEFAULT 'queued',
            prompt                  TEXT NOT NULL DEFAULT '',
            tenant_id               VARCHAR(64) DEFAULT '',
            channel                 VARCHAR(32) DEFAULT '',
            session_id              VARCHAR(256) DEFAULT '',
            user_id                 VARCHAR(256) DEFAULT '',
            adapter_id              VARCHAR(64) DEFAULT '',
            connection_id           VARCHAR(64) DEFAULT '',
            request_id              VARCHAR(128) DEFAULT '',
            trace_id                VARCHAR(128) DEFAULT '',
            original_message_id     VARCHAR(128) DEFAULT '',
            sidecar_task_id         VARCHAR(128) DEFAULT '',
            result_text             TEXT DEFAULT '',
            error_code              VARCHAR(128) DEFAULT '',
            error_message           TEXT DEFAULT '',
            callback_target_json    TEXT DEFAULT '{}',
            source_message_json     TEXT DEFAULT '{}',
            callback_sent           BOOLEAN NOT NULL DEFAULT FALSE,
            callback_error          TEXT DEFAULT '',
            locked_by               VARCHAR(128) DEFAULT '',
            locked_until            TIMESTAMPTZ,
            created_at              TIMESTAMPTZ NOT NULL,
            updated_at              TIMESTAMPTZ NOT NULL,
            started_at              TIMESTAMPTZ,
            finished_at             TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_local_agent_job_queue_due
        ON plugin_local_agent_job (status, locked_until, created_at)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_local_agent_job_tenant_created
        ON plugin_local_agent_job (tenant_id, created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_local_agent_job_tenant_created")
    op.execute("DROP INDEX IF EXISTS idx_local_agent_job_queue_due")
    op.execute("DROP TABLE IF EXISTS plugin_local_agent_job")
