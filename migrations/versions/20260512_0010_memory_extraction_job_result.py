"""add memory extraction job result metadata

Revision ID: 0010_memory_extraction_job_result
Revises: 0009_draw_task_queue_locks
Create Date: 2026-05-12

"""
from __future__ import annotations

from alembic import op

revision = "0010_memory_extraction_job_result"
down_revision = "0009_draw_task_queue_locks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF to_regclass('public.plugin_memory_extraction_job') IS NOT NULL THEN
                ALTER TABLE plugin_memory_extraction_job
                ADD COLUMN IF NOT EXISTS result_json TEXT DEFAULT '{}';
            END IF;
        END $$;
    """)


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF to_regclass('public.plugin_memory_extraction_job') IS NOT NULL THEN
                ALTER TABLE plugin_memory_extraction_job
                DROP COLUMN IF EXISTS result_json;
            END IF;
        END $$;
    """)
