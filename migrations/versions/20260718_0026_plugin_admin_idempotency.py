"""Add durable plugin lifecycle idempotency intents.

Revision ID: 0026_plugin_admin_idempotency
Revises: 0025_runtime_llm_config
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0026_plugin_admin_idempotency"
down_revision = "0025_runtime_llm_config"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plugin_lifecycle_operation",
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("plugin_name", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'in_progress'"),
        ),
        sa.Column("claim_token", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "result_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "before_state_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "after_state_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "policy_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "last_error_code",
            sa.String(length=128),
            nullable=False,
            server_default="",
        ),
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
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('in_progress', 'completed')",
            name="ck_plugin_lifecycle_operation_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_plugin_lifecycle_attempt_count_nonnegative",
        ),
        sa.CheckConstraint(
            "policy_version >= 0",
            name="ck_plugin_lifecycle_policy_version_nonnegative",
        ),
        sa.PrimaryKeyConstraint(
            "idempotency_key_hash",
            name="pk_plugin_lifecycle_operation",
        ),
    )
    op.create_index(
        "ix_plugin_lifecycle_operation_plugin_status",
        "plugin_lifecycle_operation",
        ["plugin_name", "status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_plugin_lifecycle_operation_updated",
        "plugin_lifecycle_operation",
        ["updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_plugin_lifecycle_operation_updated",
        table_name="plugin_lifecycle_operation",
    )
    op.drop_index(
        "ix_plugin_lifecycle_operation_plugin_status",
        table_name="plugin_lifecycle_operation",
    )
    op.drop_table("plugin_lifecycle_operation")
