"""Add wxbot admin CAS and durable external-effect state.

Revision ID: 0029_wxbot_admin_mutation_state
Revises: 0028_plugin_mutation_ledger
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0029_wxbot_admin_mutation_state"
down_revision = "0028_plugin_mutation_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plugin_wxbot_admin_resource_version",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("resource_key_hash", sa.String(length=64), nullable=False),
        sa.Column("resource_kind", sa.String(length=96), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("pending_mutation_id", sa.String(length=36), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "resource_key_hash",
            name="pk_plugin_wxbot_admin_resource_version",
        ),
    )
    op.create_index(
        "ix_wxbot_admin_resource_pending",
        "plugin_wxbot_admin_resource_version",
        ["tenant_id", "pending_mutation_id"],
    )

    op.create_table(
        "plugin_wxbot_admin_mutation_state",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=96), nullable=False),
        sa.Column("resource_key_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=True),
        sa.Column("desired_state_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("response_status_code", sa.Integer(), nullable=True),
        sa.Column("response_json", sa.JSON(), nullable=True),
        sa.Column("recovery_response_json", sa.JSON(), nullable=True),
        sa.Column("resource_version", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=96), nullable=False, server_default=""),
        sa.Column("trace_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("dispatch_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_plugin_wxbot_admin_mutation_state"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key_hash",
            name="uq_plugin_wxbot_admin_mutation_key",
        ),
    )
    op.create_index(
        "ix_wxbot_admin_mutation_status_updated",
        "plugin_wxbot_admin_mutation_state",
        ["status", "updated_at"],
    )
    op.create_index(
        "ix_wxbot_admin_mutation_resource",
        "plugin_wxbot_admin_mutation_state",
        ["tenant_id", "resource_key_hash", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_wxbot_admin_mutation_resource",
        table_name="plugin_wxbot_admin_mutation_state",
    )
    op.drop_index(
        "ix_wxbot_admin_mutation_status_updated",
        table_name="plugin_wxbot_admin_mutation_state",
    )
    op.drop_table("plugin_wxbot_admin_mutation_state")
    op.drop_index(
        "ix_wxbot_admin_resource_pending",
        table_name="plugin_wxbot_admin_resource_version",
    )
    op.drop_table("plugin_wxbot_admin_resource_version")
