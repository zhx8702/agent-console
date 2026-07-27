"""add draw task retry count

Revision ID: 0008_draw_task_retry_count
Revises: 0007_plugin_draw_task
Create Date: 2026-05-11

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_draw_task_retry_count"
down_revision = "0007_plugin_draw_task"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_context().as_sql:
        op.execute(
            "ALTER TABLE plugin_draw_task ADD COLUMN IF NOT EXISTS "
            "retry_count INTEGER NOT NULL DEFAULT 0"
        )
        return
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("plugin_draw_task")}
    if "retry_count" in columns:
        return
    op.add_column(
        "plugin_draw_task",
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    if op.get_context().as_sql:
        op.execute(
            "ALTER TABLE plugin_draw_task DROP COLUMN IF EXISTS retry_count"
        )
        return
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("plugin_draw_task")}
    if "retry_count" in columns:
        op.drop_column("plugin_draw_task", "retry_count")
