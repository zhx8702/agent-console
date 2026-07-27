"""Add durable idempotency and semantic audit for plugin mutations.

Revision ID: 0028_plugin_mutation_ledger
Revises: 0027_runtime_llm_idempotency
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0028_plugin_mutation_ledger"
down_revision = "0027_runtime_llm_idempotency"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "plugin_admin_mutation_idempotency",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("plugin_name", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=96), nullable=False),
        sa.Column("resource_key_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_status_code", sa.Integer(), nullable=True),
        sa.Column("response_json", JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_plugin_admin_mutation_idempotency"),
        sa.UniqueConstraint(
            "tenant_id",
            "plugin_name",
            "operation",
            "idempotency_key_hash",
            name="uq_plugin_admin_mutation_idempotency_key",
        ),
    )
    op.create_index(
        "ix_plugin_admin_mutation_idempotency_created",
        "plugin_admin_mutation_idempotency",
        ["tenant_id", "plugin_name", "created_at"],
    )

    op.create_table(
        "plugin_admin_mutation_audit",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("mutation_id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("plugin_name", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=96), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("actor_kind", sa.String(length=32), nullable=False),
        sa.Column(
            "roles_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "scope_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "before_state_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "after_state_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("reason_code", sa.String(length=96), nullable=False),
        sa.Column("reason_hash", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("trace_id", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("resource_version", sa.String(length=128), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["mutation_id"],
            ["plugin_admin_mutation_idempotency.id"],
            name="fk_plugin_admin_mutation_audit_mutation",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_plugin_admin_mutation_audit"),
        sa.UniqueConstraint("mutation_id", name="uq_plugin_admin_mutation_audit_mutation"),
    )
    op.create_index(
        "ix_plugin_admin_mutation_audit_scope_created",
        "plugin_admin_mutation_audit",
        ["tenant_id", "plugin_name", "created_at"],
    )
    op.create_index(
        "ix_plugin_admin_mutation_audit_trace",
        "plugin_admin_mutation_audit",
        ["trace_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_plugin_admin_mutation_audit_trace",
        table_name="plugin_admin_mutation_audit",
    )
    op.drop_index(
        "ix_plugin_admin_mutation_audit_scope_created",
        table_name="plugin_admin_mutation_audit",
    )
    op.drop_table("plugin_admin_mutation_audit")
    op.drop_index(
        "ix_plugin_admin_mutation_idempotency_created",
        table_name="plugin_admin_mutation_idempotency",
    )
    op.drop_table("plugin_admin_mutation_idempotency")
