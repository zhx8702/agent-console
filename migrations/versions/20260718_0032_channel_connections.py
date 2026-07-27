"""Add tenant-scoped generic channel connections.

Revision ID: 0032_channel_connections
Revises: 0031_memory_audience_dedupe
Create Date: 2026-07-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0032_channel_connections"
down_revision = "0031_memory_audience_dedupe"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.alter_column(
        "sessions",
        "channel",
        existing_type=sa.String(length=32),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
    op.add_column(
        "plugin_wxbot_reply_queue",
        sa.Column(
            "connection_id",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
    )
    # delivery_json was TEXT in the original runtime bootstrap, but some
    # adopted installations use JSON/JSONB. Cast every representation through
    # text and make malformed legacy TEXT fail closed instead of aborting the
    # migration or producing an invalid connection scope.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION pg_temp.agent_console_try_jsonb_0032(value TEXT)
        RETURNS JSONB
        LANGUAGE plpgsql
        IMMUTABLE
        STRICT
        AS $$
        BEGIN
            RETURN value::jsonb;
        EXCEPTION WHEN OTHERS THEN
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        WITH parsed AS (
            SELECT
                id,
                pg_temp.agent_console_try_jsonb_0032(delivery_json::text) AS payload
            FROM plugin_wxbot_reply_queue
            WHERE connection_id = ''
        )
        UPDATE plugin_wxbot_reply_queue AS target
        SET connection_id = CASE
            WHEN jsonb_typeof(parsed.payload -> 'connection_id') = 'string'
             AND btrim(parsed.payload ->> 'connection_id')
                 ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'
            THEN btrim(parsed.payload ->> 'connection_id')
            ELSE 'legacy-wechat-default'
        END
        FROM parsed
        WHERE target.id = parsed.id
        """
    )
    op.execute("DROP FUNCTION pg_temp.agent_console_try_jsonb_0032(TEXT)")
    op.drop_index(
        "idx_wxbot_reply_queue_tenant_command_id_unique",
        table_name="plugin_wxbot_reply_queue",
    )
    op.create_index(
        "idx_wxbot_reply_queue_tenant_command_id_unique",
        "plugin_wxbot_reply_queue",
        ["tenant_id", "connection_id", "command_id"],
        unique=True,
        postgresql_where=sa.text("command_id <> ''"),
    )
    op.create_index(
        "idx_wxbot_reply_queue_connection_claim",
        "plugin_wxbot_reply_queue",
        [
            "tenant_id",
            "connection_id",
            "status",
            "not_before",
            "created_at",
            "id",
        ],
    )
    op.create_table(
        "channel_connection",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("connection_id", sa.String(length=64), nullable=False),
        sa.Column("adapter_id", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column(
            "desired_state",
            sa.String(length=24),
            nullable=False,
            server_default="draft",
        ),
        sa.Column(
            "effective_state",
            sa.String(length=24),
            nullable=False,
            server_default="unverified",
        ),
        sa.Column(
            "config_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("secret_ref", sa.String(length=512), nullable=False, server_default=""),
        sa.Column(
            "secret_status",
            sa.String(length=24),
            nullable=False,
            server_default="missing",
        ),
        sa.Column(
            "secret_fingerprint",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column(
            "required_for_launch",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("last_probed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_probe_status",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "last_error_code",
            sa.String(length=96),
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
            "version > 0",
            name="ck_channel_connection_version",
        ),
        sa.CheckConstraint(
            "priority >= 0 AND priority <= 100000",
            name="ck_channel_connection_priority",
        ),
        sa.CheckConstraint(
            "desired_state IN ('draft', 'disabled', 'enabled')",
            name="ck_channel_connection_desired_state",
        ),
        sa.CheckConstraint(
            "effective_state IN ('unverified', 'ready', 'enabled', 'disabled', 'error')",
            name="ck_channel_connection_effective_state",
        ),
        sa.CheckConstraint(
            "secret_status IN ('missing', 'reference_configured')",
            name="ck_channel_connection_secret_status",
        ),
        sa.CheckConstraint(
            "secret_ref = '' OR secret_ref LIKE '%:%'",
            name="ck_channel_connection_secret_ref",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "connection_id",
            name="pk_channel_connection",
        ),
    )
    op.create_index(
        "ix_channel_connection_tenant_adapter",
        "channel_connection",
        ["tenant_id", "adapter_id", "priority"],
    )
    op.create_index(
        "ix_channel_connection_tenant_state",
        "channel_connection",
        ["tenant_id", "desired_state", "effective_state"],
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM sessions WHERE length(channel) > 32) THEN
                RAISE EXCEPTION
                    'cannot downgrade 0032: session channel exceeds 32 characters';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM plugin_wxbot_reply_queue
                WHERE command_id <> ''
                GROUP BY tenant_id, command_id
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade 0032: command ids overlap across connections';
            END IF;
        END
        $$
        """
    )
    op.drop_index(
        "ix_channel_connection_tenant_state",
        table_name="channel_connection",
    )
    op.drop_index(
        "ix_channel_connection_tenant_adapter",
        table_name="channel_connection",
    )
    op.drop_table("channel_connection")
    op.drop_index(
        "idx_wxbot_reply_queue_connection_claim",
        table_name="plugin_wxbot_reply_queue",
    )
    op.drop_index(
        "idx_wxbot_reply_queue_tenant_command_id_unique",
        table_name="plugin_wxbot_reply_queue",
    )
    op.create_index(
        "idx_wxbot_reply_queue_tenant_command_id_unique",
        "plugin_wxbot_reply_queue",
        ["tenant_id", "command_id"],
        unique=True,
        postgresql_where=sa.text("command_id <> ''"),
    )
    op.drop_column("plugin_wxbot_reply_queue", "connection_id")
    op.alter_column(
        "sessions",
        "channel",
        existing_type=sa.String(length=64),
        type_=sa.String(length=32),
        existing_nullable=False,
    )
