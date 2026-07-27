"""add transactional message effect intents

Revision ID: 0020_message_effect_intent
Revises: 0019_group_speech_ledger
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020_message_effect_intent"
down_revision = "0019_group_speech_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "message_effect_intent",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=512), nullable=False),
        sa.Column("source_message_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=256), nullable=False),
        sa.Column(
            "trace_id",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
        sa.Column("owner", sa.String(length=128), nullable=False),
        sa.Column("effect_type", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "context",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="prepared",
        ),
        sa.Column(
            "dry_run",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=True,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "claim_owner",
            sa.String(length=128),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "claim_token",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
        sa.Column("claim_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "status IN ('prepared', 'running', 'completed', 'failed')",
            name="ck_message_effect_intent_status",
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name="ck_message_effect_intent_attempts",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "idempotency_key",
            name="pk_message_effect_intent",
        ),
    )
    op.create_index(
        "ix_message_effect_intent_due",
        "message_effect_intent",
        ["status", "available_at", "claim_until"],
    )
    op.create_index(
        "ix_message_effect_intent_source",
        "message_effect_intent",
        ["tenant_id", "source_message_id"],
    )
    op.create_index(
        "ix_message_effect_intent_scope_created",
        "message_effect_intent",
        ["tenant_id", "session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_message_effect_intent_scope_created",
        table_name="message_effect_intent",
    )
    op.drop_index(
        "ix_message_effect_intent_source",
        table_name="message_effect_intent",
    )
    op.drop_index(
        "ix_message_effect_intent_due",
        table_name="message_effect_intent",
    )
    op.drop_table("message_effect_intent")
