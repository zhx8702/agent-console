"""Per-group inbound webhooks for third-party message push.

Revision ID: 0052_wxbot_group_webhooks
Revises: 0051_draw_runtime_config
Create Date: 2026-09-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0052_wxbot_group_webhooks"
down_revision = "0051_draw_runtime_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plugin_wxbot_group_webhook",
        sa.Column("webhook_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("connection_id", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("external_session_id", sa.String(length=256), nullable=False),
        sa.Column("canonical_session_id", sa.String(length=256), nullable=False),
        sa.Column("session_name", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_hint", sa.String(length=8), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.String(length=128), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("webhook_id", name="pk_plugin_wxbot_group_webhook"),
        sa.UniqueConstraint("token_hash", name="uq_plugin_wxbot_group_webhook_token"),
    )
    op.create_index(
        "ux_plugin_wxbot_group_webhook_active_group",
        "plugin_wxbot_group_webhook",
        ["tenant_id", "connection_id", "external_session_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
        sqlite_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ux_plugin_wxbot_group_webhook_active_group",
        table_name="plugin_wxbot_group_webhook",
    )
    op.drop_table("plugin_wxbot_group_webhook")
