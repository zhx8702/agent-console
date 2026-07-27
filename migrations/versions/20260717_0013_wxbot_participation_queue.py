"""persist wxbot social participation timing

Revision ID: 0013_wxbot_participation_queue
Revises: 0012_flow_effect_state_machine
Create Date: 2026-07-17

The wxbot plugin owns its tables and may not have created them when Alembic
runs on a fresh installation.  ``ALTER TABLE IF EXISTS`` keeps the migration
safe in both startup orders; the plugin bootstrap creates the same columns.
"""
from __future__ import annotations

from alembic import op

revision = "0013_wxbot_participation_queue"
down_revision = "0012_flow_effect_state_machine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE IF EXISTS plugin_wxbot_reply_queue
            ADD COLUMN IF NOT EXISTS participation_status VARCHAR(24) DEFAULT '',
            ADD COLUMN IF NOT EXISTS source_message_id VARCHAR(128) DEFAULT '',
            ADD COLUMN IF NOT EXISTS not_before TIMESTAMP WITH TIME ZONE,
            ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP WITH TIME ZONE
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF to_regclass('plugin_wxbot_reply_queue') IS NOT NULL THEN
                CREATE INDEX IF NOT EXISTS idx_wxbot_reply_queue_due
                    ON plugin_wxbot_reply_queue
                    (tenant_id, status, not_before, expires_at);
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_wxbot_reply_queue_due")
    op.execute(
        """
        ALTER TABLE IF EXISTS plugin_wxbot_reply_queue
            DROP COLUMN IF EXISTS expires_at,
            DROP COLUMN IF EXISTS not_before,
            DROP COLUMN IF EXISTS source_message_id,
            DROP COLUMN IF EXISTS participation_status
        """
    )
