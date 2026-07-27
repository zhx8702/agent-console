"""add plugin lifecycle state

Revision ID: 0003_plugin_state
Revises: 0002_kb_scope_session
Create Date: 2026-04-23

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_plugin_state"
down_revision = "0002_kb_scope_session"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plugin_state",
        sa.Column("plugin_name", sa.String(length=128), primary_key=True),
        sa.Column("version", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("source", sa.String(length=64), nullable=False, server_default="builtin"),
        sa.Column("installed", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("system", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column("restart_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "plugin_scope_state",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("plugin_name", sa.String(length=128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("config_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("tenant_id", "session_id", "plugin_name"),
    )

    op.create_table(
        "plugin_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("plugin_name", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="ok"),
        sa.Column("actor_id", sa.Text(), nullable=False, server_default=""),
        sa.Column("actor_type", sa.String(length=32), nullable=False, server_default="admin"),
        sa.Column("request_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("ip_address", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.execute(
        "CREATE INDEX ix_plugin_events_plugin_created "
        "ON plugin_events(plugin_name, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_plugin_events_type_created "
        "ON plugin_events(event_type, created_at DESC)"
    )
    op.create_index("ix_plugin_scope_state_plugin", "plugin_scope_state", ["plugin_name"])


def downgrade() -> None:
    op.drop_index("ix_plugin_scope_state_plugin", table_name="plugin_scope_state")
    op.drop_index("ix_plugin_events_type_created", table_name="plugin_events")
    op.drop_index("ix_plugin_events_plugin_created", table_name="plugin_events")
    op.drop_table("plugin_events")
    op.drop_table("plugin_scope_state")
    op.drop_table("plugin_state")
