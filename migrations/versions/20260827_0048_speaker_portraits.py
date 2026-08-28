"""Speaker portrait jobs and revisions.

Revision ID: 0048_speaker_portraits
Revises: 0047_local_agent_jobs
Create Date: 2026-08-27
"""

from __future__ import annotations

from alembic import op

revision = "0048_speaker_portraits"
down_revision = "0047_local_agent_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_speaker_portraits (
            id                      BIGSERIAL PRIMARY KEY,
            tenant_id               VARCHAR(64) NOT NULL,
            channel                 VARCHAR(32) NOT NULL DEFAULT 'wechat',
            source_key              VARCHAR(128) NOT NULL DEFAULT 'wxbot',
            speaker_id              VARCHAR(256) NOT NULL,
            display_name            VARCHAR(256) NOT NULL DEFAULT '',
            session_id              VARCHAR(256) NOT NULL DEFAULT '',
            current_revision_id     BIGINT,
            status                  VARCHAR(32) NOT NULL DEFAULT 'ready',
            created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_speaker_portrait_identity
        ON plugin_speaker_portraits (tenant_id, channel, source_key, speaker_id)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_speaker_portrait_revisions (
            id                      BIGSERIAL PRIMARY KEY,
            portrait_id             BIGINT NOT NULL REFERENCES plugin_speaker_portraits(id),
            schema_version          INTEGER NOT NULL DEFAULT 1,
            portrait_json           TEXT NOT NULL DEFAULT '{}',
            evidence_json           TEXT NOT NULL DEFAULT '{}',
            source                  VARCHAR(32) NOT NULL DEFAULT 'local_cli',
            job_id                  BIGINT,
            created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_speaker_portrait_revision_portrait
        ON plugin_speaker_portrait_revisions (portrait_id, id DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS plugin_speaker_portrait_jobs (
            id                      BIGSERIAL PRIMARY KEY,
            tenant_id               VARCHAR(64) NOT NULL,
            session_id              VARCHAR(256) NOT NULL,
            session_name            VARCHAR(256) NOT NULL DEFAULT '',
            speaker_id              VARCHAR(256) NOT NULL,
            speaker_name            VARCHAR(256) NOT NULL DEFAULT '',
            connection_id           VARCHAR(64) NOT NULL DEFAULT '',
            external_session_id     VARCHAR(256) NOT NULL DEFAULT '',
            status                  VARCHAR(32) NOT NULL DEFAULT 'queued',
            error                   TEXT NOT NULL DEFAULT '',
            days_limit              INTEGER NOT NULL DEFAULT 90,
            max_messages            INTEGER NOT NULL DEFAULT 4000,
            message_count           INTEGER NOT NULL DEFAULT 0,
            portrait_id             BIGINT,
            revision_id             BIGINT,
            locked_by               VARCHAR(128) NOT NULL DEFAULT '',
            locked_until            TIMESTAMPTZ,
            created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            started_at              TIMESTAMPTZ,
            finished_at             TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_speaker_portrait_job_queue
        ON plugin_speaker_portrait_jobs (status, locked_until, created_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_speaker_portrait_job_queue")
    op.execute("DROP TABLE IF EXISTS plugin_speaker_portrait_jobs")
    op.execute("DROP INDEX IF EXISTS idx_speaker_portrait_revision_portrait")
    op.execute("DROP TABLE IF EXISTS plugin_speaker_portrait_revisions")
    op.execute("DROP INDEX IF EXISTS ux_speaker_portrait_identity")
    op.execute("DROP TABLE IF EXISTS plugin_speaker_portraits")
