"""add durable inbox deduplication and transactional message outbox

Revision ID: 0017_message_reliability
Revises: 0016_wxbot_schema
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_message_reliability"
down_revision = "0016_wxbot_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Adopt crash-safe wxbot reply leases for databases that had already run
    # 0016 before the fresh-install schema learned about these columns.
    op.execute(
        "ALTER TABLE plugin_wxbot_reply_queue ADD COLUMN IF NOT EXISTS "
        "claim_owner VARCHAR(128) NOT NULL DEFAULT ''"
    )
    op.execute(
        "ALTER TABLE plugin_wxbot_reply_queue ADD COLUMN IF NOT EXISTS "
        "claim_token VARCHAR(64) NOT NULL DEFAULT ''"
    )
    op.execute(
        "ALTER TABLE plugin_wxbot_reply_queue ADD COLUMN IF NOT EXISTS "
        "claim_until TIMESTAMPTZ"
    )
    op.execute(
        "UPDATE plugin_wxbot_reply_queue SET attempt_count = 0 "
        "WHERE attempt_count IS NULL"
    )
    op.execute(
        "ALTER TABLE plugin_wxbot_reply_queue "
        "ALTER COLUMN attempt_count SET DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE plugin_wxbot_reply_queue "
        "ALTER COLUMN attempt_count SET NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_wxbot_reply_queue_claim "
        "ON plugin_wxbot_reply_queue "
        "(tenant_id, status, claim_until, not_before, created_at, id)"
    )
    op.execute(
        "ALTER TABLE plugin_wxbot_tenant_policy "
        "ALTER COLUMN group_reply_mention_sender SET DEFAULT FALSE"
    )
    op.execute(
        "ALTER TABLE plugin_wxbot_session_policy ADD COLUMN IF NOT EXISTS "
        "participation_policy_json JSONB NOT NULL DEFAULT '{}'::jsonb"
    )
    op.execute(
        "ALTER TABLE plugin_group_activity_config "
        "ALTER COLUMN max_per_day SET DEFAULT 1"
    )

    # This stable compatibility marker, rather than Alembic's exact head,
    # defines whether old and new replicas may overlap during a rolling
    # deployment.  Backwards-compatible migrations keep level 1; any future
    # breaking migration must bump it before changing the runtime contract.
    op.create_table(
        "app_schema_contract",
        sa.Column("contract_name", sa.String(length=64), nullable=False),
        sa.Column("compatibility_level", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "compatibility_level > 0",
            name="ck_app_schema_contract_level",
        ),
        sa.PrimaryKeyConstraint(
            "contract_name",
            name="pk_app_schema_contract",
        ),
    )
    op.execute(
        sa.text(
            "INSERT INTO app_schema_contract "
            "(contract_name, compatibility_level) "
            "VALUES ('agent-console-runtime', 1)"
        )
    )

    # Channel-native group/user identifiers can exceed the legacy core limits.
    # Align every core session scope before reliability rows begin referencing
    # the same identifiers.
    op.alter_column(
        "sessions",
        "session_id",
        existing_type=sa.String(length=64),
        type_=sa.String(length=256),
        existing_nullable=False,
    )
    op.alter_column(
        "sessions",
        "user_id",
        existing_type=sa.String(length=128),
        type_=sa.String(length=256),
        existing_nullable=False,
    )
    op.alter_column(
        "turns",
        "session_id",
        existing_type=sa.String(length=64),
        type_=sa.String(length=256),
        existing_nullable=False,
    )
    for table_name in ("faqs", "kb_documents", "kb_chunks"):
        op.alter_column(
            table_name,
            "session_id",
            existing_type=sa.String(length=128),
            type_=sa.String(length=256),
            existing_nullable=False,
        )

    op.create_table(
        "processed_messages",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("message_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=256), nullable=False),
        sa.Column("user_id", sa.String(length=256), nullable=False),
        sa.Column(
            "trace_id",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="processing",
        ),
        sa.Column(
            "route_label",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "reason",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "error_type",
            sa.String(length=128),
            nullable=False,
            server_default="",
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
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ("
            "'processing', 'completed', 'intentionally_suppressed', "
            "'permanent_failure'"
            ")",
            name="ck_processed_messages_status",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "message_id",
            name="pk_processed_messages",
        ),
    )
    op.create_index(
        "ix_processed_messages_tenant_session_created",
        "processed_messages",
        ["tenant_id", "session_id", "created_at"],
    )

    op.create_table(
        "message_outbox",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("reply_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=256), nullable=False),
        sa.Column(
            "trace_id",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
        sa.Column("stream", sa.String(length=128), nullable=False),
        sa.Column("partition_key", sa.String(length=384), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "headers",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default="pending",
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
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "lease_owner",
            sa.String(length=128),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "lease_token",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "published_message_id",
            sa.String(length=128),
            nullable=False,
            server_default="",
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_error",
            sa.Text(),
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
        sa.CheckConstraint(
            "status IN ('pending', 'publishing', 'published', 'dead_letter')",
            name="ck_message_outbox_status",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "reply_id",
            name="pk_message_outbox",
        ),
    )
    op.create_index(
        "ix_message_outbox_due",
        "message_outbox",
        ["status", "available_at", "lease_until"],
    )
    op.create_index(
        "ix_message_outbox_tenant_session_created",
        "message_outbox",
        ["tenant_id", "session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_message_outbox_tenant_session_created",
        table_name="message_outbox",
    )
    op.drop_index("ix_message_outbox_due", table_name="message_outbox")
    op.drop_table("message_outbox")
    op.drop_index(
        "ix_processed_messages_tenant_session_created",
        table_name="processed_messages",
    )
    op.drop_table("processed_messages")
    op.drop_table("app_schema_contract")

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM sessions
                WHERE length(session_id) > 64 OR length(user_id) > 128
            ) OR EXISTS (
                SELECT 1 FROM turns WHERE length(session_id) > 64
            ) OR EXISTS (
                SELECT 1 FROM faqs WHERE length(session_id) > 128
            ) OR EXISTS (
                SELECT 1 FROM kb_documents WHERE length(session_id) > 128
            ) OR EXISTS (
                SELECT 1 FROM kb_chunks WHERE length(session_id) > 128
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade 0017: identifiers exceed legacy limits';
            END IF;
        END
        $$;
        """
    )
    for table_name in ("kb_chunks", "kb_documents", "faqs"):
        op.alter_column(
            table_name,
            "session_id",
            existing_type=sa.String(length=256),
            type_=sa.String(length=128),
            existing_nullable=False,
        )
    op.alter_column(
        "turns",
        "session_id",
        existing_type=sa.String(length=256),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.alter_column(
        "sessions",
        "user_id",
        existing_type=sa.String(length=256),
        type_=sa.String(length=128),
        existing_nullable=False,
    )
    op.alter_column(
        "sessions",
        "session_id",
        existing_type=sa.String(length=256),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
