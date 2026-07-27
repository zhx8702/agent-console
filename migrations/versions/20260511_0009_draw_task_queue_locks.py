"""add draw task queue schedule and locks

Revision ID: 0009_draw_task_queue_locks
Revises: 0008_draw_task_retry_count
Create Date: 2026-05-11

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_draw_task_queue_locks"
down_revision = "0008_draw_task_retry_count"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_context().as_sql:
        op.execute(
            "ALTER TABLE plugin_draw_task ADD COLUMN IF NOT EXISTS "
            "next_run_at TIMESTAMPTZ"
        )
        op.execute(
            "ALTER TABLE plugin_draw_task ADD COLUMN IF NOT EXISTS "
            "locked_until TIMESTAMPTZ"
        )
        op.execute(
            "ALTER TABLE plugin_draw_task ADD COLUMN IF NOT EXISTS "
            "locked_by VARCHAR(128) DEFAULT ''"
        )
    else:
        bind = op.get_bind()
        inspector = sa.inspect(bind)
        columns = {
            column["name"]
            for column in inspector.get_columns("plugin_draw_task")
        }
        if "next_run_at" not in columns:
            op.add_column(
                "plugin_draw_task",
                sa.Column("next_run_at", sa.DateTime(timezone=True)),
            )
        if "locked_until" not in columns:
            op.add_column(
                "plugin_draw_task",
                sa.Column("locked_until", sa.DateTime(timezone=True)),
            )
        if "locked_by" not in columns:
            op.add_column(
                "plugin_draw_task",
                sa.Column("locked_by", sa.String(length=128), server_default=""),
            )
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_draw_task_queue_due
        ON plugin_draw_task (status, next_run_at, locked_until)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_draw_task_queue_due")
    if op.get_context().as_sql:
        op.execute(
            "ALTER TABLE plugin_draw_task DROP COLUMN IF EXISTS locked_by"
        )
        op.execute(
            "ALTER TABLE plugin_draw_task DROP COLUMN IF EXISTS locked_until"
        )
        op.execute(
            "ALTER TABLE plugin_draw_task DROP COLUMN IF EXISTS next_run_at"
        )
        return
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("plugin_draw_task")}
    if "locked_by" in columns:
        op.drop_column("plugin_draw_task", "locked_by")
    if "locked_until" in columns:
        op.drop_column("plugin_draw_task", "locked_until")
    if "next_run_at" in columns:
        op.drop_column("plugin_draw_task", "next_run_at")
