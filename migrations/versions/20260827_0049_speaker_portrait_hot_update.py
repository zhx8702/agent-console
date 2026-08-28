"""Speaker portrait hot-update counters and job mode.

Revision ID: 0049_speaker_portrait_hot_update
Revises: 0048_speaker_portraits
Create Date: 2026-08-27
"""

from __future__ import annotations

from alembic import op

revision = "0049_speaker_portrait_hot_update"
down_revision = "0048_speaker_portraits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE plugin_speaker_portraits
            ADD COLUMN IF NOT EXISTS pending_messages INTEGER NOT NULL DEFAULT 0
        """
    )
    op.execute(
        """
        ALTER TABLE plugin_speaker_portraits
            ADD COLUMN IF NOT EXISTS last_message_at VARCHAR(64) NOT NULL DEFAULT ''
        """
    )
    op.execute(
        """
        ALTER TABLE plugin_speaker_portraits
            ADD COLUMN IF NOT EXISTS last_full_at TIMESTAMPTZ
        """
    )
    op.execute(
        """
        ALTER TABLE plugin_speaker_portraits
            ADD COLUMN IF NOT EXISTS hot_update_enabled BOOLEAN NOT NULL DEFAULT TRUE
        """
    )
    op.execute(
        """
        ALTER TABLE plugin_speaker_portrait_jobs
            ADD COLUMN IF NOT EXISTS mode VARCHAR(16) NOT NULL DEFAULT 'full'
        """
    )
    op.execute(
        """
        ALTER TABLE plugin_speaker_portrait_jobs
            ADD COLUMN IF NOT EXISTS since_timestamp VARCHAR(64) NOT NULL DEFAULT ''
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_speaker_portrait_hot_update
        ON plugin_speaker_portraits (hot_update_enabled, pending_messages, updated_at)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_speaker_portrait_hot_update")
    op.execute("ALTER TABLE plugin_speaker_portrait_jobs DROP COLUMN IF EXISTS since_timestamp")
    op.execute("ALTER TABLE plugin_speaker_portrait_jobs DROP COLUMN IF EXISTS mode")
    op.execute("ALTER TABLE plugin_speaker_portraits DROP COLUMN IF EXISTS hot_update_enabled")
    op.execute("ALTER TABLE plugin_speaker_portraits DROP COLUMN IF EXISTS last_full_at")
    op.execute("ALTER TABLE plugin_speaker_portraits DROP COLUMN IF EXISTS last_message_at")
    op.execute("ALTER TABLE plugin_speaker_portraits DROP COLUMN IF EXISTS pending_messages")
