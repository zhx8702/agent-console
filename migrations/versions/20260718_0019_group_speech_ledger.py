"""add shared durable group speech ledger and conservative warmup defaults

Revision ID: 0019_group_speech_ledger
Revises: 0018_social_policy_contract
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0019_group_speech_ledger"
down_revision = "0018_social_policy_contract"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "social_group_speech_ledger",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=256), nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("output_kind", sa.String(length=32), nullable=False),
        sa.Column("author_kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "text_fingerprint",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
        sa.Column("emoji", sa.String(length=32), nullable=False, server_default=""),
        sa.Column(
            "catchphrase",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "provider_message_id",
            sa.String(length=128),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "observed_message_id",
            sa.String(length=128),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "metadata_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "reserved_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "release_reason",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
        sa.CheckConstraint(
            "output_kind IN ('ordinary', 'proactive', 'repeater', 'report', "
            "'external_bot', 'human_observation')",
            name="ck_social_group_speech_output_kind",
        ),
        sa.CheckConstraint(
            "author_kind IN ('bot', 'human')",
            name="ck_social_group_speech_author_kind",
        ),
        sa.CheckConstraint(
            "status IN ('reserved', 'committed', 'released')",
            name="ck_social_group_speech_status",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_social_group_speech_ledger"),
        sa.UniqueConstraint(
            "tenant_id",
            "session_id",
            "idempotency_key",
            name="uq_social_group_speech_idempotency",
        ),
    )
    op.create_index(
        "ix_social_group_speech_scope_occurred",
        "social_group_speech_ledger",
        ["tenant_id", "session_id", "occurred_at"],
    )
    op.create_index(
        "ix_social_group_speech_active_budget",
        "social_group_speech_ledger",
        ["tenant_id", "session_id", "status", "occurred_at"],
    )
    op.create_index(
        "uq_social_group_speech_observed_message",
        "social_group_speech_ledger",
        ["tenant_id", "session_id", "observed_message_id"],
        unique=True,
        postgresql_where=sa.text("observed_message_id <> ''"),
    )

    op.alter_column(
        "plugin_group_activity_config",
        "quiet_start",
        existing_type=sa.String(length=5),
        server_default="23:00",
        existing_nullable=False,
    )
    op.alter_column(
        "plugin_group_activity_config",
        "idle_minutes",
        existing_type=sa.Integer(),
        server_default="180",
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "plugin_group_activity_config",
        "idle_minutes",
        existing_type=sa.Integer(),
        server_default="60",
        existing_nullable=False,
    )
    op.alter_column(
        "plugin_group_activity_config",
        "quiet_start",
        existing_type=sa.String(length=5),
        server_default="22:00",
        existing_nullable=False,
    )
    op.drop_index(
        "uq_social_group_speech_observed_message",
        table_name="social_group_speech_ledger",
    )
    op.drop_index(
        "ix_social_group_speech_active_budget",
        table_name="social_group_speech_ledger",
    )
    op.drop_index(
        "ix_social_group_speech_scope_occurred",
        table_name="social_group_speech_ledger",
    )
    op.drop_table("social_group_speech_ledger")
