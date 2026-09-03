"""Make speaker portrait hot updates lossless.

Revision ID: 0050_speaker_portrait_cursor
Revises: 0049_speaker_portrait_hot_update
Create Date: 2026-09-02
"""

from __future__ import annotations

from alembic import op

revision = "0050_speaker_portrait_cursor"
down_revision = "0049_speaker_portrait_hot_update"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE plugin_speaker_portraits
            ADD COLUMN IF NOT EXISTS last_distilled_message_at VARCHAR(64) NOT NULL DEFAULT ''
        """
    )
    op.execute(
        """
        ALTER TABLE plugin_speaker_portrait_jobs
            ADD COLUMN IF NOT EXISTS claimed_pending_messages INTEGER NOT NULL DEFAULT 0
        """
    )
    op.execute(
        """
        UPDATE plugin_speaker_portraits AS p
        SET last_distilled_message_at = COALESCE(
            NULLIF(split_part(r.evidence_json::jsonb ->> 'time_span', ' ~ ', 2), ''),
            ''
        )
        FROM plugin_speaker_portrait_revisions AS r
        WHERE r.id = p.current_revision_id
          AND p.last_distilled_message_at = ''
        """
    )
    op.execute(
        """
        UPDATE plugin_speaker_portraits AS p
        SET session_id = COALESCE(
            (
                SELECT NULLIF(j.external_session_id, '')
                FROM plugin_speaker_portrait_jobs AS j
                WHERE j.tenant_id = p.tenant_id
                  AND j.speaker_id = p.speaker_id
                  AND j.status = 'completed'
                  AND j.external_session_id <> ''
                  AND j.external_session_id NOT LIKE 'cx1:%'
                ORDER BY j.id DESC
                LIMIT 1
            ),
            p.session_id
        )
        WHERE p.session_id LIKE 'cx1:%'
        """
    )
    op.execute(
        """
        UPDATE plugin_speaker_portrait_jobs AS j
        SET portrait_id = p.id
        FROM plugin_speaker_portraits AS p
        WHERE j.portrait_id IS NULL
          AND j.tenant_id = p.tenant_id
          AND j.speaker_id = p.speaker_id
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_speaker_portrait_job_attempt
        ON plugin_speaker_portrait_jobs (portrait_id, mode, created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_speaker_portrait_job_attempt")
    op.execute(
        "ALTER TABLE plugin_speaker_portrait_jobs "
        "DROP COLUMN IF EXISTS claimed_pending_messages"
    )
    op.execute(
        "ALTER TABLE plugin_speaker_portraits "
        "DROP COLUMN IF EXISTS last_distilled_message_at"
    )
