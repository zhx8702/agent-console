"""Persist live draw endpoint overrides for the console.

Revision ID: 0051_draw_runtime_config
Revises: 0050_speaker_portrait_cursor
Create Date: 2026-09-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0051_draw_runtime_config"
down_revision = "0050_speaker_portrait_cursor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plugin_draw_runtime_config",
        sa.Column("config_key", sa.String(length=32), nullable=False),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "overrides_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("updated_by", sa.String(length=128), nullable=False, server_default=""),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "version >= 0",
            name="ck_plugin_draw_runtime_config_version_nonnegative",
        ),
        sa.PrimaryKeyConstraint("config_key", name="pk_plugin_draw_runtime_config"),
    )


def downgrade() -> None:
    op.drop_table("plugin_draw_runtime_config")
